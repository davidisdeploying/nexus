"""
Live seat-activity event store (absorbed from delta's sessions-viewer).

The Nexus now hosts the collector itself: hook events POST to /events,
land in a local SQLite store on worker2, and fan out to connected browsers over a
WebSocket. This is the *live* half of the page; the poll-based fleet + jobs/
relay-run panels are the other half. The two halves share one app, one origin, one gate.

Schema is ported verbatim from the delta collector so the hook payload shape is
unchanged — a fresh event history on worker2 is fine (we do NOT migrate delta's DB).
The store lives OUTSIDE the vault (under Nexus's host-local state root): high-churn
tool-activity rows are not vault material, and keeping them off Syncthing avoids
write amplification across the mesh.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings
from .runtime_paths import EVENTS_DB

log = logging.getLogger("nexus.events")

# events.db sits in Nexus's host-local state root, not in the vault. High-churn, local-only,
# rebuildable — the opposite of the durable vault records.
DB_PATH = EVENTS_DB

# Fields accepted from a hook POST body. ts/id are server-assigned.
EVENT_FIELDS = ("seat", "host", "session_id", "run_token", "event_type",
                "tool_name", "summary", "payload")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                seat TEXT,
                host TEXT,
                session_id TEXT,
                run_token TEXT,
                event_type TEXT,
                tool_name TEXT,
                summary TEXT,
                payload TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def insert_event(body: dict) -> dict:
    """Persist one hook event and return the stored row dict (with id + ts)."""
    ts = datetime.now(timezone.utc).isoformat()
    vals = {k: body.get(k) for k in EVENT_FIELDS}
    # payload may arrive as a dict/list -> store as JSON text
    if vals["payload"] is not None and not isinstance(vals["payload"], str):
        try:
            vals["payload"] = json.dumps(vals["payload"])
        except Exception:
            vals["payload"] = str(vals["payload"])

    conn = _db()
    try:
        cur = conn.execute(
            """INSERT INTO events
               (ts, seat, host, session_id, run_token, event_type,
                tool_name, summary, payload)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, vals["seat"], vals["host"], vals["session_id"],
             vals["run_token"], vals["event_type"], vals["tool_name"],
             vals["summary"], vals["payload"]),
        )
        conn.commit()
        row_id = cur.lastrowid
    finally:
        conn.close()
    return {"id": row_id, "ts": ts, **vals}


def prune_events(
    retention_days: int,
    *,
    db_path: Path = DB_PATH,
    now: datetime | None = None,
    vacuum: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Delete expired activity rows without touching other shared DB state.

    Scheduled maintenance leaves freed pages for SQLite to reuse. ``vacuum`` is
    reserved for explicit offline maintenance and is never used by the scheduler.
    Pass ``conn`` to run inside a caller-owned transaction (the daily sweep
    prunes events + notification_log + run_watch_seen in one bounded
    transaction against the shared events.db file); otherwise this opens and
    commits its own connection, unchanged from prior standalone behavior.
    """
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    cutoff_text = cutoff.isoformat()
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(db_path)
    try:
        if owns_conn:
            conn.execute("PRAGMA busy_timeout=5000")
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        cursor = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff_text,))
        if owns_conn:
            conn.commit()
        if vacuum:
            conn.execute("VACUUM")
        after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "retention_days": retention_days,
            "cutoff": cutoff_text,
            "before": before,
            "deleted": cursor.rowcount,
            "after": after,
            "vacuumed": vacuum,
        }
    finally:
        if owns_conn:
            conn.close()


def prune_retention_sweep(
    *,
    db_path: Path = DB_PATH,
    now: datetime | None = None,
    events_retention_days: int | None = None,
    notification_log_retention_days: int | None = None,
    run_watch_seen_retention_days: int | None = None,
) -> dict:
    """The daily events-retention job's full body: events, notification_log,
    and run_watch_seen all pruned in ONE bounded SQL transaction against the
    single shared events.db file (all three tables live in the same file —
    see notify_store's module docstring). One commit, so a mid-sweep failure
    leaves every table exactly as it was, never a partial prune."""
    from . import notify_store  # local import: notify_store has no reason to import events

    events_days = events_retention_days if events_retention_days is not None else settings.events_retention_days
    notif_days = (
        notification_log_retention_days
        if notification_log_retention_days is not None
        else settings.notification_log_retention_days
    )
    watch_days = (
        run_watch_seen_retention_days
        if run_watch_seen_retention_days is not None
        else settings.run_watch_seen_retention_days
    )

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        events_result = prune_events(events_days, now=now, conn=conn)
        notification_result = notify_store.prune_notification_log(notif_days, now=now, conn=conn)
        run_watch_result = notify_store.prune_run_watch_seen(watch_days, now=now, conn=conn)
        conn.commit()
    finally:
        conn.close()
    return {
        "events": events_result,
        "notification_log": notification_result,
        "run_watch_seen": run_watch_result,
    }


async def run_event_retention() -> None:
    """Daily scheduler entry point; keep SQLite work off the event loop."""
    result = await asyncio.to_thread(prune_retention_sweep)
    ev, nl, rw = result["events"], result["notification_log"], result["run_watch_seen"]
    log.info(
        "retention sweep complete: events deleted=%d retained=%d cutoff=%s | "
        "notification_log deleted=%d retained=%d cutoff=%s | "
        "run_watch_seen deleted=%d retained=%d cutoff=%s",
        ev["deleted"], ev["after"], ev["cutoff"],
        nl["deleted"], nl["after"], nl["cutoff"],
        rw["deleted"], rw["after"], rw["cutoff"],
    )


def newest_ts() -> str | None:
    """ISO ts of the most recent event (or None on empty feed). Cheap single-row
    read for the dashboard's first-paint 'idle · last event Xago' affordance; the
    client takes over recency tracking from the live list after load."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT ts FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row["ts"] if row else None


def read_events(since: int = 0, limit: int = 200) -> list[dict]:
    """Backfill: rows id>since (or last N), oldest->newest. Cheap tail read."""
    limit = max(1, min(limit, 2000))
    conn = _db()
    try:
        if since > 0:
            rows = conn.execute(
                "SELECT * FROM events WHERE id>? ORDER BY id ASC LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) "
                "ORDER BY id ASC",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


class Hub:
    """Tracks connected WS clients and broadcasts row dicts. A slow or dead
    client is dropped without blocking ingest (send failures are swallowed and
    the client is queued for removal). Ported from the delta collector — the
    same fan-out already proven through the Cloudflare tunnel behind Access."""

    def __init__(self) -> None:
        self._clients: set = set()
        self._lock = asyncio.Lock()

    async def register(self, ws) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, row: dict) -> None:
        text = json.dumps(row)
        async with self._lock:
            targets = list(self._clients)
        dead = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


hub = Hub()
