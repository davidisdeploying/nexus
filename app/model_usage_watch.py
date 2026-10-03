"""Exact-once PWA notifications for durable model-quota history events."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import notify_store
from .config import settings
from .notify import notify

log = logging.getLogger("nexus.model_usage_watch")
CENTRAL = ZoneInfo("America/Chicago")
RESET_WINDOWS = {"five_hour", "weekly", "fable_weekly"}
TRACKER_STALE_SECONDS = 15 * 60


def _events_after(db_path: Path, event_id: int, limit: int = 100) -> list[dict]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT * FROM usage_events
               WHERE id>? ORDER BY id ASC LIMIT ?""",
            (event_id, max(1, min(limit, 500))),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _max_event_id(db_path: Path) -> int:
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM usage_events"
        ).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _tracker_status(
    db_path: Path,
    *,
    now: float | None = None,
) -> tuple[bool, int | None, str]:
    """Whether the latest collection has any usable provider telemetry."""
    now = time.time() if now is None else now
    if not db_path.is_file():
        return False, None, "history database unavailable"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(
            """SELECT captured_at,COUNT(*) AS providers,SUM(ok) AS usable
               FROM usage_samples
               WHERE captured_at=(SELECT MAX(captured_at) FROM usage_samples)"""
        ).fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return False, None, "no quota samples"
    captured_at = int(row[0])
    if now - captured_at > TRACKER_STALE_SECONDS:
        return False, captured_at, "collector stale"
    if int(row[2] or 0) == 0:
        return False, captured_at, "all provider sources unavailable"
    return True, captured_at, "tracking"


def _window_label(value: str | None) -> str:
    return (value or "quota").replace("_", "-")


def _reset_label(epoch: int | None) -> str:
    if not epoch:
        return "unknown"
    return datetime.fromtimestamp(epoch, CENTRAL).strftime("%a %b %-d, %-I:%M %p %Z")


def _notification(event: dict) -> dict | None:
    event_type = str(event.get("event_type") or "")
    window_name = str(event.get("window") or "")
    if event_type != "window_rolled_over" or window_name not in RESET_WINDOWS:
        return None
    provider = str(event.get("provider") or "provider").title()
    window = _window_label(window_name)
    condition = f"model_quota_{event_type}"

    return {
        "source": "model_usage_watch",
        "condition": condition,
        "host": f"{event.get('provider')}:{event.get('window') or 'all'}",
        "event_key": f"model-usage-event:{event['id']}",
        "title": f"{provider} {window} quota reset",
        "body": f"New window ends {_reset_label(event.get('resets_at'))}.",
        "navigate": "/activity?tab=models",
        "emoji": "🔄",
    }


async def scan_once(db_path: Path | None = None) -> dict:
    db_path = db_path or settings.model_usage_history_db
    tracker_available, tracker_captured_at, tracker_reason = _tracker_status(db_path)
    previous_tracker_available = (
        notify_store.get_model_usage_tracker_available()
    )
    tracker_fired = 0
    if previous_tracker_available is None:
        notify_store.set_model_usage_tracker_available(tracker_available)
    elif previous_tracker_available and not tracker_available:
        result = await notify({
            "source": "model_usage_watch",
            "condition": "model_quota_tracker_unavailable",
            "host": "fleet:all",
            "event_key": (
                "model-usage-tracker-loss:"
                f"{tracker_captured_at or int(time.time() // 60)}"
            ),
            "title": "⚠️ Model usage tracker unavailable",
            "body": (
                "No Claude, Codex, or Gemini quota data is currently "
                f"trackable ({tracker_reason})."
            ),
            "navigate": "/activity?tab=models",
            "emoji": "⚠️",
        })
        tracker_fired = int(not result.get("suppressed", False))
        notify_store.set_model_usage_tracker_available(False)
    elif not previous_tracker_available and tracker_available:
        # Recovery is intentionally silent; it merely arms the next outage edge.
        notify_store.set_model_usage_tracker_available(True)

    watermark = notify_store.get_model_usage_watch_watermark()
    if watermark is None:
        baseline = _max_event_id(db_path)
        notify_store.set_model_usage_watch_watermark(baseline)
        log.info("model_usage_watch: seeded watermark at event %d", baseline)
        return {
            "seeded": True,
            "watermark": baseline,
            "seen": 0,
            "fired": tracker_fired,
            "tracker_available": tracker_available,
        }

    events = _events_after(db_path, watermark)
    fired = tracker_fired
    for event in events:
        note = _notification(event)
        if note is not None:
            result = await notify(note)
            fired += int(not result.get("suppressed", False))
        # Advance after notify. If the process dies between notify and this
        # write, notify's event_key ledger suppresses the replay next tick.
        notify_store.set_model_usage_watch_watermark(event["id"])
        watermark = event["id"]
    return {
        "seeded": False,
        "watermark": watermark,
        "seen": len(events),
        "fired": fired,
        "tracker_available": tracker_available,
    }
