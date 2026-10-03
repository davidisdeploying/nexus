"""Rate-limited manual model-usage refresh through the systemd oneshot."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import settings


@dataclass
class ModelUsageRefreshResult:
    ran: bool
    throttled: bool = False
    already_running: bool = False
    age_seconds: int | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class ModelUsageRefreshRunner:
    """One in-process launch at a time, with a cache-age throttle."""

    def __init__(
        self,
        *,
        output: Path | None = None,
        min_interval_seconds: int = 60,
        timeout_seconds: float = 45,
    ) -> None:
        self.output = output or (
            settings.relay_root / "heartbeats" / "quota" / "model-usage.json"
        )
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self._lock = asyncio.Lock()

    def _age_seconds(self) -> int | None:
        try:
            return max(0, int(time.time() - self.output.stat().st_mtime))
        except OSError:
            return None

    async def run(self) -> ModelUsageRefreshResult:
        age = self._age_seconds()
        if age is not None and age < self.min_interval_seconds:
            return ModelUsageRefreshResult(
                ran=False, throttled=True, age_seconds=age
            )
        if self._lock.locked():
            return ModelUsageRefreshResult(
                ran=False, already_running=True, age_seconds=age
            )
        async with self._lock:
            # A timer/manual caller may have refreshed while this request waited.
            age = self._age_seconds()
            if age is not None and age < self.min_interval_seconds:
                return ModelUsageRefreshResult(
                    ran=False, throttled=True, age_seconds=age
                )
            try:
                process = await asyncio.create_subprocess_exec(
                    "systemctl",
                    "--user",
                    "start",
                    "nexus-model-usage.service",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                return ModelUsageRefreshResult(
                    ran=False,
                    age_seconds=age,
                    error=f"{type(exc).__name__}: unable to start refresh",
                )
            try:
                _, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=self.timeout_seconds
                )
            except TimeoutError:
                process.kill()
                await process.communicate()
                return ModelUsageRefreshResult(
                    ran=False, age_seconds=age, error="refresh timeout"
                )
            if process.returncode:
                detail = stderr.decode("utf-8", "replace").strip()[:240]
                return ModelUsageRefreshResult(
                    ran=False,
                    age_seconds=age,
                    error=detail or f"systemctl exit {process.returncode}",
                )
            return ModelUsageRefreshResult(
                ran=True, age_seconds=self._age_seconds()
            )


model_usage_refresh_runner = ModelUsageRefreshRunner()
