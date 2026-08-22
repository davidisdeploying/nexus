"""Bounded collectors for the unified cloud-model usage card.

Claude's authenticated CLI uses an undocumented first-party utilization
endpoint.  We call that endpoint directly for precise structured values and
fall back to the official interactive ``/usage`` panel whenever its private
contract or credential state changes.

Gemini's public quota endpoint does not expose the rolling five-hour/week
subscription buckets shown by its own ``/usage`` panel.  That panel force-
refreshes Gemini's private quota client, so it remains the authenticated
fallback boundary for those values.

Both collectors use isolated marked HOME directories, remove disposable
conversation state, and atomically write one small cache consumed by
``seatboard``.  Credential values are never written to that cache or logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any

import pexpect

from .gemini_quota_rpc import SOURCE as GEMINI_RPC_SOURCE
from .gemini_quota_rpc import collect_gemini_rpc


ANSI_RE = re.compile(
    rb"(?:\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-_])"
)
MARKER = ".nexus-model-usage-home"

# The collector must never run against, or clean, the invoking user's real
# HOME - only a marked isolated collector HOME. Resolved once at import, before
# any HOME override, so the guard protects whatever the real home actually is
# rather than one hard-coded path.
LIVE_HOME = Path(os.path.expanduser("~")).resolve()
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _clean(raw: bytes | bytearray) -> str:
    text = ANSI_RE.sub(b"", bytes(raw)).decode("utf-8", "replace")
    text = text.replace("\r", "\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _read_until(
    child: pexpect.spawn,
    raw: bytearray,
    needle: str,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = child.read_nonblocking(size=65536, timeout=0.25)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF as exc:
            raise RuntimeError(f"CLI exited before {needle!r}") from exc
        raw.extend(chunk)
        if needle in _clean(raw):
            return
    raise RuntimeError(
        f"timed out waiting for {needle!r}; tail={_clean(raw)[-600:]}"
    )


def _spawn(
    executable: str,
    args: list[str],
    home: Path,
    *,
    cwd: str = str(Path(os.path.expanduser("~"))),
) -> pexpect.spawn:
    if not (home / MARKER).is_file():
        raise RuntimeError(f"refusing unmarked collector HOME: {home}")
    if home.resolve() == LIVE_HOME:
        raise RuntimeError("refusing live user HOME")
    env = os.environ.copy()
    env["HOME"] = str(home)
    return pexpect.spawn(
        executable,
        args,
        cwd=cwd,
        env=env,
        encoding=None,
        timeout=20,
        dimensions=(55, 180),
    )


def parse_gemini_usage(text: str) -> dict[str, Any]:
    section = text.split("GEMINI MODELS", 1)[-1].split(
        "CLAUDE AND GPT MODELS", 1
    )[0]
    windows: dict[str, Any] = {}
    for label, key in (
        ("Weekly", "weekly"),
        ("Five Hour", "five_hour"),
    ):
        match = re.search(
            rf"{label} Limit(?P<body>.*?)(?=(?:Weekly|Five Hour) Limit|$)",
            section,
            re.S,
        )
        if not match:
            continue
        body = match.group("body")
        exact_match = re.search(r"(\d+(?:\.\d+)?)%", body)
        if not exact_match:
            continue
        remaining = float(exact_match.group(1))
        remaining_match = re.search(
            r"(\d+)% remaining(?:\s*·\s*Refreshes in ([^\n]+))?",
            body,
        )
        if remaining_match:
            rounded_remaining = int(remaining_match.group(1))
            refresh = remaining_match.group(2)
        elif "Quota available" in body:
            rounded_remaining = 100
            refresh = None
        else:
            continue
        windows[key] = {
            "used_percent": round(max(0.0, 100.0 - remaining), 2),
            "remaining_percent": rounded_remaining,
            "refreshes_in": refresh.strip() if refresh else None,
        }
    if set(windows) != {"weekly", "five_hour"}:
        raise ValueError("could not parse both Gemini Gemini quota windows")
    return {
        "ok": True,
        "source": "agy /usage",
        "group": "Gemini models",
        "windows": windows,
    }


def parse_claude_usage(text: str) -> dict[str, Any]:
    def window(pattern: str) -> dict[str, Any]:
        match = re.search(
            pattern + r".*?(\d+)%\s*used.*?Resets\s*([^\n]+)",
            text,
            re.S,
        )
        if not match:
            raise ValueError(
                f"could not parse Claude window: {pattern}; "
                f"tail={text[-1200:]}"
            )
        return {
            "used_percent": int(match.group(1)),
            "resets": match.group(2).strip(),
        }

    result: dict[str, Any] = {
        "ok": True,
        "source": "claude /usage",
        "windows": {
            "five_hour": window(r"Current\s*session"),
            "weekly": window(r"Current\s*week\s*\(all\s*models\)"),
        },
    }
    try:
        result["windows"]["fable_weekly"] = window(
            r"Current\s*week\s*\(Fable\)"
        )
    except ValueError:
        pass
    return result


def parse_claude_api_usage(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize Claude's private structured utilization response."""

    def window(key: str) -> dict[str, Any]:
        raw = payload.get(key)
        if not isinstance(raw, dict):
            raise ValueError(f"Claude usage response missing {key}")
        utilization = raw.get("utilization")
        resets_at = raw.get("resets_at")
        if not isinstance(utilization, (int, float)) or not isinstance(
            resets_at, str
        ):
            raise ValueError(f"Claude usage response malformed {key}")
        return {
            "used_percent": float(utilization),
            "resets_at": resets_at,
        }

    windows = {
        "five_hour": window("five_hour"),
        "weekly": window("seven_day"),
    }
    limits = payload.get("limits")
    if isinstance(limits, list):
        for limit in limits:
            if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
                continue
            scope = limit.get("scope") or {}
            model = scope.get("model") if isinstance(scope, dict) else {}
            display_name = (
                model.get("display_name") if isinstance(model, dict) else None
            )
            if str(display_name).lower() != "fable":
                continue
            percent = limit.get("percent")
            resets_at = limit.get("resets_at")
            if isinstance(percent, (int, float)) and isinstance(resets_at, str):
                windows["fable_weekly"] = {
                    "used_percent": float(percent),
                    "resets_at": resets_at,
                }
            break
    return {
        "ok": True,
        "source": "claude internal usage",
        "windows": windows,
    }


def _marked_home(home: Path) -> Path:
    if not (home / MARKER).is_file():
        raise RuntimeError(f"refusing unmarked collector HOME: {home}")
    if home.resolve() == LIVE_HOME:
        raise RuntimeError("refusing live user HOME")
    return home


def collect_claude_direct(home: Path, timeout: float = 10) -> dict[str, Any]:
    """Call the same private utilization endpoint used by Claude Code."""
    _marked_home(home)
    credentials = home / ".claude" / ".credentials.json"
    auth = json.loads(credentials.read_text()).get("claudeAiOauth") or {}
    access_token = auth.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Claude OAuth access token unavailable")
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-version": "2023-06-01",
            "User-Agent": "nexus-model-usage/1",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Claude usage response is not an object")
    return parse_claude_api_usage(payload)


def collect_gemini_cli(home: Path, agy: str) -> dict[str, Any]:
    child = _spawn(
        agy,
        ["--agent", "fleet-strategy", "--model", "gemini-3.1-pro-low"],
        home,
    )
    raw = bytearray()
    try:
        _read_until(child, raw, "? for shortcuts", 15)
        child.send(b"/usage\r")
        _read_until(child, raw, "Within each group", 15)
        return parse_gemini_usage(_clean(raw))
    finally:
        child.close(force=True)


def collect_gemini(home: Path, agy: str) -> dict[str, Any]:
    """Prefer structured quota RPC; retain authenticated CLI as fallback."""
    try:
        return collect_gemini_rpc(home)
    except Exception as direct_error:
        result = collect_gemini_cli(home, agy)
        result["fallback_from"] = GEMINI_RPC_SOURCE
        result["direct_error"] = type(direct_error).__name__
        return result


def collect_claude_cli(home: Path, claude: str) -> dict[str, Any]:
    child = _spawn(claude, [], home)
    raw = bytearray()
    try:
        # Claude's animated status line can render "manual mode" across cursor
        # updates, while the shortcuts marker is stable once input is ready.
        _read_until(child, raw, "forshortcuts", 15)
        child.send(b"/usage\r")
        _read_until(child, raw, "Usage credits", 20)
        return parse_claude_usage(_clean(raw))
    finally:
        child.close(force=True)


def collect_claude(home: Path, claude: str) -> dict[str, Any]:
    """Prefer Claude's structured endpoint; retain the CLI panel as fallback."""
    try:
        return collect_claude_direct(home)
    except Exception as direct_error:
        result = collect_claude_cli(home, claude)
        result["fallback_from"] = "claude internal usage"
        result["direct_error"] = type(direct_error).__name__
        return result


def _clean_conversation_state(home: Path, surface: str) -> None:
    """Delete only disposable state inside a marked isolated collector HOME."""
    if not (home / MARKER).is_file() or home.resolve() == LIVE_HOME:
        raise RuntimeError(f"refusing cleanup outside collector HOME: {home}")
    if surface == "gemini":
        root = home / ".gemini" / "gemini-cli"
        shutil.rmtree(root / "conversations", ignore_errors=True)
        shutil.rmtree(root / "brain", ignore_errors=True)
        shutil.rmtree(root / "crashes", ignore_errors=True)
        shutil.rmtree(root / "log", ignore_errors=True)
        (root / "cli.log").unlink(missing_ok=True)
        for name in (
            "history.jsonl",
            "conversation_summaries.db",
            "conversation_summaries.db-shm",
            "conversation_summaries.db-wal",
        ):
            (root / name).unlink(missing_ok=True)
        for name in ("conversation_metadata.json", "last_conversations.json"):
            (root / "cache" / name).unlink(missing_ok=True)
    elif surface == "claude":
        root = home / ".claude"
        shutil.rmtree(root / "projects", ignore_errors=True)
        shutil.rmtree(root / "sessions", ignore_errors=True)
        shutil.rmtree(root / "session-env", ignore_errors=True)
        shutil.rmtree(root / "file-history", ignore_errors=True)
        (root / "history.jsonl").unlink(missing_ok=True)
        for tmp in home.glob(".claude.json.tmp.*"):
            tmp.unlink(missing_ok=True)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--claude-home", type=Path, required=True)
    parser.add_argument("--gemini-home", type=Path, required=True)
    parser.add_argument("--history-db", type=Path)
    parser.add_argument(
        "--claude", default=os.path.expanduser("~/.local/bin/claude")
    )
    parser.add_argument(
        "--agy", default=os.path.expanduser("~/.local/bin/agy")
    )
    args = parser.parse_args()

    payload: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "claude": {"ok": False, "error": "not collected"},
        "gemini": {"ok": False, "error": "not collected"},
    }
    try:
        payload["claude"] = collect_claude(args.claude_home, args.claude)
    except Exception as exc:  # one provider never suppresses the other
        payload["claude"] = {"ok": False, "error": str(exc)}
    finally:
        _clean_conversation_state(args.claude_home, "claude")
    try:
        payload["gemini"] = collect_gemini(
            args.gemini_home, args.agy
        )
    except Exception as exc:
        payload["gemini"] = {"ok": False, "error": str(exc)}
    finally:
        _clean_conversation_state(args.gemini_home, "gemini")

    payload["ok"] = bool(
        payload["claude"].get("ok") or payload["gemini"].get("ok")
    )
    if args.history_db:
        try:
            from .model_usage_history import record_snapshot

            inserted = record_snapshot(
                payload, args.output.parent, args.history_db
            )
            payload["history"] = {"ok": True, "inserted": inserted}
        except Exception as exc:  # history failure must not suppress live quota
            payload["history"] = {
                "ok": False,
                "error_class": type(exc).__name__,
            }
    _atomic_write(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
