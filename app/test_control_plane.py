import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import control_plane, routes


def sample() -> dict:
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "overall": "ok",
        "cards": [
            {"id": name, "title": name, "file": f"{name}-index.md",
             "revision": "2026-08-01.1", "exists": True, "status": "ok", "summary": "current"}
            for name in ("fleet", "roadmap", "conventions", "instructions", "automation")
        ],
        "fleet": {"hosts": []},
        "roadmaps": {"check": {"results": []}},
        "conventions": {"check": {"results": []}},
        "instructions": {"check": {"counts": {}, "results": []}},
        "automations": {"entries": [], "check": {"counts": {}, "results": []}},
    }


class ControlPlaneCacheTests(unittest.TestCase):
    def test_read_and_project_five_cards(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "control-plane.json"
            path.write_text(json.dumps(sample()))
            data, error = control_plane.read_cache(path)
        projected = control_plane.project(data, error)
        self.assertTrue(projected["available"])
        self.assertEqual(len(projected["cards"]), 5)
        self.assertEqual(projected["counts"]["ok"], 5)
        self.assertFalse(projected["is_stale"])

    def test_stale_projection(self):
        data = sample()
        data["generated_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.assertTrue(control_plane.project(data)["is_stale"])


class ControlPlaneRouteTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)

    def test_api_is_cache_only_and_additive(self):
        with patch.object(control_plane, "read_cache", return_value=(sample(), None)):
            response = self.client.get("/api/control-plane")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(len(response.json()["cards"]), 5)

    def test_indexes_page_has_all_five_index_sections(self):
        with patch.object(control_plane, "read_cache", return_value=(sample(), None)):
            response = self.client.get("/operations?tab=indexes")
        self.assertEqual(response.status_code, 200)
        self.assertIn("operations · Nexus", response.text)
        self.assertIn('href="/operations?tab=indexes" aria-current="page"', response.text)
        self.assertNotIn('href="/control-plane"', response.text)
        for label in ("fleet", "roadmaps", "conventions", "instructions", "automations"):
            self.assertIn(label, response.text)

    def test_legacy_control_plane_route_redirects_to_indexes(self):
        response = self.client.get("/control-plane", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/operations?tab=indexes")

    def test_legacy_indexes_route_redirects_to_operations(self):
        response = self.client.get("/indexes", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/operations?tab=indexes")


if __name__ == "__main__":
    unittest.main()
