import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import routes
from .models import Health, NodeStatus, ProbeResult, StatusSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class HealthPageTests(unittest.TestCase):
    def snapshot(self) -> StatusSnapshot:
        return StatusSnapshot(
            generated_at=datetime(2026, 8, 2, 2, 0, tzinfo=timezone.utc),
            overall=Health.OK,
            duration_ms=214,
            nodes=[NodeStatus(
                name="alpha",
                display_name="Alpha",
                health=Health.OK,
                probes=[ProbeResult(
                    node="alpha", kind="disk", health=Health.OK, value="20%",
                )],
            )],
        )

    def test_legacy_health_route_redirects_to_operations(self) -> None:
        response = _client().get("/health", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/operations?tab=health")

    def test_unknown_operations_lens_redirects_to_health(self) -> None:
        response = _client().get("/operations?tab=unknown", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/operations?tab=health")

    @patch.object(routes, "app_chrome_context", new_callable=AsyncMock)
    @patch.object(routes, "read_history")
    @patch.object(routes, "read_snapshot")
    def test_health_page_uses_shared_shell_and_cached_evidence(
        self, read_snapshot, read_history, app_chrome_context,
    ) -> None:
        snap = self.snapshot()
        read_snapshot.return_value = snap
        read_history.return_value = [snap.history_line()]
        app_chrome_context.return_value = {
            "chrome_snap": snap,
            "chrome_accent": routes.FRAME_ACCENT,
            "chrome_unread": 0,
            "chrome_stamp": "2026-08-01 9:00 PM",
        }

        response = _client().get("/operations?tab=health")

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/operations" aria-current="page"', response.text)
        self.assertIn('href="/operations?tab=health" aria-current="page"', response.text)
        self.assertIn("<title>operations · Nexus</title>", response.text)
        self.assertNotIn("fleet health", response.text.lower())
        self.assertIn("Alpha", response.text)
        self.assertIn("20%", response.text)
        self.assertIn('href="/operations?tab=conformance"', response.text)
        self.assertIn('href="/operations?tab=watchdogs"', response.text)
        self.assertIn('href="/operations?tab=indexes"', response.text)

    def test_operations_nav_is_immediately_left_of_cli_control(self) -> None:
        source = (REPO_ROOT / "templates" / "_app_shell.html").read_text()
        self.assertLess(source.index('href="/operations"'), source.index('href="/control"'))


if __name__ == "__main__":
    unittest.main()
