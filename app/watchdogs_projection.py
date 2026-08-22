"""
Cadence-aware live overlay over the static app/watchdogs_registry.py rows
(FLEET-AUTO-BUILD-20260802-panel-live-watchdog-evidence).

The registry is hand-authored point-in-time metadata; five Alpha rows were
hard-coded WARN (stale_evidence) forever because "no edge fired" looks
identical to "nothing is checking" from a purely static snapshot. This module
overlays two kinds of LIVE, LOCAL evidence on top of the static rows:

  - APScheduler job-completion receipts (app/scheduler.py's listener writes
    them to events.db via notify_store.record_scheduler_receipt) for
    thermal-watch, health-watch, conformance-watch, and deadman-ping.
  - The weekly notification self-test's own notification_log receipt
    (channel='nexus-selftest') for the transport-canary row.

No request-time SSH/systemctl/journalctl/subprocess/network call is ever made
here -- only local sqlite reads via notify_store, same discipline as the
static registry's own "no live probe" contract. Absence of a fired alarm/
action edge is NEVER treated as unhealthy here: a current, ok execution
receipt is sufficient evidence that an edge-triggered watcher is alive.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import notify_store
from .watchdogs_registry import REGISTRY

# registry row id -> (scheduler job id, max receipt age in seconds before the
# row is considered stale evidence rather than active).
_CADENCE_OVERLAYS: dict[str, tuple[str, int]] = {
    "alpha-aps-thermal-watch": ("thermal-watch", 3 * 60),
    "alpha-aps-health-watch": ("health-watch", 3 * 60),
    "alpha-aps-conformance-watch": ("conformance-watch", 15 * 60),
    "alpha-aps-deadman-ping": ("deadman-ping", 15 * 60),
}

SELFTEST_ROW_ID = "alpha-aps-nexus-selftest"
SELFTEST_MAX_AGE_SECONDS = 8 * 24 * 60 * 60

_DETAIL_LIMIT = 260
_CACHE_SECONDS = 15.0
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_rows: list[dict] | None = None


def _bounded(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _parse_utc(value)
    return max(0.0, (now - parsed).total_seconds()) if parsed else None


def _apply_cadence_overlay(row: dict, job_id: str, max_age: int,
                           receipts: dict[str, dict], now: datetime) -> dict:
    receipt = receipts.get(job_id)
    if receipt is None:
        row["status"] = "stale_evidence"
        row["status_detail"] = _bounded(
            f"No scheduler execution receipt recorded yet for job '{job_id}'."
        )
        return row
    age = _age_seconds(receipt.get("completed_at"), now)
    outcome = receipt.get("outcome")
    if outcome != "ok":
        row["status"] = "stale_evidence"
        row["status_detail"] = _bounded(
            f"Latest '{job_id}' receipt is non-ok ({outcome}): {receipt.get('detail') or 'no detail'}"
        )
        return row
    if age is None:
        row["status"] = "stale_evidence"
        row["status_detail"] = _bounded(
            f"Latest '{job_id}' receipt has an invalid completed_at timestamp."
        )
        return row
    if age > max_age:
        row["status"] = "stale_evidence"
        row["status_detail"] = _bounded(
            f"Latest '{job_id}' receipt is {int(age)}s old, exceeding the {max_age}s cadence window."
        )
        return row
    row["status"] = "active"
    row["status_detail"] = _bounded(
        f"Live '{job_id}' execution receipt {int(age)}s old, outcome ok."
    )
    return row


def _apply_selftest_overlay(row: dict, selftest: dict | None, now: datetime) -> dict:
    if selftest is None:
        row["status"] = "stale_evidence"
        row["status_detail"] = "No weekly self-test notification receipt has been recorded yet."
        return row
    age = _age_seconds(selftest.get("created_at"), now)
    if age is None:
        row["status"] = "stale_evidence"
        row["status_detail"] = "Latest self-test receipt has an invalid created_at timestamp."
        return row
    if age > SELFTEST_MAX_AGE_SECONDS:
        row["status"] = "stale_evidence"
        row["status_detail"] = _bounded(
            f"Latest self-test receipt is {int(age)}s old, exceeding the {SELFTEST_MAX_AGE_SECONDS}s (8 day) window."
        )
        return row
    pwa_ok = bool(selftest.get("sent_pwa"))
    ntfy_ok = bool(selftest.get("sent_ntfy"))
    if not (pwa_ok and ntfy_ok):
        row["status"] = "stale_evidence"
        row["status_detail"] = _bounded(
            f"Latest self-test receipt failed a transport: PWA={'ok' if pwa_ok else 'FAILED'}, "
            f"ntfy={'ok' if ntfy_ok else 'FAILED'}."
        )
        return row
    row["status"] = "active"
    row["status_detail"] = _bounded(
        f"Latest weekly self-test passed {int(age)}s ago (PWA + ntfy both delivered)."
    )
    return row


def _project(rows: list[dict], receipts: dict[str, dict],
             selftest: dict | None, now: datetime) -> list[dict]:
    projected = []
    for source_row in rows:
        row = dict(source_row)
        if row["status"] == "retired":
            projected.append(row)
            continue
        overlay = _CADENCE_OVERLAYS.get(row["id"])
        if overlay:
            job_id, max_age = overlay
            projected.append(_apply_cadence_overlay(row, job_id, max_age, receipts, now))
            continue
        if row["id"] == SELFTEST_ROW_ID:
            projected.append(_apply_selftest_overlay(row, selftest, now))
            continue
        # Non-runtime static rows (systemd guards, kernel watchdogs, docker
        # healthchecks, etc.) are preserved exactly as authored -- this
        # projection layer only has live evidence for the five overlaid rows.
        projected.append(row)
    return projected


def _build() -> list[dict]:
    now = datetime.now(timezone.utc)
    job_ids = {job_id for job_id, _ in _CADENCE_OVERLAYS.values()}
    receipts = {}
    for job_id in job_ids:
        receipt = notify_store.get_scheduler_receipt(job_id)
        if receipt is not None:
            receipts[job_id] = receipt
    selftest = notify_store.get_last_selftest_receipt()
    return _project(REGISTRY, receipts, selftest, now)


def get_projected_registry(host: str | None = None, *, force: bool = False) -> list[dict]:
    """Static registry rows overlaid with live cadence evidence, cached
    briefly so ordinary page navigation stays cheap (same discipline as
    system_status.get_system_status's cache)."""
    global _cache_at, _cache_rows
    now_mono = time.monotonic()
    if force or _cache_rows is None or now_mono - _cache_at >= _CACHE_SECONDS:
        with _cache_lock:
            now_mono = time.monotonic()
            if force or _cache_rows is None or now_mono - _cache_at >= _CACHE_SECONDS:
                _cache_rows = _build()
                _cache_at = now_mono
    rows = _cache_rows
    if host is not None:
        rows = [row for row in rows if row["host"] == host]
    return [dict(row) for row in rows]


def projected_summary() -> dict:
    """Per-host counts + flagged (stale_evidence/orphaned, never retired)
    counts computed off the projected rows -- the same shape as
    watchdogs_registry.summary(), but reflecting live evidence."""
    rows = get_projected_registry()
    hosts: dict[str, dict] = {}
    for row in rows:
        h = hosts.setdefault(row["host"], {"host": row["host"], "count": 0, "flagged": 0})
        h["count"] += 1
        if row["status"] in ("stale_evidence", "orphaned"):
            h["flagged"] += 1
    return {
        "hosts": [hosts[h] for h in ("alpha", "charlie", "delta", "echo") if h in hosts],
        "total": len(rows),
    }
