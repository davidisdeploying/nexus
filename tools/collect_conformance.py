#!/usr/bin/env python3
"""Bounded, read-only fleet conformance collector.

The manifest is declarative. No manifest value is evaluated by a shell, HTTP
handlers never run these probes, and captured evidence is deliberately small.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # package import in tests; script import under systemd ExecStart
    from .collect_control_plane import collect_and_write as collect_control_plane
except ImportError:  # pragma: no cover - exercised by the real script entrypoint
    from collect_control_plane import collect_and_write as collect_control_plane

ROOT = Path("/home/david/nexus")
MANIFEST = ROOT / "conformance" / "checks.json"
STATE_ROOT = Path(os.environ.get("NEXUS_STATE_DIR")
                  or os.environ.get("PANEL_STATE_DIR")
                  or os.environ.get("FLEET_NEXUS_STATE_DIR")
                  or Path.home() / ".local" / "state" / "nexus")
CACHE = STATE_ROOT / "generated" / "conformance.json"
HISTORY = STATE_ROOT / "generated" / "conformance-history.jsonl"
MAX_HISTORY = 1000
TIMEOUT = 12


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bounded(value: object, limit: int = 240) -> str:
    return " ".join(str(value).split())[:limit]


def run_on(host: str, argv: list[str], timeout: int = TIMEOUT) -> tuple[int, str, str]:
    if host == "alpha":
        command = argv
    else:
        # ssh concatenates its command words and the remote shell re-parses them,
        # so any argument containing a space or metacharacter is torn apart unless
        # it is quoted here. Local execution passes argv straight to execve and
        # needs no quoting, which is why a check can pass on alpha and fail on
        # every other host. shlex.quote is a no-op for the plain tokens the
        # existing checks use.
        remote = " ".join(shlex.quote(a) for a in argv)
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "--", remote]
    try:
        proc = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        return proc.returncode, bounded(proc.stdout), bounded(proc.stderr)
    except subprocess.TimeoutExpired:
        return 124, "", f"probe exceeded {timeout}s"
    except OSError as exc:
        return 127, "", bounded(exc)


def check(check_id: str, category: str, host: str, expected: str, rc: int,
          stdout: str, stderr: str) -> dict[str, Any]:
    actual = stdout or stderr or f"exit {rc}"
    probe_failed = rc in (124, 127)
    state = "ok" if rc == 0 and stdout == expected else ("unknown" if probe_failed else "error")
    return {
        "id": check_id, "category": category, "host": host, "state": state,
        "expected": expected, "actual": bounded(actual), "checked_at": now_utc(),
        "failure_class": "probe_failure" if probe_failed else ("drift" if state == "error" else None),
    }


GIT_PUSH_RECEIPT_MAX_AGE_HOURS = 36
# Receipts contain one bounded row per allowlisted repository.  The Alpha
# receipt crossed 4 KiB as the allowlist grew, so 4,096 bytes truncated valid
# JSON before parsing and manufactured a permanent "malformed" failure.
RECEIPT_READ_MAX_BYTES = 64 * 1024


def _read_remote_file(host: str, path: str, timeout: int = TIMEOUT) -> tuple[int, str, str]:
    """Like run_on, but returns full (unbounded-by-240-chars) stdout so a small
    JSON receipt can be parsed whole; the RESULT derived from it is what gets
    bounded before it's ever stored (see git_push_receipt_check)."""
    if host == "alpha":
        command = ["cat", path]
    else:
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "--", "cat", path]
    try:
        proc = subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout[:RECEIPT_READ_MAX_BYTES], bounded(proc.stderr)
    except subprocess.TimeoutExpired:
        return 124, "", f"probe exceeded {timeout}s"
    except OSError as exc:
        return 127, "", bounded(exc)


def git_push_receipt_check(host: str, path: str) -> dict[str, Any]:
    """Read a fleet-git-push success receipt and store ONLY sanitized, bounded
    evidence (ok, host, finished time, age) -- never the raw receipt content.
    Timeout/executable failure -> unknown; a valid-but-failed, stale,
    wrong-host, or malformed receipt -> error."""
    check_id = f"receipt:{host}:fleet-git-push"
    expected = "ok=True, host match, age <= 36h"
    rc, out, err = _read_remote_file(host, path)

    if rc in (124, 127):
        return {
            "id": check_id, "category": "receipts", "host": host, "state": "unknown",
            "expected": expected, "actual": bounded(err or f"exit {rc}"),
            "checked_at": now_utc(), "failure_class": "probe_failure",
        }
    if rc != 0:
        return {
            "id": check_id, "category": "receipts", "host": host, "state": "error",
            "expected": expected, "actual": bounded(err or f"receipt unreadable (exit {rc})"),
            "checked_at": now_utc(), "failure_class": "drift",
        }

    try:
        raw = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {
            "id": check_id, "category": "receipts", "host": host, "state": "error",
            "expected": expected, "actual": "malformed JSON receipt",
            "checked_at": now_utc(), "failure_class": "drift",
        }

    receipt_ok = raw.get("ok") is True
    receipt_host = raw.get("host")
    finished_at = raw.get("finished_at")
    finished_dt = None
    if isinstance(finished_at, str):
        try:
            finished_dt = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if finished_dt.tzinfo is None:
                finished_dt = finished_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            finished_dt = None

    age_hours = None
    if finished_dt is not None:
        age_hours = max(0.0, (datetime.now(timezone.utc) - finished_dt).total_seconds() / 3600.0)

    host_matches = isinstance(receipt_host, str) and receipt_host == host
    fresh = age_hours is not None and age_hours <= GIT_PUSH_RECEIPT_MAX_AGE_HOURS
    is_ok = receipt_ok and host_matches and fresh

    sanitized = (
        f"ok={receipt_ok} host={bounded(receipt_host, 40)} "
        f"finished={finished_at if isinstance(finished_at, str) else 'invalid'} "
        f"age={f'{age_hours:.1f}h' if age_hours is not None else 'unknown'}"
    )
    return {
        "id": check_id, "category": "receipts", "host": host,
        "state": "ok" if is_ok else "error",
        "expected": expected, "actual": bounded(sanitized),
        "checked_at": now_utc(), "failure_class": None if is_ok else "drift",
    }


def apply_transition_metadata(row: dict[str, Any], old: dict[str, Any]) -> None:
    """Mutate `row` in place with additive per-check transition metadata,
    derived from the previous cache row `old` (an empty dict for a check id
    that didn't exist in the previous cache -- a new check seeds without
    fabricating a transition). Preserves last_ok_at/last_ok_actual, which are
    set separately by the caller."""
    current_state = row["state"]
    current_ok = current_state == "ok"
    old_state = old.get("state") if old else None

    row["previous_state"] = old_state

    if not old:
        row["state_changed_at"] = row["checked_at"]
        row["consecutive_non_ok_scans"] = 0 if current_ok else 1
        row["first_non_ok_at"] = None if current_ok else row["checked_at"]
        row["last_recovered_at"] = None
        return

    old_ok = old_state == "ok"
    row["state_changed_at"] = (
        row["checked_at"] if current_state != old_state
        else (old.get("state_changed_at") or row["checked_at"])
    )
    row["last_recovered_at"] = old.get("last_recovered_at")

    if current_ok:
        row["consecutive_non_ok_scans"] = 0
        row["first_non_ok_at"] = None
        if not old_ok:
            row["last_recovered_at"] = row["checked_at"]
    else:
        row["consecutive_non_ok_scans"] = old.get("consecutive_non_ok_scans") or 0
        if not old_ok:
            row["consecutive_non_ok_scans"] += 1
            row["first_non_ok_at"] = old.get("first_non_ok_at") or row["checked_at"]
        else:
            row["consecutive_non_ok_scans"] = 1
            row["first_non_ok_at"] = row["checked_at"]


def collect(manifest: dict[str, Any], control_plane: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    contract_hashes: dict[str, str] = {}
    for host in manifest["hosts"]:
        path = manifest["agent_contract"]["macbook_path" if host == "macbook" else "linux_path"]
        rc, out, err = run_on(host, ["shasum", "-a", "256", path])
        digest = out.split()[0] if rc == 0 and out else ""
        if digest:
            contract_hashes[host] = digest
        results.append({
            "id": f"agents:{host}", "category": "contract", "host": host,
            "state": "ok" if digest else "error", "expected": "readable SHA-256",
            "actual": digest or err or f"exit {rc}", "checked_at": now_utc(),
        })
    reference = contract_hashes.get("alpha")
    for row in results:
        if row["category"] == "contract" and reference and row["actual"] != reference:
            row["state"] = "error"
            row["expected"] = f"fleet hash {reference[:12]}"
            row["actual"] = f"hash {str(row['actual'])[:12]}"

    for source, target in manifest["ssh_edges"]:
        rc, out, err = run_on(source, [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            target, "--", "printf", "SSH_OK",
        ])
        results.append(check(f"ssh:{source}:{target}", "ssh", source, "SSH_OK", rc, out, err))

    for host, unit, expected in manifest["user_units"]:
        rc, out, err = run_on(host, ["systemctl", "--user", "is-active", unit])
        results.append(check(f"unit:{host}:{unit}", "service", host, expected, rc, out, err))

    for host, unit in manifest.get("enablement_units", []):
        rc, out, err = run_on(host, ["systemctl", "--user", "is-enabled", unit])
        results.append(check(f"enabled:{host}:{unit}", "service", host, "enabled", rc, out, err))

    for host, path, kind in manifest["required_paths"]:
        if kind == "python-compiles":
            # A single Syncthing-carried canonical reaches every host at once, so a
            # bad edit breaks all of them together. Without this the only alarm is
            # the 26h backup-freshness check, which fires long after the fact.
            # ast.parse rather than py_compile: the canonical lives in the
            # Syncthing-replicated vault, and py_compile would write __pycache__
            # there on every scan, churning the whole mesh.
            rc, out, err = run_on(host, [
                "python3", "-c",
                "import ast,sys; ast.parse(open(sys.argv[1]).read())", path,
            ])
            results.append({
                "id": f"compiles:{host}:{path}", "category": "path", "host": host,
                "state": "ok" if rc == 0 else "error",
                "expected": "compiles", "actual": "compiles" if rc == 0 else bounded(err or f"exit {rc}"),
                "checked_at": now_utc(),
                "failure_class": None if rc == 0 else "drift",
            })
            continue
        if kind.startswith("symlink:"):
            # "test -f" follows symlinks, so it cannot tell a link to the canonical
            # copy from a divergent real file sitting in its place. Resolve instead.
            want = kind.split(":", 1)[1]
            rc, out, err = run_on(host, ["readlink", "-f", path])
            actual = out.strip()
            ok = rc == 0 and actual == want
            results.append({
                "id": f"path:{host}:{path}", "category": "path", "host": host,
                "state": "ok" if ok else "error",
                "expected": f"resolves to {want}",
                "actual": actual or bounded(err or f"exit {rc}"),
                "checked_at": now_utc(),
                "failure_class": None if ok else "drift",
            })
            continue
        flag = "-f" if kind == "file" else "-d"
        rc, out, err = run_on(host, ["test", flag, path])
        results.append({
            "id": f"path:{host}:{path}", "category": "path", "host": host,
            "state": "ok" if rc == 0 else "error", "expected": f"{kind} exists",
            "actual": "present" if rc == 0 else bounded(err or f"exit {rc}"),
            "checked_at": now_utc(),
        })

    for host, path, max_age_seconds in manifest.get("fresh_paths", []):
        rc, out, err = run_on(host, ["stat", "-c", "%Y", path])
        try:
            age_seconds = max(0, int(time.time()) - int(out))
        except (TypeError, ValueError):
            age_seconds = None
        probe_failed = rc in (124, 127)
        state = (
            "ok" if rc == 0 and age_seconds is not None and age_seconds <= max_age_seconds
            else ("unknown" if probe_failed else "error")
        )
        results.append({
            "id": f"fresh:{host}:{path}", "category": "backup", "host": host,
            "state": state, "expected": f"age <= {max_age_seconds}s",
            "actual": f"age {age_seconds}s" if age_seconds is not None else bounded(err or f"exit {rc}"),
            "checked_at": now_utc(),
            "failure_class": "probe_failure" if probe_failed else ("drift" if state == "error" else None),
        })

    for source_host, source_path, mirror_host, mirror_path in manifest.get("git_mirrors", []):
        source_rc, source_out, source_err = run_on(
            source_host, ["git", "-C", source_path, "rev-parse", "HEAD"]
        )
        mirror_rc, mirror_out, mirror_err = run_on(
            mirror_host, ["git", "-C", mirror_path, "rev-parse", "HEAD"]
        )
        probe_failed = source_rc in (124, 127) or mirror_rc in (124, 127)
        matches = (
            source_rc == 0 and mirror_rc == 0 and source_out
            and source_out == mirror_out
        )
        state = "ok" if matches else ("unknown" if probe_failed else "error")
        actual = (
            f"match {source_out[:12]}" if matches
            else bounded(
                f"source={source_out or source_err or source_rc} "
                f"mirror={mirror_out or mirror_err or mirror_rc}"
            )
        )
        results.append({
            "id": f"mirror:{source_host}:{source_path}:{mirror_host}:{mirror_path}",
            "category": "backup", "host": source_host, "state": state,
            "expected": "source and mirror HEAD match", "actual": actual,
            "checked_at": now_utc(),
            "failure_class": "probe_failure" if probe_failed else ("drift" if state == "error" else None),
        })

    for host, path in manifest.get("git_push_receipts", []):
        results.append(git_push_receipt_check(host, path))

    control_plane = control_plane or {}
    cards = control_plane.get("cards", [])
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        state = str(card.get("status", "unknown"))
        if state not in {"ok", "warning", "error", "unknown"}:
            state = "unknown"
        title = str(card.get("title") or card.get("id") or "index")
        results.append({
            "id": f"governance:{card.get('id', title)}",
            "category": "governance",
            "host": "alpha",
            "title": f"{title.title()} index",
            "state": state,
            "expected": "canonical index and validator current",
            "actual": bounded(f"revision={card.get('revision', 'unknown')} {card.get('summary', '')}"),
            "checked_at": now_utc(),
            "failure_class": None if state == "ok" else ("probe_failure" if state == "unknown" else "drift"),
            "navigate": f"/control-plane#{card.get('id', title)}",
        })
    return results


def main() -> int:
    started = time.monotonic()
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if manifest.get("version") != 2:
            raise ValueError("unsupported manifest schema")
        control_plane = collect_control_plane()
        checks = collect(manifest, control_plane)
        collector_error = None
    except Exception as exc:
        checks = []
        collector_error = bounded(exc)
    previous: dict[str, Any] = {}
    prior: dict[str, Any] = {}
    try:
        prior = json.loads(CACHE.read_text(encoding="utf-8"))
        previous = {row["id"]: row for row in prior.get("checks", []) if isinstance(row, dict)}
    except (OSError, json.JSONDecodeError):
        pass
    for row in checks:
        old = previous.get(row["id"], {})
        if row["state"] == "ok":
            row["last_ok_at"] = row["checked_at"]
            row["last_ok_actual"] = row["actual"]
        else:
            row["last_ok_at"] = old.get("last_ok_at")
            row["last_ok_actual"] = old.get("last_ok_actual")
        apply_transition_metadata(row, old)
    counts = {name: sum(1 for row in checks if row["state"] == name)
              for name in ("ok", "warning", "error", "unknown")}
    overall = "error" if collector_error or counts["error"] else (
        "warning" if counts["warning"] or counts["unknown"] else "ok"
    )
    check_ids = sorted(str(row.get("id", "")) for row in checks)
    check_set_sha256 = hashlib.sha256("\n".join(check_ids).encode()).hexdigest()
    manifest_revision = manifest.get("manifest_revision") if "manifest" in locals() else None
    previous_fingerprint = prior.get("check_set_sha256")
    if not previous_fingerprint and isinstance(prior.get("checks"), list):
        prior_ids = sorted(str(row.get("id", "")) for row in prior["checks"] if isinstance(row, dict))
        if prior_ids:
            previous_fingerprint = hashlib.sha256("\n".join(prior_ids).encode()).hexdigest()
    payload = {
        "version": 2, "generated_at": now_utc(), "overall": overall,
        "manifest_revision": manifest_revision,
        "check_set_sha256": check_set_sha256,
        "policy_changed": bool(previous_fingerprint and previous_fingerprint != check_set_sha256),
        "categories": manifest.get("categories", []) if "manifest" in locals() else [],
        "counts": counts, "collector_error": collector_error,
        "duration_seconds": round(time.monotonic() - started, 3), "checks": checks,
    }
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, CACHE)
    with HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "generated_at": payload["generated_at"], "overall": overall,
            "counts": counts, "collector_error": collector_error,
            "manifest_revision": manifest_revision,
            "check_set_sha256": check_set_sha256,
            "policy_changed": payload["policy_changed"],
        }, separators=(",", ":")) + "\n")
    lines = HISTORY.read_text(encoding="utf-8").splitlines()[-MAX_HISTORY:]
    history_tmp = HISTORY.with_suffix(".jsonl.tmp")
    history_tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(history_tmp, HISTORY)
    return 1 if collector_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
