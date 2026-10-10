"""Nexus-wide, cache-only system status registry.

The dashboard's fleet snapshot is only one source of truth.  This module folds
every status-producing Nexus area into one small contract without launching
SSH, subprocess, or network probes on a request.  Collectors are isolated: a
broken or missing source becomes an explicit WARN record instead of taking the
status page down or silently reading as healthy.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from . import conformance, control_plane, notify_store
from .activity import read_cache as read_activity_cache
from .semantic_index_watch import read_status as read_semantic_index_status
from .config import JOB_NONJOB_KINDS, settings
from .scheduler import jobs_summary
from .store import read_snapshot
from .watchdogs_projection import get_projected_registry


STATUS_RANK = {"ok": 0, "warn": 1, "critical": 2}
EXPECTED_PROVIDERS = {"claude", "codex", "gemini"}
EXPECTED_SCHEDULER_JOBS = {
    "heartbeat",
    "events-retention",
    "gallery-library-scan",
    "run-watcher",
    "thermal-watch",
    "health-watch",
    "milestone-watch",
    "model-usage-watch",
    "conformance-watch",
    "nexus-selftest",
    "deadman-ping",
}
MODULE_ORDER = (
    "nexus-runtime",
    "fleet-health",
    "fleet-jobs",
    "fleet-workers",
    "conformance",
    "control-plane",
    "activity",
    "model-usage",
    "scheduler",
    "notifications",
    "watchdogs",
    "semantic-index",
    "cli-control",
)

_CACHE_SECONDS = 15.0
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_payload: dict[str, Any] | None = None


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _utc(value)
    return max(0.0, (now - parsed).total_seconds()) if parsed else None


def _bounded(value: Any, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _health(value: Any) -> str:
    raw = getattr(value, "value", value)
    return {"crit": "critical", "critical": "critical", "error": "critical",
            "warn": "warn", "warning": "warn", "unknown": "warn",
            "amber": "warn", "yellow": "warn"}.get(str(raw).lower(), "ok")


def _worst(*values: str) -> str:
    return max(values or ("ok",), key=lambda value: STATUS_RANK.get(value, 1))


def _check(label: str, status: str = "ok", value: Any = "",
           detail: Any = "", href: str | None = None) -> dict[str, Any]:
    return {
        "label": _bounded(label, 100),
        "status": status if status in STATUS_RANK else "warn",
        "value": _bounded(value, 120),
        "detail": _bounded(detail),
        "href": href,
    }


def _module(module_id: str, title: str, group: str, status: str,
            summary: str, href: str, *, detail: str = "",
            updated_at: Any = None,
            checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    stamp = _utc(updated_at)
    return {
        "id": module_id,
        "title": title,
        "group": group,
        "status": status if status in STATUS_RANK else "warn",
        "summary": _bounded(summary, 180),
        "detail": _bounded(detail),
        "href": href,
        "updated_at": stamp.isoformat().replace("+00:00", "Z") if stamp else None,
        "checks": checks or [],
    }


def _safe(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"value": fn(), "error": None}
    except Exception as exc:  # one collector must never take down the registry
        return {"value": None, "error": type(exc).__name__}


def _read_conformance() -> dict[str, Any]:
    data, error = conformance.read_cache()
    return conformance.project_conformance(
        data, error=error, history=conformance.read_history()
    )


def _read_control_plane() -> dict[str, Any]:
    data, error = control_plane.read_cache()
    return control_plane.project(data, error)


def _read_activity() -> dict[str, Any]:
    data, error = read_activity_cache()
    return {"data": data, "error": error}


def _read_notifications() -> dict[str, Any]:
    # Deliberately reduce subscriptions to non-secret health fields.  Endpoints,
    # auth material, and P-256 keys never enter this status contract.
    subscriptions = notify_store.list_active_subscriptions()
    rows = notify_store.list_notifications(250)
    selftest = next(
        (row for row in rows if row.get("channel") == "nexus-selftest"), None
    )
    return {
        "subscriptions": [
            {
                "device_label": row.get("device_label") or "device",
                "consecutive_failures": int(row.get("consecutive_failures") or 0),
                "last_send_at": row.get("last_send_at"),
                "last_confirm_at": row.get("last_confirm_at"),
            }
            for row in subscriptions
        ],
        "selftest": ({
            "created_at": selftest.get("created_at"),
            "sent_pwa": bool(selftest.get("sent_pwa")),
            "sent_ntfy": bool(selftest.get("sent_ntfy")),
        } if selftest else None),
    }


def _read_control_sessions() -> dict[str, Any]:
    # Lazy import avoids a module cycle: gemini_remote imports the shared
    # shell context, which imports this registry.  This function runs only after
    # app startup, when the managers are fully initialized.
    from .gemini_remote import HOSTS, PROVIDERS, terminal_manager, worker_manager

    strategies = []
    for session in terminal_manager.sessions.values():
        public = session.public_status()
        strategies.append({
            key: public.get(key)
            for key in ("host", "provider", "running", "started_at", "exited_at", "exit_code")
        })
    workers = []
    for job in worker_manager.jobs.values():
        workers.append({
            "job_id": job.job_id,
            "host": job.host,
            "provider": job.provider,
            "state": job.state,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
            "exit_code": job.exit_code,
        })
    return {
        "host_count": len(HOSTS),
        "provider_count": len(PROVIDERS),
        "strategies": strategies,
        "workers": workers,
    }


def read_sources() -> dict[str, dict[str, Any]]:
    """Read only local/cache-backed sources; never trigger a fresh probe."""
    return {
        "snapshot": _safe(read_snapshot),
        "conformance": _safe(_read_conformance),
        "control_plane": _safe(_read_control_plane),
        "activity": _safe(_read_activity),
        "scheduler": _safe(jobs_summary),
        "notifications": _safe(_read_notifications),
        "watchdogs": _safe(get_projected_registry),
        "semantic_index": _safe(read_semantic_index_status),
        "cli_control": _safe(_read_control_sessions),
    }


def _source(sources: dict[str, dict[str, Any]], key: str) -> tuple[Any, str | None]:
    wrapped = sources.get(key) or {}
    return wrapped.get("value"), wrapped.get("error") or None


def build_system_status(sources: dict[str, dict[str, Any]],
                        now: datetime | None = None) -> dict[str, Any]:
    """Pure roll-up from dependency-injectable source values."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    modules: list[dict[str, Any]] = []

    snap, snap_error = _source(sources, "snapshot")
    if snap_error or snap is None:
        modules.append(_module(
            "nexus-runtime", "Nexus heartbeat", "Core", "critical",
            "No readable fleet heartbeat", "/operations?tab=health",
            detail=f"Cached heartbeat unavailable ({snap_error or 'missing snapshot'}).",
        ))
    else:
        age = _age_seconds(snap.generated_at, now)
        critical_after = settings.heartbeat_interval_seconds * 2
        warn_after = settings.heartbeat_interval_seconds * 1.35
        state = "critical" if age is None or age >= critical_after else (
            "warn" if age >= warn_after else "ok"
        )
        summary = "Heartbeat timestamp is invalid" if age is None else (
            f"Heartbeat is {int(age)}s old · {len(snap.nodes)} nodes"
        )
        modules.append(_module(
            "nexus-runtime", "Nexus heartbeat", "Core", state, summary,
            "/operations?tab=health", updated_at=snap.generated_at,
            detail="The shared snapshot and scheduler freshness gate used across Nexus.",
        ))

    if snap_error or snap is None:
        modules.append(_module(
            "fleet-health", "Fleet health", "Fleet", "critical",
            "Fleet probe results are unavailable", "/operations?tab=health",
        ))
    else:
        fleet_checks = []
        for node in snap.nodes:
            issues = [probe for probe in node.probes if _health(probe.health) != "ok"]
            detail = "; ".join(
                f"{probe.kind}: {probe.detail or probe.value or probe.health.value}"
                for probe in issues[:4]
            )
            fleet_checks.append(_check(
                node.display_name or node.name, _health(node.health),
                f"{len(node.probes)} probes", detail,
                f"/#node-{node.name.lower().replace(' ', '-')}",
            ))
        modules.append(_module(
            "fleet-health", "Fleet health", "Fleet", _health(snap.overall),
            f"{len(snap.nodes)} nodes · overall {snap.overall.value.upper()}",
            "/operations?tab=health", updated_at=snap.generated_at,
            checks=fleet_checks,
        ))

    jobs = [] if snap is None else list((snap.work or {}).get("jobs") or [])
    jobs = [job for job in jobs if (job.get("kind") or job.get("type")) not in JOB_NONJOB_KINDS]
    job_checks = []
    for job in jobs:
        raw = str(job.get("state") or "unknown").lower()
        state = "critical" if raw == "failed" else "warn" if raw in {"stalled", "unknown"} else "ok"
        job_checks.append(_check(
            job.get("label") or job.get("job") or "job", state, raw,
            job.get("detail") or job.get("phase") or "", "/activity?tab=jobs",
        ))
    job_state = _worst(*(row["status"] for row in job_checks)) if job_checks else "ok"
    modules.append(_module(
        "fleet-jobs", "Fleet jobs", "Fleet", job_state,
        f"{len(jobs)} heartbeat-backed jobs" if jobs else "No active job records",
        "/activity?tab=jobs", updated_at=getattr(snap, "generated_at", None),
        checks=job_checks,
    ))

    seats = [] if snap is None else list(((snap.seats or {}).get("seats") or []))
    seats = [seat for seat in seats if not seat.get("usage_card")]
    worker_checks = []
    for seat in seats:
        raw = str(seat.get("state") or "unknown").lower()
        state = "critical" if raw == "died" else "warn" if raw not in {
            "idle", "busy", "running", "done", "succeeded"
        } else "ok"
        worker_checks.append(_check(
            seat.get("label") or seat.get("seat") or "worker", state,
            seat.get("badge") or raw, seat.get("primary") or "", "/activity?tab=workers",
        ))
    worker_state = _worst(*(row["status"] for row in worker_checks)) if worker_checks else (
        "warn" if snap is None else "ok"
    )
    modules.append(_module(
        "fleet-workers", "Worker seats", "Fleet", worker_state,
        f"{len(seats)} worker seats tracked" if seats else "Worker seat state unavailable",
        "/activity?tab=workers", updated_at=getattr(snap, "generated_at", None),
        checks=worker_checks,
    ))

    conform, conform_error = _source(sources, "conformance")
    if conform_error or not conform or not conform.get("available"):
        modules.append(_module(
            "conformance", "Fleet conformance", "Governance", "warn",
            "Conformance evidence is unavailable", "/operations?tab=conformance",
            detail=f"Collector unavailable ({conform_error or (conform or {}).get('error') or 'no cache'}).",
        ))
    else:
        conform_checks = []
        for category in conform.get("categories", []):
            for row in category.get("checks", []):
                if str(row.get("state")) == "ok":
                    continue
                conform_checks.append(_check(
                    row.get("human_title") or row.get("title") or row.get("id"),
                    _health(row.get("state")), row.get("actual") or row.get("state"),
                    row.get("expected") or row.get("failure_class") or "",
                    row.get("navigate") or "/operations?tab=conformance",
                ))
        state = _health(conform.get("overall"))
        if conform.get("is_stale"):
            state = _worst(state, "warn")
        modules.append(_module(
            "conformance", "Fleet conformance", "Governance", state,
            conform.get("outcome_headline") or "Conformance cache loaded",
            "/operations?tab=conformance", updated_at=conform.get("generated_at"),
            detail="Declared contracts, SSH paths, required services, files, receipts, mirrors, and governance checks.",
            checks=conform_checks,
        ))

    indexes, indexes_error = _source(sources, "control_plane")
    if indexes_error or not indexes or not indexes.get("available"):
        modules.append(_module(
            "control-plane", "Control-plane indexes", "Governance", "warn",
            "Index evidence is unavailable", "/operations?tab=indexes",
            detail=f"Collector unavailable ({indexes_error or (indexes or {}).get('error') or 'no cache'}).",
        ))
    else:
        index_checks = [
            _check(row.get("title") or row.get("id") or "index",
                   _health(row.get("status")), row.get("summary") or row.get("status"),
                   row.get("file") or "", f"/operations?tab=indexes#{row.get('id', '')}")
            for row in indexes.get("cards", [])
        ]
        state = _health(indexes.get("overall"))
        if indexes.get("is_stale"):
            state = _worst(state, "warn")
        modules.append(_module(
            "control-plane", "Control-plane indexes", "Governance", state,
            f"{len(index_checks)} canonical indexes tracked",
            "/operations?tab=indexes", updated_at=indexes.get("generated_at"),
            checks=index_checks,
        ))

    activity, activity_source_error = _source(sources, "activity")
    activity_data = (activity or {}).get("data") if isinstance(activity, dict) else None
    activity_error = activity_source_error or ((activity or {}).get("error") if isinstance(activity, dict) else None)
    if activity_error or not activity_data:
        modules.append(_module(
            "activity", "Activity analytics", "Analytics", "warn",
            "Activity cache is unavailable", "/activity",
            detail=f"Collector unavailable ({activity_error or 'no cache'}).",
        ))
    else:
        age = _age_seconds(activity_data.get("generated_at"), now)
        host_errors = activity_data.get("host_errors") or {}
        state = "warn" if age is None or age > 1800 or host_errors else "ok"
        checks = [
            _check(str(host), "warn", "collector error", error, "/activity")
            for host, error in list(host_errors.items())[:20]
        ]
        modules.append(_module(
            "activity", "Activity analytics", "Analytics", state,
            f"Cache age {int(age)}s" if age is not None else "Cache timestamp is invalid",
            "/activity", updated_at=activity_data.get("generated_at"),
            detail="Commit, push, session, and normalized assistant-turn analytics.",
            checks=checks,
        ))

    usage, usage_error = _source(sources, "model_usage")
    if not usage and snap is not None:
        usage_tile = next(
            (seat for seat in ((snap.seats or {}).get("seats") or []) if seat.get("usage_card")),
            None,
        )
        if usage_tile:
            latest = []
            for row in usage_tile.get("provider_usage") or []:
                updated_ms = row.get("updated_ms")
                latest.append({
                    "provider": row.get("provider"),
                    "captured_at": (float(updated_ms) / 1000 if isinstance(updated_ms, (int, float)) else None),
                    "ok": bool(updated_ms and row.get("source")),
                    "source": row.get("source"),
                    "error_class": None,
                })
            usage = {
                "generated_at": getattr(snap, "generated_at", None),
                "latest": latest,
            }
    if usage_error or not usage:
        modules.append(_module(
            "model-usage", "Model usage", "Analytics", "warn",
            "Quota history is unavailable", "/activity?tab=models",
            detail=f"Collector unavailable ({usage_error or 'no history'}).",
        ))
    else:
        latest = usage.get("latest") or []
        by_provider = {str(row.get("provider")): row for row in latest}
        usage_checks = []
        for provider in sorted(EXPECTED_PROVIDERS):
            row = by_provider.get(provider)
            age = _age_seconds((row or {}).get("captured_at"), now)
            status = "warn" if not row or not row.get("ok") or age is None or age > 1200 else "ok"
            usage_checks.append(_check(
                provider.title(), status,
                "unavailable" if not row or not row.get("ok") else f"updated {int(age or 0)}s ago",
                (row or {}).get("error_class") or (row or {}).get("source") or "",
                f"/activity?tab=models",
            ))
        routing_candidates = []
        for seat in seats:
            if seat.get("usage_card"):
                routing_candidates = ((seat.get("routing") or {}).get("candidates") or [])
        # The usage card is excluded from `seats` above, so read it from the raw list.
        for seat in ([] if snap is None else ((snap.seats or {}).get("seats") or [])):
            if seat.get("usage_card"):
                routing_candidates = ((seat.get("routing") or {}).get("candidates") or [])
                break
        route_states = [str(row.get("state") or "RED").upper() for row in routing_candidates]
        all_routes_red = bool(route_states) and all(state == "RED" for state in route_states)
        route_status = "critical" if all_routes_red else (
            "warn" if any(state in {"RED", "YELLOW", "AMBER"} for state in route_states) else "ok"
        )
        for row in routing_candidates:
            route_state = str(row.get("state") or "RED").upper()
            if route_state == "GREEN":
                continue
            usage_checks.append(_check(
                f"{str(row.get('provider') or 'provider').title()} worker routing",
                "critical" if all_routes_red else "warn",
                route_state,
                f"{row.get('model') or 'model unavailable'} · {row.get('reason') or 'routing capacity reduced'}",
                "/activity?tab=models",
            ))
        state = _worst(route_status, *(row["status"] for row in usage_checks))
        modules.append(_module(
            "model-usage", "Model usage", "Analytics", state,
            f"{len(by_provider)}/{len(EXPECTED_PROVIDERS)} provider feeds current",
            "/activity?tab=models", updated_at=usage.get("generated_at"),
            detail="Quota collectors and worker-routing capacity across Claude, Codex, and Gemini.",
            checks=usage_checks,
        ))

    scheduler_rows, scheduler_error = _source(sources, "scheduler")
    if scheduler_error or scheduler_rows is None:
        modules.append(_module(
            "scheduler", "Nexus scheduler", "Core", "warn",
            "Scheduler registry is unavailable", "/api/scheduler",
            detail=f"Collector unavailable ({scheduler_error or 'no registry'}).",
        ))
    else:
        registered = {str(row.get("id")) for row in scheduler_rows}
        missing = sorted(EXPECTED_SCHEDULER_JOBS - registered)
        no_next = sorted(str(row.get("id")) for row in scheduler_rows if not row.get("next_run"))
        state = "critical" if missing else "warn" if no_next else "ok"
        checks = [
            *[_check(job, "critical", "missing", "Required APScheduler job is not registered.") for job in missing],
            *[_check(job, "warn", "paused", "Registered job has no next run.") for job in no_next],
        ]
        modules.append(_module(
            "scheduler", "Nexus scheduler", "Core", state,
            f"{len(registered)}/{len(EXPECTED_SCHEDULER_JOBS)} required jobs registered",
            "/api/scheduler", checks=checks,
        ))

    notifications, notification_error = _source(sources, "notifications")
    if notification_error or not notifications:
        modules.append(_module(
            "notifications", "Notification delivery", "Delivery", "warn",
            "Notification health is unavailable", "/notifications?tab=preferences",
            detail=f"Collector unavailable ({notification_error or 'no state'}).",
        ))
    else:
        subscriptions = notifications.get("subscriptions") or []
        selftest = notifications.get("selftest")
        checks = []
        state = "ok"
        for row in subscriptions:
            failures = int(row.get("consecutive_failures") or 0)
            sub_state = "critical" if failures >= 10 else "warn" if failures >= 3 else "ok"
            checks.append(_check(
                row.get("device_label") or "device", sub_state,
                f"{failures} consecutive send failures", "Active PWA push subscription.",
                "/notifications?tab=preferences",
            ))
            state = _worst(state, sub_state)
        if not subscriptions:
            state = _worst(state, "warn")
            checks.append(_check("PWA push", "warn", "no active devices",
                                 "No active push subscription is registered."))
        if not selftest:
            state = _worst(state, "warn")
            checks.append(_check("Weekly transport canary", "warn", "no receipt",
                                 "No self-test result has been recorded."))
            updated_at = None
        else:
            updated_at = selftest.get("created_at")
            test_age = _age_seconds(updated_at, now)
            recent_pwa = any(
                (send_age := _age_seconds(row.get("last_send_at"), now)) is not None
                and test_age is not None
                and abs(send_age - test_age) <= 180
                for row in subscriptions
            )
            pwa_ok = bool(selftest.get("sent_pwa")) or recent_pwa
            ntfy_ok = bool(selftest.get("sent_ntfy"))
            if test_age is None or test_age > 8 * 86400:
                canary_state = "warn"
                canary_value = "stale canary"
            elif not pwa_ok and not ntfy_ok:
                canary_state = "critical"
                canary_value = "both transports failed"
            elif not pwa_ok or not ntfy_ok:
                canary_state = "warn"
                canary_value = "one transport failed"
            else:
                canary_state = "ok"
                canary_value = "PWA + ntfy passed"
            state = _worst(state, canary_state)
            checks.append(_check(
                "Weekly transport canary", canary_state, canary_value,
                f"PWA {'passed' if pwa_ok else 'failed'} · ntfy {'passed' if ntfy_ok else 'failed'}",
                "/notifications?tab=preferences",
            ))
        modules.append(_module(
            "notifications", "Notification delivery", "Delivery", state,
            f"{len(subscriptions)} active push device{'s' if len(subscriptions) != 1 else ''}",
            "/notifications?tab=preferences", updated_at=updated_at,
            checks=checks,
        ))

    watchdog_rows, watchdog_error = _source(sources, "watchdogs")
    if watchdog_error or watchdog_rows is None:
        modules.append(_module(
            "watchdogs", "Watchdog inventory", "Governance", "warn",
            "Watchdog registry is unavailable", "/operations?tab=watchdogs",
            detail=f"Collector unavailable ({watchdog_error or 'no registry'}).",
        ))
    else:
        flagged = [row for row in watchdog_rows if row.get("status") in {"stale_evidence", "orphaned"}]
        checks = [
            _check(row.get("label") or row.get("id"), "warn", row.get("status"),
                   row.get("status_detail") or "", "/operations?tab=watchdogs")
            for row in flagged
        ]
        modules.append(_module(
            "watchdogs", "Watchdog inventory", "Governance",
            "warn" if flagged else "ok",
            f"{len(watchdog_rows)} mechanisms · {len(flagged)} flagged",
            "/operations?tab=watchdogs", checks=checks,
        ))

    semantic_index, semantic_index_error = _source(sources, "semantic_index")
    if semantic_index_error or not semantic_index:
        modules.append(_module(
            "semantic-index", "Semantic index", "Modules", "warn",
            "Semantic index status is unavailable", "/",
            detail=f"Collector unavailable ({semantic_index_error or 'no receipt'}).",
        ))
    else:
        state = {"RED": "critical", "AMBER": "warn", "GREEN": "ok"}.get(
            str(semantic_index.get("health") or "").upper(), "warn"
        )
        if semantic_index.get("no_receipt"):
            summary = f"No receipt — {semantic_index.get('detail')}"
        else:
            summary = (
                f"{int(semantic_index.get('markdown_docs') or 0):,} markdown · "
                f"{int(semantic_index.get('transcript_docs') or 0):,} transcript docs · "
                f"ran {float(semantic_index.get('age_hours') or 0):.1f}h ago · "
                f"primary/fallback "
                f"{'current' if semantic_index.get('fallback_matches_primary') else 'diverged'}"
            )
            if semantic_index.get("detail"):
                summary = f"{summary} · {semantic_index['detail']}"
        modules.append(_module(
            "semantic-index", "Semantic index", "Modules", state, summary, "/",
            updated_at=(
                f"{semantic_index.get('last_run_utc')} UTC"
                if semantic_index.get("last_run_utc") else None
            ),
        ))

    control, control_error = _source(sources, "cli_control")
    if control_error or not control:
        modules.append(_module(
            "cli-control", "CLI control", "Delivery", "warn",
            "CLI control state is unavailable", "/control",
            detail=f"Collector unavailable ({control_error or 'no state'}).",
        ))
    else:
        checks = []
        cutoff = now.timestamp() - 86400
        for row in control.get("strategies") or []:
            if row.get("running") or row.get("exit_code") in {None, 0}:
                continue
            if float(row.get("exited_at") or 0) < cutoff:
                continue
            checks.append(_check(
                f"{str(row.get('provider')).title()} on {row.get('host')}", "warn",
                f"exit {row.get('exit_code')}", "Strategy terminal exited non-zero in the last 24 hours.",
                "/control",
            ))
        for row in control.get("workers") or []:
            if row.get("state") not in {"failed", "timed_out"}:
                continue
            if float(row.get("finished_at") or row.get("created_at") or 0) < cutoff:
                continue
            checks.append(_check(
                f"{str(row.get('provider')).title()} worker on {row.get('host')}", "warn",
                row.get("state"), "Control worker failed in the last 24 hours.", "/control",
            ))
        state = "warn" if checks else "ok"
        modules.append(_module(
            "cli-control", "CLI control", "Delivery", state,
            f"{control.get('provider_count', 0)} providers · {control.get('host_count', 0)} hosts",
            "/control", checks=checks,
        ))

    order = {module_id: index for index, module_id in enumerate(MODULE_ORDER)}
    modules.sort(key=lambda row: order.get(row["id"], len(order)))
    overall = _worst(*(row["status"] for row in modules))
    counts = {state: sum(row["status"] == state for row in modules) for state in STATUS_RANK}
    issue_checks = sum(
        check["status"] != "ok" for module in modules for check in module.get("checks", [])
    )
    return {
        "version": 1,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "overall": overall,
        "label": overall.upper(),
        "counts": counts,
        "module_count": len(modules),
        "issue_count": issue_checks,
        "modules": modules,
    }


def get_system_status(*, force: bool = False) -> dict[str, Any]:
    """Return a briefly cached registry so page navigation stays cheap."""
    global _cache_at, _cache_payload
    now_mono = time.monotonic()
    if not force and _cache_payload is not None and now_mono - _cache_at < _CACHE_SECONDS:
        return _cache_payload
    with _cache_lock:
        now_mono = time.monotonic()
        if not force and _cache_payload is not None and now_mono - _cache_at < _CACHE_SECONDS:
            return _cache_payload
        payload = build_system_status(read_sources())
        _cache_payload = payload
        _cache_at = now_mono
        return payload
