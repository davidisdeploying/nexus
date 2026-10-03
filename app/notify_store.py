"""
Nexus notifications — Phase 0 storage foundations.

Tables only: push_subscription, notification_log, alerts (schema per
panel-notifications-design.md §C.3 + §E.3). Nothing sends a push yet — that's
Phase 2/3 (send_push, the auto-router). Right now the only writer is the
/api/notify stub, which logs-and-does-nothing-else so the pipeline can be
exercised end to end before anything user-visible depends on it.

Shares events.db (same rationale as app/events.py: high-churn, local-only,
rebuildable, deliberately outside the vault/Syncthing).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .runtime_paths import EVENTS_DB

DB_PATH = EVENTS_DB


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
            """CREATE TABLE IF NOT EXISTS push_subscription (
                id           INTEGER PRIMARY KEY,
                device_label TEXT NOT NULL,
                endpoint     TEXT NOT NULL UNIQUE,
                p256dh       TEXT NOT NULL,
                auth         TEXT NOT NULL,
                ua           TEXT,
                created_at   TEXT NOT NULL,
                last_send_at TEXT,
                last_confirm_at TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                active       INTEGER DEFAULT 1
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS notification_log (
                id         INTEGER PRIMARY KEY,
                event_key  TEXT NOT NULL,
                channel    TEXT NOT NULL,
                prio       INTEGER NOT NULL,
                title      TEXT NOT NULL,
                body       TEXT,
                navigate   TEXT,
                created_at TEXT NOT NULL,
                read_at    TEXT,
                sent_pwa   INTEGER DEFAULT 0,
                sent_ntfy  INTEGER DEFAULT 0
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nlog_eventkey_time "
            "ON notification_log(event_key, created_at)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS alerts (
                id               INTEGER PRIMARY KEY,
                condition_key    TEXT,
                host             TEXT,
                first_seen       TEXT,
                last_seen        TEXT,
                resolved_at      TEXT,
                last_notified_at TEXT
            )"""
        )
        # Phase-2 flag: the emoji glyph shown on the feed row and prepended to
        # the push title, stored separately from `title` so a future re-skin of
        # either surface doesn't require re-parsing the title string.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(notification_log)")}
        if "emoji" not in cols:
            conn.execute("ALTER TABLE notification_log ADD COLUMN emoji TEXT")
        # Phase-3: the relay-outcome watcher's persisted NOW-watermark + seen-set
        # (app/run_watcher.py). `outcome='baseline'` marks a run directory that
        # already existed when the watermark was established — it is permanently
        # excluded from notify(), which is what makes the no-backfill guarantee
        # survive a service restart (this table IS the watermark).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS run_watch_seen (
                token   TEXT PRIMARY KEY,
                seat    TEXT,
                outcome TEXT,
                seen_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS run_watch_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        # Model-usage event watcher watermark. Kept in events.db with the
        # notification ledger so a Nexus restart cannot replay old quota
        # history into iOS pushes.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS model_usage_watch_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        # Live watchdog evidence (FLEET-AUTO-BUILD-20260802-panel-live-
        # watchdog-evidence): one latest execution receipt per stable
        # APScheduler job id, written by the scheduler's EVENT_JOB_EXECUTED/
        # EVENT_JOB_ERROR/EVENT_JOB_MISSED listener (app/scheduler.py). This
        # is the cadence-aware evidence the watchdog projection layer overlays
        # onto the static app/watchdogs_registry.py rows -- job_id is the
        # PRIMARY KEY so every write is an upsert of the single latest row,
        # never an unbounded append.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scheduler_job_receipt (
                job_id       TEXT PRIMARY KEY,
                scheduled_at TEXT,
                completed_at TEXT NOT NULL,
                outcome      TEXT NOT NULL,
                detail       TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def log_notify_event(body: dict) -> dict:
    """Accept-and-log only (Phase 0 stub): insert one notification_log row from
    a /api/notify POST body. No push is sent, no dedup/routing runs yet — that
    is the Phase 3 auto-router. event_key falls back to a source+condition
    composite so every stub call still lands a coherent dedup key later."""
    source = str(body.get("source") or "unknown")
    condition = body.get("condition")
    token = body.get("token")
    event_key = str(condition or token or source)
    title = str(body.get("title") or source)
    return insert_notification(
        event_key=event_key,
        channel="stub",
        prio=3,
        title=title,
        body=body.get("body"),
        navigate=None,
        emoji=body.get("emoji"),
    )


def insert_notification(
    event_key: str, channel: str, prio: int, title: str,
    body: str | None = None, navigate: str | None = None,
    emoji: str | None = None,
) -> dict:
    """Insert one notification_log row. The single write path for both the
    /api/notify stub and send_push (app/push.py) — the feed must record an
    event even when every push delivery fails, so this always runs FIRST."""
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "event_key": event_key, "channel": channel, "prio": prio,
        "title": title, "body": body, "navigate": navigate,
        "emoji": emoji, "created_at": now,
    }
    conn = _db()
    try:
        cur = conn.execute(
            """INSERT INTO notification_log
               (event_key, channel, prio, title, body, navigate, emoji, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (row["event_key"], row["channel"], row["prio"], row["title"],
             row["body"], row["navigate"], row["emoji"], row["created_at"]),
        )
        conn.commit()
        row_id = cur.lastrowid
    finally:
        conn.close()
    return {"id": row_id, **row}


def update_notification_pwa(row_id: int, sent: bool) -> None:
    """Record whether send_push actually delivered to at least one targeted
    subscription for this notification_log row. Written once, right after the
    send, so the row is a self-contained receipt — no later, unrelated push's
    push_subscription.last_send_at can change what THIS row says happened."""
    conn = _db()
    try:
        conn.execute(
            "UPDATE notification_log SET sent_pwa=? WHERE id=?",
            (1 if sent else 0, row_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_notification_ntfy(row_id: int, sent: bool) -> None:
    """Phase 5: record whether the ntfy send for an already-inserted
    notification_log row succeeded. Called after send_ntfy (fire-and-update,
    not part of the insert) since ntfy and the PWA push are sent independently
    and the row already exists by the time this runs."""
    conn = _db()
    try:
        conn.execute(
            "UPDATE notification_log SET sent_ntfy=? WHERE id=?",
            (1 if sent else 0, row_id),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Phase 1 readers — the nexus-log feed and one-alert lookup. Read-only; no new
# tables/columns. Never raise: an unreadable/missing db yields an empty result
# so a detail route can 200 with a graceful empty-state instead of a 500.
# --------------------------------------------------------------------------- #
def list_notifications(limit: int = 100) -> list[dict]:
    """notification_log, newest-first, for the Notifications inbox."""
    limit = max(1, min(int(limit), 500))
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM notification_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_last_selftest_at() -> str | None:
    """Most recent notification_log.created_at with channel='nexus-selftest'
    (app/self_test.py's weekly canary) — this IS the "last self-test time"
    record; no separate state table needed since the row already carries it."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT created_at FROM notification_log "
            "WHERE channel='nexus-selftest' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row["created_at"] if row else None


def get_last_selftest_receipt() -> dict | None:
    """The full latest nexus-selftest notification_log row (created_at,
    sent_pwa, sent_ntfy) -- the evidence the watchdog projection layer uses
    for the weekly transport-canary row, distinct from get_last_selftest_at's
    timestamp-only read. Never raise: an uninitialized table yields None."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT created_at, sent_pwa, sent_ntfy FROM notification_log "
            "WHERE channel='nexus-selftest' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "created_at": row["created_at"],
        "sent_pwa": bool(row["sent_pwa"]),
        "sent_ntfy": bool(row["sent_ntfy"]),
    }


def get_alert(alert_id: int) -> dict | None:
    """One `alerts` row by id, or None (the table is empty pre-Phase-2 —
    'no such alert' is the expected common case, not an error)."""
    conn = _db()
    try:
        r = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def count_unread() -> int:
    """Unread notification_log rows — the interim app_badge source (design
    §C.2/§D-5 recommends pending-approvals; that table has no rows yet, so
    unread-feed-count is the acceptable stand-in until Phase 3/5)."""
    conn = _db()
    try:
        r = conn.execute(
            "SELECT COUNT(*) AS n FROM notification_log WHERE read_at IS NULL"
        ).fetchone()
    finally:
        conn.close()
    return int(r["n"]) if r else 0


def confirm_navigate(path: str) -> None:
    """?nf=1 handling (design §B.2/§I.5): mark the most recent unread
    notification_log row whose `navigate` targets this path as read, and
    best-effort-stamp last_confirm_at on every ACTIVE subscription (a device
    identity isn't known from a bare page load, so this is a fleet-wide proxy
    signal, not a per-device ack — noted as an interim choice in the Phase 2
    report). Never raises: a confirm miss is silent, mirroring the read-only
    helpers above."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        row = conn.execute(
            """SELECT id FROM notification_log
               WHERE read_at IS NULL AND (navigate = ? OR navigate LIKE ?)
               ORDER BY id DESC LIMIT 1""",
            (path, path + "?%"),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE notification_log SET read_at=? WHERE id=?", (now, row["id"])
            )
        conn.execute(
            "UPDATE push_subscription SET last_confirm_at=? WHERE active=1", (now,)
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — confirm is best-effort, never breaks the page
        pass
    finally:
        conn.close()


def mark_all_read() -> int:
    """Mark every unread notification_log row read. Returns rows affected."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE notification_log SET read_at=? WHERE read_at IS NULL", (now,)
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Phase 2 — push_subscription CRUD (app/push.py is the only sender; routes.py
# calls upsert/deactivate directly from the subscribe/unsubscribe endpoints).
# --------------------------------------------------------------------------- #
def upsert_subscription(endpoint: str, p256dh: str, auth: str,
                         device_label: str, ua: str | None) -> dict:
    """Insert-or-reactivate on `endpoint` (UNIQUE). Re-subscribes are normal
    and expected (design §C.3) — a returning endpoint clears consecutive
    failures and reactivates rather than erroring on the UNIQUE conflict."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO push_subscription
               (device_label, endpoint, p256dh, auth, ua, created_at, active,
                consecutive_failures)
               VALUES (?,?,?,?,?,?,1,0)
               ON CONFLICT(endpoint) DO UPDATE SET
                 device_label=excluded.device_label,
                 p256dh=excluded.p256dh,
                 auth=excluded.auth,
                 ua=excluded.ua,
                 active=1,
                 consecutive_failures=0""",
            (device_label, endpoint, p256dh, auth, ua, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM push_subscription WHERE endpoint=?", (endpoint,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def deactivate_subscription(endpoint: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE push_subscription SET active=0 WHERE endpoint=?", (endpoint,)
        )
        conn.commit()
    finally:
        conn.close()


def list_active_subscriptions() -> list[dict]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM push_subscription WHERE active=1"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_subscription_by_endpoint(endpoint: str) -> dict | None:
    """One push_subscription row by endpoint, or None if this device has never
    subscribed (Phase 6a nag: the current device's own subscription health)."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM push_subscription WHERE endpoint=?", (endpoint,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def mark_subscription_sent(endpoint: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            """UPDATE push_subscription
               SET last_send_at=?, consecutive_failures=0 WHERE endpoint=?""",
            (now, endpoint),
        )
        conn.commit()
    finally:
        conn.close()


def mark_subscription_gone(endpoint: str) -> None:
    """Hard failure (404/410/403 — dead or VAPID-mismatched endpoint):
    deactivate immediately, no retry budget."""
    conn = _db()
    try:
        conn.execute(
            "UPDATE push_subscription SET active=0 WHERE endpoint=?", (endpoint,)
        )
        conn.commit()
    finally:
        conn.close()


def bump_subscription_failure(endpoint: str, deactivate_at: int = 10) -> None:
    """Soft failure (network error, 5xx, timeout): count it; deactivate once
    consecutive_failures reaches the threshold (design §C.4)."""
    conn = _db()
    try:
        conn.execute(
            """UPDATE push_subscription
               SET consecutive_failures = consecutive_failures + 1,
                   active = CASE WHEN consecutive_failures + 1 >= ? THEN 0 ELSE active END
               WHERE endpoint=?""",
            (deactivate_at, endpoint),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Phase 3 — dedup ledger read (app/notify.py) + run_watch NOW-watermark
# (app/run_watcher.py). notification_log doubles as the dedup ledger (design
# §D.2): exact-once run/milestone events check for ANY prior row with the same
# event_key; health alerts check only within a re-fire window.
# --------------------------------------------------------------------------- #
def notification_exists(event_key: str, since_seconds: int | None = None) -> bool:
    """True if a notification_log row with this event_key already exists —
    forever (since_seconds=None, exact-once) or within the trailing window
    (health alerts' 30-min re-fire guard)."""
    conn = _db()
    try:
        if since_seconds is None:
            row = conn.execute(
                "SELECT 1 FROM notification_log WHERE event_key=? LIMIT 1",
                (event_key,),
            ).fetchone()
        else:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=since_seconds)).isoformat()
            row = conn.execute(
                "SELECT 1 FROM notification_log WHERE event_key=? AND created_at>=? LIMIT 1",
                (event_key, cutoff),
            ).fetchone()
    finally:
        conn.close()
    return row is not None


def run_watch_initialized() -> bool:
    """Has the watcher's NOW-watermark already been established? False only
    on the very first tick ever (fresh events.db or a table that predates this
    feature) — every tick after that must skip the baseline pass entirely, or
    a restart mid-flight would re-baseline and swallow a real in-flight run."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT value FROM run_watch_meta WHERE key='initialized_at'"
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def set_run_watch_initialized() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO run_watch_meta (key, value) VALUES ('initialized_at', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


def run_watch_seen(token: str) -> bool:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT 1 FROM run_watch_seen WHERE token=?", (token,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def mark_run_watch_seen(token: str, seat: str, outcome: str) -> None:
    """Insert-or-ignore: a token is marked seen exactly once, whether that's
    the baseline pass (outcome='baseline') or a real terminal outcome. Ignore
    on conflict rather than upsert — the first write wins, so a scan racing
    the baseline pass can never flip a row backwards."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO run_watch_seen (token, seat, outcome, seen_at)
               VALUES (?,?,?,?)
               ON CONFLICT(token) DO NOTHING""",
            (token, seat, outcome, now),
        )
        conn.commit()
    finally:
        conn.close()


def run_watch_seen_count() -> int:
    """Total baselined/notified tokens — used by verification to prove the
    watermark covers every pre-existing run directory."""
    conn = _db()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM run_watch_seen").fetchone()
    finally:
        conn.close()
    return int(row["n"]) if row else 0


def get_model_usage_watch_watermark() -> int | None:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT value FROM model_usage_watch_meta WHERE key='last_event_id'"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def set_model_usage_watch_watermark(event_id: int) -> None:
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO model_usage_watch_meta(key,value)
               VALUES('last_event_id',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(max(0, int(event_id))),),
        )
        conn.commit()
    finally:
        conn.close()


def get_model_usage_tracker_available() -> bool | None:
    """Persisted edge state for the aggregate all-provider telemetry watcher."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT value FROM model_usage_watch_meta "
            "WHERE key='tracker_available'"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row["value"] == "1"


def set_model_usage_tracker_available(available: bool) -> None:
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO model_usage_watch_meta(key,value)
               VALUES('tracker_available',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            ("1" if available else "0",),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Scheduler execution receipts (FLEET-AUTO-BUILD-20260802-panel-live-
# watchdog-evidence). One latest row per job_id -- an upsert, not a log --
# so this table stays O(job count), never grows unbounded. outcome is one of
# "ok" / "error" / "missed"; detail is bounded non-secret free text.
# --------------------------------------------------------------------------- #
_RECEIPT_DETAIL_LIMIT = 300


def record_scheduler_receipt(
    job_id: str, *, outcome: str, completed_at: str,
    scheduled_at: str | None = None, detail: str | None = None,
) -> None:
    if outcome not in ("ok", "error", "missed"):
        raise ValueError(f"invalid scheduler receipt outcome: {outcome!r}")
    bounded_detail = " ".join(str(detail or "").split())
    if len(bounded_detail) > _RECEIPT_DETAIL_LIMIT:
        bounded_detail = bounded_detail[: _RECEIPT_DETAIL_LIMIT - 1] + "…"
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO scheduler_job_receipt
                   (job_id, scheduled_at, completed_at, outcome, detail)
               VALUES (?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                   scheduled_at=excluded.scheduled_at,
                   completed_at=excluded.completed_at,
                   outcome=excluded.outcome,
                   detail=excluded.detail""",
            (job_id, scheduled_at, completed_at, outcome, bounded_detail or None),
        )
        conn.commit()
    finally:
        conn.close()


def get_scheduler_receipt(job_id: str) -> dict | None:
    """The single latest receipt for one job id, or None if it has never
    completed a tick since the table was created (a fresh deploy, or a job
    that has not yet fired) -- or if init_db() has not run yet against this
    DB_PATH at all (a bare test app with no lifespan). Never raise: same
    unreadable/missing-table-yields-empty-result discipline as
    list_notifications."""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM scheduler_job_receipt WHERE job_id=?", (job_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return dict(row) if row else None


def list_scheduler_receipts() -> list[dict]:
    """Every job's latest receipt -- naturally bounded to the registered
    APScheduler job count (a couple dozen rows at most), for projection/tests.
    Never raise: an uninitialized table yields an empty list."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM scheduler_job_receipt ORDER BY job_id"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Retention (FLEET-WORKER2-BUILD-20260721-panel-bounded-retention). Both prune
# on their own authoritative timestamp column (notification_log.created_at,
# run_watch_seen.seen_at — both NOT NULL, server-assigned ISO8601 UTC, same
# shape events.prune_events already cuts on). `conn`, when passed, lets the
# daily scheduler job run every table's DELETE inside ONE bounded transaction
# against the shared events.db file instead of committing per table.
# --------------------------------------------------------------------------- #
def prune_notification_log(
    retention_days: int,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: Path = DB_PATH,
    now: datetime | None = None,
) -> dict:
    """Delete notification_log rows older than `retention_days`, cut on
    created_at. Read/unread state (read_at) does not affect eligibility —
    a stale unread row is still stale."""
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    cutoff_text = ((now or datetime.now(timezone.utc)) - timedelta(days=retention_days)).isoformat()
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(db_path)
    try:
        if owns_conn:
            conn.execute("PRAGMA busy_timeout=5000")
        before = conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
        cursor = conn.execute("DELETE FROM notification_log WHERE created_at < ?", (cutoff_text,))
        if owns_conn:
            conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
        return {
            "table": "notification_log",
            "retention_days": retention_days,
            "cutoff": cutoff_text,
            "before": before,
            "deleted": cursor.rowcount,
            "after": after,
        }
    finally:
        if owns_conn:
            conn.close()


def prune_run_watch_seen(
    retention_days: int,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: Path = DB_PATH,
    now: datetime | None = None,
) -> dict:
    """Delete run_watch_seen rows older than `retention_days`, cut on
    seen_at — the moment a token was baselined or resolved, not the run's own
    activity. A pruned row that somehow still has a live run directory past
    the window would re-evaluate on the next tick; run directories are
    cleaned up well inside 30 days in practice, so this trades an
    unbounded ledger for that (currently theoretical) edge."""
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    cutoff_text = ((now or datetime.now(timezone.utc)) - timedelta(days=retention_days)).isoformat()
    owns_conn = conn is None
    if owns_conn:
        conn = sqlite3.connect(db_path)
    try:
        if owns_conn:
            conn.execute("PRAGMA busy_timeout=5000")
        before = conn.execute("SELECT COUNT(*) FROM run_watch_seen").fetchone()[0]
        cursor = conn.execute("DELETE FROM run_watch_seen WHERE seen_at < ?", (cutoff_text,))
        if owns_conn:
            conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM run_watch_seen").fetchone()[0]
        return {
            "table": "run_watch_seen",
            "retention_days": retention_days,
            "cutoff": cutoff_text,
            "before": before,
            "deleted": cursor.rowcount,
            "after": after,
        }
    finally:
        if owns_conn:
            conn.close()


# --------------------------------------------------------------------------- #
# `alerts` CRUD (app/thermal_watch.py) — one row per (condition_key, host)
# tracks an edge-triggered health condition's own watermark, independent of
# notify()'s 30-min health-alert re-fire window (app/notify.py). `last_seen`
# doubles as the watermark value being compared each sweep (a guard event
# timestamp for thermal_halt, or a "warn"/"clear"-style state string for a
# level-crossing condition) — repurposing the existing column rather than
# adding new ones. `last_notified_at` set means a real notify() fired for the
# open episode; `resolved_at` set means that episode is closed.
# --------------------------------------------------------------------------- #
def get_alert_by_condition(condition_key: str, host: str) -> dict | None:
    """Most recent alerts row for this condition_key/host, or None if this
    condition has never been evaluated before (the WATERMARK-FROM-NOW seed
    case)."""
    conn = _db()
    try:
        row = conn.execute(
            """SELECT * FROM alerts WHERE condition_key=? AND host=?
               ORDER BY id DESC LIMIT 1""",
            (condition_key, host),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_alerts_by_condition(condition_key: str) -> list[dict]:
    """All latest watermark rows for one watcher condition."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE condition_key=? ORDER BY id", (condition_key,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def seed_alert(condition_key: str, host: str, watermark: str) -> dict:
    """Baseline insert on the FIRST-EVER evaluation of a condition_key/host
    pair: records the current state as already-handled with no notification
    sent, so a pre-existing condition (e.g. charlie's already-resolved halt)
    never fires the moment this watcher starts running."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        cur = conn.execute(
            """INSERT INTO alerts (condition_key, host, first_seen, last_seen,
                                    resolved_at, last_notified_at)
               VALUES (?,?,?,?,NULL,NULL)""",
            (condition_key, host, now, watermark),
        )
        conn.commit()
        row_id = cur.lastrowid
    finally:
        conn.close()
    return {"id": row_id, "condition_key": condition_key, "host": host,
            "first_seen": now, "last_seen": watermark,
            "resolved_at": None, "last_notified_at": None}


def mark_alert_notified(alert_id: int, watermark: str) -> None:
    """A real notify() fired for this alert: advance the watermark, stamp
    last_notified_at, and reopen (clear resolved_at) so the paired recovery
    edge becomes eligible."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            """UPDATE alerts SET last_seen=?, last_notified_at=?, resolved_at=NULL
               WHERE id=?""",
            (watermark, now, alert_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_alert_resolved(alert_id: int) -> None:
    """Close the open episode after its recovery notify() fired."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute("UPDATE alerts SET resolved_at=? WHERE id=?", (now, alert_id))
        conn.commit()
    finally:
        conn.close()


def update_alert_seen(alert_id: int, watermark: str) -> None:
    """Advance last_seen WITHOUT marking notified — for level-crossing
    conditions recording a silent state transition (e.g. warn->clear,
    stale->fresh) that doesn't itself fire a notification."""
    conn = _db()
    try:
        conn.execute("UPDATE alerts SET last_seen=? WHERE id=?", (watermark, alert_id))
        conn.commit()
    finally:
        conn.close()


def retire_alert(alert_id: int) -> None:
    """Close a watermark whose declared check was intentionally retired.

    Retirement is a policy migration, not a fleet recovery, so this writes no
    notification and leaves the historical notification log untouched.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            "UPDATE alerts SET last_seen='retired', resolved_at=? WHERE id=?",
            (now, alert_id),
        )
        conn.commit()
    finally:
        conn.close()
