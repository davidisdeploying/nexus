"""
Generic health-condition -> notify() evaluator (Phase 4a,
panel-notifications-design.md). Generalizes app/thermal_watch.py's proven
per-condition `alerts` skeleton (watermark-from-now seed, rising/falling
edge, silent-transition update) and ADDS a long-persist reminder re-fire
across the rest of notify.py's HEALTH_CONDITIONS catalog: disk_warn,
disk_critical, backup_stale, service_down, heartbeat_stale.

READS ONLY the latest StatusSnapshot (store.read_snapshot(), the same
status.json probes.py already writes every heartbeat sweep) plus the local
heartbeat JSON files thermal_watch.py already reads directly — it NEVER
re-runs a probe or opens a new ssh connection (the thermal-probe-lighten
lesson: don't add load to an already-expensive sweep).

DEFERRED (do not wire here — see recon FLEET-WORKER2-RECON-20260710-phase4-scope
§Recommended-scope): `gpu_xid_fault` (no Xid/error-code parsing exists in
probes.py — needs new detection work, not just a notify() hookup) and
`thermal_critical` (superseded by the already-wired thermal_halt slice in
thermal_watch.py; no separate producer exists or is needed).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import notify_store
from .config import FLEET, settings
from .jobs.gallery import is_settled_terminal
from .models import Health
from .notify import notify
from .store import read_snapshot

log = logging.getLogger("nexus.health_watch")

# A still-open alarm re-fires this often so a continuously-bad condition (e.g.
# delta's currently-baselined 75h-stale backup) resurfaces ~twice a day
# instead of alarming once and then going silent forever until it clears.
REMIND_AFTER_HOURS = 12.0

# Hysteresis dead bands: how far a value must drop BELOW the alarm threshold
# before the condition is considered cleared, so a value hovering right at the
# boundary doesn't flap-fire on every 60s tick.
DISK_CLEAR_MARGIN_PCT = 5
BACKUP_CLEAR_MARGIN_HOURS = 4

# heartbeat_stale staleness thresholds. host-*.json rides the 300s fleet sweep
# (settings.heartbeat_interval_seconds) — 600s reuses thermal_watch's own
# GUARD_DARK_STALE_SECONDS (~2 missed sweeps). gallery-library-scan.json ticks
# every 60s (scheduler.py's "gallery-library-scan" job) WHILE the scan is
# active/non-terminal, so a tighter constant fits (~5 missed ticks) for that
# case. Once the file settles into a confirmed done/failed sample (see
# jobs.gallery.is_settled_terminal), the producer intentionally throttles
# itself down to a discovery poll every TERMINAL_POLL_INTERVAL_SECONDS
# (15 min) instead — this threshold is exempted for that record shape below
# rather than applied blindly to a file that no longer updates every 60s.
HOST_HEARTBEAT_STALE_SECONDS = 600
GALLERY_HEARTBEAT_STALE_SECONDS = 300

_DISK_NODES = ("charlie", "delta", "worker2")                    # carry a `disk`/`disk_local` probe
_BACKUP_NODES = ("delta",)                                     # carry a `backup` probe
_SERVICE_DOWN_NODES = ("charlie", "delta", "echo")    # carry a `tailscale_ping` probe


def _label_for(node_name: str, raw_kind: str) -> str:
    """`app/jobs/heartbeat.py` (line ~39) overwrites ProbeResult.kind with the
    node's display label before it's persisted to status.json — e.g.
    tailscale_ping -> "ping · tailnet" for charlie/delta/echo. A
    lookup against the raw ProbeKind string silently finds nothing once that
    relabel has happened, so every probe lookup here must go through this."""
    for node in FLEET:
        if node.name == node_name:
            return node.labels.get(raw_kind, raw_kind)
    return raw_kind


def _find_probe(snap, node_name: str, kind: str):
    for node in snap.nodes:
        if node.name == node_name:
            for p in node.probes:
                if p.kind == kind:
                    return p
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    """Never raises — a partial write / missing file / bad JSON degrades to
    None (mirrors thermal_watch._read_json)."""
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _heartbeat_age_seconds(data: dict[str, Any] | None) -> float | None:
    """Prefer ISO `updated_at` (host-*.json); fall back to epoch `ts`
    (gallery-library-scan.json has no updated_at key)."""
    if not data:
        return None
    updated_at = data.get("updated_at")
    if updated_at:
        try:
            dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except ValueError:
            pass
    ts = data.get("ts")
    if ts is not None:
        try:
            return time.time() - float(ts)
        except (TypeError, ValueError):
            return None
    return None


def _reminder_due(last_notified_at: str | None, first_seen: str | None) -> bool:
    basis = last_notified_at or first_seen
    if not basis:
        return False
    try:
        dt = datetime.fromisoformat(str(basis).replace("Z", "+00:00"))
    except ValueError:
        return False
    age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    return age_h >= REMIND_AFTER_HOURS


async def evaluate(
    condition_key: str,
    host: str,
    is_alarm: bool,
    is_clear: bool,
    render: Callable[[str], dict],
) -> dict:
    """CORE generalized evaluator — one `alerts` row per (condition_key,
    host), same shape as every evaluator in thermal_watch.py. `is_alarm` /
    `is_clear` are ALREADY hysteresis-resolved by the caller (each condition's
    own dead band against the raw probe value); when neither is true the
    state holds whatever it was last tick (thermal_watch's dead-zone pattern,
    generalized). Never raises — a bad probe read degrades to a no-op tick for
    that condition, not a wedged scheduler."""
    row = notify_store.get_alert_by_condition(condition_key, host)
    if row is None:
        # WATERMARK-FROM-NOW: baseline the CURRENT state as already-seen, so a
        # pre-existing condition (delta backup_stale, echo
        # service_down — both CRIT right now) never fires the instant this
        # watcher starts running. Only a state that CHANGES after this seed
        # can ever fire.
        seed_state = "alarm" if is_alarm else "clear"
        notify_store.seed_alert(condition_key, host, seed_state)
        log.info("health_watch: seeded %s/%s watermark at state=%s",
                  condition_key, host, seed_state)
        return {"fired": False, "reason": "seeded watermark", "state": seed_state}

    prev = row.get("last_seen") or "clear"
    state = "alarm" if is_alarm else "clear" if is_clear else prev

    fired = False
    if state == "alarm" and prev != "alarm":
        note = render("alarm")
        result = await notify({
            "source": "health_watch", "condition": condition_key, "host": host,
            "alert_id": row["id"], **note,
        })
        notify_store.mark_alert_notified(row["id"], state)
        fired = not result.get("suppressed", False)
    elif state == "alarm" and prev == "alarm":
        if _reminder_due(row.get("last_notified_at"), row.get("first_seen")):
            note = render("reminder")
            result = await notify({
                "source": "health_watch", "condition": condition_key, "host": host,
                "alert_id": row["id"], **note,
            })
            notify_store.mark_alert_notified(row["id"], state)
            fired = not result.get("suppressed", False)
    elif state == "clear" and prev == "alarm":
        note = render("recovery")
        result = await notify({
            # A DISTINCT condition string from the alarm's — see notify.py's
            # HEALTH_CONDITIONS comment: reusing condition_key here would give
            # the recovery the SAME event_key as its alarm and get swallowed
            # by the 30-min health dedup window.
            "source": "health_watch", "condition": f"{condition_key}_recovery",
            "host": host, "alert_id": row["id"], **note,
        })
        notify_store.mark_alert_resolved(row["id"])
        notify_store.update_alert_seen(row["id"], state)  # advance watermark off "alarm"
        fired = not result.get("suppressed", False)
    elif state != prev:
        notify_store.update_alert_seen(row["id"], state)

    return {"fired": fired, "state": state}


# --------------------------------------------------------------------------- #
# disk_warn / disk_critical — probe kind "disk" (charlie/delta via probe_disk,
# worker2 via probe_disk_local — both persist kind="disk", never labeled).
# --------------------------------------------------------------------------- #
def _disk_render(node_name: str, pct_str: str, condition_key: str) -> Callable[[str], dict]:
    label = "critical" if condition_key == "disk_critical" else "warning"
    emoji = "\U0001f195\U0001f4be" if condition_key == "disk_critical" else "⚠️\U0001f4be"

    def render(edge: str) -> dict:
        if edge == "recovery":
            return {"title": f"✅\U0001f4be Disk back to normal — {node_name}",
                     "body": f"{pct_str} used, back under threshold"}
        prefix = "Reminder: " if edge == "reminder" else ""
        return {"title": f"{emoji} Disk {label} — {node_name}",
                 "body": f"{prefix}{pct_str} used"}
    return render


async def _eval_disk(snap) -> list[dict]:
    results = []
    for node_name in _DISK_NODES:
        probe = _find_probe(snap, node_name, "disk")
        if probe is None or probe.value is None:
            continue
        try:
            pct = float(str(probe.value).rstrip("%"))
        except ValueError:
            continue
        for condition_key, crit_condition in (("disk_warn", False), ("disk_critical", True)):
            alarm_t = settings.disk_crit_pct if crit_condition else settings.disk_warn_pct
            clear_t = alarm_t - DISK_CLEAR_MARGIN_PCT
            is_alarm, is_clear = pct >= alarm_t, pct <= clear_t
            r = await evaluate(condition_key, node_name, is_alarm, is_clear,
                                _disk_render(node_name, probe.value, condition_key))
            results.append({"condition": condition_key, "host": node_name, **r})
    return results


# --------------------------------------------------------------------------- #
# backup_stale — probe kind "backup" (delta only, via probe_backup_freshness).
# Single condition in the catalog (no separate backup_critical), so it alarms
# at the WARN threshold, matching disk_warn's simpler cousin.
# --------------------------------------------------------------------------- #
def _backup_render(node_name: str, value_str: str) -> Callable[[str], dict]:
    def render(edge: str) -> dict:
        if edge == "recovery":
            return {"title": f"✅\U0001f4e6 Backup fresh again — {node_name}",
                     "body": f"last run {value_str}"}
        prefix = "Reminder: " if edge == "reminder" else ""
        return {"title": f"\U0001f4e6⚠️ Backup stale — {node_name}",
                 "body": f"{prefix}last successful run {value_str}"}
    return render


async def _eval_backup(snap) -> list[dict]:
    results = []
    for node_name in _BACKUP_NODES:
        probe = _find_probe(snap, node_name, "backup")
        if probe is None or probe.value is None:
            continue
        try:
            age_h = float(str(probe.value).split("h", 1)[0])
        except ValueError:
            continue
        warn_h = settings.backup_stale_warn_hours
        is_alarm = age_h >= warn_h
        is_clear = age_h <= (warn_h - BACKUP_CLEAR_MARGIN_HOURS)
        r = await evaluate("backup_stale", node_name, is_alarm, is_clear,
                            _backup_render(node_name, probe.value))
        results.append({"condition": "backup_stale", "host": node_name, **r})
    return results


# --------------------------------------------------------------------------- #
# service_down — keyed off `tailscale_ping` CRIT (recon's "cleanest single
# signal" recommendation: reachability, live on charlie/delta/echo
# today via echo's tailnet-unreachable CRIT). worker2 has no
# tailscale_ping probe (a box pinging itself is meaningless — config.py) so it
# is intentionally excluded.
# --------------------------------------------------------------------------- #
def _service_render(node_name: str, value_str: str | None) -> Callable[[str], dict]:
    def render(edge: str) -> dict:
        if edge == "recovery":
            return {"title": f"✅\U0001f4e1 {node_name} reachable again",
                     "body": "tailnet ping ok"}
        prefix = "Reminder: " if edge == "reminder" else ""
        return {"title": f"\U0001f195\U0001f4e1 {node_name} unreachable",
                 "body": f"{prefix}tailnet ping: {value_str or 'unreachable'}"}
    return render


async def _eval_service_down(snap) -> list[dict]:
    results = []
    for node_name in _SERVICE_DOWN_NODES:
        kind = _label_for(node_name, "tailscale_ping")
        probe = _find_probe(snap, node_name, kind)
        if probe is None:
            continue
        is_alarm = probe.health == Health.CRIT
        is_clear = not is_alarm
        r = await evaluate("service_down", node_name, is_alarm, is_clear,
                            _service_render(node_name, probe.value))
        results.append({"condition": "service_down", "host": node_name, **r})
    return results


# --------------------------------------------------------------------------- #
# heartbeat_stale — generalizes thermal_watch._evaluate_guard_dark across
# every locally-synced heartbeat file worth watching, beyond just the thermal
# guard's own. Deliberately scoped to host-*.json + the gallery job heartbeat
# (long-job MILESTONE detection off this same file — a queue pct crossing
# 100% — is Phase 4b, out of scope here); relay-lane freshness (from-worker1/
# from-worker5 mtime) is a recon-flagged candidate not wired this pass, since
# probe_relay_lanes/probe_tower_liveness already surface it on the worker2 card
# and a third source would need its own alerts-row host key with no live CRIT
# proof case today.
# --------------------------------------------------------------------------- #
def _heartbeat_render(host: str, age_s: float | None) -> Callable[[str], dict]:
    def render(edge: str) -> dict:
        if edge == "recovery":
            return {"title": f"✅\U0001f493 {host} heartbeat fresh again",
                     "body": "resumed updating"}
        prefix = "Reminder: " if edge == "reminder" else ""
        age_txt = f"{int(age_s)}s stale" if age_s is not None else "missing"
        return {"title": f"\U0001f494 {host} heartbeat stale",
                 "body": f"{prefix}{age_txt}"}
    return render


async def _eval_heartbeat_stale(snap) -> list[dict]:
    results = []
    sources = (
        ("charlie", settings.heartbeats_dir / "host-charlie.json", HOST_HEARTBEAT_STALE_SECONDS),
        ("delta", settings.heartbeats_dir / "host-delta.json", HOST_HEARTBEAT_STALE_SECONDS),
        ("gallery-library-scan", settings.heartbeats_dir / "gallery-library-scan.json",
         GALLERY_HEARTBEAT_STALE_SECONDS),
    )
    for host, path, threshold_s in sources:
        data = _read_json(path)
        if host == "gallery-library-scan" and is_settled_terminal(data):
            # A settled-terminal Gallery record (state done/failed, ended_at
            # stamped, queues present) is EXPECTED to stop updating every
            # tick — the producer itself throttles to a 15-min discovery
            # poll once terminal (see jobs.gallery.run_gallery_heartbeat). Age
            # alone can't distinguish that from a dead producer, so exempt
            # this exact settled shape from the age threshold; anything else
            # (active, missing, malformed, exception-fallback failed with no
            # queues list) still falls through to the normal age check.
            age_s = _heartbeat_age_seconds(data)
            is_alarm = False
            is_clear = True
        else:
            age_s = _heartbeat_age_seconds(data)
            is_alarm = data is None or age_s is None or age_s > threshold_s
            is_clear = not is_alarm
        r = await evaluate("heartbeat_stale", host, is_alarm, is_clear,
                            _heartbeat_render(host, age_s))
        results.append({"condition": "heartbeat_stale", "host": host, **r})
    return results


async def scan_once() -> dict:
    """One 60s sweep tick, driven off the LATEST status.json snapshot (never
    re-probes). Never raises — a bad snapshot or a notify() hiccup degrades to
    an empty result for that slice, not a wedged scheduler (mirrors
    thermal_watch.scan_once)."""
    snap = read_snapshot()
    if snap is None:
        return {"reason": "no snapshot yet"}

    out: dict[str, list[dict]] = {}
    for key, fn in (
        ("disk", _eval_disk),
        ("backup", _eval_backup),
        ("service_down", _eval_service_down),
        ("heartbeat_stale", _eval_heartbeat_stale),
    ):
        try:
            out[key] = await fn(snap)
        except Exception:
            log.exception("health_watch: %s evaluation failed", key)
            out[key] = []
    return out
