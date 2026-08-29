"""
Thermal-halt -> nexus-alarm detector (targeted Phase-4 slice,
panel-notifications-design.md). Watches charlie's thermal-guard heartbeat —
already synced onto worker2 by Syncthing, no ssh, no charlie mutation — for the
guard's hard-halt-at-110C action and notify()s nexus-alarm exactly once per
halt event, then nexus-post once Gallery's ML container recovers. Also covers
two secondary edges off the same heartbeat: an early "approaching emergency"
warning, the guard heartbeat itself going stale/dark, and (Phase 4c) the
guard capping auto-restarts and staying down (`thermal_guard_holdoff`).

Shape mirrors app/run_watcher.py: an `alerts` row (notify_store) is both the
"have we already notified this edge" watermark and the open/resolved state,
independent of notify()'s own 30-min health-alert re-fire window.

WATERMARK-FROM-NOW (same discipline as run_watcher's NOW-watermark): the
FIRST-EVER evaluation of a condition_key/host pair seeds an `alerts` row from
the CURRENT state, marked already-notified, so a pre-existing condition
(charlie's already-resolved 09:02:18Z halt) never fires the moment this
watcher starts running. Only a state strictly newer than the seed fires.

The core evaluators take already-parsed heartbeat dicts so they can be driven
directly by an injected-dict test harness with no real files or ssh involved;
`scan_once()` is the thin real-sweep wrapper that reads the vault heartbeats
and calls them.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import notify_store
from .config import settings
from .notify import notify

log = logging.getLogger("nexus.thermal_watch")

HOST = "charlie"
THERMAL_HEARTBEAT: Path = settings.heartbeats_dir / "thermal-guard-charlie.json"
GALLERY_HEARTBEAT: Path = settings.heartbeats_dir / "gallery-library-scan.json"
JOBS_NAVIGATE = "/jobs/gallery-library-scan?nf=1"

CONDITION_HALT = "thermal_halt"
CONDITION_APPROACH = "thermal_approaching"
CONDITION_DARK = "thermal_guard_dark"
CONDITION_HOLDOFF = "thermal_holdoff"

_HALT_RE = re.compile(r"^stopped:")

# Phase 3b secondary thresholds — safely below the guard's 110C emergency
# stop, with hysteresis so a single sweep at 95.0 doesn't flap.
APPROACH_WARN_C = 95.0
APPROACH_CLEAR_C = 85.0
GUARD_DARK_STALE_SECONDS = 600


def _read_json(path: Path) -> dict[str, Any] | None:
    """Never raises — a partial write / missing file / bad JSON degrades to
    None, which every evaluator below treats as 'nothing to report'."""
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _gallery_ml_healthy(gallery: dict[str, Any] | None) -> bool:
    """gallery.py computes ml_health_status from `docker inspect` but only
    persists it folded into the heartbeat's `message` string ("CUDA ready" /
    "CUDA not ready") — there is no standalone ml_health_status key on disk,
    so this is the actual signal available to a heartbeat reader."""
    if not gallery:
        return False
    return "CUDA ready" in str(gallery.get("message") or "")


async def evaluate_halt_and_recovery(guard: dict[str, Any] | None,
                                      gallery: dict[str, Any] | None) -> dict:
    """CORE: HALT edge -> nexus-alarm, RECOVERY edge -> nexus-post. Returns a
    summary dict for logging/verification; never raises internally (the
    caller — scan_once — still wraps this defensively)."""
    if not guard:
        return {"halt_fired": False, "recovery_fired": False, "reason": "no guard heartbeat"}

    action = str(guard.get("thermal_guard_action") or "")
    event = str(guard.get("thermal_guard_last_event") or "")
    temp_c = guard.get("temp_c")
    threshold = guard.get("emergency_threshold_c")

    row = notify_store.get_alert_by_condition(CONDITION_HALT, HOST)
    if row is None:
        notify_store.seed_alert(CONDITION_HALT, HOST, event)
        log.info("thermal_watch: seeded %s watermark at event=%s", CONDITION_HALT, event)
        return {"halt_fired": False, "recovery_fired": False, "reason": "seeded watermark"}

    halt_fired = False
    if _HALT_RE.match(action) and event and event > (row.get("last_seen") or ""):
        result = await notify({
            "source": "thermal_watch",
            "condition": CONDITION_HALT,
            "host": HOST,
            "alert_id": row["id"],
            "title": f"\U0001f525\U0001f6d1 Thermal halt — {HOST}",
            "body": (f"{temp_c}°C ≥ {threshold}°C emergency threshold · "
                     f"{action} · event {event}"),
            "navigate": JOBS_NAVIGATE,
        })
        notify_store.mark_alert_notified(row["id"], event)
        halt_fired = not result.get("suppressed", False)
        row = notify_store.get_alert_by_condition(CONDITION_HALT, HOST)  # refreshed state

    recovery_fired = False
    armed = bool(row.get("last_notified_at")) and not row.get("resolved_at")
    if armed:
        recovered = _gallery_ml_healthy(gallery) or (bool(action) and not _HALT_RE.match(action))
        if recovered:
            result = await notify({
                "source": "thermal_watch",
                "condition": "thermal_recovery",
                "host": HOST,
                "alert_id": row["id"],
                "title": f"✅\U0001f321️ Gallery recovered after thermal halt — {HOST}",
                "body": f"ML health restored after the halt at event {row.get('last_seen')}",
                "navigate": JOBS_NAVIGATE,
            })
            notify_store.mark_alert_resolved(row["id"])
            recovery_fired = not result.get("suppressed", False)

    return {"halt_fired": halt_fired, "recovery_fired": recovery_fired}


async def _evaluate_approaching(guard: dict[str, Any] | None) -> bool:
    """Phase 3b: early warning safely below the guard's own 110C halt."""
    if not guard:
        return False
    temp_c = guard.get("temp_c")
    if temp_c is None:
        return False

    row = notify_store.get_alert_by_condition(CONDITION_APPROACH, HOST)
    if row is None:
        notify_store.seed_alert(CONDITION_APPROACH, HOST,
                                 "warn" if temp_c >= APPROACH_WARN_C else "clear")
        return False

    prev = row.get("last_seen") or "clear"
    if temp_c >= APPROACH_WARN_C:
        state = "warn"
    elif temp_c <= APPROACH_CLEAR_C:
        state = "clear"
    else:
        state = prev  # hysteresis dead zone: hold whatever it was

    if state == "warn" and prev != "warn":
        result = await notify({
            "source": "thermal_watch",
            "condition": CONDITION_APPROACH,
            "host": HOST,
            "alert_id": row["id"],
            "title": f"\U0001f321️⚠️ Approaching thermal emergency — {HOST}",
            "body": (f"{temp_c}°C (warn ≥{APPROACH_WARN_C}°C, "
                     f"emergency {guard.get('emergency_threshold_c')}°C)"),
            "navigate": JOBS_NAVIGATE,
        })
        notify_store.mark_alert_notified(row["id"], state)
        return not result.get("suppressed", False)

    if state != prev:
        notify_store.update_alert_seen(row["id"], state)
    return False


async def _evaluate_guard_dark(guard: dict[str, Any] | None) -> bool:
    """Phase 3b: the guard heartbeat itself going stale/missing means the
    thermal safety net is blind — worth a (non-critical) heads-up."""
    age_s: float | None = None
    if guard:
        updated_at = str(guard.get("updated_at") or "")
        try:
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - dt).total_seconds()
        except ValueError:
            age_s = None
    stale = guard is None or age_s is None or age_s > GUARD_DARK_STALE_SECONDS
    state = "stale" if stale else "fresh"

    row = notify_store.get_alert_by_condition(CONDITION_DARK, HOST)
    if row is None:
        notify_store.seed_alert(CONDITION_DARK, HOST, state)
        return False

    prev = row.get("last_seen") or "fresh"
    if state == "stale" and prev != "stale":
        result = await notify({
            "source": "thermal_watch",
            "condition": CONDITION_DARK,
            "host": HOST,
            "alert_id": row["id"],
            "title": f"\U0001f4a4\U0001f321️ Thermal safety net dark — {HOST}",
            "body": ("thermal-guard heartbeat missing" if guard is None
                      else f"heartbeat stale {int(age_s or 0)}s"),
            "navigate": JOBS_NAVIGATE,
        })
        notify_store.mark_alert_notified(row["id"], state)
        return not result.get("suppressed", False)

    if state != prev:
        notify_store.update_alert_seen(row["id"], state)
    return False


async def _evaluate_holdoff(guard: dict[str, Any] | None) -> bool:
    """Phase 4c: the guard's own auto-restart cap — it stopped retrying and
    is staying down (thermal runaway / cooling failure needing manual
    intervention). Un-alarmed by evaluate_halt_and_recovery above, which only
    matches `^stopped:` actions, not this separate `thermal_guard_holdoff`
    flag. Simple false<->true edge trigger (mirrors _evaluate_guard_dark's
    stale/fresh shape); WATERMARK-FROM-NOW seeds from the current value so a
    heartbeat already showing holdoff=false at startup fires nothing."""
    holdoff = bool(guard.get("thermal_guard_holdoff")) if guard else False
    state = "true" if holdoff else "false"

    row = notify_store.get_alert_by_condition(CONDITION_HOLDOFF, HOST)
    if row is None:
        notify_store.seed_alert(CONDITION_HOLDOFF, HOST, state)
        return False

    prev = row.get("last_seen") or "false"
    if state == prev:
        return False

    if state == "true":
        result = await notify({
            "source": "thermal_watch",
            "condition": CONDITION_HOLDOFF,
            "host": HOST,
            "alert_id": row["id"],
            "title": f"\U0001f525⛔ Thermal guard holdoff — {HOST} "
                     "(staying halted, manual intervention)",
            "body": (f"guard capped auto-restarts and is staying down · "
                     f"action={guard.get('thermal_guard_action')} · "
                     f"event={guard.get('thermal_guard_last_event')}"),
            "navigate": JOBS_NAVIGATE,
        })
        notify_store.mark_alert_notified(row["id"], state)
        return not result.get("suppressed", False)

    result = await notify({
        "source": "thermal_watch",
        "condition": "thermal_holdoff_recovery",
        "host": HOST,
        "alert_id": row["id"],
        "title": f"✅\U0001f321️ Thermal guard holdoff cleared — {HOST}",
        "body": "guard resumed normal auto-restart handling",
        "navigate": JOBS_NAVIGATE,
    })
    notify_store.mark_alert_notified(row["id"], state)
    return not result.get("suppressed", False)


async def scan_once() -> dict:
    """One sweep tick: CORE halt/recovery + SECONDARY approaching/guard-dark.
    Never raises — a bad heartbeat file or a notify() hiccup degrades to a
    no-op result for that slice, not a wedged scheduler (mirrors
    run_watcher.scan_once)."""
    guard = _read_json(THERMAL_HEARTBEAT)
    gallery = _read_json(GALLERY_HEARTBEAT)

    try:
        core = await evaluate_halt_and_recovery(guard, gallery)
    except Exception:
        log.exception("thermal_watch: halt/recovery evaluation failed")
        core = {"halt_fired": False, "recovery_fired": False, "reason": "exception"}

    try:
        approach_fired = await _evaluate_approaching(guard)
    except Exception:
        log.exception("thermal_watch: approaching-emergency evaluation failed")
        approach_fired = False

    try:
        dark_fired = await _evaluate_guard_dark(guard)
    except Exception:
        log.exception("thermal_watch: guard-dark evaluation failed")
        dark_fired = False

    try:
        holdoff_fired = await _evaluate_holdoff(guard)
    except Exception:
        log.exception("thermal_watch: holdoff evaluation failed")
        holdoff_fired = False

    return {**core, "approach_fired": approach_fired, "dark_fired": dark_fired,
            "holdoff_fired": holdoff_fired}
