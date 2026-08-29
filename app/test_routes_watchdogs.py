"""
Focused route tests for the PANEL-4 Watchdogs surface (GET /watchdogs,
GET /api/watchdogs). Mirrors app/test_api_scheduler_route.py's direct-import
pattern: router mounted on a bare FastAPI app, so a 404 here means "route
absent." Separately verifies that no mutation verb (POST/PUT/PATCH/DELETE) is
registered under /watchdogs* or /api/watchdogs*, and that the registry module
never shells out.
"""
from __future__ import annotations

import inspect
import re
import tempfile
import unittest

from . import watchdogs_registry as _wdreg
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import notify_store, routes, watchdogs_projection, watchdogs_registry


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class ApiWatchdogsRouteTests(unittest.TestCase):
    def test_get_api_watchdogs_returns_all_31_rows_and_summary(self):
        resp = _make_client().get("/api/watchdogs")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["rows"]), 31)
        self.assertEqual(body["summary"]["total"], sum(_wdreg._EXPECTED_HOST_COUNTS.values()))

    def test_get_api_watchdogs_host_filter(self):
        resp = _make_client().get("/api/watchdogs", params={"host": "charlie"})
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["rows"]
        self.assertEqual(len(rows), _wdreg._EXPECTED_HOST_COUNTS["charlie"])
        self.assertTrue(all(r["host"] == "charlie" for r in rows))

    def test_get_api_watchdogs_unknown_host_returns_empty_list(self):
        resp = _make_client().get("/api/watchdogs", params={"host": "nope"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["rows"], [])


class WatchdogsPageRouteTests(unittest.TestCase):
    def test_legacy_watchdogs_route_redirects_to_operations(self):
        resp = _make_client().get("/watchdogs", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/operations?tab=watchdogs")

    def test_get_watchdogs_page_200_and_lists_four_hosts(self):
        resp = _make_client().get("/operations?tab=watchdogs")
        self.assertEqual(resp.status_code, 200)
        text = resp.text
        for host in ("alpha", "charlie", "delta", "echo"):
            self.assertIn(host, text)

    def test_watchdogs_uses_operations_typography_and_palette(self):
        resp = _make_client().get("/operations?tab=watchdogs")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('class="watchdogs-body nexus-app-body"', resp.text)
        self.assertIn('class="watchdogs-shell nexus-page-content"', resp.text)
        self.assertIn('/static/watchdogs.css?v=', resp.text)
        self.assertNotIn('class="wiki-body', resp.text)

        css = (Path(__file__).resolve().parent.parent / "static" / "watchdogs.css").read_text()
        self.assertIn("--watchdogs-ui:var(--font-ui)", css)
        self.assertIn("--watchdogs-mono:var(--font-mono)", css)
        self.assertIn("background:color-mix(in srgb,var(--stone-2) 84%,var(--panel))", css)

    def test_watchdogs_page_initial_dom_has_no_row_detail_markup(self):
        """Bounded-DOM contract: the server-rendered page must not embed any
        of the 31 rows' own detail text -- only the 4 host summary rows.
        Detail rows are mounted client-side (static/watchdogs.js) only after
        a host is expanded."""
        resp = _make_client().get("/operations?tab=watchdogs")
        # A row's `source_of_truth` field is never emitted server-side.
        self.assertNotIn("source_of_truth", resp.text)
        for row in watchdogs_registry.REGISTRY:
            self.assertNotIn(row["id"], resp.text)

    def test_dashboard_has_bounded_watchdogs_summary_module(self):
        resp = _make_client().get("/")
        self.assertEqual(resp.status_code, 200)
        # The lead reads "<total> registered mechanisms"; an earlier revision
        # said "protective mechanisms" and this test still asserted the old
        # wording long after the template changed.
        self.assertIn("registered mechanisms", resp.text)
        self.assertIn(str(sum(_wdreg._EXPECTED_HOST_COUNTS.values())), resp.text)
        for host in _wdreg._EXPECTED_HOST_COUNTS:
            self.assertIn(host, resp.text)
        for row in watchdogs_registry.REGISTRY:
            self.assertNotIn(row["id"], resp.text)

    def test_dashboard_uses_singular_mechanism_for_temple(self):
        resp = _make_client().get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(">1 mechanism<", resp.text)
        self.assertNotIn(">1 mechanisms<", resp.text)


class ApiWatchdogsProjectionTests(unittest.TestCase):
    """FLEET-AUTO-BUILD-20260802-panel-live-watchdog-evidence: /api/watchdogs
    must serve the live-evidence-overlaid projection, not the static registry
    -- a fresh, ok scheduler receipt must flip the row from the static
    stale_evidence to active, and the returned summary's flagged count must
    match."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = Path(self._tmpdir.name) / "events.db"
        notify_store.init_db()
        watchdogs_projection.get_projected_registry(force=True)

    def tearDown(self):
        notify_store.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def test_row_flips_active_once_a_live_ok_receipt_lands(self):
        before = _make_client().get("/api/watchdogs", params={"host": "alpha"}).json()["rows"]
        before_row = next(r for r in before if r["id"] == "alpha-aps-thermal-watch")
        self.assertEqual(before_row["status"], "stale_evidence")

        notify_store.record_scheduler_receipt(
            "thermal-watch", outcome="ok",
            completed_at=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        )
        watchdogs_projection.get_projected_registry(force=True)  # bust the 15s cache
        after = _make_client().get("/api/watchdogs", params={"host": "alpha"}).json()["rows"]
        after_row = next(r for r in after if r["id"] == "alpha-aps-thermal-watch")
        self.assertEqual(after_row["status"], "active")

    def test_delta_tower_row_is_retired_and_never_flagged_via_the_route(self):
        resp = _make_client().get("/api/watchdogs", params={"host": "delta"})
        rows = resp.json()["rows"]
        row = next(r for r in rows if r["id"] == "delta-systemd-tower")
        self.assertEqual(row["status"], "retired")
        delta_summary = next(h for h in resp.json()["summary"]["hosts"] if h["host"] == "delta")
        self.assertEqual(delta_summary["flagged"], 0)


class NoMutationVerbTests(unittest.TestCase):
    def test_no_post_put_patch_delete_under_watchdogs_paths(self):
        for route in routes.router.routes:
            path = getattr(route, "path", "")
            if path.startswith("/watchdogs") or path.startswith("/api/watchdogs"):
                methods = getattr(route, "methods", set()) or set()
                forbidden = methods & {"POST", "PUT", "PATCH", "DELETE"}
                self.assertFalse(
                    forbidden, f"route {path} registers mutation verb(s) {forbidden}"
                )

    def test_watchdogs_routes_reject_post(self):
        client = _make_client()
        self.assertEqual(client.post("/api/watchdogs").status_code, 405)
        self.assertEqual(client.post("/watchdogs").status_code, 405)


class NoSubprocessOrNetworkTests(unittest.TestCase):
    """The registry module is pure static data -- no systemctl/journalctl/
    ssh/subprocess/socket CALL may ever be made from it or from the two
    route handlers that read it. (systemctl/journalctl command strings
    legitimately appear as prose in each row's `source_of_truth` field --
    a human-verification hint, never executed -- so this matches actual
    Python invocation syntax, not those string literals.)"""

    _FORBIDDEN = re.compile(
        r"\bsubprocess\.|\bos\.system\(|\bos\.popen\(|\bPopen\(|paramiko|"
        r"\bsocket\.(connect|create_connection)\(",
    )

    def test_registry_module_source_has_no_shellouts(self):
        src = Path(watchdogs_registry.__file__).read_text()
        self.assertIsNone(self._FORBIDDEN.search(src), "registry module shells out")

    def test_route_handlers_source_has_no_shellouts(self):
        for fn in (routes.api_watchdogs, routes.watchdogs_index):
            src = inspect.getsource(fn)
            self.assertIsNone(self._FORBIDDEN.search(src), f"{fn.__name__} shells out")


if __name__ == "__main__":
    unittest.main()
