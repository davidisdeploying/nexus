from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from unittest.mock import patch

from app import routes, system_status
from app.models import Health, NodeStatus, ProbeResult, StatusSnapshot
from app.system_status import (
    EXPECTED_SCHEDULER_JOBS,
    MODULE_ORDER,
    build_system_status,
    read_sources,
)


NOW = datetime(2026, 8, 2, 17, 0, tzinfo=timezone.utc)


def wrapped(value=None, error=None):
    return {"value": value, "error": error}


def healthy_sources() -> dict:
    snap = StatusSnapshot(
        generated_at=NOW - timedelta(seconds=30),
        overall=Health.OK,
        nodes=[NodeStatus(
            name="alpha",
            health=Health.OK,
            probes=[ProbeResult(node="alpha", kind="nexus", health=Health.OK, value="up")],
        )],
        work={"jobs": []},
        seats={
            "seats": [
                {"seat": "worker2", "label": "Worker2", "state": "idle", "badge": "FREE"},
                {
                    "usage_card": True,
                    "routing": {"candidates": [
                        {"provider": "claude", "state": "GREEN"},
                        {"provider": "codex", "state": "GREEN"},
                        {"provider": "gemini", "state": "GREEN"},
                    ]},
                },
            ]
        },
    )
    stamp = NOW.isoformat().replace("+00:00", "Z")
    epoch = int(NOW.timestamp()) - 30
    return {
        "snapshot": wrapped(snap),
        "conformance": wrapped({
            "available": True,
            "overall": "ok",
            "is_stale": False,
            "generated_at": stamp,
            "outcome_headline": "All checks pass",
            "categories": [],
        }),
        "control_plane": wrapped({
            "available": True,
            "overall": "ok",
            "is_stale": False,
            "generated_at": stamp,
            "cards": [{"id": "fleet", "title": "fleet", "status": "ok", "summary": "current"}],
        }),
        "activity": wrapped({
            "data": {"generated_at": stamp, "host_errors": {}},
            "error": None,
        }),
        "model_usage": wrapped({
            "generated_at": stamp,
            "latest": [
                {"provider": provider, "captured_at": epoch, "ok": 1, "source": "test"}
                for provider in ("claude", "codex", "gemini")
            ],
        }),
        "scheduler": wrapped([
            {"id": job, "name": job, "next_run": stamp}
            for job in sorted(EXPECTED_SCHEDULER_JOBS)
        ]),
        "notifications": wrapped({
            "subscriptions": [{
                "device_label": "phone",
                "consecutive_failures": 0,
                "last_send_at": stamp,
            }],
            "selftest": {"created_at": stamp, "sent_pwa": True, "sent_ntfy": True},
        }),
        "watchdogs": wrapped([]),
        "semantic_index": wrapped({
            "health": "GREEN", "no_receipt": False, "age_hours": 2,
            "last_run_utc": "2026-08-03 08:21", "markdown_docs": 7086,
            "transcript_docs": 5572, "detail": None,
        }),
        "cli_control": wrapped({
            "host_count": 3,
            "provider_count": 3,
            "strategies": [],
            "workers": [],
        }),
    }


class SystemStatusRollupTests(unittest.TestCase):
    def test_healthy_registry_has_every_declared_module(self) -> None:
        result = build_system_status(healthy_sources(), now=NOW)
        self.assertEqual(result["overall"], "ok")
        self.assertEqual(result["module_count"], len(MODULE_ORDER))
        self.assertEqual(tuple(row["id"] for row in result["modules"]), MODULE_ORDER)
        self.assertEqual(result["counts"], {"ok": len(MODULE_ORDER), "warn": 0, "critical": 0})

    def test_conformance_error_is_critical_and_keeps_exact_evidence(self) -> None:
        sources = healthy_sources()
        sources["conformance"] = wrapped({
            "available": True,
            "overall": "error",
            "is_stale": False,
            "generated_at": NOW.isoformat(),
            "outcome_headline": "9 of 10 declared checks pass (1 error)",
            "categories": [{"checks": [{
                "id": "service:alpha:nexus",
                "human_title": "Required service (Nexus on alpha)",
                "state": "error",
                "actual": "inactive",
                "expected": "active and enabled",
            }]}],
        })
        result = build_system_status(sources, now=NOW)
        module = next(row for row in result["modules"] if row["id"] == "conformance")
        self.assertEqual(result["overall"], "critical")
        self.assertEqual(module["status"], "critical")
        self.assertEqual(module["checks"][0]["value"], "inactive")

    def test_stale_heartbeat_and_failed_job_are_critical(self) -> None:
        sources = healthy_sources()
        snap = sources["snapshot"]["value"]
        snap.generated_at = NOW - timedelta(minutes=20)
        snap.work = {"jobs": [{"job": "backup", "state": "failed", "detail": "exit 1"}]}
        result = build_system_status(sources, now=NOW)
        states = {row["id"]: row["status"] for row in result["modules"]}
        self.assertEqual(states["nexus-runtime"], "critical")
        self.assertEqual(states["fleet-jobs"], "critical")
        self.assertEqual(result["overall"], "critical")

    def test_missing_non_core_cache_fails_safe_to_warn(self) -> None:
        sources = healthy_sources()
        sources["activity"] = wrapped(error="JSONDecodeError")
        result = build_system_status(sources, now=NOW)
        activity = next(row for row in result["modules"] if row["id"] == "activity")
        self.assertEqual(result["overall"], "warn")
        self.assertEqual(activity["status"], "warn")
        self.assertIn("JSONDecodeError", activity["detail"])

    def test_notification_canary_degrades_one_transport_and_critical_on_both(self) -> None:
        sources = healthy_sources()
        sources["notifications"]["value"]["selftest"]["sent_ntfy"] = False
        one_down = build_system_status(sources, now=NOW)
        notifications = next(row for row in one_down["modules"] if row["id"] == "notifications")
        self.assertEqual(notifications["status"], "warn")
        self.assertEqual(notifications["checks"][-1]["value"], "one transport failed")

        both_sources = copy.deepcopy(sources)
        both_sources["notifications"]["value"]["selftest"]["sent_pwa"] = False
        both_sources["notifications"]["value"]["subscriptions"][0]["last_send_at"] = None
        both_down = build_system_status(both_sources, now=NOW)
        notifications = next(row for row in both_down["modules"] if row["id"] == "notifications")
        self.assertEqual(notifications["status"], "critical")
        self.assertEqual(both_down["overall"], "critical")

    def test_missing_scheduler_registration_is_critical(self) -> None:
        sources = healthy_sources()
        sources["scheduler"]["value"] = [
            row for row in sources["scheduler"]["value"] if row["id"] != "heartbeat"
        ]
        result = build_system_status(sources, now=NOW)
        scheduler = next(row for row in result["modules"] if row["id"] == "scheduler")
        self.assertEqual(scheduler["status"], "critical")
        self.assertEqual(scheduler["checks"][0]["label"], "heartbeat")

    def test_reduced_model_route_has_a_visible_explanation(self) -> None:
        sources = healthy_sources()
        usage_tile = sources["snapshot"]["value"].seats["seats"][1]
        usage_tile["routing"]["candidates"][1].update({
            "state": "YELLOW",
            "model": "gpt-5.6-terra",
            "reason": "five_hour_unknown",
        })
        result = build_system_status(sources, now=NOW)
        usage = next(row for row in result["modules"] if row["id"] == "model-usage")
        self.assertEqual(usage["status"], "warn")
        routing = next(check for check in usage["checks"] if "routing" in check["label"])
        self.assertEqual(routing["value"], "YELLOW")
        self.assertIn("five_hour_unknown", routing["detail"])


class WatchdogsProjectionSourceTests(unittest.TestCase):
    """FLEET-AUTO-BUILD-20260802-panel-live-watchdog-evidence: system_status
    must read the live-evidence-overlaid projection, not the static registry
    directly, so a stale_evidence row here reflects real cadence evidence."""

    def test_read_sources_watchdogs_key_uses_the_projection_layer(self) -> None:
        projected = [{"id": "alpha-aps-thermal-watch", "host": "alpha", "status": "active"}]
        with patch.object(system_status, "get_projected_registry", return_value=projected) as mock_proj:
            sources = read_sources()
        mock_proj.assert_called_once_with()
        self.assertEqual(sources["watchdogs"]["value"], projected)
        self.assertIsNone(sources["watchdogs"]["error"])

    def test_watchdogs_module_is_warn_when_projection_reports_stale_evidence(self) -> None:
        sources = healthy_sources()
        sources["watchdogs"] = wrapped([
            {"id": "alpha-aps-thermal-watch", "host": "alpha", "status": "active",
             "label": "thermal", "status_detail": "ok"},
            {"id": "alpha-aps-health-watch", "host": "alpha", "status": "stale_evidence",
             "label": "health-watch", "status_detail": "No scheduler execution receipt recorded yet."},
        ])
        result = build_system_status(sources, now=NOW)
        module = next(row for row in result["modules"] if row["id"] == "watchdogs")
        self.assertEqual(module["status"], "warn")
        self.assertEqual(module["summary"], "2 mechanisms · 1 flagged")
        self.assertEqual(module["checks"][0]["label"], "health-watch")

    def test_watchdogs_module_is_ok_when_projection_reports_no_stale_rows(self) -> None:
        sources = healthy_sources()
        sources["watchdogs"] = wrapped([
            {"id": "alpha-aps-thermal-watch", "host": "alpha", "status": "active",
             "label": "thermal", "status_detail": "ok"},
            {"id": "delta-systemd-tower", "host": "delta", "status": "retired",
             "label": "Tower (retired secondary)", "status_detail": "retired"},
        ])
        result = build_system_status(sources, now=NOW)
        module = next(row for row in result["modules"] if row["id"] == "watchdogs")
        self.assertEqual(module["status"], "ok")
        self.assertEqual(module["summary"], "2 mechanisms · 0 flagged")


class SystemStatusRouteTests(unittest.TestCase):
    @staticmethod
    def client() -> TestClient:
        app = FastAPI()
        app.include_router(routes.router)
        return TestClient(app)

    def test_api_exposes_bounded_registry_contract(self) -> None:
        response = self.client().get("/api/system-status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["module_count"], len(MODULE_ORDER))
        self.assertEqual(tuple(row["id"] for row in body["modules"]), MODULE_ORDER)
        serialized = response.text.lower()
        for forbidden in ("p256dh", '"auth"', '"endpoint"', '"prompt"', '"output"'):
            self.assertNotIn(forbidden, serialized)

    def test_detail_page_has_fleet_header_four_lenses_and_rollup(self) -> None:
        response = self.client().get("/system-status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>fleet status · Nexus</title>", response.text)
        self.assertIn('href="/system-status" data-system-status', response.text)
        self.assertIn("<h2>fleet status</h2>", response.text)
        self.assertIn('aria-label="Status views"', response.text)
        for lens in ("overview", "systems", "governance", "services"):
            self.assertIn(f'href="/system-status?tab={lens}"', response.text)
        self.assertIn('<h3 id="statusLensTitle">overview</h3>', response.text)
        self.assertIn("What needs attention", response.text)
        self.assertIn('src="/static/app_shell.js?v=', response.text)

    def test_status_lenses_partition_every_module_exactly_once(self) -> None:
        rendered = {
            lens: self.client().get(f"/system-status?tab={lens}")
            for lens in ("systems", "governance", "services")
        }
        for response in rendered.values():
            self.assertEqual(response.status_code, 200)

        for module_id in MODULE_ORDER:
            occurrences = sum(
                response.text.count(f'id="module-{module_id}"')
                for response in rendered.values()
            )
            self.assertEqual(occurrences, 1, module_id)

        self.assertIn('id="module-nexus-runtime"', rendered["systems"].text)
        self.assertIn('id="module-conformance"', rendered["governance"].text)
        self.assertIn('id="module-notifications"', rendered["services"].text)

    def test_invalid_status_lens_redirects_to_overview(self) -> None:
        response = self.client().get("/system-status?tab=retired", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/system-status?tab=overview")


if __name__ == "__main__":
    unittest.main()
