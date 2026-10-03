import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import routes


REPO_ROOT = Path(__file__).resolve().parent.parent


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class WorkerActivityRouteTests(unittest.TestCase):
    def sample_runs(self) -> list[dict]:
        return [{
            "token": "FLEET-WORKER1-RECON-20260801-sample",
            "seat": "worker1",
            "seat_class": "seat-worker1",
            "state": "done",
            "age": "2h ago",
        }]

    @patch.object(routes.herospath, "list_runs")
    def test_worker_runs_are_a_third_activity_view(self, list_runs) -> None:
        list_runs.return_value = self.sample_runs()
        response = _client().get("/activity?tab=workers")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-tab="workers"', response.text)
        self.assertIn('<span>Workers</span>', response.text)
        self.assertIn('<h3 id="activityLensTitle">workers</h3>', response.text)
        self.assertIn('id="workers"', response.text)
        self.assertIn(
            '/activity/workers/FLEET-WORKER1-RECON-20260801-sample',
            response.text,
        )

    def test_legacy_index_redirects_into_activity(self) -> None:
        response = _client().get("/hero-path", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/activity?tab=workers")

    def test_legacy_session_preserves_limit_query(self) -> None:
        response = _client().get(
            "/hero-path/FLEET-WORKER1-RECON-20260801-sample?limit=all",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            "/activity/workers/FLEET-WORKER1-RECON-20260801-sample?limit=all",
        )

    def test_user_facing_surfaces_use_worker_activity_name(self) -> None:
        for relative in (
            "templates/dashboard.html",
            "templates/activity.html",
            "templates/detail_run.html",
            "templates/hero_path.html",
            "templates/hero_path_session.html",
        ):
            text = (REPO_ROOT / relative).read_text().lower()
            self.assertNotIn("hero's path", text, relative)

    def test_new_ui_generates_only_activity_worker_links(self) -> None:
        dashboard = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        dashboard_js = (REPO_ROOT / "static" / "dashboard.js").read_text()
        activity_js = (REPO_ROOT / "static" / "activity.js").read_text()
        session = (REPO_ROOT / "templates" / "hero_path_session.html").read_text()

        self.assertIn('module_shell("worker activity"', dashboard)
        self.assertNotIn('href="/hero-path', dashboard)
        self.assertNotIn('href="/hero-path', dashboard_js)
        self.assertIn('["overview", "models", "workers", "jobs"]', activity_js)
        self.assertIn('/activity/workers/ws?token=', session)
        self.assertNotIn('/hero-path/ws?token=', session)

    @patch.object(routes, "read_snapshot")
    @patch.object(routes.herospath, "list_runs", return_value=[])
    def test_jobs_are_a_fourth_activity_view(self, _list_runs, read_snapshot) -> None:
        read_snapshot.return_value = SimpleNamespace(work={"jobs": [{
            "job": "nas3-photo-mirror",
            "state": "done",
            "host": "charlie",
        }]})
        response = _client().get("/activity?tab=jobs")

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-tab="jobs"', response.text)
        self.assertIn('<span>Jobs</span>', response.text)
        self.assertIn('<h3 id="activityLensTitle">jobs</h3>', response.text)
        self.assertIn('id="jobs"', response.text)
        self.assertIn("nas3-photo-mirror", response.text)

    def test_legacy_jobs_index_redirects_into_activity(self) -> None:
        response = _client().get("/jobs", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/activity?tab=jobs")

    def test_dashboard_swaps_semantic_index_and_jobs_activity_positions(self) -> None:
        dashboard = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        semantic_index = dashboard.index('module_shell("semantic index"')
        workers = dashboard.index('module_shell("worker activity"')
        jobs = dashboard.index('module_shell("jobs activity"')
        self.assertLess(workers, jobs)


if __name__ == "__main__":
    unittest.main()
