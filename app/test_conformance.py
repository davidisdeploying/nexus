import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.conformance import (
    CATEGORY_DEFS,
    derive_stability,
    format_central,
    history_strip,
    project_conformance,
    read_cache,
    read_history,
)
from app import routes
from app.main import app


class ConformanceCacheTests(unittest.TestCase):
    def test_reads_valid_cache(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps({"version": 1, "generated_at": "2026-01-01T00:00:00Z"}))
            data, error = read_cache(path)
            self.assertIsNone(error)
            self.assertEqual(data["version"], 1)

    def test_reads_additive_v2_cache(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps({"version": 2, "generated_at": "2026-01-01T00:00:00Z"}))
            data, error = read_cache(path)
            self.assertIsNone(error)
            self.assertEqual(data["version"], 2)

    def test_rejects_wrong_schema(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps({"version": 3, "generated_at": "x"}))
            data, error = read_cache(path)
            self.assertIsNone(data)
            self.assertIn("unsupported", error)

    def test_rejects_missing_generated_at(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "state.json"
            path.write_text(json.dumps({"version": 1}))
            data, error = read_cache(path)
            self.assertIsNone(data)
            self.assertIn("generated_at", error)


class ConformanceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.sample_cache = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "overall": "ok",
            "counts": {"ok": 6, "warning": 0, "error": 0, "unknown": 0},
            "duration_seconds": 1.25,
            "checks": [
                {
                    "id": "agents:alpha",
                    "category": "contract",
                    "host": "alpha",
                    "state": "ok",
                    "expected": "hash1",
                    "actual": "hash1",
                    "checked_at": "2026-07-30T10:00:00Z",
                    "last_ok_at": "2026-07-30T10:00:00Z",
                    "last_ok_actual": "hash1",
                },
                {
                    "id": "ssh:alpha:charlie",
                    "category": "ssh",
                    "host": "alpha",
                    "state": "ok",
                    "expected": "SSH_OK",
                    "actual": "SSH_OK",
                    "checked_at": "2026-07-30T10:00:01Z",
                    "last_ok_at": "2026-07-30T10:00:01Z",
                    "last_ok_actual": "SSH_OK",
                },
                {
                    "id": "unit:alpha:nexus.service",
                    "category": "service",
                    "host": "alpha",
                    "state": "ok",
                    "expected": "active",
                    "actual": "active",
                    "checked_at": "2026-07-30T10:00:02Z",
                    "last_ok_at": "2026-07-30T10:00:02Z",
                    "last_ok_actual": "active",
                },
                {
                    "id": "path:alpha:/etc/config.json",
                    "category": "path",
                    "host": "alpha",
                    "state": "ok",
                    "expected": "file exists",
                    "actual": "present",
                    "checked_at": "2026-07-30T10:00:03Z",
                    "last_ok_at": "2026-07-30T10:00:03Z",
                    "last_ok_actual": "present",
                },
                {
                    "id": "fresh:alpha:/backups/latest",
                    "category": "backup",
                    "host": "alpha",
                    "state": "ok",
                    "expected": "age <= 93600s",
                    "actual": "age 100s",
                    "checked_at": "2026-07-30T10:00:04Z",
                    "last_ok_at": "2026-07-30T10:00:04Z",
                    "last_ok_actual": "age 100s",
                },
                {
                    "id": "mirror:alpha:/vaults:delta:/mirror.git",
                    "category": "backup",
                    "host": "alpha",
                    "state": "ok",
                    "expected": "match",
                    "actual": "match abc123",
                    "checked_at": "2026-07-30T10:00:05Z",
                    "last_ok_at": "2026-07-30T10:00:05Z",
                    "last_ok_actual": "match abc123",
                },
                {
                    "id": "receipt:alpha:fleet-git-push",
                    "category": "receipts",
                    "host": "alpha",
                    "state": "ok",
                    "expected": "ok=True, host match, age <= 36h",
                    "actual": "ok=True host=alpha finished=2026-07-30T09:00:00Z age=1.0h",
                    "checked_at": "2026-07-30T10:00:06Z",
                    "last_ok_at": "2026-07-30T10:00:06Z",
                    "last_ok_actual": "ok=True host=alpha finished=2026-07-30T09:00:00Z age=1.0h",
                },
            ],
        }
        self.sample_cache["counts"] = {"ok": 7, "warning": 0, "error": 0, "unknown": 0}

    def test_categorization_and_seven_ordered_categories(self):
        proj = project_conformance(self.sample_cache)
        self.assertTrue(proj["available"])
        self.assertEqual(proj["total_checks"], 7)
        self.assertEqual(len(proj["categories"]), 7)

        expected_keys = ["agents", "ssh", "service", "path", "receipts", "fresh", "mirror"]
        expected_titles = [
            "Operating contract (agents)",
            "Worker access (ssh)",
            "Required services (service)",
            "Required files (path)",
            "Automation receipts (receipts)",
            "Snapshot freshness (fresh)",
            "Vault mirror (mirror)",
        ]
        expected_short_titles = [
            "Contract", "SSH", "Services", "Files", "Receipts", "Snapshots", "Mirror",
        ]

        for i, key in enumerate(expected_keys):
            self.assertEqual(proj["categories"][i]["key"], key)
            self.assertEqual(proj["categories"][i]["title"], expected_titles[i])
            self.assertEqual(proj["categories"][i]["short_title"], expected_short_titles[i])
            self.assertEqual(proj["categories"][i]["count"], 1)
            self.assertEqual(proj["categories"][i]["ok_count"], 1)
            self.assertEqual(len(proj["grouped_checks"][key]), 1)

    def test_headline_and_staleness_fresh(self):
        proj = project_conformance(self.sample_cache)
        self.assertEqual(proj["outcome_headline"], "All 7 required checks pass")
        self.assertFalse(proj["is_stale"])

    def test_headline_and_staleness_stale_and_failing(self):
        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=2500)).isoformat().replace("+00:00", "Z")
        cache = copy.deepcopy(self.sample_cache)
        cache["generated_at"] = stale_time
        cache["overall"] = "error"
        cache["counts"] = {"ok": 6, "warning": 0, "error": 1, "unknown": 0}
        cache["checks"][0]["state"] = "error"
        cache["checks"][0]["actual"] = "mismatch_hash"

        proj = project_conformance(cache)
        self.assertTrue(proj["is_stale"])
        self.assertEqual(proj["outcome_headline"], "6 of 7 required checks pass (1 error)")

    def test_informational_peer_failure_does_not_degrade_required_outcome(self):
        cache = copy.deepcopy(self.sample_cache)
        cache["checks"].append({
            "id": "ssh:alpha:macbook", "category": "ssh", "host": "alpha",
            "state": "error", "expected": "SSH_OK", "actual": "offline",
            "checked_at": cache["generated_at"], "impact": "informational",
        })
        cache["counts"] = {"ok": 7, "warning": 0, "error": 1, "unknown": 0}
        projection = project_conformance(cache)
        self.assertEqual(projection["overall"], "ok")
        self.assertEqual(projection["required_total"], 7)
        self.assertEqual(projection["informational_total"], 1)
        self.assertEqual(projection["non_ok_count"], 0)
        self.assertEqual(projection["informational_non_ok_count"], 1)
        self.assertIn("All 7 required checks pass", projection["outcome_headline"])
        self.assertIn("1 informational peer check non-OK", projection["outcome_headline"])
        peer = next(c for c in projection["grouped_checks"]["ssh"] if c["id"].endswith("macbook"))
        self.assertIn("informational peer", peer["human_title"])

    def test_central_time_formatting(self):
        iso_utc = "2026-07-30T10:23:49.103271Z"
        formatted = format_central(iso_utc)
        self.assertIn("2026-07-30 05:23:49", formatted)
        self.assertTrue("CDT" in formatted or "CST" in formatted or "America/Chicago" in formatted)

    def test_preserves_raw_cache(self):
        original = copy.deepcopy(self.sample_cache)
        _ = project_conformance(self.sample_cache)
        self.assertEqual(self.sample_cache, original)

    def test_recently_recovered_surfaces_qualifying_rows(self):
        now = datetime.now(timezone.utc)
        cache = copy.deepcopy(self.sample_cache)
        cache["checks"][0]["last_recovered_at"] = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        cache["checks"][1]["last_recovered_at"] = (now - timedelta(hours=48)).isoformat().replace("+00:00", "Z")

        proj = project_conformance(cache, now=now)
        recovered_ids = [row["id"] for row in proj["recently_recovered"]]
        self.assertIn(cache["checks"][0]["id"], recovered_ids)
        self.assertNotIn(cache["checks"][1]["id"], recovered_ids)  # outside the 24h window

    def test_recently_recovered_empty_when_no_rows_qualify(self):
        proj = project_conformance(self.sample_cache)
        self.assertEqual(proj["recently_recovered"], [])


class StabilityAndHistoryTests(unittest.TestCase):
    def _sample(self, overall: str, generated_at: str) -> dict:
        return {"generated_at": generated_at, "overall": overall,
                "counts": {"ok": 54, "warning": 0, "error": 0, "unknown": 0}, "collector_error": None}

    def test_derive_stability_all_same_state_is_at_least_qualified(self):
        history = [self._sample("ok", f"2026-07-30T10:0{i}:00Z") for i in range(5)]
        stability = derive_stability(history)
        self.assertEqual(stability["current_state"], "ok")
        self.assertEqual(stability["consecutive_scans"], 5)
        self.assertTrue(stability["at_least_qualifier"])
        self.assertIsNone(stability["last_transition"])

    def test_derive_stability_finds_last_transition_within_window(self):
        history = [
            self._sample("error", "2026-07-30T10:00:00Z"),
            self._sample("error", "2026-07-30T10:05:00Z"),
            self._sample("ok", "2026-07-30T10:10:00Z"),
            self._sample("ok", "2026-07-30T10:15:00Z"),
        ]
        stability = derive_stability(history)
        self.assertEqual(stability["current_state"], "ok")
        self.assertEqual(stability["consecutive_scans"], 2)
        self.assertFalse(stability["at_least_qualifier"])
        self.assertEqual(stability["last_transition"]["from"], "error")
        self.assertEqual(stability["last_transition"]["to"], "ok")
        self.assertEqual(stability["last_transition"]["at"], "2026-07-30T10:10:00Z")

    def test_derive_stability_empty_history(self):
        stability = derive_stability([])
        self.assertEqual(stability["current_state"], "unknown")
        self.assertEqual(stability["consecutive_scans"], 0)
        self.assertFalse(stability["at_least_qualifier"])

    def test_history_strip_caps_at_count_and_preserves_order(self):
        base = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
        history = [
            self._sample("ok", (base + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"))
            for i in range(120)
        ]
        strip = history_strip(history, count=96)
        self.assertEqual(len(strip), 96)
        self.assertEqual(strip[0]["generated_at"], (base + timedelta(minutes=24)).isoformat().replace("+00:00", "Z"))
        self.assertEqual(strip[-1]["generated_at"], (base + timedelta(minutes=119)).isoformat().replace("+00:00", "Z"))

    def test_read_history_skips_unparseable_lines(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "history.jsonl"
            path.write_text(
                '{"generated_at":"2026-07-30T10:00:00Z","overall":"ok"}\n'
                "not json\n"
                '{"generated_at":"2026-07-30T10:05:00Z","overall":"ok"}\n'
            )
            rows = read_history(path)
        self.assertEqual(len(rows), 2)

    def test_read_history_missing_file_returns_empty(self):
        self.assertEqual(read_history(Path("/nonexistent/history.jsonl")), [])


class ManifestPolicyTests(unittest.TestCase):
    """The manifest owns shape and categories, never a frozen fleet total."""

    def test_manifest_v2_retires_legacy_path_and_declares_governance(self):
        manifest_path = (
            Path(__file__).resolve().parent.parent / "conformance" / "checks.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], 2)
        self.assertTrue(manifest["manifest_revision"].startswith("CONFORMANCE-POLICY-"))
        self.assertIn("governance", [row["key"] for row in manifest["categories"]])
        paths = [row[1] for row in manifest["required_paths"]]
        self.assertNotIn("/home/david/Vaults/homelab-vault/conventions/README.md", paths)
        self.assertTrue(paths)
        # Shape, not a frozen count: this class exists precisely so the manifest
        # can gain rows without a magic number to bump. Each row is
        # [host, absolute path, kind], where kind is "file", "dir", or
        # "symlink:<absolute target>" for a path that must resolve to a canonical copy.
        for row in manifest["required_paths"]:
            self.assertEqual(len(row), 3, row)
            host, path, kind = row
            self.assertTrue(path.startswith("/"), row)
            if kind.startswith("symlink:"):
                self.assertTrue(kind.split(":", 1)[1].startswith("/"), row)
            else:
                self.assertIn(kind, {"file", "dir", "python-compiles"}, row)

    def test_compendium_ota_is_an_ordinary_required_service(self):
        manifest_path = (
            Path(__file__).resolve().parent.parent / "conformance" / "checks.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn(["alpha", "compendium-ota.service", "active"], manifest["user_units"])
        self.assertIn(["alpha", "compendium-ota.service"], manifest["enablement_units"])
        required = {row[1]: row[2] for row in manifest["required_paths"]}
        self.assertEqual(required["/home/david/.config/systemd/user/compendium-ota.service"], "file")
        self.assertEqual(required["/home/david/compendium-ota/manifest.plist"], "file")
        self.assertEqual(required["/home/david/compendium-ota/CompendiumBridge.ipa"], "file")


def _make_client() -> TestClient:
    test_app = FastAPI()
    test_app.include_router(routes.router)
    return TestClient(test_app)


class ConformanceRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = _make_client()

    def test_api_conformance_backward_compatibility(self):
        res = self.client.get("/api/conformance")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("stale", data)
        self.assertIn("version", data)
        self.assertIn("generated_at", data)
        self.assertIn("counts", data)
        self.assertIn("checks", data)

    def test_legacy_conformance_route_redirects_to_operations(self):
        res = self.client.get("/conformance", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["location"], "/operations?tab=conformance")

    def test_conformance_page_content_markers(self):
        res = self.client.get("/operations?tab=conformance")
        self.assertEqual(res.status_code, 200)
        html = res.text

        # 1. Title and Header
        self.assertIn("<title>operations · Nexus</title>", html)
        self.assertNotIn("fleet conformance", html.lower())
        self.assertIn("Nexus", html)
        self.assertIn('href="/operations" aria-current="page"', html)
        self.assertIn('href="/operations?tab=conformance" aria-current="page"', html)

        # 2. Status Hero and Read-only drift text
        self.assertIn("detects drift and never performs automatic repairs", html)

        # 3. What the checks cover (7 categories)
        self.assertIn("What the checks cover", html)
        self.assertIn("Operating contract (agents)", html)
        self.assertIn("Worker access (ssh)", html)
        self.assertIn("Required services (service)", html)
        self.assertIn("Required files (path)", html)
        self.assertIn("Automation receipts (receipts)", html)
        self.assertIn("Snapshot freshness (fresh)", html)
        self.assertIn("Vault mirror (mirror)", html)

        # 3b. Compact history strip + collapsible category groups
        self.assertIn("history-strip", html)
        self.assertIn('<details class="cat-group"', html)

        # 4. How to read the states
        self.assertIn("How to read the states", html)
        self.assertIn("Exact match", html)
        self.assertIn("Verified mismatch / drift", html)

        # 5. What green does not prove limitations
        self.assertIn("What green does not prove", html)
        self.assertIn("Not complete fleet health", html)
        self.assertIn("Not Temple/Lookout", html)
        self.assertIn("Not provider auth", html)
        self.assertIn("Not application behavior", html)
        self.assertIn("Not NAS or GPU health", html)
        self.assertIn("Not a restore drill", html)
        self.assertIn("Cache timestamp matters", html)

        # 6. Separate Health Timeline surface clarification
        self.assertIn("Health Timeline", html)
        self.assertIn("separate machine-heartbeat surface", html)

    def test_dashboard_page_conformance_module(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.text

        dashboard_source = (Path(__file__).resolve().parent.parent / "templates" / "dashboard.html").read_text()
        self.assertIn('module_shell("conformance"', dashboard_source)
        self.assertNotIn("fleet conformance", html.lower())
        # The module renders conformance.outcome_headline, which reads
        # "All N required checks pass" / "N of M required checks pass (...)".
        # "declared drift detection" was an older subtitle and by now existed
        # nowhere in the app except this assertion.
        self.assertIn("required checks", html)
        self.assertIn('href="/operations?tab=conformance"', html)
        self.assertIn("Indexes (governance)", html)
        self.assertIn("Scanned", html)
