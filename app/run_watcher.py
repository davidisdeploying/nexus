"""
Relay-outcome watcher — Phase 3 (design panel-notifications-design.md §D.3).

The authoritative source for run outcomes. The relay's completion curl (a
Phase-3 TODO on the launcher side) is only a latency optimization; THIS
watcher is what actually detects outcomes, because turn-end death can only be
detected by the ABSENCE of a completion sentinel — there is no event to curl.

Terminal-state detection per run directory `from-{seat}/runs/{token}/`:
  - `done` sentinel present -> success (exit "0"/empty) or failure (else)
  - `status.json` reports a non-"running" status -> mapped to that outcome
  - no sentinel, `status.json` "started" marker older than RUN_MAX_RUNTIME
    -> turn_end_death (synthesized; §D.3)
  - otherwise: still in flight, checked again next tick

⚠️ NOW-watermark (critical anti-spam guardrail, see the BUILD prompt): on the
very first tick this process ever runs, EVERY run directory already on disk —
regardless of its current state — is baselined into run_watch_seen with
outcome='baseline' and is NEVER notified, no matter what state it reaches
later. Only run directories that did not exist at baseline time are eligible
for notify(). The watermark (run_watch_meta) and the seen-set (run_watch_seen)
both live in events.db via notify_store, so a service restart re-reads the
SAME watermark and never re-baselines, never backfills.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from . import notify_store
from .config import settings
from .notify import notify

log = logging.getLogger("nexus.run_watcher")

# A `started` marker (status.json) with no completion sentinel after this long
# is dead, not slow — matches the existing "died" convention used elsewhere
# for the same run-state shape (herospath._run_state, work.read_relay_runs).
RUN_MAX_RUNTIME_SECONDS = 6 * 60 * 60

_STATUS_RE = re.compile(r'"status"\s*:\s*"([^"]+)"')


def _run_dirs() -> list[tuple[str, str, Path]]:
    """(seat, token, dir) for every run directory currently on disk, across
    every from-{seat}/runs/ lane present in the vault (not a fixed seat list —
    new seats get picked up automatically)."""
    out: list[tuple[str, str, Path]] = []
    for runs_dir in sorted(settings.relay_root.glob("from-*/runs")):
        seat = runs_dir.parent.name.removeprefix("from-")
        try:
            dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in dirs:
            out.append((seat, d.name, d))
    return out


def _status_field(d: Path) -> str:
    try:
        text = (d / "status.json").read_text(errors="replace")[:2000]
    except OSError:
        return ""
    m = _STATUS_RE.search(text)
    return m.group(1) if m else ""


def _terminal_outcome(d: Path, mtime: float) -> str | None:
    """None if still in flight; otherwise the outcome string classify() (in
    app/notify.py) knows how to route."""
    done_f = d / "done"
    if done_f.exists():
        try:
            code = done_f.read_text(errors="replace").strip()
        except OSError:
            code = ""
        return "success" if code in ("", "0") else "failure"

    status = _status_field(d)
    if status and status != "running":
        if "blocked" in status:
            return "blocked_awaiting_approval"
        if "collision" in status:
            return "collision"
        if "abort" in status or "restore" in status:
            return "abort_restore"
        return "failure"  # an unrecognized non-running status is still a terminal failure

    if (time.time() - mtime) > RUN_MAX_RUNTIME_SECONDS:
        return "turn_end_death"
    return None


def _exit_code(d: Path) -> int | None:
    try:
        code = (d / "done").read_text(errors="replace").strip()
        return int(code)
    except (OSError, ValueError):
        return None


async def scan_once() -> dict:
    """One watcher tick: establish the watermark on first-ever call, otherwise
    evaluate every run directory not yet in run_watch_seen. Returns a summary
    dict for logging/verification (never raises — one bad run dir is skipped,
    not fatal to the tick)."""
    if not notify_store.run_watch_initialized():
        dirs = _run_dirs()
        for seat, token, _d in dirs:
            notify_store.mark_run_watch_seen(token, seat, "baseline")
        notify_store.set_run_watch_initialized()
        log.info("run_watcher: NOW-watermark established, baselined %d existing run(s)",
                  len(dirs))
        return {"baselined": len(dirs), "notified": 0, "skipped": 0}

    notified = 0
    skipped = 0
    for seat, token, d in _run_dirs():
        if notify_store.run_watch_seen(token):
            continue
        try:
            mtime = d.stat().st_mtime
        except OSError:
            skipped += 1
            continue
        outcome = _terminal_outcome(d, mtime)
        if outcome is None:
            continue  # still running and not stale — re-check next tick

        event = {"source": "run_watcher", "token": token, "seat": seat, "outcome": outcome}
        if outcome == "failure":
            event["exit_code"] = _exit_code(d)
        try:
            await notify(event)
        except Exception:  # noqa: BLE001 — one bad event must not wedge the tick
            log.exception("run_watcher: notify() failed for token=%s", token)
            skipped += 1
            continue
        notify_store.mark_run_watch_seen(token, seat, outcome)
        notified += 1

    return {"baselined": 0, "notified": notified, "skipped": skipped}
