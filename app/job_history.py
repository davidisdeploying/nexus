"""
Logical-job attempt history — the durable spine behind the job cards.

A job that stops/fails/restarts many times is ONE logical job (keyed by its
`job` id) with many ATTEMPTS. This module persists every attempt and derives the
per-job aggregate — Σ active-time across attempts, wall-span, outcomes-summary —
so a card shows the honest total (time summed across all runs), not just the
current run's hours.

Store: a local SQLite ``jobs_history.db`` beside events.db in Nexus's host-local state root, OUTSIDE the vault.
Attempt rows are local, rebuildable operational state (like events.db), not
durable vault records — keeping them off Syncthing avoids write-amp across the
mesh. Every call site in work.py guards this module: any failure here degrades
one card's aggregate to live-only and NEVER touches the sweep.

Model
-----
attempts:  one row per (process) attempt of a logical job.
  run_id         PK — helper: a uuid stamped on the process's first beat;
                       adapter (inferred): "<job>:<pid>:<start_epoch>".
  job            logical-job id (the card key; many attempts share it)
  host, started_utc, ended_utc(NULL=open), outcome(running|done|failed|ended),
  progress_done, progress_total, duration_s, source(live|backfill),
  pid, started_epoch  (last two: adapter-inference bookkeeping)

Derived per job (aggregate()): attempts_count, active_time_s = Σ duration_s,
wall_span = first started → last ended(or now), outcomes_summary, latest_progress.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .runtime_paths import JOBS_HISTORY_DB

log = logging.getLogger("nexus.job_history")

# Beside events.db in Nexus's host-local state root: local and rebuildable.
DB_PATH = JOBS_HISTORY_DB

# A new inferred attempt is opened only when the process's uptime resets past
# this many seconds (or the pid changes). Well above per-sweep timing jitter in
# `now - etimes`, so a live attempt keeps matching its own open row.
RESET_TOLERANCE_S = 300

# Central-time offset for the dual-stamp. CDT = UTC-5 (summer); the vault
# convention stamps UTC first, Central second.
_CENTRAL_OFFSET_H = 5
_CENTRAL_LABEL = "CDT"

VALID_OUTCOMES = ("running", "done", "failed", "ended")


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def dual_stamp(epoch: float | None) -> str:
    """`YYYY-MM-DD HH:MM UTC / HH:MM CDT` — the fleet's dual-stamp for a boundary."""
    if epoch is None:
        return ""
    u = datetime.fromtimestamp(epoch, timezone.utc)
    c = u - timedelta(hours=_CENTRAL_OFFSET_H)
    return f"{u:%Y-%m-%d %H:%M} UTC / {c:%H:%M} {_CENTRAL_LABEL}"


def _fmt_dur(secs: float | None) -> str:
    secs = int(max(0, secs or 0))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Idempotent schema create. Cheap to call every sweep."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS attempts (
                run_id         TEXT PRIMARY KEY,
                job            TEXT NOT NULL,
                host           TEXT,
                started_utc    TEXT,
                ended_utc      TEXT,
                outcome        TEXT,
                progress_done  INTEGER,
                progress_total INTEGER,
                duration_s     INTEGER,
                source         TEXT,
                pid            TEXT,
                started_epoch  REAL,
                updated_utc    TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_job "
            "ON attempts(job, started_epoch)"
        )
        # Manual "mark done" mutes. A muted job is force-rendered `done` and its
        # inferred adapter is short-circuited so it stops re-opening an attempt
        # every sweep the live PID is seen. Auto-cleared when the job genuinely
        # relaunches (new pid, or uptime reset past RESET_TOLERANCE_S) — see
        # is_muted(). marked_utc is the dual-stamp of when David hit the button.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS muted "
            "(job TEXT PRIMARY KEY, muted_pid TEXT, muted_started REAL, "
            "muted_epoch REAL, marked_utc TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# manual "mark done" mutes — a durable close + adapter short-circuit
# --------------------------------------------------------------------------- #
def mark_done(job: str, now_epoch: float, pid=None,
              started_epoch: float | None = None) -> dict[str, Any]:
    """Close the job's open attempt as `done` AND record a mute so the inferred
    adapter stops re-opening it every sweep the live PID is seen. The mute is
    keyed to (pid, started_epoch): a genuine relaunch clears it (see is_muted).
    If pid/started_epoch aren't passed, take them from the open attempt. Returns
    the stored mute row as a dict."""
    open_a = get_open_attempt(job)
    if open_a:
        close_attempt(open_a["run_id"], now_epoch, "done")
        if pid is None:
            pid = open_a.get("pid")
        if started_epoch is None:
            started_epoch = open_a.get("started_epoch")
    conn = _db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO muted "
            "(job, muted_pid, muted_started, muted_epoch, marked_utc) "
            "VALUES (?,?,?,?,?)",
            (job, str(pid) if pid is not None else None, started_epoch,
             now_epoch, dual_stamp(now_epoch)),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "job": job,
        "muted_pid": str(pid) if pid is not None else None,
        "muted_started": started_epoch,
        "muted_epoch": now_epoch,
        "marked_utc": dual_stamp(now_epoch),
    }


def is_muted(job: str, pid=None, started_epoch: float | None = None) -> bool:
    """True if `job` is currently muted. Mirrors _record_inferred_attempt's
    is_new test: a new PID, or an uptime reset past RESET_TOLERANCE_S, means the
    job genuinely relaunched — clear the stale mute and return False so the card
    goes live again. Otherwise it's still the same muted run → True."""
    conn = _db()
    try:
        r = conn.execute("SELECT * FROM muted WHERE job=?", (job,)).fetchone()
    finally:
        conn.close()
    if r is None:
        return False
    row = _row(r)
    relaunched = (
        (pid is not None and row.get("muted_pid") is not None
         and str(pid) != str(row["muted_pid"]))
        or (started_epoch is not None and row.get("muted_started") is not None
            and started_epoch > row["muted_started"] + RESET_TOLERANCE_S)
    )
    if relaunched:
        clear_mute(job)
        return False
    return True


def clear_mute(job: str) -> None:
    """Drop a job's mute — the un-mute safety hatch (POST .../undone)."""
    conn = _db()
    try:
        conn.execute("DELETE FROM muted WHERE job=?", (job,))
        conn.commit()
    finally:
        conn.close()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {k: r[k] for k in r.keys()}


def get_open_attempt(job: str) -> dict[str, Any] | None:
    """The job's newest still-open (ended_utc IS NULL) attempt, or None."""
    conn = _db()
    try:
        r = conn.execute(
            "SELECT * FROM attempts WHERE job=? AND ended_utc IS NULL "
            "ORDER BY started_epoch DESC LIMIT 1",
            (job,),
        ).fetchone()
    finally:
        conn.close()
    return _row(r) if r else None


def get_attempts(job: str) -> list[dict[str, Any]]:
    """All attempts for a job, oldest → newest."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE job=? ORDER BY started_epoch ASC, rowid ASC",
            (job,),
        ).fetchall()
    finally:
        conn.close()
    return [_row(r) for r in rows]


def open_attempt(run_id: str, job: str, host: str | None, started_epoch: float,
                 source: str, done=None, total=None, pid=None,
                 outcome: str = "running") -> None:
    """Insert a fresh attempt. INSERT OR IGNORE keeps a re-derived id idempotent."""
    now_iso = _iso(started_epoch)
    conn = _db()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO attempts
               (run_id, job, host, started_utc, ended_utc, outcome,
                progress_done, progress_total, duration_s, source,
                pid, started_epoch, updated_utc)
               VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?,?)""",
            (run_id, job, host, dual_stamp(started_epoch), outcome,
             _int(done), _int(total), 0, source,
             str(pid) if pid is not None else None, started_epoch, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def update_attempt(run_id: str, now_epoch: float, done=None, total=None,
                   duration_s=None, pid=None) -> None:
    """Refresh an open attempt in place (progress + live duration)."""
    conn = _db()
    try:
        conn.execute(
            """UPDATE attempts SET progress_done=?, progress_total=?,
               duration_s=?, pid=COALESCE(?,pid), updated_utc=?
               WHERE run_id=? AND ended_utc IS NULL""",
            (_int(done), _int(total), _int(duration_s),
             str(pid) if pid is not None else None, _iso(now_epoch), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def close_attempt(run_id: str, ended_epoch: float, outcome: str,
                  done=None, total=None, duration_s=None) -> None:
    """Close an attempt with a terminal outcome. Idempotent on an already-closed row."""
    if outcome not in VALID_OUTCOMES:
        outcome = "ended"
    conn = _db()
    try:
        conn.execute(
            """UPDATE attempts SET ended_utc=?, outcome=?,
               progress_done=COALESCE(?,progress_done),
               progress_total=COALESCE(?,progress_total),
               duration_s=COALESCE(?,duration_s), updated_utc=?
               WHERE run_id=?""",
            (dual_stamp(ended_epoch), outcome, _int(done), _int(total),
             _int(duration_s), _iso(ended_epoch), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def seed_attempt(run_id: str, job: str, host: str | None, started_epoch: float,
                 ended_epoch: float | None, outcome: str, done=None, total=None,
                 source: str = "backfill") -> None:
    """Insert (or replace) a fully-formed historical attempt — the backfill path."""
    dur = int(max(0, (ended_epoch - started_epoch))) if ended_epoch else None
    conn = _db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO attempts
               (run_id, job, host, started_utc, ended_utc, outcome,
                progress_done, progress_total, duration_s, source,
                pid, started_epoch, updated_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, job, host, dual_stamp(started_epoch),
             dual_stamp(ended_epoch) if ended_epoch else None,
             outcome if outcome in VALID_OUTCOMES else "ended",
             _int(done), _int(total), dur, source, None, started_epoch,
             _iso(ended_epoch) if ended_epoch else _iso(started_epoch)),
        )
        conn.commit()
    finally:
        conn.close()


def purge_job(job: str) -> int:
    """Delete every attempt for a job (test-data teardown). Returns rows removed."""
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM attempts WHERE job=?", (job,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _int(x):
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# aggregation
# --------------------------------------------------------------------------- #
def _summarize_outcomes(rows: list[dict[str, Any]]) -> str:
    """e.g. "2 failed · 1 stopped · running" — failed/stopped/done counts, then
    a trailing 'running' if an attempt is open."""
    cnt = Counter((r.get("outcome") or "") for r in rows)
    parts: list[str] = []
    for oc, label in (("failed", "failed"), ("ended", "stopped"), ("done", "done")):
        n = cnt.get(oc, 0)
        if n:
            parts.append(f"{n} {label}")
    if cnt.get("running"):
        parts.append("running")
    return " · ".join(parts)


def aggregate(job: str, now_epoch: float,
              rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Derive the per-job aggregate. Isolated: bad rows are skipped, never raise."""
    rows = rows if rows is not None else get_attempts(job)
    if not rows:
        return {}
    active = sum((r.get("duration_s") or 0) for r in rows)
    starts = [r["started_epoch"] for r in rows if r.get("started_epoch")]
    first = min(starts) if starts else None
    open_rows = [r for r in rows if not r.get("ended_utc")]
    if open_rows:
        last_end = now_epoch
    else:
        ends = [_epoch(_utc_of(r.get("ended_utc"))) for r in rows]
        ends = [e for e in ends if e]
        last_end = max(ends) if ends else now_epoch
    wall = int(max(0, last_end - first)) if first else 0
    latest = rows[-1]
    n = len(rows)
    return {
        "attempts_count": n,
        "prior_attempts": n - 1,
        "active_time_s": int(active),
        "active_time": _fmt_dur(active),
        "wall_span_s": wall,
        "wall_span": _fmt_dur(wall),
        "outcomes_summary": _summarize_outcomes(rows),
        "first_started": rows[0].get("started_utc") or "",
        "latest_progress": {
            "done": latest.get("progress_done"),
            "total": latest.get("progress_total"),
        },
        "has_history": n > 1,
    }


def _utc_of(stamp: str | None) -> str | None:
    """Pull the ISO-ish UTC leading part out of a dual-stamp for epoch parsing.
    A dual-stamp is 'YYYY-MM-DD HH:MM UTC / HH:MM CDT'."""
    if not stamp:
        return None
    head = stamp.split(" UTC")[0].strip()
    return head.replace(" ", "T") if head else None
