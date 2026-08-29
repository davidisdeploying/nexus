"""End-to-end health for Charlie's primary and Alpha's fallback index."""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

log = logging.getLogger("nexus.semantic_index_watch")

INDEX_ROOT = Path(os.environ.get(
    "TOWER_INDEX_ROOT", Path.home() / ".local" / "share" / "tower" / "index"
))
INDEX_PATH = INDEX_ROOT / "vault.db"
MANIFEST_PATH = INDEX_ROOT / "index-manifest.json"
MAINT_HOST = "charlie"
MAINT_UNIT = "fleet-maint.service"
PRIMARY_URL = "http://100.64.0.35:8766/metadata"
PROBE_TIMEOUT_SECONDS = 20
STALE_AMBER_HOURS = 26
STALE_RED_HOURS = 30
SYNCTHING_FOLDER_IDS = ("tower-index", "tower-index")

_SUMMARY_RC_RE = re.compile(r"reindex_rc=(?P<rc>-?\d+)")
_REMOTE_SCRIPT = (
    'export XDG_RUNTIME_DIR=/run/user/$(id -u); '
    'printf "RESULT=%s\\n" "$(systemctl --user show ' + MAINT_UNIT + ' -p Result --value)"; '
    'printf "EXEC_STATUS=%s\\n" "$(systemctl --user show ' + MAINT_UNIT + ' -p ExecMainStatus --value)"; '
    'TS="$(systemctl --user show ' + MAINT_UNIT + ' -p ExecMainExitTimestamp --value)"; '
    'printf "EXIT_EPOCH=%s\\n" "$(date -u -d "$TS" +%s 2>/dev/null || echo)"; '
    'printf "SUMMARY=%s\\n" "$(journalctl --user -u ' + MAINT_UNIT + ' -n 300 --no-pager -o cat '
    '2>/dev/null | grep -F \'SUMMARY:\' | tail -1)"; '
    'printf "PRIMARY_JSON=%s\\n" "$(curl -fsS --max-time 8 ' + PRIMARY_URL + ' 2>/dev/null || echo)"'
)

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
    try:
        exit_epoch = float(fields["EXIT_EPOCH"]) if fields.get("EXIT_EPOCH") else None
    except ValueError:
        exit_epoch = None
    try:
        exec_status = int(fields["EXEC_STATUS"])
    except ValueError:
        exec_status = None
    summary = fields.get("SUMMARY") or ""
    match = _SUMMARY_RC_RE.search(summary)
    try:
        primary = json.loads(fields.get("PRIMARY_JSON") or "{}")
    except json.JSONDecodeError:
        primary = {}
    return {
        "result": fields["RESULT"] or None,
        "exec_status": exec_status,
        "exit_epoch": exit_epoch,
        "reindex_rc": int(match.group("rc")) if match else None,
        "summary": summary or None,
        "primary": primary if isinstance(primary, dict) else {},
    }


async def probe_maint_once() -> None:
    rc, stdout, stderr = _run_probe()
    if rc != 0:
        detail = (stderr or stdout or f"rc={rc}").strip()
        log.info("semantic_index_watch probe failed rc=%s: %s", rc, detail[:200])
        _maint_cache.update(data=None, error=f"maint probe failed (rc={rc})", checked_at=time.time())
        return
    parsed = _parse_probe(stdout)
    if parsed is None:
        _maint_cache.update(data=None, error="maint probe: unparseable output", checked_at=time.time())
    else:
        _maint_cache.update(data=parsed, error=None, checked_at=time.time())


def _read_syncthing() -> dict[str, Any]:
    candidates = (
        Path.home() / ".local" / "state" / "syncthing" / "config.xml",
        Path.home() / ".config" / "syncthing" / "config.xml",
    )
    config = next((path for path in candidates if path.exists()), None)
    if config is None:
        return {"ok": False, "error": "Syncthing config unavailable"}
    try:
        key = ET.parse(config).getroot().findtext("./gui/apikey")
        if not key:
            raise ValueError("API key missing")
        for folder_id in SYNCTHING_FOLDER_IDS:
            encoded = urllib.parse.quote(folder_id, safe="")
            request = urllib.request.Request(
                f"http://127.0.0.1:8384/rest/db/status?folder={encoded}",
                headers={"X-API-Key": key},
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    status = json.loads(response.read())
                return {
                    "ok": not bool(status.get("error")),
                    "folder_id": folder_id,
                    "state": status.get("state"),
                    "error": status.get("error") or None,
                    "need_files": int(status.get("needFiles") or 0),
                    "need_bytes": int(status.get("needBytes") or 0),
                    "global_bytes": int(status.get("globalBytes") or 0),
                    "local_bytes": int(status.get("localBytes") or 0),
                }
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
        return {"ok": False, "error": "Tower index folder not configured"}
    except (OSError, ValueError, ET.ParseError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Syncthing status unavailable: {type(exc).__name__}"}


def _read_index_counts() -> dict[str, Any]:
    try:
        st = INDEX_PATH.stat()
    except OSError as exc:
        return {"ok": False, "error": f"fallback index unavailable: {type(exc).__name__}"}
    db = None
    try:
        db = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True, timeout=5)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        markdown_docs, latest_indexed_at = db.execute(
            "SELECT COUNT(*), MAX(indexed_at) FROM notes"
        ).fetchone()
        metadata = dict(db.execute("SELECT key,value FROM index_meta")) if "index_meta" in tables else {}
        counts = {
            "markdown_docs": int(markdown_docs),
            "markdown_chunks": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] if "chunks" in tables else 0,
            "transcript_docs": db.execute("SELECT COUNT(*) FROM transcript_docs").fetchone()[0] if "transcript_docs" in tables else 0,
            "transcript_chunks": db.execute("SELECT COUNT(*) FROM transcript_chunks").fetchone()[0] if "transcript_chunks" in tables else 0,
        }
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "error": f"fallback index unreadable: {type(exc).__name__}"}
    finally:
        if db is not None:
            db.close()
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        manifest = {}
    completed = metadata.get("build_completed_at", "")
    return {
        "ok": True,
        **counts,
        "build_id": metadata.get("build_id", ""),
        "build_completed_at": completed,
        "manifest_build_id": manifest.get("build_id", ""),
        "manifest_matches_database": bool(manifest) and manifest.get("build_id") == metadata.get("build_id"),
        "latest_document_indexed_at": latest_indexed_at,
        "database_bytes": st.st_size,
        "published_at": completed or datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_at_source": "index_meta.build_completed_at" if completed else "database_file_mtime",
        "authoritative_build_timestamp_available": bool(completed),
        "syncthing": _read_syncthing(),
    }


def read_status() -> dict[str, Any]:
    fallback = _read_index_counts()
    cached = _maint_cache.get("data")
    if cached is None:
        return {
            "health": "RED", "no_receipt": True,
            "detail": _maint_cache.get("error") or "maint receipt not probed yet",
            "index_ok": bool(fallback.get("ok")),
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
    primary = cached.get("primary") or {}
    transport = fallback.get("syncthing") or {}
    reasons: list[str] = []
    if cached.get("result") != "success" or cached.get("exec_status") != 0:
        reasons.append(f"unit {cached.get('result')}/{cached.get('exec_status')}")
    if cached.get("reindex_rc") is None:
        reasons.append("no SUMMARY receipt")
    elif cached.get("reindex_rc") != 0:
        reasons.append(f"reindex_rc={cached.get('reindex_rc')}")
    if not primary.get("ok"):
        reasons.append("Charlie primary unavailable")
    elif not primary.get("manifest_matches_database"):
        reasons.append("Charlie manifest/database generation mismatch")
    if not fallback.get("ok"):
        reasons.append(str(fallback.get("error")))
    elif not fallback.get("manifest_matches_database"):
        reasons.append("Alpha fallback manifest/database generation mismatch")
    if primary.get("build_id") and fallback.get("build_id") != primary.get("build_id"):
        reasons.append("Alpha fallback generation differs from Charlie primary")
    if not transport.get("ok"):
        reasons.append(str(transport.get("error") or "Syncthing unhealthy"))
    elif transport.get("state") != "idle" or transport.get("need_files") or transport.get("need_bytes"):
        reasons.append(
            f"Syncthing {transport.get('state')} need={int(transport.get('need_bytes') or 0):,}B"
        )
    if int(fallback.get("markdown_docs") or 0) <= 0:
        reasons.append("markdown index empty")
    if int(fallback.get("transcript_docs") or 0) <= 0:
        reasons.append("transcript collection empty")
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
        "reindex_rc": cached.get("reindex_rc"),
        "primary_ok": bool(primary.get("ok")),
        "primary_build_id": primary.get("build_id"),
        "fallback_build_id": fallback.get("build_id"),
        "fallback_matches_primary": bool(primary.get("build_id")) and fallback.get("build_id") == primary.get("build_id"),
        "markdown_ok": int(fallback.get("markdown_docs") or 0) > 0,
        "transcript_ok": int(fallback.get("transcript_docs") or 0) > 0,
        "index_ok": bool(fallback.get("ok")),
        "index_error": fallback.get("error"),
        "markdown_docs": fallback.get("markdown_docs"),
        "markdown_chunks": fallback.get("markdown_chunks"),
        "transcript_docs": fallback.get("transcript_docs"),
        "transcript_chunks": fallback.get("transcript_chunks"),
        "database_bytes": fallback.get("database_bytes"),
        "published_at": fallback.get("published_at"),
        "published_at_source": fallback.get("published_at_source"),
        "latest_document_indexed_at": fallback.get("latest_document_indexed_at"),
        "syncthing": transport,
    }
