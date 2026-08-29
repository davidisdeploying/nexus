#!/usr/bin/env python3
"""
nexus_heartbeat_archive — reversible, evidence-gated archival of terminal
one-shot heartbeat job records out of the live heartbeats/ directory.

Moves ONLY (never deletes): os.replace, same filesystem, into a single
timestamped `heartbeats/archive/nexus7-terminal-archive-<UTC>/` directory per
invocation. Gated by an explicit per-job allowlist (`archive_allowlist.json`
next to this script by default) — a record not named in the allowlist is
never touched, no inference from filename/shape.

Dry-run is the default and performs zero filesystem writes (report goes to
stdout only). `--apply` is required to actually move anything. Every applied
run writes one `manifest.json` recording exactly what moved (with hash/size/
mtime/state/kind/evidence and a literal restore command) and what was
preserved/skipped and why.

No deletion code path. No HTTP/API route — this is a standalone CLI script,
not imported by the Nexus app.

Usage:
    python3 nexus_heartbeat_archive.py                     # dry-run, stdout report
    python3 nexus_heartbeat_archive.py --apply              # perform the move

Restore a moved record:
    cp -p heartbeats/archive/nexus7-terminal-archive-<UTC>/<job>.json \\
          heartbeats/<job>.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HEARTBEATS_DIR = Path.home() / "Vaults" / "loupe-vault" / "heartbeats"
DEFAULT_ALLOWLIST = Path(__file__).with_name("archive_allowlist.json")

MIN_AGE_HOURS = 24.0
HARD_DENY_STATES = {"failed", "error", "blocked", "running", "ended", "stalled"}
# Any job id containing one of these substrings is refused regardless of
# allowlist membership — a safety net independent of the config file, so an
# allowlist typo/accident can never archive a live sidecar or diagnostic record.
HARD_DENY_ID_SUBSTRINGS = ("host-", "thermal", "gallery", "nas3", "temple")
ARCHIVE_PREFIX = "nexus7-terminal-archive-"
REQUIRED_SHAPE_KEYS = ("job", "state", "run_id")


def _utc_stamp(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_allowlist(path: Path) -> dict:
    """Accepts either {"jobs": {id: {...}}} or a flat {id: {...}} mapping."""
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    return data.get("jobs", data)


def _is_hard_denied_id(job_id: str) -> bool:
    lowered = job_id.lower()
    return any(s in lowered for s in HARD_DENY_ID_SUBSTRINGS)


def evaluate(fp: Path, allowlist: dict, now: float) -> dict:
    """Evaluate one heartbeat file against every gate. Always returns a dict
    describing the decision (never raises for expected conditions) so a
    caller can build a full preserved/skipped audit trail."""
    job_id = fp.stem
    result = {
        "job": job_id,
        "original_path": str(fp),
        "eligible": False,
        "reason": None,
        "evidence": None,
        "state": None,
        "kind": None,
        "size": None,
        "mtime": None,
        "sha256": None,
    }

    # Hard-denied ids win regardless of allowlist membership — checked first,
    # before even looking at the allowlist, so an allowlist accident can't help.
    if _is_hard_denied_id(job_id):
        result["reason"] = "hard_denied_id"
        return result

    if job_id not in allowlist:
        result["reason"] = "not_in_allowlist"
        return result

    try:
        st = fp.stat()
    except FileNotFoundError:
        result["reason"] = "missing_file"
        return result

    try:
        with open(fp) as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        result["reason"] = "unreadable_or_invalid_json"
        return result

    result["size"] = st.st_size
    result["mtime"] = st.st_mtime
    result["state"] = payload.get("state")
    result["kind"] = payload.get("kind")
    result["sha256"] = _sha256(fp)

    if not all(k in payload for k in REQUIRED_SHAPE_KEYS):
        result["reason"] = "wrong_shape_not_a_job_record"
        return result

    state = payload.get("state")
    # Hard-denied states are checked before the "must equal done" check so the
    # manifest reason is specific (denylist wins over a generic mismatch).
    if state in HARD_DENY_STATES:
        result["reason"] = f"hard_denied_state:{state}"
        return result
    if state != "done":
        result["reason"] = f"state_not_done:{state}"
        return result

    kind = payload.get("kind")
    if kind is not None and kind != "job":
        result["reason"] = f"unrecognized_kind:{kind}"
        return result

    age_hours = (now - st.st_mtime) / 3600.0
    if age_hours < MIN_AGE_HOURS:
        result["reason"] = f"too_young:{age_hours:.1f}h<{MIN_AGE_HOURS}h"
        return result

    result["eligible"] = True
    result["reason"] = "eligible"
    result["evidence"] = allowlist[job_id].get("evidence")
    return result


def scan(heartbeats_dir: Path, allowlist: dict, now: float) -> list[dict]:
    """Top-level glob only — never recursive. Matches the live Jobs panel's
    own scan (app/work.py: `settings.heartbeats_dir.glob("*.json")`), so this
    tool never descends into archive/ or inline/."""
    files = sorted(heartbeats_dir.glob("*.json"))
    return [evaluate(fp, allowlist, now) for fp in files]


def apply_archive(heartbeats_dir: Path, allowlist: dict, decisions: list[dict],
                   now_fn=time.time) -> dict:
    """Move every currently-eligible decision, re-validating each file
    immediately before its move (catches a same-tick rewrite/race between
    scan and move). Always creates the timestamped archive directory and
    writes manifest.json, even if zero files end up moved, so every --apply
    invocation leaves an audit trail."""
    run_ts = _utc_stamp(now_fn())
    archive_dir = heartbeats_dir / "archive" / f"{ARCHIVE_PREFIX}{run_ts}"

    for d in decisions:
        if not d["eligible"]:
            continue
        fp = Path(d["original_path"])
        fresh = evaluate(fp, allowlist, now_fn())
        if not fresh["eligible"] or fresh["sha256"] != d["sha256"]:
            d["eligible"] = False
            d["reason"] = "race_detected_at_move_time"
            continue

        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / fp.name
        if dest.exists():
            d["eligible"] = False
            d["reason"] = "collision"
            continue

        try:
            os.replace(fp, dest)  # atomic, same filesystem — never copy+delete
        except OSError as e:
            d["eligible"] = False
            d["reason"] = f"move_failed:{e}"
            continue

        d["reason"] = "moved"
        d["destination_path"] = str(dest)
        d["moved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        d["restore_cmd"] = f"cp -p {dest} {fp}"

    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = [d for d in decisions if d.get("moved_at")]
    manifest = {
        "run_ts": run_ts,
        "heartbeats_dir": str(heartbeats_dir),
        "archive_dir": str(archive_dir),
        "moved": moved,
        "preserved_or_skipped": [d for d in decisions if not d.get("moved_at")],
    }
    with open(archive_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--heartbeats-dir", type=Path, default=DEFAULT_HEARTBEATS_DIR)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--apply", action="store_true",
                         help="Actually move eligible files. Default is dry-run "
                              "(report only, zero filesystem writes).")
    args = parser.parse_args(argv)

    allowlist = load_allowlist(args.allowlist)
    now = time.time()
    decisions = scan(args.heartbeats_dir, allowlist, now)

    if not args.apply:
        report = {
            "dry_run": True,
            "heartbeats_dir": str(args.heartbeats_dir),
            "allowlist_path": str(args.allowlist),
            "eligible_count": sum(1 for d in decisions if d["eligible"]),
            "decisions": decisions,
        }
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0

    manifest = apply_archive(args.heartbeats_dir, allowlist, decisions)
    print(json.dumps(manifest, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
