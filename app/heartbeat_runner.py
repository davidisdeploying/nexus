"""
Single-flight wrapper around the expensive fleet heartbeat sweep.

The scheduler already prevents overlapping scheduled instances, but manual
POST /api/run/heartbeat and startup kicks used to call run_heartbeat directly.
This module makes every entrypoint share one in-process lock: one sweep at a
time, with request callers receiving the latest cached snapshot when a sweep is
already in flight.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .semantic_index_watch import probe_maint_once
from .jobs.heartbeat import run_heartbeat
from .models import StatusSnapshot
from .store import read_snapshot

log = logging.getLogger("nexus.heartbeat_runner")


@dataclass
class HeartbeatRunResult:
    snap: StatusSnapshot | None
    ran: bool
    already_running: bool = False
    started_at: str | None = None


class HeartbeatRunner:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current_started_at: str | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def current_started_at(self) -> str | None:
        return self._current_started_at

    async def run(self, *, if_idle: bool = False) -> HeartbeatRunResult:
        if if_idle and self._lock.locked():
            return HeartbeatRunResult(
                snap=read_snapshot(),
                ran=False,
                already_running=True,
                started_at=self._current_started_at,
            )

        async with self._lock:
            self._current_started_at = datetime.now(timezone.utc).isoformat()
            try:
                snap = await run_heartbeat()
                # Compendium loopback liveness belongs to the same five-minute
                # single-flight cadence as the fleet sweep. Keeping it here
                # prevents a second scheduler job from drifting or overlapping
                # while preserving the dashboard's no-network-on-render rule.
                await probe_maint_once()
                return HeartbeatRunResult(
                    snap=snap,
                    ran=True,
                    already_running=False,
                    started_at=self._current_started_at,
                )
            finally:
                self._current_started_at = None

    def create_startup_task(self) -> asyncio.Task:
        task = asyncio.create_task(self.run(if_idle=True), name="startup-heartbeat")

        def _log_done(done: asyncio.Task) -> None:
            try:
                result = done.result()
                if result.ran and result.snap is not None:
                    log.info(
                        "startup heartbeat complete: overall=%s in %dms",
                        result.snap.overall.value,
                        result.snap.duration_ms,
                    )
            except Exception:
                log.exception("startup heartbeat failed")

        task.add_done_callback(_log_done)
        return task


heartbeat_runner = HeartbeatRunner()


async def run_scheduled_heartbeat() -> StatusSnapshot | None:
    result = await heartbeat_runner.run(if_idle=True)
    if result.already_running:
        log.info("scheduled heartbeat skipped: sweep already running since %s",
                 result.started_at)
    return result.snap
