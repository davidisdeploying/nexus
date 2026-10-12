"""
Nexus notifications — Phase 6b external dead-man's switch (design: Option A,
external cloud check).

app/jobs/heartbeat.py already pings settings.heartbeat_ping_url at the end of
every fleet sweep, keyed to FLEET health (crit vs not) — that answers "is the
fleet OK". This module answers a narrower, more fundamental question: "is the
Nexus PROCESS ITSELF alive and ticking" — Nexus crash, worker2 down, tunnel
down, or a whole-site power/network outage all show up here as pings simply
stopping, independent of what the fleet probes say. If the scheduler dies,
this job's own ticks stop, so the external check's grace window is what
eventually pages David — no in-process detection of "the scheduler stopped"
is possible from inside the scheduler that stopped.

URL source is secrets/deadman_ping_url.txt (600, not synced), read fresh every
cycle like config.Settings.ntfy_topic — so David can drop the URL in with NO
restart. Absent/empty file -> clean no-op, never logged above debug, never
raises.
"""
from __future__ import annotations

import logging
import time

import httpx

from .config import settings
from .store import read_snapshot

log = logging.getLogger("nexus.deadman")

_TIMEOUT_SECONDS = 10

# How often the switch ticks. A config constant (mirrors self_test.py's
# module-local SELF_TEST_* constants) rather than a Settings field — this is a
# fixed cadence decision, not a per-deploy tunable.
DEADMAN_PING_INTERVAL_SECONDS = 300

DEADMAN_PING_URL_FILE = "deadman_ping_url.txt"


def _read_ping_url() -> str | None:
    """Fresh off disk every cycle (chmod 600, not synced) — same read-fresh,
    fail-to-None discipline as Settings.ntfy_topic. None if not yet
    provisioned -> the job no-ops rather than pinging an empty/guessable URL."""
    path = settings.secrets_dir / DEADMAN_PING_URL_FILE
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _self_health() -> bool:
    """Cheap in-process health gate, same signal /healthz exposes to the
    PANEL · LOOPBACK / SCHEDULER probes: the last fleet-sweep snapshot exists
    and is fresh within 2x the sweep interval. Deliberately does not re-probe
    the fleet or touch the network — this must stay cheap enough to run every
    tick regardless of what the fleet itself looks like."""
    snap = read_snapshot()
    if snap is None:
        return False
    age_s = time.time() - snap.generated_at.timestamp()
    return age_s < (settings.heartbeat_interval_seconds * 2)


async def deadman_ping_once() -> dict:
    """One tick: no-op if unprovisioned, else GET the healthy URL or the
    /fail suffix depending on the self-health gate. Never raises — a bad
    ping must not crash the scheduler, same contract as ntfy.send_ntfy.

    Returns a small non-secret structured result (no URL/endpoint) for the
    scheduler's execution-receipt listener and the watchdog projection layer:
    only an HTTP 2xx response counts as `ok`. An unprovisioned URL is a
    neutral no-op (`ok=None`, `provisioned=False`) — it must never report a
    fabricated success."""
    url = _read_ping_url()
    if not url:
        log.debug("deadman: no ping URL provisioned -> no-op")
        return {"provisioned": False, "ok": None, "detail": "no ping URL provisioned"}

    healthy = _self_health()
    target = url if healthy else url.rstrip("/") + "/fail"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.get(target)
        ok = 200 <= resp.status_code < 300
        return {
            "provisioned": True, "ok": ok, "status_code": resp.status_code,
            "healthy": healthy,
            "detail": f"status={resp.status_code} healthy={healthy}",
        }
    except Exception as e:  # noqa: BLE001 — a failed ping must not crash the scheduler
        # Exception strings from HTTP clients may embed the request URL. The
        # endpoint is a credential, so neither logs nor durable scheduler
        # receipts may retain the exception message.
        error_type = type(e).__name__
        log.warning("deadman: ping failed (healthy=%s, error_type=%s)", healthy, error_type)
        return {
            "provisioned": True, "ok": False, "healthy": healthy,
            "detail": f"{error_type}: ping failed",
        }
