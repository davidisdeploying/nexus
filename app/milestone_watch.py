"""
Per-queue + whole-scan MILESTONE notifications for gallery-library-scan
(Phase 4b, panel-notifications-design.md). Reads the same
gallery-library-scan.json heartbeat app/jobs/gallery.py already writes every
60s (no re-probe, no ssh) and fires a ONE-SHOT nexus-post the instant a
queue's `done` count reaches its `total`, plus one more when the whole scan
reaches its own terminal state.

Completion is checked against the raw `done >= total` counts, NOT the
displayed `pct` field — pct is rounded to one decimal (app/jobs/gallery.py's
_queue_entry), so a queue can show pct=100.0 while done is still a few assets
short of total (observed live: metadata read done=108560/total=108597 ->
pct=100.0 on 2026-07-10). Using done>=total avoids firing early on that
rounding artifact.

Scan-complete uses the heartbeat's own top-level `state` field
(app/jobs/gallery.py:303-307): "done" is a genuine distinct terminal state the
producer sets when `metadata_done >= total and ml_errors == 0` — i.e. it is
driven off the metadata queue specifically, not "every queue at 100%", so a
scan can read state=="done" while e.g. the video queue is still encoding.
That's the producer's own definition of "the library scan is complete" and is
used as-is rather than re-derived from the per-queue counts.

Milestones are one-shot edges, not alarms: no recovery, no reminder re-fire
(unlike app/health_watch.py's HEALTH_CONDITIONS). WATERMARK-FROM-NOW seeds
every queue/scan state already true for the CURRENT run_id as already-fired
on first-ever evaluation, so a queue (or the scan) already complete when this
watcher starts does not fire the moment it starts running. A NEW run_id
(tomorrow's fresh scan) resets each queue's + the scan's fired flag, so
completion fires again on the next run.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from . import notify_store
from .config import settings
from .notify import notify

log = logging.getLogger("nexus.milestone_watch")

HOST = "charlie"
GALLERY_HEARTBEAT: Path = settings.heartbeats_dir / "gallery-library-scan.json"
JOBS_NAVIGATE = "/jobs/gallery-library-scan?nf=1"

CONDITION_QUEUE_PREFIX = "milestone_gallery_"
CONDITION_SCAN_COMPLETE = "milestone_gallery_scan_complete"


def _read_json(path: Path) -> dict[str, Any] | None:
    """Never raises — a partial write / missing file / bad JSON degrades to
    None (mirrors thermal_watch/health_watch's own _read_json)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def _evaluate_milestone(
    condition_key: str,
    host: str,
    run_id: str,
    is_complete: bool,
    render: Callable[[], dict],
) -> dict:
    """One-shot-per-run_id evaluator. `alerts.last_seen` is repurposed to hold
    "{run_id}:fired" or "{run_id}:pending" (the same repurposed-column trick
    thermal_watch/health_watch use for their own state strings) — a compact
    per-run watermark with no new columns/tables needed. Never raises — a bad
    read degrades to a no-op tick for that condition, not a wedged scheduler."""
    row = notify_store.get_alert_by_condition(condition_key, host)
    if row is None:
        # WATERMARK-FROM-NOW: baseline the CURRENT run's state as already-seen
        # so a queue/scan already complete for today's run_id never fires the
        # instant this watcher starts. Only a LATER crossing (this run or a
        # fresh one) can ever fire.
        seed = f"{run_id}:{'fired' if is_complete else 'pending'}"
        notify_store.seed_alert(condition_key, host, seed)
        log.info("milestone_watch: seeded %s/%s watermark at %s",
                  condition_key, host, seed)
        return {"fired": False, "reason": "seeded watermark", "state": seed}

    prev_run_id, _, prev_state = (row.get("last_seen") or "").partition(":")
    already_fired = prev_run_id == run_id and prev_state == "fired"

    if is_complete and not already_fired:
        note = render()
        result = await notify({
            "source": "milestone_watch", "condition": condition_key, "host": host,
            "alert_id": row["id"], "navigate": JOBS_NAVIGATE, **note,
        })
        notify_store.mark_alert_notified(row["id"], f"{run_id}:fired")
        return {"fired": not result.get("suppressed", False), "state": "fired"}

    # Not complete yet, or already fired for this exact run_id: just keep the
    # watermark current (e.g. a NEW run_id starting below the threshold again
    # resets the flag to "pending" with no notify) so the next real crossing
    # compares against the right baseline.
    new_state = f"{run_id}:{'fired' if is_complete else 'pending'}"
    if new_state != (row.get("last_seen") or ""):
        notify_store.update_alert_seen(row["id"], new_state)
    return {"fired": False, "state": new_state}


def _render_queue(queue_name: str, run_id: str) -> Callable[[], dict]:
    def render() -> dict:
        return {
            "title": f"✅ gallery {queue_name} queue complete",
            "body": f"run {run_id}",
        }
    return render


def _render_scan_complete(run_id: str) -> Callable[[], dict]:
    def render() -> dict:
        return {
            "title": "✅ gallery library scan complete",
            "body": f"run {run_id}",
        }
    return render


async def evaluate_heartbeat(data: dict[str, Any]) -> dict:
    """Core evaluator over an already-parsed heartbeat dict — kept separate
    from scan_once() so a test harness can drive it with an injected dict, no
    real file or scheduler involved (mirrors thermal_watch's split)."""
    run_id = str(data.get("run_id") or "")
    if not run_id:
        return {"reason": "heartbeat missing run_id"}

    queues = data.get("queues")
    if not isinstance(queues, list):
        queues = []

    queue_results: list[dict] = []
    for q in queues:
        if not isinstance(q, dict):
            continue
        name = str(q.get("name") or "")
        if not name:
            continue
        try:
            done = float(q.get("done"))
            total = float(q.get("total"))
        except (TypeError, ValueError):
            continue
        is_complete = total > 0 and done >= total
        condition_key = f"{CONDITION_QUEUE_PREFIX}{name}"
        try:
            r = await _evaluate_milestone(condition_key, HOST, run_id, is_complete,
                                           _render_queue(name, run_id))
        except Exception:
            log.exception("milestone_watch: queue %s evaluation failed", name)
            r = {"fired": False, "reason": "error"}
        queue_results.append({"condition": condition_key, "host": HOST, **r})

    scan_complete = data.get("state") == "done"
    try:
        scan_result = await _evaluate_milestone(
            CONDITION_SCAN_COMPLETE, HOST, run_id, scan_complete,
            _render_scan_complete(run_id),
        )
    except Exception:
        log.exception("milestone_watch: scan-complete evaluation failed")
        scan_result = {"fired": False, "reason": "error"}

    return {
        "queues": queue_results,
        "scan_complete": {"condition": CONDITION_SCAN_COMPLETE, "host": HOST, **scan_result},
    }


async def scan_once() -> dict:
    """One 60s sweep tick. Never raises — a missing/bad heartbeat degrades to
    an empty result, not a wedged scheduler (mirrors thermal_watch.scan_once /
    health_watch.scan_once)."""
    data = _read_json(GALLERY_HEARTBEAT)
    if data is None:
        return {"reason": "no gallery heartbeat yet"}
    return await evaluate_heartbeat(data)
