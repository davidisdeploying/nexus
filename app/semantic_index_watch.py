"""
Read-only Nexus view onto the nightly semantic index rebuild.

Two independent sources, deliberately not conflated:

1. Did it run, and did the reindex succeed? -- charlie's **user-scope**
   `fleet-maint.service` (daily 08:15 UTC + up to 15m jitter, GPU 0).
   Probed over SSH by the central single-flight heartbeat, never per-render.
2. Are both collections populated? -- local read of tower's normalized
   state path, `~/.local/share/tower/index/vault.db`: the high-signal
   Markdown index (`notes`/`chunks`) and the separate transcript-evidence
   collection (`transcript_docs`/`transcript_chunks`).

SCOPE TRAP: `fleet-maint` is a **user** unit on charlie. Querying the system
scope returns `Result=success` / `ExecMainStatus=0` for a unit that does not
exist there, which would render a permanently green tile for a job that never
ran. The probe therefore always exports XDG_RUNTIME_DIR and passes --user.

The unit covers reindex *and* a .trash purge, so unit success alone is not
proof the index rebuilt. Health keys on `reindex_rc` from the SUMMARY line.

The index database exposes no authoritative build timestamp; its file mtime is
reported separately and explicitly labelled, never relabelled as a build time.

Replaces app/compendium_watch.py, retired 2026-08-03: the Library
rebuild it watched is no longer the fleet's index path.
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("nexus.semantic_index_watch")

INDEX_PATH = Path(os.environ.get(
    "TOWER_INDEX_PATH",
    Path.home() / ".local" / "share" / "tower" / "index" / "vault.db",
))
MAINT_HOST = "charlie"
MAINT_UNIT = "fleet-maint.service"
PROBE_TIMEOUT_SECONDS = 15

# Daily 08:15 UTC + up to 15m randomized delay, so successive successful runs
# can legitimately sit ~24h15m apart. A single missed run trips RED well before
# the following day's window.
STALE_AMBER_HOURS = 26
STALE_RED_HOURS = 30

_SUMMARY_RC_RE = re.compile(r"reindex_rc=(?P<rc>-?\d+)")

# Fixed script; no interpolation of caller-supplied data.
_REMOTE_SCRIPT = (
    'export XDG_RUNTIME_DIR=/run/user/$(id -u); '
    'printf "RESULT=%s\\n" "$(systemctl --user show ' + MAINT_UNIT + ' -p Result --value)"; '
    'printf "EXEC_STATUS=%s\\n" "$(systemctl --user show ' + MAINT_UNIT + ' -p ExecMainStatus --value)"; '
    'TS="$(systemctl --user show ' + MAINT_UNIT + ' -p ExecMainExitTimestamp --value)"; '
    'printf "EXIT_EPOCH=%s\\n" "$(date -u -d "$TS" +%s 2>/dev/null || echo)"; '
    'printf "SUMMARY=%s\\n" "$(journalctl --user -u ' + MAINT_UNIT + ' -n 300 --no-pager -o cat '
    '2>/dev/null | grep -F \'SUMMARY:\' | tail -1)"'
)

# Refreshed by app/heartbeat_runner.py's single-flight heartbeat, mirroring the
# retired compendium tile's cached-probe contract: no network call on render.
_maint_cache: dict[str, Any] = {"data": None, "error": None, "checked_at": None}


def _run_probe() -> tuple[int, str, str]:
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        MAINT_HOST, "--", "sh", "-c", _REMOTE_SCRIPT,
    ]
    try:
        proc = subprocess.run(
            argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=PROBE_TIMEOUT_SECONDS, check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"probe exceeded {PROBE_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


def _parse_probe(stdout: str) -> dict[str, Any] | None:
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    if "RESULT" not in fields or "EXEC_STATUS" not in fields:
        return None

    epoch_text = fields.get("EXIT_EPOCH") or ""
    try:
        exit_epoch: float | None = float(epoch_text) if epoch_text else None
    except ValueError:
        exit_epoch = None

    summary = fields.get("SUMMARY") or ""
    rc_match = _SUMMARY_RC_RE.search(summary)
    reindex_rc = int(rc_match.group("rc")) if rc_match else None

    try:
        exec_status: int | None = int(fields["EXEC_STATUS"])
    except ValueError:
        exec_status = None

    return {
        "result": fields["RESULT"] or None,
        "exec_status": exec_status,
        "exit_epoch": exit_epoch,
        "reindex_rc": reindex_rc,
        "summary": summary or None,
    }


async def probe_maint_once() -> None:
    """Scheduled job body -- one bounded SSH to charlie, result cached for
    read_status() to consume without a fresh network call."""
    rc, stdout, stderr = _run_probe()
    if rc != 0:
        detail = (stderr or stdout or f"rc={rc}").strip()
        log.info("semantic_index_watch: maint probe failed rc=%s: %s", rc, detail[:200])
        _maint_cache["data"] = None
        _maint_cache["error"] = f"maint probe failed (rc={rc})"
    else:
        parsed = _parse_probe(stdout)
        if parsed is None:
            log.info("semantic_index_watch: maint probe returned unparseable output")
            _maint_cache["data"] = None
            _maint_cache["error"] = "maint probe: unparseable output"
        else:
            _maint_cache["data"] = parsed
            _maint_cache["error"] = None
    _maint_cache["checked_at"] = time.time()


def _read_index_counts() -> dict[str, Any]:
    """Local, read-only. Mirrors tower vaultsearch.index_metadata's
    honesty contract: file mtime is reported as publication time only, never as
    an authoritative build timestamp."""
    try:
        st = os.stat(INDEX_PATH)
    except OSError as exc:
        return {"ok": False, "error": f"index unavailable: {type(exc).__name__}"}

    db = None
    try:
        db = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True, timeout=5)

        def _table_exists(name: str) -> bool:
            return db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None

        markdown_docs, latest_indexed_at = db.execute(
            "SELECT COUNT(*), MAX(indexed_at) FROM notes"
        ).fetchone()
        markdown_chunks = (
            db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            if _table_exists("chunks") else 0
        )
        transcript_present = _table_exists("transcript_docs")
        transcript_docs = (
            db.execute("SELECT COUNT(*) FROM transcript_docs").fetchone()[0]
            if transcript_present else 0
        )
        transcript_chunks = (
            db.execute("SELECT COUNT(*) FROM transcript_chunks").fetchone()[0]
            if _table_exists("transcript_chunks") else 0
        )
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "error": f"index unreadable: {type(exc).__name__}"}
    finally:
        if db is not None:
            db.close()

    return {
        "ok": True,
        "markdown_docs": int(markdown_docs),
        "markdown_chunks": int(markdown_chunks),
        "transcript_docs": int(transcript_docs),
        "transcript_chunks": int(transcript_chunks),
        "latest_document_indexed_at": latest_indexed_at,
        "database_bytes": st.st_size,
        "published_at": datetime.datetime.fromtimestamp(
            st.st_mtime, datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_at_source": "database_file_mtime",
        "authoritative_build_timestamp_available": False,
    }


def read_status() -> dict[str, Any]:
    """Synchronous and local: the SSH leg is the separately-scheduled
    _maint_cache, never probed here. An absent probe, a failed unit, a non-zero
    reindex_rc, or an empty collection is RED with an explicit reason -- never a
    silent green."""
    index = _read_index_counts()
    cached = _maint_cache.get("data")
    cache_error = _maint_cache.get("error")

    if cached is None:
        return {
            "health": "RED", "no_receipt": True,
            "detail": cache_error or "maint receipt not probed yet",
            "index_ok": bool(index.get("ok")),
        }
    if cached.get("exit_epoch") is None:
        return {
            "health": "RED", "no_receipt": True,
            "detail": f"{MAINT_UNIT} has no recorded run on {MAINT_HOST}",
        }

    age_hours = (time.time() - float(cached["exit_epoch"])) / 3600
    last_run_utc = datetime.datetime.fromtimestamp(
        float(cached["exit_epoch"]), datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M")

    reindex_rc = cached.get("reindex_rc")
    unit_ok = cached.get("result") == "success" and cached.get("exec_status") == 0
    reindex_ok = reindex_rc == 0
    markdown_ok = bool(index.get("ok")) and int(index.get("markdown_docs") or 0) > 0
    transcript_ok = bool(index.get("ok")) and int(index.get("transcript_docs") or 0) > 0

    reasons: list[str] = []
    if not index.get("ok"):
        reasons.append(str(index.get("error")))
    else:
        if not markdown_ok:
            reasons.append("markdown index empty")
        if not transcript_ok:
            reasons.append("transcript collection empty")
    if not unit_ok:
        reasons.append(f"unit {cached.get('result')}/{cached.get('exec_status')}")
    if reindex_rc is None:
        reasons.append("no SUMMARY receipt")
    elif not reindex_ok:
        reasons.append(f"reindex_rc={reindex_rc}")
    if age_hours > STALE_RED_HOURS:
        reasons.append(f"last run {age_hours:.1f}h ago")

    if reasons:
        health = "RED"
    elif age_hours > STALE_AMBER_HOURS:
        health = "AMBER"
        reasons.append(f"last run {age_hours:.1f}h ago")
    else:
        health = "GREEN"

    return {
        "health": health,
        "no_receipt": False,
        "detail": " · ".join(reasons) if reasons else None,
        "host": MAINT_HOST,
        "last_run_utc": last_run_utc,
        "age_hours": age_hours,
        "result": cached.get("result"),
        "exec_status": cached.get("exec_status"),
        "reindex_rc": reindex_rc,
        "markdown_ok": markdown_ok,
        "transcript_ok": transcript_ok,
        "index_ok": bool(index.get("ok")),
        "index_error": index.get("error"),
        "markdown_docs": index.get("markdown_docs"),
        "markdown_chunks": index.get("markdown_chunks"),
        "transcript_docs": index.get("transcript_docs"),
        "transcript_chunks": index.get("transcript_chunks"),
        "database_bytes": index.get("database_bytes"),
        "published_at": index.get("published_at"),
        "published_at_source": index.get("published_at_source"),
        "latest_document_indexed_at": index.get("latest_document_indexed_at"),
    }
