"""
Vault-backed persistence for the dashboard state.

status.json  — the latest snapshot, atomically swapped (never a torn read).
history.jsonl — append-only one-line-per-run log for sparklines and uptime.

The vault is the source of truth, consistent with the fleet's local-first,
plain-files posture. No database until full-text query over history earns one.
"""
from __future__ import annotations

import json
from collections import deque
import os
import stat
import tempfile
from pathlib import Path

from .config import settings
from .models import StatusSnapshot

# 7 days at the existing 5-minute heartbeat cadence (FLEET-WORKER2-BUILD-
# 20260721-nexus-bounded-retention). read_history's own cap mirrors this.
MAX_HISTORY_ROWS = 2016
# Slack before an atomic rewrite fires, so steady-state appends stay O(1)
# instead of rewriting the file every heartbeat.
HISTORY_TRIM_SLACK_ROWS = 288
HISTORY_TRIM_THRESHOLD_ROWS = MAX_HISTORY_ROWS + HISTORY_TRIM_SLACK_ROWS


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # atomic swap on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_snapshot(snap: StatusSnapshot) -> None:
    _atomic_write(settings.status_file, snap.model_dump_json(indent=2))
    line = json.dumps(snap.history_line(), separators=(",", ":"))
    with open(settings.history_file, "a") as f:
        f.write(line + "\n")
    _maybe_trim_history(settings.history_file)


def _line_count(path: Path) -> int:
    with open(path, "rb") as f:
        return sum(1 for _ in f)


def _maybe_trim_history(path: Path) -> None:
    """Cheap read-only line count every append; only the rare over-threshold
    tick pays for the atomic rewrite in trim_history."""
    try:
        if not path.exists() or _line_count(path) <= HISTORY_TRIM_THRESHOLD_ROWS:
            return
    except OSError:
        return
    trim_history(path)


def trim_history(path: Path | None = None, keep: int = MAX_HISTORY_ROWS) -> dict:
    """Atomically rewrite history.jsonl down to the newest `keep` valid rows,
    in chronological order. Malformed lines are dropped (same tolerance
    read_history already applies) rather than counted toward `keep`. A no-op,
    no-rewrite return when the file is already at or under `keep` rows.

    File mode/owner are preserved across the rewrite: _atomic_write's mkstemp
    always produces a fresh 0600 file (fine for status.json, which is always
    written that way), but history.jsonl is normally append-only via plain
    open() and has its own long-lived mode — this restores it after the
    atomic replace.
    """
    p = path or settings.history_file
    if not p.exists():
        return {"before": 0, "kept": 0, "malformed": 0, "trimmed": False}

    kept: deque[str] = deque(maxlen=keep)
    total = 0
    malformed = 0
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            total += 1
            line = ln.rstrip("\n")
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            kept.append(line)

    if total <= keep:
        return {"before": total, "kept": len(kept), "malformed": malformed, "trimmed": False}

    try:
        st = p.stat()
    except OSError:
        st = None

    data = "".join(line + "\n" for line in kept)
    _atomic_write(p, data)

    if st is not None:
        try:
            os.chmod(p, stat.S_IMODE(st.st_mode))
            os.chown(p, st.st_uid, st.st_gid)
        except OSError:
            pass

    return {"before": total, "kept": len(kept), "malformed": malformed, "trimmed": True}


def read_snapshot() -> StatusSnapshot | None:
    p = settings.status_file
    if not p.exists():
        return None
    try:
        return StatusSnapshot.model_validate_json(p.read_text())
    except Exception:
        return None


def read_history(limit: int = 288) -> list[dict]:
    """Most-recent rows without reading the entire append-only file."""
    p = settings.history_file
    if not p.exists():
        return []
    limit = max(1, min(int(limit), MAX_HISTORY_ROWS))
    rows = []
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        for ln in deque(fh, maxlen=limit):
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return rows
