"""
The scheduler layer — the part that makes this an OS surface and not a status
page. Jobs are registered here with stable ids so the dashboard can list them,
show last-run, and (later) trigger or reschedule them from the UI.

Today: one job, `heartbeat`. The taxonomy this leaves room for, borrowed from
the self-hosted agentic-OS dashboards:
  heartbeat      -> this file's job (fleet sweep + dead-man's switch)   [now]
  standup        -> nightly vault digest (new/changed notes, stale TODOs) [later]
  audit          -> weekly broken-wikilink / hygiene sweep via relay      [later]
  consolidation  -> wiki_build.py (already exists as its own timer)       [existing]
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    JobExecutionEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import notify_store
from .config import settings
from .conformance_watch import scan_once as conformance_watch_scan_once
from .deadman import DEADMAN_PING_INTERVAL_SECONDS, deadman_ping_once
from .events import run_event_retention
from .jobs.gallery import run_gallery_heartbeat
from .health_watch import scan_once as health_watch_scan_once
from .heartbeat_runner import run_scheduled_heartbeat
from .milestone_watch import scan_once as milestone_watch_scan_once
from .model_usage_watch import scan_once as model_usage_watch_scan_once
from .run_watcher import scan_once as run_watcher_scan_once
from .self_test import (
    SELF_TEST_DAY_OF_WEEK,
    SELF_TEST_HOUR,
    SELF_TEST_MINUTE,
    SELF_TEST_TIMEZONE,
    run_self_test,
)
from .thermal_watch import scan_once as thermal_watch_scan_once

log = logging.getLogger("nexus.scheduler")

scheduler = AsyncIOScheduler(timezone="UTC")

_RECEIPT_LISTENER_MASK = EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
_receipt_listener_registered = False


def _receipt_outcome(event: JobExecutionEvent) -> tuple[str, str]:
    """Classify one job-completion event into the (outcome, detail) pair the
    watchdog projection layer overlays onto the static registry. An
    unexceptional job is `ok` unless its own structured return explicitly
    contains `ok=False` -- "no edge fired this tick" is a normal, healthy
    return for an edge-triggered watcher, not evidence of failure."""
    if event.code == EVENT_JOB_MISSED:
        return "missed", "APScheduler misfire: job execution was missed."
    if event.code == EVENT_JOB_ERROR:
        exc = event.exception
        # Exception messages are intentionally excluded: libraries commonly
        # include URLs, command arguments, or other secret-bearing context in
        # them, and this detail is persisted durably.
        detail = f"{type(exc).__name__}: job raised" if exc is not None else "Job raised an exception."
        return "error", detail
    retval = event.retval
    if isinstance(retval, dict) and retval.get("ok") is False:
        detail = retval.get("detail") or retval.get("error") or "Job reported ok=False."
        return "error", str(detail)
    if isinstance(retval, dict):
        return "ok", ", ".join(f"{k}={v}" for k, v in list(retval.items())[:6])
    return "ok", "Job completed without a structured failure signal."


def _on_job_event(event: JobExecutionEvent) -> None:
    """APScheduler listener: persists one execution receipt per job id. Must
    never raise or block the scheduler loop -- a receipt-persistence failure
    degrades watchdog evidence, it must not degrade the scheduler itself."""
    try:
        outcome, detail = _receipt_outcome(event)
        scheduled_at = (
            event.scheduled_run_time.astimezone(timezone.utc).isoformat()
            if getattr(event, "scheduled_run_time", None) else None
        )
        notify_store.record_scheduler_receipt(
            event.job_id, outcome=outcome,
            completed_at=datetime.now(timezone.utc).isoformat(),
            scheduled_at=scheduled_at, detail=detail,
        )
    except Exception:  # noqa: BLE001 — a broken receipt write must not crash the scheduler
        log.exception("scheduler: failed to persist execution receipt for job_id=%s",
                      getattr(event, "job_id", "?"))


def _register_receipt_listener() -> None:
    """Idempotent: register_jobs() may run more than once in a process (e.g.
    a startup path re-entering after a test fixture), and add_listener has no
    built-in dedup -- a module-level guard is what keeps this a single
    listener rather than a growing stack of duplicate receipt writers."""
    global _receipt_listener_registered
    if _receipt_listener_registered:
        return
    scheduler.add_listener(_on_job_event, _RECEIPT_LISTENER_MASK)
    _receipt_listener_registered = True


def register_jobs() -> None:
    scheduler.add_job(
        run_scheduled_heartbeat,
        trigger=IntervalTrigger(seconds=settings.heartbeat_interval_seconds),
        id="heartbeat",
        name="Fleet heartbeat",
        max_instances=1,       # never overlap a slow sweep with the next tick
        coalesce=True,         # if we fell behind, run once, not N times
        misfire_grace_time=60,
    )
    scheduler.add_job(
        run_event_retention,
        trigger=CronTrigger(hour=8, minute=35, timezone="UTC"),
        id="events-retention",
        name="Live activity retention",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_gallery_heartbeat,
        trigger=IntervalTrigger(seconds=60),
        id="gallery-library-scan",
        name="Gallery library scan",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    # Phase 3 (panel-notifications-design.md §D.3): scans from-{seat}/runs/ for
    # terminal states and calls notify(). The watermark/seen-set persist in
    # events.db (notify_store.run_watch_*), so this tick is safe to run on
    # every startup — it never re-baselines or backfills after the first ever
    # call. See app/run_watcher.py.
    scheduler.add_job(
        run_watcher_scan_once,
        trigger=IntervalTrigger(seconds=120),
        id="run-watcher",
        name="Relay run-outcome watcher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    # Targeted Phase-4 slice (FLEET-WORKER2-BUILD-20260710-thermal-halt-alarm):
    # charlie's thermal-guard hard-halts gallery at 110C and leaves it down
    # silently. Watches the already-synced thermal-guard-charlie.json heartbeat
    # (no ssh, no charlie mutation) and routes ONLY the thermal-halt/recovery
    # (+approaching/guard-dark) conditions through notify(). Watermark/edge
    # state persists in events.db (notify_store.alerts). See app/thermal_watch.py.
    scheduler.add_job(
        thermal_watch_scan_once,
        trigger=IntervalTrigger(seconds=60),
        id="thermal-watch",
        name="Thermal-halt alarm watcher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    # Phase 4a (FLEET-WORKER2-BUILD-20260710-panel-notify-phase4a): generalizes
    # thermal_watch's alerts-row skeleton across the rest of notify.py's
    # HEALTH_CONDITIONS — disk_warn/disk_critical/backup_stale/service_down/
    # heartbeat_stale. Reads the LATEST status.json snapshot only; never
    # re-probes. Watermark/edge/reminder state persists in events.db
    # (notify_store.alerts), same table thermal_watch uses. See app/health_watch.py.
    scheduler.add_job(
        health_watch_scan_once,
        trigger=IntervalTrigger(seconds=60),
        id="health-watch",
        name="Generic health-condition alarm watcher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    # Phase 4b (FLEET-WORKER2-BUILD-20260710-panel-notify-phase4b): one-shot
    # per-queue + whole-scan MILESTONE notifications off the same
    # gallery-library-scan.json heartbeat run_gallery_heartbeat already writes
    # (no re-probe). Watermark/fired-flag state persists in events.db
    # (notify_store.alerts, same table thermal_watch/health_watch use). See
    # app/milestone_watch.py.
    scheduler.add_job(
        milestone_watch_scan_once,
        trigger=IntervalTrigger(seconds=60),
        id="milestone-watch",
        name="Gallery scan milestone watcher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        model_usage_watch_scan_once,
        trigger=IntervalTrigger(seconds=60),
        id="model-usage-watch",
        name="Model quota event notifier",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    # CONFORMANCE-2 part D (FLEET-WORKER2-BUILD-20260730-conformance2-signal-
    # durability): independent, cache-only watcher over the fleet conformance
    # collector's OWN cache -- no SSH/systemd/file probes of its own. Fires
    # notify() once per check transition (ok<->non-ok) and once per cache
    # stale/unavailable<->fresh edge; watermark state persists in events.db
    # (notify_store.alerts). See app/conformance_watch.py.
    scheduler.add_job(
        conformance_watch_scan_once,
        trigger=IntervalTrigger(seconds=300),
        id="conformance-watch",
        name="Fleet conformance transition watcher",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    # Phase 6a (FLEET-WORKER2-BUILD-20260710-panel-notify-phase6a): weekly
    # dual-transport canary (app/self_test.py) — if iOS silently drops the PWA
    # subscription (Apple returns success for a dead sub) or ntfy stops
    # delivering, THIS is what surfaces it, since nothing in send_push's own
    # error handling can ever catch a false-success. CronTrigger, not
    # IntervalTrigger, so it lands at a stable wall-clock time weekly rather
    # than drifting off process-start; explicit America/Chicago override so
    # "Sunday 09:00" tracks David's local clock through DST regardless of the
    # scheduler's own UTC default.
    scheduler.add_job(
        run_self_test,
        trigger=CronTrigger(
            day_of_week=SELF_TEST_DAY_OF_WEEK, hour=SELF_TEST_HOUR,
            minute=SELF_TEST_MINUTE, timezone=SELF_TEST_TIMEZONE,
        ),
        id="nexus-selftest",
        name="Weekly notification self-test",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Phase 6b (FLEET-WORKER2-BUILD-20260710-panel-notify-phase6b): the EXTERNAL
    # dead-man's switch — pings an off-box healthchecks.io-style URL every tick
    # so a Nexus crash, worker2 outage, tunnel death, or whole-site power/network
    # outage is caught by the external service's own grace-window alert rather
    # than by anything running on worker2 (which would be exactly what died).
    # Distinct from app/jobs/heartbeat.py's existing settings.heartbeat_ping_url
    # push, which is keyed to FLEET health (crit vs not); this one is keyed to
    # the Nexus PROCESS's own health (app.deadman._self_health) and is inert
    # until secrets/deadman_ping_url.txt is provisioned. See app/deadman.py.
    scheduler.add_job(
        deadman_ping_once,
        trigger=IntervalTrigger(seconds=DEADMAN_PING_INTERVAL_SECONDS),
        id="deadman-ping",
        name="External dead-man's switch pinger",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    _register_receipt_listener()
    log.info("registered %d job(s)", len(scheduler.get_jobs()))


def jobs_summary() -> list[dict]:
    """For the /api/scheduler route and the dashboard's schedule panel."""
    return [
        {
            "id": j.id,
            "name": j.name,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
        }
        for j in scheduler.get_jobs()
    ]
