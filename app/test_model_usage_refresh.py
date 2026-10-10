from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import routes
from .model_usage_refresh import (
    ModelUsageRefreshResult,
    ModelUsageRefreshRunner,
)


class _Process:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return b"", self._stderr

    def kill(self) -> None:
        self.killed = True


class ModelUsageRefreshRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_cache_is_throttled_without_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "model-usage.json"
            output.write_text("{}")
            runner = ModelUsageRefreshRunner(
                output=output, min_interval_seconds=60
            )
            with patch("asyncio.create_subprocess_exec") as spawn:
                result = await runner.run()
        self.assertFalse(result.ran)
        self.assertTrue(result.throttled)
        spawn.assert_not_called()

    async def test_stale_cache_starts_systemd_oneshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "model-usage.json"
            output.write_text("{}")
            old = time.time() - 120
            os.utime(output, (old, old))
            runner = ModelUsageRefreshRunner(
                output=output, min_interval_seconds=60
            )
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_Process()),
            ) as spawn:
                result = await runner.run()
        self.assertTrue(result.ran)
        spawn.assert_awaited_once_with(
            "systemctl",
            "--user",
            "start",
            "nexus-model-usage.service",
            stdout=-3,
            stderr=-1,
        )

    async def test_systemd_failure_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = ModelUsageRefreshRunner(output=Path(tmp) / "missing")
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=_Process(1, b"failed safely")),
            ):
                result = await runner.run()
        self.assertFalse(result.ran)
        self.assertEqual(result.error, "failed safely")

    async def test_subprocess_launch_failure_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = ModelUsageRefreshRunner(output=Path(tmp) / "missing")
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=FileNotFoundError("systemctl")),
            ):
                result = await runner.run()
        self.assertFalse(result.ran)
        self.assertEqual(
            result.error, "FileNotFoundError: unable to start refresh"
        )


class ModelUsageRefreshRouteTests(unittest.TestCase):
    def test_scan_returns_quota_refresh_state(self) -> None:
        app = FastAPI()
        app.include_router(routes.router)
        snap = SimpleNamespace(
            model_dump=lambda mode: {
                "overall": "ok",
                "nodes": [],
                "generated_at": "2026-07-27T17:00:00Z",
            }
        )
        heartbeat = SimpleNamespace(
            snap=snap,
            ran=True,
            already_running=False,
            started_at="2026-07-27T17:00:00+00:00",
        )
        usage = ModelUsageRefreshResult(ran=True, age_seconds=0)
        with patch.object(
            routes.heartbeat_runner, "run", new=AsyncMock(return_value=heartbeat)
        ), patch.object(
            routes.model_usage_refresh_runner,
            "run",
            new=AsyncMock(return_value=usage),
        ):
            response = TestClient(app).post("/api/run/heartbeat")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["model_usage"],
            {
                "ran": True,
                "throttled": False,
                "already_running": False,
                "age_seconds": 0,
                "error": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
