"""
Focused route tests for the PANEL-3 scheduler/jobs URL-collision fix
(FLEET-WORKER2-BUILD-20260723-slate3-scheduler-route).

GET /api/jobs was renamed to GET /api/scheduler to stop naming two unrelated
resources under one prefix: the APScheduler registered-task registry
(jobs_summary()) vs. the heartbeat-derived job cards mutated by
POST /api/jobs/{job}/done|undone (unchanged by this rename).

These tests exercise app.routes.router directly on a bare FastAPI app
(mirroring app/test_routes_jobs_sort.py's direct-import pattern), so a 404 here
means "route absent."
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import routes


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class ApiSchedulerRouteTests(unittest.TestCase):
    def test_get_api_scheduler_returns_registry(self):
        fixture = [{"id": "heartbeat", "name": "Heartbeat", "next_run": "2026-07-23T12:00:00+00:00"}]
        with patch("app.routes.jobs_summary", return_value=fixture):
            resp = _make_client().get("/api/scheduler")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), fixture)

    def test_get_bare_api_jobs_no_longer_resolves(self):
        resp = _make_client().get("/api/jobs")
        self.assertEqual(resp.status_code, 404)

    def test_no_route_collision_between_scheduler_and_job_cards(self):
        paths = {r.path for r in routes.router.routes}
        self.assertIn("/api/scheduler", paths)
        self.assertNotIn("/api/jobs", paths)
        self.assertIn("/api/jobs/{job}/done", paths)
        self.assertIn("/api/jobs/{job}/undone", paths)


class ApiJobDoneUndoneUnchangedTests(unittest.TestCase):
    """POST /api/jobs/{job}/done|undone share the /api/jobs prefix string with
    the renamed GET route -- this is the one regression risk worth pinning."""

    def test_done_marks_known_job(self):
        snap = SimpleNamespace(work={"jobs": [{"job": "gallery-scan", "pid": 123, "uptime_s": 60}]})
        with patch("app.routes.read_snapshot", return_value=snap), \
             patch("app.job_history.init_db"), \
             patch("app.job_history.mark_done", return_value={"job": "gallery-scan", "muted": True}) as mock_mark:
            resp = _make_client().post("/api/jobs/gallery-scan/done")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["job"], "gallery-scan")
        mock_mark.assert_called_once()

    def test_done_rejects_unknown_job(self):
        snap = SimpleNamespace(work={"jobs": []})
        with patch("app.routes.read_snapshot", return_value=snap), \
             patch("app.job_history.init_db"):
            resp = _make_client().post("/api/jobs/no-such-job/done")
        self.assertEqual(resp.status_code, 404)

    def test_undone_clears_mute_and_is_a_harmless_noop_for_unknown_job(self):
        with patch("app.job_history.init_db"), \
             patch("app.job_history.clear_mute") as mock_clear:
            resp = _make_client().post("/api/jobs/anything/undone")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "job": "anything"})
        mock_clear.assert_called_once_with("anything")


if __name__ == "__main__":
    unittest.main()
