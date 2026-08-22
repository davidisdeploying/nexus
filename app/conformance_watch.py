"""
Independent, cache-only conformance transition watcher (CONFORMANCE-2 part D).

Every 5 minutes this reads ONLY the local conformance cache (app.conformance.
read_cache) -- never SSH, systemd, or a fresh probe of its own; the collector
(tools/collect_conformance.py, via nexus-conformance.timer) is the sole
producer of that cache. Per-check alarms require two consecutive non-ok
collector scans, then fire once on the confirmed rising edge and once on the
matching recovery edge. A one-scan failure that recovers stays silent. Cache
staleness/unavailability remains immediate because it means the evidence
producer itself stopped. No condition repeats while its state holds, unlike
app/health_watch.py's 12h reminder re-fire.

Watermark state persists per (condition_key, host) in events.db
(notify_store.alerts), the same table health_watch.py and thermal_watch.py
use, via the same WATERMARK-FROM-NOW seed pattern: a brand-new check id (the
manifest grew) or the very first tick this watcher ever runs just baselines
silently, so no deployment/backfill notification is ever sent -- only a state
that CHANGES after the seed can fire. A restart replays nothing because the
seed/edge state lives in events.db, not in-process memory.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import notify_store
from .conformance import CACHE, STALE_THRESHOLD_SECONDS, read_cache
from .notify import notify

log = logging.getLogger("nexus.conformance_watch")

CACHE_CONDITION_KEY = "conformance_cache"
CACHE_ALERT_HOST = "fleet"
CHECK_CONDITION_KEY = "conformance_check"
CHECK_ALARM_MIN_NON_OK_SCANS = 2


def _edge_stamp(now: datetime) -> str:
    """A per-tick suffix baked into event_key so every genuine edge gets its
    own notify() dedup identity (mirrors model_usage_watch's per-event-id
    keys) -- our OWN alerts-row watermark below is what actually decides
    whether this tick fires at all; this stamp just keeps two real, distinct
    edges from ever colliding under notify()'s own event_key ledger."""
    return now.strftime("%Y%m%dT%H%M%SZ")


async def _fire_edge(
    condition_key: str,
    host: str,
    is_ok: bool,
    render_alarm: Callable[[], dict],
    render_recovery: Callable[[], dict],
) -> dict[str, Any]:
    """One (condition_key, host) alerts row: silent WATERMARK-FROM-NOW seed on
    the first-ever evaluation; otherwise fires exactly once per ok<->non-ok
    edge and stays silent for as long as the state holds -- no reminder."""
    row = notify_store.get_alert_by_condition(condition_key, host)
    if row is None:
        seed_state = "ok" if is_ok else "non_ok"
        notify_store.seed_alert(condition_key, host, seed_state)
        log.info("conformance_watch: seeded %s/%s watermark at state=%s",
                  condition_key, host, seed_state)
        return {"seeded": True, "fired": False, "state": seed_state}

    prev = row.get("last_seen") or "ok"
    state = "ok" if is_ok else "non_ok"
    if prev == "retired":
        # A deliberately retired check that is later reintroduced starts a
        # fresh baseline. It must not fabricate a recovery edge.
        notify_store.update_alert_seen(row["id"], state)
        return {"seeded": True, "fired": False, "state": state}
    if state == prev:
        return {"seeded": False, "fired": False, "state": state}

    if state == "non_ok":
        result = await notify(render_alarm())
        notify_store.mark_alert_notified(row["id"], state)
    else:
        result = await notify(render_recovery())
        notify_store.mark_alert_resolved(row["id"])
        notify_store.update_alert_seen(row["id"], state)

    return {"seeded": False, "fired": not result.get("suppressed", False), "state": state}


async def _scan_checks(cache: dict[str, Any], now: datetime) -> dict[str, int]:
    seeded = fired_alarm = fired_recovery = retired = 0
    raw_checks = cache.get("checks", [])
    if not isinstance(raw_checks, list):
        raw_checks = []

    active_ids: set[str] = set()
    for ch in raw_checks:
        if not isinstance(ch, dict):
            continue
        check_id = str(ch.get("id") or "")
        if not check_id:
            continue
        active_ids.add(check_id)
        is_ok = ch.get("state") == "ok"

        # The collector carries this counter across cache generations.  Keep
        # the persisted alert watermark at "ok" through a one-scan failure;
        # if the next collection recovers, no fabricated alarm/recovery pair
        # is emitted.  Brand-new and retired checks still reach _fire_edge so
        # their existing silent-baseline behavior is preserved.
        alert_row = notify_store.get_alert_by_condition(CHECK_CONDITION_KEY, check_id)
        if (
            not is_ok
            and alert_row is not None
            and alert_row.get("last_seen") == "ok"
            and int(ch.get("consecutive_non_ok_scans") or 0) < CHECK_ALARM_MIN_NON_OK_SCANS
        ):
            continue

        def render_alarm(check_id=check_id, ch=ch) -> dict:
            return {
                "source": "conformance_watch",
                "condition": "conformance_check_drift",
                "host": check_id,
                "event_key": f"conformance-check:{check_id}:alarm:{_edge_stamp(now)}",
                "title": f"⚠️ Fleet conformance drift — {check_id}",
                "body": (
                    f"state={ch.get('state')} expected={ch.get('expected')} "
                    f"actual={ch.get('actual')}"
                )[:200],
                "navigate": "/operations?tab=conformance",
                "emoji": "⚠️",
            }

        def render_recovery(check_id=check_id) -> dict:
            return {
                "source": "conformance_watch",
                "condition": "conformance_check_recovery",
                "host": check_id,
                "event_key": f"conformance-check:{check_id}:recovery:{_edge_stamp(now)}",
                "title": f"✅ Fleet conformance recovered — {check_id}",
                "body": "back to ok",
                "navigate": "/operations?tab=conformance",
                "emoji": "✅",
            }

        result = await _fire_edge(
            CHECK_CONDITION_KEY, check_id, is_ok, render_alarm, render_recovery
        )
        if result["seeded"]:
            seeded += 1
        elif result["fired"] and result["state"] == "non_ok":
            fired_alarm += 1
        elif result["fired"] and result["state"] == "ok":
            fired_recovery += 1

    for row in notify_store.list_alerts_by_condition(CHECK_CONDITION_KEY):
        check_id = str(row.get("host") or "")
        if check_id and check_id not in active_ids and row.get("last_seen") != "retired":
            notify_store.retire_alert(row["id"])
            retired += 1

    return {"seeded": seeded, "fired_alarm": fired_alarm,
            "fired_recovery": fired_recovery, "retired": retired}


def _cache_is_fresh(data: dict[str, Any] | None, error: str | None, now: datetime) -> bool:
    if error or not data:
        return False
    try:
        gen_dt = datetime.fromisoformat(str(data.get("generated_at")).replace("Z", "+00:00"))
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return False
    age_seconds = (now - gen_dt).total_seconds()
    return age_seconds <= STALE_THRESHOLD_SECONDS


async def _scan_cache_freshness(
    data: dict[str, Any] | None, error: str | None, now: datetime
) -> dict[str, Any]:
    is_ok = _cache_is_fresh(data, error, now)

    def render_alarm() -> dict:
        return {
            "source": "conformance_watch",
            "condition": "conformance_cache_stale",
            "host": CACHE_ALERT_HOST,
            "event_key": f"conformance-cache:alarm:{_edge_stamp(now)}",
            "title": "⚠️ Fleet conformance cache stale or unavailable",
            "body": (error or "cache age exceeded the 2100s stale threshold")[:200],
            "navigate": "/operations?tab=conformance",
            "emoji": "⚠️",
        }

    def render_recovery() -> dict:
        return {
            "source": "conformance_watch",
            "condition": "conformance_cache_recovery",
            "host": CACHE_ALERT_HOST,
            "event_key": f"conformance-cache:recovery:{_edge_stamp(now)}",
            "title": "✅ Fleet conformance cache fresh again",
            "body": "background collector resumed",
            "navigate": "/operations?tab=conformance",
            "emoji": "✅",
        }

    return await _fire_edge(
        CACHE_CONDITION_KEY, CACHE_ALERT_HOST, is_ok, render_alarm, render_recovery
    )


async def scan_once(
    cache_path: Path | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """One 5-minute tick. The ONLY read this performs is read_cache -- never
    SSH/systemd/a fresh probe of any kind. Never raises: a bad cache or a
    notify() hiccup degrades to an empty result for that slice, not a wedged
    scheduler (mirrors thermal_watch.scan_once / health_watch.scan_once)."""
    now = now or datetime.now(timezone.utc)
    try:
        data, error = read_cache(cache_path or CACHE)
    except Exception:
        log.exception("conformance_watch: read_cache failed")
        data, error = None, "conformance cache read failed"

    try:
        check_result = await _scan_checks(data or {}, now)
    except Exception:
        log.exception("conformance_watch: per-check scan failed")
        check_result = {"seeded": 0, "fired_alarm": 0, "fired_recovery": 0, "retired": 0}

    try:
        cache_result = await _scan_cache_freshness(data, error, now)
    except Exception:
        log.exception("conformance_watch: cache-freshness scan failed")
        cache_result = {"seeded": False, "fired": False, "state": "unknown"}

    return {"checks": check_result, "cache": cache_result}
