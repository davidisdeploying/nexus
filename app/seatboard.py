"""
Per-node availability board — the live "what is each node doing" strip.

One tile per worker node (charlie, delta, alpha) plus Localworker. Each tile reports,
at a glance, whether that node is FREE or BUSY and what it last did / is doing.
Legacy worker ids remain read-only source aliases and never appear as card labels.

Authoritative state comes from the SAME ``from-{seat}/runs/<token>/`` metadata the
"relay runs" work-panel reads (``work.read_relay_runs``): a ``done`` sentinel (its
content = exit code) marks a finished run, its absence a live one (or, past 6h with
no sentinel, an orphaned/dead one). ``status.json`` inside the run dir carries the
``started_at`` we time the ETA off.

ETA is an HONEST ESTIMATE, never a real countdown — a headless run emits no progress.
We estimate it from history: the MEDIAN completed-run duration for that seat+kind,
minus elapsed. Relay runs are not recorded in ``jobs_history.db`` (only the video-cull
job is), so the accumulated run history IS the set of completed run dirs on disk —
that's the "real store" for relay-run durations, and what we median over here.

Folded into ``status.json`` each sweep (``snap.seats``), exactly like ``work``:
free-form, never load-bearing for fleet health, off the rollup. Fully isolated — any
read/parse failure degrades one tile to ``no recent runs``/FREE and never touches the
sweep. The dashboard also flips a tile instantly off the scan-log WebSocket (a
SessionStart → BUSY, a Stop/SessionEnd → just-finished/FREE) and reconciles against
this authoritative sweep when it next lands; the local/no-token worker2 variant is a
pure client concern (an ad-hoc CLI session has no run dir).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings
from .runtime_paths import GEMINI_STATE_DIR, CONTROL_STATE_DIR
from .seats import CARD_INFO as CARDS

log = logging.getLogger("nexus.seatboard")

TOWER_ROOT = Path(os.environ.get("TOWER_ROOT", os.path.expanduser("~/tower")))
if TOWER_ROOT.is_dir() and str(TOWER_ROOT) not in sys.path:
    sys.path.insert(0, str(TOWER_ROOT))

try:
    from quota_router import QuotaRouter  # noqa: E402
except ImportError:  # optional integration
    QuotaRouter = None
    log.info("quota router not importable from %s; routing panel disabled", TOWER_ROOT)


class _UnavailableQuotaRouter:
    """Stand-in when Tower is not deployed alongside this dashboard."""

    def recommend(self, *_args, **_kwargs) -> dict:
        return {"ok": False, "reason": "quota router unavailable", "candidates": []}


quota_router = QuotaRouter() if QuotaRouter is not None else _UnavailableQuotaRouter()

# from-{seat}/runs lives under the relay root, split from vault_root
# (loupe-vault, heartbeats only — see settings.heartbeats_dir below) on 2026-07-10.
RELAY = settings.relay_root
CONTROL_RUN_ROOTS = (
    CONTROL_STATE_DIR / "runs",
    GEMINI_STATE_DIR / "runs",
)

# seat id -> (node/host name, color token, display label). Colors match the scan
# log EXACTLY: Worker1/delta = cyan, Worker5/charlie = amber, Worker2/alpha = green.
# worker2's node is "alpha" (its physical host, matching the machine card's
# display_name) — kept distinct from the seat key "worker2" so the tile's secondary
# label never re-shows the seat name (FLEET-BUILD-20260710-seat-card-alpha-host).
# Canonical ordered (seat, node, color, label) list now lives in app/seats.py —
# imported above as SEATS so every other module shares this one source.

# A run with no `done` sentinel that hasn't been touched in this long reads as an
# orphaned/dead run, not a live one — mirrors work.read_relay_runs.
ORPHAN_AFTER_S = 6 * 3600

# worker2's run.sh and this Nexus process share ONE host (alpha), so the `pid`
# recorded in worker2's status.json is meaningful against /proc HERE — unlike the
# 4 remote seats, whose pid belongs to a different machine entirely. This lets a
# dead worker2 pid be classified `died` immediately instead of waiting out
# ORPHAN_AFTER_S (FLEET-WORKER2-BUILD-20260722-panel-worker2-dead-pid; see
# FLEET-WORKER2-RECON-20260722-tower-duplicate-run-forensics /
# FLEET-WORKER2-RECON-20260722-tower-duplicate-collision-state for the zombie
# runs that motivated this).
_LOCAL_PID_SOURCES = {"alpha", "worker2"}
# Finished/died context stays on the tile this long; after that it reverts to
# "no recent runs" + FREE.
FRESH_WINDOW_S = 15 * 60
QUOTA_STALE_S = 15 * 60
MODEL_USAGE_STALE_S = 45 * 60
CENTRAL = ZoneInfo("America/Chicago")


def _generated_epoch(payload: dict[str, Any]) -> float:
    generated = payload.get("generated_at", "")
    return datetime.fromisoformat(generated.replace("Z", "+00:00")).timestamp()


def _pct(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown"
    value = float(value)
    if 0 < value < 1:
        return "<1%"
    return f"{round(value):d}%"


def _format_reset(epoch: float | None, *, approximate: bool = False) -> str:
    if epoch is None:
        return "unavailable"
    reset = datetime.fromtimestamp(epoch, CENTRAL)
    prefix = "~" if approximate else ""
    return (
        f"{prefix}{reset:%a %b} {reset.day} · "
        f"{reset.strftime('%-I:%M %p %Z')}"
    )


def _claude_reset_epoch(value: Any, generated: float) -> float | None:
    if not isinstance(value, str):
        return None
    clean = value.split("(", 1)[0].strip()
    match = re.fullmatch(
        r"(?:(?P<month>[A-Za-z]{3})(?P<day>\d{1,2}),)?"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?P<ampm>am|pm)",
        clean,
        re.I,
    )
    if not match:
        return None
    base = datetime.fromtimestamp(generated, CENTRAL)
    hour = int(match.group("hour")) % 12
    if match.group("ampm").lower() == "pm":
        hour += 12
    if match.group("month"):
        month = datetime.strptime(match.group("month").title(), "%b").month
        target = datetime(
            base.year,
            month,
            int(match.group("day")),
            hour,
            int(match.group("minute")),
            tzinfo=CENTRAL,
        )
        if target < base - timedelta(days=1):
            target = target.replace(year=base.year + 1)
    else:
        target = base.replace(
            hour=hour,
            minute=int(match.group("minute")),
            second=0,
            microsecond=0,
        )
        if target <= base:
            target += timedelta(days=1)
    return target.timestamp()


def _countdown_reset_epoch(value: Any, generated: float) -> float | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"\s*(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?",
        value,
    )
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(part or 0) for part in match.groups())
    return generated + days * 86400 + hours * 3600 + minutes * 60


def _iso_reset_epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _claude_window_reset(window: dict[str, Any], generated: float) -> float | None:
    return _iso_reset_epoch(window.get("resets_at")) or _claude_reset_epoch(
        window.get("resets"), generated
    )


def _gemini_window_reset(
    window: dict[str, Any],
    generated: float,
) -> tuple[float | None, bool]:
    exact = _iso_reset_epoch(window.get("resets_at"))
    if exact is not None:
        return exact, False
    projected = _countdown_reset_epoch(window.get("refreshes_in"), generated)
    return projected, projected is not None


def _numeric_pct(value: Any) -> float | None:
    """Return a bounded percentage for progress bars, or honest unavailability."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(0.0, min(100.0, float(value)))


def _provider_usage(
    provider: str,
    label: str,
    source: str,
    generated: float | None,
    five_used: Any,
    weekly_used: Any,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "label": label,
        "source": source,
        "updated_ms": round(generated * 1000) if generated is not None else None,
        "five_hour_used": _numeric_pct(five_used),
        "weekly_used": _numeric_pct(weekly_used),
    }


def _codex_usage(
    now: float,
) -> tuple[dict[str, Any], float | None, dict[str, Any]]:
    freshest: tuple[float, dict[str, Any]] | None = None
    for path in (RELAY / "heartbeats" / "quota").glob("*-codex.json"):
        try:
            payload = json.loads(path.read_text())
            if not payload.get("ok"):
                continue
            generated = _generated_epoch(payload)
            if now - generated > QUOTA_STALE_S:
                continue
            if freshest is None or generated > freshest[0]:
                freshest = (generated, payload)
        except Exception:
            continue
    if freshest is None:
        return {
            "usage": "Codex · unavailable",
            "resets": ("5h ↻ unavailable", "wk ↻ unavailable"),
        }, None, _provider_usage(
            "codex", "Codex", "codex app-server", None, None, None
        )
    generated, payload = freshest
    limits = payload.get("rateLimits") or {}
    windows = [
        window for window in (
            limits.get("primary") or {},
            limits.get("secondary") or {},
        )
        if window
    ]
    weekly = next(
        (window for window in windows if window.get("windowDurationMins") == 10080),
        {},
    )
    five = next(
        (window for window in windows if window.get("windowDurationMins") == 300),
        {},
    )
    weekly_used = weekly.get("usedPercent")
    five_used = five.get("usedPercent")
    reached = bool(
        limits.get("rateLimitReachedType")
        or limits.get("spendControlReached")
        or (isinstance(weekly_used, int) and weekly_used >= 100)
        or (isinstance(five_used, int) and five_used >= 100)
    )
    if reached:
        usage = "Codex · exhausted"
    else:
        parts = []
        if isinstance(weekly_used, int):
            parts.append(f"{weekly_used}% used wk")
        if isinstance(five_used, int):
            parts.append(f"{five_used}% used 5h")
        usage = "Codex · " + (" · ".join(parts) if parts else "available")
    return {
        "usage": usage,
        "resets": (
            f"5h ↻ {_format_reset(five.get('resetsAt'))}",
            f"wk ↻ {_format_reset(weekly.get('resetsAt'))}",
        ),
    }, generated, _provider_usage(
        "codex",
        "Codex",
        "codex app-server",
        generated,
        five_used,
        weekly_used,
    )


def _model_usage_tile(now: float) -> dict[str, Any]:
    """The retired Worker4 slot, repurposed as one honest three-provider card."""
    items: list[dict[str, Any]] = []
    providers: dict[str, dict[str, Any]] = {}
    generated_values: list[float] = []
    path = RELAY / "heartbeats" / "quota" / "model-usage.json"
    try:
        payload = json.loads(path.read_text())
        generated = _generated_epoch(payload)
        if now - generated > MODEL_USAGE_STALE_S:
            raise ValueError("stale")
        generated_values.append(generated)
        claude = payload.get("claude") or {}
        if claude.get("ok"):
            windows = claude.get("windows") or {}
            weekly_window = windows.get("weekly") or {}
            five_window = windows.get("five_hour") or {}
            items.append({
                "usage": (
                    f"Claude · {_pct(weekly_window.get('used_percent'))} used wk"
                    f" · {_pct(five_window.get('used_percent'))} used 5h"
                ),
                "resets": (
                    "5h ↻ " + _format_reset(_claude_window_reset(
                        five_window, generated
                    )),
                    "wk ↻ " + _format_reset(_claude_window_reset(
                        weekly_window, generated
                    )),
                ),
            })
            providers["claude"] = _provider_usage(
                "claude",
                "Claude",
                str(claude.get("source") or "claude internal usage"),
                generated,
                five_window.get("used_percent"),
                weekly_window.get("used_percent"),
            )
        else:
            items.append({
                "usage": "Claude · unavailable",
                "resets": ("5h ↻ unavailable", "wk ↻ unavailable"),
            })
            providers["claude"] = _provider_usage(
                "claude",
                "Claude",
                str(claude.get("source") or "claude internal usage"),
                generated,
                None,
                None,
            )
        gemini = payload.get("gemini") or {}
        if gemini.get("ok"):
            windows = gemini.get("windows") or {}
            weekly_window = windows.get("weekly") or {}
            five_window = windows.get("five_hour") or {}
            five_reset, five_approximate = _gemini_window_reset(
                five_window, generated
            )
            weekly_reset, weekly_approximate = _gemini_window_reset(
                weekly_window, generated
            )
            items.append({
                "usage": (
                    f"Gemini · {_pct(weekly_window.get('used_percent'))} used wk"
                    f" · {_pct(five_window.get('used_percent'))} used 5h"
                ),
                "resets": (
                    "5h ↻ " + _format_reset(
                        five_reset, approximate=five_approximate
                    ),
                    "wk ↻ " + _format_reset(
                        weekly_reset, approximate=weekly_approximate
                    ),
                ),
            })
            providers["gemini"] = _provider_usage(
                "gemini",
                "Gemini",
                str(
                    gemini.get("source")
                    or "cloudcode retrieveUserQuotaSummary"
                ),
                generated,
                five_window.get("used_percent"),
                weekly_window.get("used_percent"),
            )
        else:
            items.append({
                "usage": "Gemini · unavailable",
                "resets": ("5h ↻ unavailable", "wk ↻ unavailable"),
            })
            providers["gemini"] = _provider_usage(
                "gemini",
                "Gemini",
                str(
                    gemini.get("source")
                    or "cloudcode retrieveUserQuotaSummary"
                ),
                generated,
                None,
                None,
            )
    except Exception:
        items.extend((
            {
                "usage": "Claude · unavailable",
                "resets": ("5h ↻ unavailable", "wk ↻ unavailable"),
            },
            {
                "usage": "Gemini · unavailable",
                "resets": ("5h ↻ unavailable", "wk ↻ unavailable"),
            },
        ))
        providers["claude"] = _provider_usage(
            "claude", "Claude", "claude internal usage", None, None, None
        )
        providers["gemini"] = _provider_usage(
            "gemini",
            "Gemini",
            "cloudcode retrieveUserQuotaSummary",
            None,
            None,
        )

    codex_item, codex_generated, codex_provider = _codex_usage(now)
    items.insert(1, codex_item)
    providers["codex"] = codex_provider
    if codex_generated is not None:
        generated_values.append(codex_generated)
    newest = max(generated_values) if generated_values else None
    primary = (
        f"updated {max(0, int((now - newest) / 60))}m ago"
        if newest is not None
        else "quota sources unavailable"
    )
    live = any("unavailable" not in item["usage"] for item in items)
    worker_route = quota_router.recommend(lane="worker", size="small")
    state_rank = {"GREEN": 0, "YELLOW": 1, "RED": 2}
    worker_candidates = sorted(
        (
            {
                "provider": candidate.get("provider"),
                "model": candidate.get("model"),
                "state": str(candidate.get("state") or "RED").upper(),
                "score": candidate.get("score"),
                "reason": candidate.get("reason"),
                "selected": (
                    candidate.get("provider") == worker_route.get("provider")
                    and candidate.get("model") == worker_route.get("model")
                ),
            }
            for candidate in (worker_route.get("candidates") or [])
            if isinstance(candidate, dict)
        ),
        key=lambda candidate: (
            state_rank.get(candidate["state"], 3),
            -(candidate["score"] or 0),
            candidate["provider"] or "",
        ),
    )
    return {
        "seat": "worker4",
        "node": "fleet",
        "color": "gold",
        "label": "Model Usage",
        "sub": "cloud quotas",
        "state": "idle",
        "kind": None,
        "token": None,
        "full_token": None,
        "started_ms": None,
        "ended_ms": None,
        "median_s": None,
        "badge": "LIVE" if live else "UNKNOWN",
        "primary": primary,
        "inline": None,
        "provider_line": "",
        "usage_card": True,
        "usage_items": items,
        "provider_usage": [
            providers[name] for name in ("claude", "codex", "gemini")
        ],
        "routing": {
            "worker": worker_route,
            "candidates": worker_candidates,
        },
    }


def _runtime_model(sj_path: Path) -> tuple[str | None, str | None]:
    """Provider/model recorded by the worker harness, if present."""
    try:
        payload = json.loads(sj_path.read_text())
    except Exception:
        return None, None
    provider = payload.get("provider")
    model = payload.get("model")
    return (
        str(provider).strip().lower() if provider else None,
        str(model).strip() if model else None,
    )


def _model_badge(
    provider: str | None,
    model: str | None,
) -> dict[str, str] | None:
    """Compact provider mark + honest model label for one active worker."""
    raw_provider = (provider or "").strip().lower()
    raw_model = (model or "").strip()
    low_model = raw_model.lower()
    if not raw_provider:
        if "sonnet" in low_model or "opus" in low_model or "claude" in low_model:
            raw_provider = "claude"
        elif "gemini" in low_model:
            raw_provider = "gemini"
        elif "gpt-oss" in low_model or "ollama" in low_model:
            raw_provider = "local"
        elif low_model.startswith("gpt-") or "codex" in low_model:
            raw_provider = "codex"
    family = {
        "anthropic": "claude",
        "claude": "claude",
        "openai": "codex",
        "codex": "codex",
        "gemini": "gemini",
        "google": "gemini",
        "gemini": "gemini",
        "localworker": "local",
        "local": "local",
        "ollama": "local",
    }.get(raw_provider, "unknown")
    if not raw_model and family == "unknown":
        return None
    if low_model == "sonnet":
        label = "Claude Sonnet"
    elif low_model == "opus":
        label = "Claude Opus"
    elif low_model == "gpt-5.6-terra":
        label = "GPT-5.6 Terra"
    elif low_model == "gpt-oss:20b":
        label = "GPT-OSS 20B"
    elif low_model == "gemini-3.6-flash-high":
        label = "Gemini 3.6 Flash"
    elif raw_model:
        label = raw_model.replace(":", " ").replace("-", " ").title()
    else:
        label = {
            "claude": "Claude",
            "codex": "Codex",
            "gemini": "Gemini",
            "local": "Local",
        }.get(family, "Model")
    mark = {
        "claude": "✳",
        "codex": "◎",
        "gemini": "✦",
        "local": "⬡",
        "unknown": "●",
    }[family]
    return {"family": family, "label": label[:36], "mark": mark}


def _kind_of(token: str, lane: str | None) -> str | None:
    """BUILD vs RECON off the run metadata. The token always carries the segment
    (FLEET-[SEAT-]BUILD|RECON-YYYYMMDD-...); the status.json `lane` is the
    fallback (prompts = BUILD, recon = RECON)."""
    up = (token or "").upper()
    if "-RECON-" in up:
        return "RECON"
    if "-BUILD-" in up:
        return "BUILD"
    if lane == "recon":
        return "RECON"
    if lane == "prompts":
        return "BUILD"
    return None


def _short_token(token: str) -> str:
    """Trim the ceremonial prefix+date to the human slug: e.g.
    FLEET-BUILD-20260704-seat-availability-board -> seat-availability-board."""
    return re.sub(r"^FLEET-.*?\d{8}-", "", token) or token


def _started_epoch(sj_path: Path) -> tuple[float | None, str | None]:
    """(started_epoch, lane) parsed from a run's status.json, or (None, None)."""
    try:
        j = json.loads(sj_path.read_text())
    except Exception:
        return None, None
    lane = j.get("lane")
    sa = j.get("started_at")
    if isinstance(sa, (int, float)) and not isinstance(sa, bool):
        return float(sa), lane
    if isinstance(sa, str) and sa:
        try:
            from datetime import datetime
            return (datetime.fromisoformat(sa.replace("Z", "+00:00")).timestamp(),
                    lane)
        except (ValueError, TypeError):
            return None, lane
    return None, lane


def _status_pid(sj_path: Path) -> int | None:
    """The `pid` recorded in a run's status.json, or None on any parse failure,
    or if it's missing/malformed. Feeds the worker2-only liveness probe below;
    None here always means "fall back to heartbeat/mtime", never "dead"."""
    try:
        j = json.loads(sj_path.read_text())
    except Exception:
        return None
    pid = j.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def _worker2_pid_state(pid: int, token: str, run_dir: Path) -> str:
    """"dead" / "alive" / "unknown" for a worker2-local pid, bounded and never
    raising. "unknown" covers every case where we can't be confident — a
    permission error or a transient read failure — and the caller must treat
    that exactly like a missing pid: fall back to heartbeat/mtime rather than
    guess either way.

    A live pid is further checked against /proc/<pid>/cmdline for ownership
    (bounded: one small file read) to guard against PID reuse — the OS handing
    this same pid number to an unrelated process after the original died. A
    pid that exists but whose cmdline doesn't reference this run's token or
    run dir is treated as "dead" (from this run's point of view, it's not
    there anymore), not as a false "alive".
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except OSError:
        # e.g. PermissionError (EPERM) — the pid slot may or may not be ours;
        # not confident enough to call it either way.
        return "unknown"
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(errors="replace")
    except FileNotFoundError:
        # process vanished between the kill(0) probe and this read.
        return "dead"
    except OSError:
        return "unknown"
    if token in cmdline or str(run_dir) in cmdline:
        return "alive"
    return "dead"


def _scan_seat(seat: str, now: float) -> list[dict[str, Any]]:
    """Every run dir for one source id, parsed to a compact record. Never raises."""
    base = RELAY / f"from-{seat}" / "runs"
    out: list[dict[str, Any]] = []
    try:
        dirs = [d for d in base.iterdir() if d.is_dir()]
    except Exception:
        return out
    for d in dirs:
        try:
            mtime = d.stat().st_mtime
        except Exception:
            continue
        token = d.name
        started, lane = _started_epoch(d / "status.json")
        provider, model = _runtime_model(d / "status.json")
        kind = _kind_of(token, lane)
        done_f = d / "done"
        if done_f.exists():
            try:
                code = done_f.read_text()[:32].strip()
            except Exception:
                code = ""
            state = "done" if code in ("", "0") else "died"
            try:
                ended = done_f.stat().st_mtime
            except Exception:
                ended = mtime
            running = False
        else:
            pid_state = "unknown"
            if seat in _LOCAL_PID_SOURCES:
                pid = _status_pid(d / "status.json")
                if pid is not None:
                    pid_state = _worker2_pid_state(pid, token, d)
            if pid_state == "dead":
                # confirmed-gone pid trumps ORPHAN_AFTER_S -- no need to wait
                # out the window when we already know the process isn't there.
                state, ended, running = "died", mtime, False
            elif pid_state == "alive":
                # confirmed-live pid trumps ORPHAN_AFTER_S the other way too.
                state, ended, running = "running", None, True
            elif (now - mtime) > ORPHAN_AFTER_S:
                # no sentinel for >6h: orphaned -> treat as died, ended ~ last touch.
                state, ended, running = "died", mtime, False
            else:
                state, ended, running = "running", None, True
        dur = int(ended - started) if (ended and started) else None
        out.append({
            "token": token, "kind": kind, "state": state, "running": running,
            "started": started, "ended": ended, "dur": dur, "mtime": mtime,
            "provider": provider, "model": model, "source": "relay",
        })
    return out


def _scan_control_workers(node: str, now: float) -> list[dict[str, Any]]:
    """Active provider-pinned worker jobs from Nexus's private stores."""
    out: list[dict[str, Any]] = []
    try:
        paths = [path for root in CONTROL_RUN_ROOTS for path in root.glob("*/run.json")]
    except Exception:
        return out
    for path in paths:
        try:
            payload = json.loads(path.read_text())
            if payload.get("host") != node or payload.get("state") != "running":
                continue
            started = float(payload["started_at"])
            timeout_s = int(payload.get("timeout_seconds") or 0)
            if timeout_s <= 0 or now - started > timeout_s + 120:
                continue
            mtime = path.stat().st_mtime
        except Exception:
            continue
        out.append({
            "token": payload.get("job_id"),
            "kind": "BUILD",
            "state": "running",
            "running": True,
            "started": started,
            "ended": None,
            "dur": None,
            "mtime": mtime,
            "provider": payload.get("provider") or "gemini",
            "model": payload.get("model") or "gemini-3.6-flash-high",
            "source": "control",
        })
    return out


def _median_duration(runs: list[dict[str, Any]], kind: str | None) -> int | None:
    """Median duration of COMPLETED (exit-0) runs for this kind, or None if there
    is no such history yet — in which case the caller must NOT invent an ETA."""
    ds = [r["dur"] for r in runs
          if r["state"] == "done" and r["kind"] == kind
          and r["dur"] and r["dur"] > 0]
    if not ds:
        return None
    return int(median(ds))


def _mins(secs: float) -> int:
    return int(max(0, round(secs / 60.0)))


def _num(v):
    """int/float or None — never raises. Bools are not numbers here."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_inline(seat: str, now: float,
                 active_token: str | None) -> dict[str, Any] | None:
    """The seat's INLINE progress record (heartbeats/inline/<seat>.json), or None.

    Separate lane from the top-level heartbeats/<job>.json the jobs panel reads —
    the panel's non-recursive *.json glob never sees this subdir. Gated so a stale
    or wrong-run beat can't light a bar:
      • ts older than `job_stale_seconds` → None (the tile falls back to today).
      • a `token` in the record that doesn't match the seat's active run → None
        (a leftover beat from a previous run must not bleed onto a new one).
      • no done/total → None (nothing to draw a bar with).
    Emits an `eta_s`/`eta_at_ms` pair (progress-derived, may be None) so the client
    counts a real ETA DOWN between sweeps instead of guessing from the median.
    """
    try:
        path = settings.heartbeats_dir / "inline" / f"{seat}.json"
        j = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(j, dict):
        return None
    # a terminal beat clears the bar immediately
    if j.get("state") in ("done", "failed"):
        return None
    # freshness off ts (ISO-8601 Z or epoch); no ts → treat as stale.
    ts = j.get("ts")
    age = None
    try:
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            age = now - float(ts)
        elif isinstance(ts, str) and ts:
            from datetime import datetime
            age = now - datetime.fromisoformat(
                ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        age = None
    if age is None or age > settings.job_stale_seconds:
        return None
    # token binding: if the beat names a run, it must be THIS seat's active run.
    tok = j.get("token")
    if tok and active_token and str(tok) != str(active_token):
        return None
    done, total = _num(j.get("done")), _num(j.get("total"))
    if done is None or not total or total <= 0:
        return None
    done_i, total_i = int(done), int(total)
    pct = round(100.0 * done_i / total_i, 1)
    rec: dict[str, Any] = {
        "done": done_i, "total": total_i, "pct": min(100.0, max(0.0, pct)),
        "ts_ms": int((now - age) * 1000),
    }
    if j.get("label"):
        rec["label"] = str(j["label"])[:48]
    r = _num(j.get("rate"))
    if r is not None:
        rec["rate"] = round(float(r), 2)
        rec["unit"] = str(j.get("unit") or "rate")[:16]
    if j.get("message"):
        rec["message"] = str(j["message"])[:120]
    return rec


def _inline_eta_s(inline: dict[str, Any] | None,
                  elapsed: float | None) -> float | None:
    """Progress-derived remaining seconds from observed done/total over elapsed.
    Unit-free and honest (gets more accurate as `done` grows); None when done==0
    (no velocity yet), 0.0 when already at/over total (finishing up)."""
    if not inline or not elapsed or elapsed <= 0:
        return None
    d, t = inline.get("done"), inline.get("total")
    if not t:
        return None
    if d >= t:
        return 0.0
    if d <= 0:
        return None
    return elapsed * (t - d) / d


def _primary_line(state: str, kind: str | None, elapsed: float | None,
                  ago: float | None, median_s: int | None,
                  local: bool = False, inline: dict[str, Any] | None = None,
                  inline_eta_s: float | None = None) -> str:
    """The tile's context line. Kept in lockstep with the client-side renderer so
    a live refresh and a fresh page load read identically.

    When a fresh INLINE record is present, the ETA is derived from real progress
    (done/total over elapsed) and shown WITHOUT the `est.` tag — it's a measurement,
    not a guess. With no inline progress we fall back to the `est.`-labeled median.
    """
    k = kind or "run"
    if state in ("busy", "running"):
        if local:
            return f"in session (local) · {_mins(elapsed or 0)}m elapsed"
        if inline:
            lbl = f" · {inline['label']}" if inline.get("label") else ""
            prog = f"{inline['done']}/{inline['total']}"
            if inline_eta_s is None:
                return f"job in progress · {k}{lbl} · {prog}"
            if inline_eta_s <= 0:
                return f"job in progress · {k}{lbl} · {prog} · finishing up…"
            return (f"job in progress · {k}{lbl} · {prog} · "
                    f"ETA ~{max(1, _mins(inline_eta_s))}m")
        if median_s is None:
            # no history for this seat+kind: honest elapsed, never a fake ETA.
            return f"running · {_mins(elapsed or 0)}m elapsed"
        remaining = median_s - (elapsed or 0)
        if remaining <= 0:
            return f"job in progress · {k} · finishing up…"
        return f"job in progress · {k} · ETA ~{max(1, _mins(remaining))}m est."
    if state == "done":
        m = _mins(ago or 0)
        when = "just now" if m == 0 else f"{m} min ago"
        return f"finished {k} {when}"
    if state == "died":
        m = _mins(ago or 0)
        when = "just now" if m == 0 else f"{m} min ago"
        return f"died {when}"
    return "no recent runs"


def _tile(seat: str, node: str, color: str, label: str,
          now: float, run_sources: tuple[str, ...] | None = None) -> dict[str, Any]:
    """One node's tile. Isolated: any failure yields a clean idle/FREE tile."""
    base = {
        "seat": seat, "node": node, "color": color, "label": label, "sub": "",
        "state": "idle", "kind": None, "token": None, "full_token": None,
        "started_ms": None, "ended_ms": None, "median_s": None,
        "badge": "FREE", "primary": "no recent runs", "inline": None,
        "provider_line": "",
        "model_badge": (
            _model_badge("local", "gpt-oss:20b") if seat == "localworker" else None
        ),
    }
    try:
        sources = run_sources or (seat,)
        by_token: dict[str, dict[str, Any]] = {}
        for source in sources:
            for run in _scan_seat(source, now):
                previous = by_token.get(run["token"])
                if previous is None or run["mtime"] > previous["mtime"]:
                    by_token[run["token"]] = run
        runs = list(by_token.values()) + _scan_control_workers(node, now)
    except Exception as e:  # noqa: BLE001 — a tile never breaks the sweep
        log.warning("seat %s scan failed: %s", seat, e)
        return base
    if not runs:
        return base

    live = [r for r in runs if r["running"]]
    if live:
        cur = max(live, key=lambda r: (r["started"] or r["mtime"]))
        median_s = _median_duration(runs, cur["kind"])
        elapsed = (now - cur["started"]) if cur["started"] else (now - cur["mtime"])
        # A busy seat may be doing a long INLINE phase and publishing progress to
        # heartbeats/inline/<seat>.json — bind it to THIS run's token so a stale
        # beat can't leak onto a new run. When present it drives a bar + a real
        # (progress-derived) ETA; absent, the tile behaves exactly as before.
        inline = None
        for source in dict.fromkeys((seat, *sources)):
            inline = _read_inline(source, now, cur["token"])
            if inline is not None:
                break
        eta_s = _inline_eta_s(inline, elapsed)
        if inline is not None:
            inline["eta_s"] = int(eta_s) if eta_s is not None else None
            inline["eta_at_ms"] = int(now * 1000)
        base.update({
            "state": "busy", "kind": cur["kind"],
            "token": (
                _short_token(cur["token"])
                if cur.get("source") == "relay" and cur.get("token")
                else None
            ),
            "full_token": (
                cur["token"] if cur.get("source") == "relay" else None
            ),
            "started_ms": int((cur["started"] or cur["mtime"]) * 1000),
            "median_s": median_s, "badge": "BUSY", "inline": inline,
            "model_badge": (
                _model_badge(cur.get("provider"), cur.get("model"))
                or base["model_badge"]
            ),
            "primary": _primary_line("busy", cur["kind"], elapsed, None, median_s,
                                     inline=inline, inline_eta_s=eta_s),
        })
        return base

    # nothing live — surface the most-recently-finished run if it's still fresh.
    finished = [r for r in runs if not r["running"] and r["ended"]]
    if finished:
        cur = max(finished, key=lambda r: r["ended"])
        ago = now - cur["ended"]
        if ago <= FRESH_WINDOW_S:
            base.update({
                "state": cur["state"], "kind": cur["kind"],
                "token": _short_token(cur["token"]), "full_token": cur["token"],
                "ended_ms": int(cur["ended"] * 1000),
                "badge": "DIED" if cur["state"] == "died" else "FREE",
                "primary": _primary_line(cur["state"], cur["kind"], None, ago, None),
            })
    return base


def read_seat_board(now: float | None = None) -> dict[str, Any]:
    """The node-card strip payload: {seats: [...], generated_ms}. Folded into
    snap.seats each sweep. Never raises; a failed card degrades to idle/FREE."""
    now = now if now is not None else time.time()
    tiles = []
    for seat, node, color, label, run_sources in CARDS:
        try:
            tiles.append(_tile(seat, node, color, label, now, run_sources))
        except Exception as e:  # noqa: BLE001
            log.warning("node card %s failed: %s", seat, e)
            tiles.append({
                "seat": seat, "node": node, "color": color, "label": label,
                "sub": "", "state": "idle", "kind": None, "token": None,
                "full_token": None, "started_ms": None, "ended_ms": None,
                "median_s": None, "badge": "FREE", "primary": "no recent runs",
                "inline": None, "provider_line": "",
                "model_badge": (
                    _model_badge("local", "gpt-oss:20b")
                    if seat == "localworker" else None
                ),
            })
    tiles.append(_model_usage_tile(now))
    return {"seats": tiles, "generated_ms": int(now * 1000)}
