"""
Focused stdlib tests for HEALTH-TIMELINE-2: the additive v2 history schema
(models.StatusSnapshot.history_line), the 24h projection module
(health_timeline.project_health_timeline), the new bounded /api/health-timeline
route, and the dashboard's Health Timeline DOM/CSS/JS contract.

These pin: v1 fields (t/overall/nodes/ms) are byte-identical in shape to
before; v2 additions (sample_version/issues/metrics) are bounded and never
carry detail/target/source_host; a v1 row (no sample_version) still projects
correctly with cause=None; incident grouping treats a WARN->CRIT run as ONE
incident; streak/healthy-%/cadence-gap/duration-percentile/correlated-
transition math; the endpoint never probes and clamps its `hours` param; and
the dashboard template/CSS/JS carry the required markers (readable subtitle,
opacity:1 base for the dynamically-rendered body, a narrow-viewport rule, and
the central-poll-controller-only wiring for the timeline's fetch).
"""
from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import health_timeline, routes
from .models import Health, NodeStatus, ProbeResult, StatusSnapshot

REPO_ROOT = Path(__file__).resolve().parent.parent


def _snapshot(node_probes: dict[str, list[ProbeResult]]) -> StatusSnapshot:
    nodes = [NodeStatus(name=name, probes=probes) for name, probes in node_probes.items()]
    snap = StatusSnapshot(nodes=nodes)
    snap.recompute_rollups()
    return snap


class HistoryLineV2Tests(unittest.TestCase):
    def test_v1_fields_unchanged_in_shape(self):
        snap = _snapshot({
            "charlie": [ProbeResult(node="charlie", kind="ping", health=Health.OK, value="12 ms", latency_ms=12)],
        })
        line = snap.history_line()
        self.assertEqual(set(("t", "overall", "nodes", "ms")) - set(line), set())
        self.assertEqual(line["overall"], "ok")
        self.assertEqual(line["nodes"], {"charlie": "ok"})
        self.assertEqual(line["ms"], 0)
        self.assertIsInstance(line["t"], str)

    def test_v2_additive_fields_present(self):
        snap = _snapshot({"charlie": [ProbeResult(node="charlie", kind="ping", health=Health.OK)]})
        line = snap.history_line()
        self.assertEqual(line["sample_version"], 2)
        self.assertIn("issues", line)
        self.assertIn("metrics", line)

    def test_ok_probes_never_appear_in_issues(self):
        snap = _snapshot({"charlie": [ProbeResult(node="charlie", kind="ping", health=Health.OK, value="ok")]})
        line = snap.history_line()
        self.assertNotIn("charlie", line["issues"])

    def test_non_ok_issue_is_bounded_and_excludes_sensitive_fields(self):
        probe = ProbeResult(
            node="charlie", kind="disk", health=Health.CRIT, value="97%",
            detail="/ used on charlie.tailnet — df -P / | tail -1",
            source_host="charlie", target="charlie:22", method="df",
            error_class="disk_crit",
        )
        snap = _snapshot({"charlie": [probe]})
        line = snap.history_line()
        issue = line["issues"]["charlie"][0]
        self.assertEqual(set(issue), {"kind", "health", "value", "method", "error_class"})
        self.assertEqual(issue["kind"], "disk")
        self.assertEqual(issue["health"], "crit")
        self.assertEqual(issue["value"], "97%")
        self.assertEqual(issue["method"], "df")
        self.assertEqual(issue["error_class"], "disk_crit")
        self.assertNotIn("detail", issue)
        self.assertNotIn("target", issue)
        self.assertNotIn("source_host", issue)

    def test_issue_fields_are_length_bounded(self):
        long_value = "x" * 500
        probe = ProbeResult(node="charlie", kind="disk", health=Health.WARN, value=long_value)
        snap = _snapshot({"charlie": [probe]})
        line = snap.history_line()
        issue = line["issues"]["charlie"][0]
        from .models import _HISTORY_FIELD_MAX_LEN
        self.assertLessEqual(len(issue["value"]), _HISTORY_FIELD_MAX_LEN)

    def test_issues_capped_per_node(self):
        from .models import HISTORY_MAX_ISSUES_PER_NODE
        probes = [
            ProbeResult(node="charlie", kind=f"probe{i}", health=Health.WARN, value="x")
            for i in range(HISTORY_MAX_ISSUES_PER_NODE + 5)
        ]
        snap = _snapshot({"charlie": probes})
        line = snap.history_line()
        self.assertEqual(len(line["issues"]["charlie"]), HISTORY_MAX_ISSUES_PER_NODE)

    def test_metrics_extract_latency_and_disk_mem_percent(self):
        probes = [
            ProbeResult(node="charlie", kind="ping", health=Health.OK, value="12 ms", latency_ms=12),
            ProbeResult(node="charlie", kind="disk", health=Health.OK, value="41%"),
            ProbeResult(node="charlie", kind="mem", health=Health.WARN, value="88%"),
        ]
        snap = _snapshot({"charlie": probes})
        metrics = snap.history_line()["metrics"]["charlie"]
        by_label = {m["label"]: m for m in metrics}
        self.assertEqual(by_label["ping"]["ms"], 12)
        self.assertEqual(by_label["disk"]["pct"], 41)
        self.assertEqual(by_label["mem"]["pct"], 88)

    def test_metrics_capped_per_node(self):
        from .models import HISTORY_MAX_METRICS_PER_NODE
        probes = [
            ProbeResult(node="charlie", kind=f"p{i}", health=Health.OK, latency_ms=i)
            for i in range(HISTORY_MAX_METRICS_PER_NODE + 5)
        ]
        snap = _snapshot({"charlie": probes})
        metrics = snap.history_line()["metrics"]["charlie"]
        self.assertEqual(len(metrics), HISTORY_MAX_METRICS_PER_NODE)

    def test_probe_with_no_latency_or_percent_yields_no_metric(self):
        probe = ProbeResult(node="charlie", kind="tunnel", health=Health.OK, value="up · 3 conn")
        snap = _snapshot({"charlie": [probe]})
        metrics = snap.history_line()["metrics"]
        self.assertNotIn("charlie", metrics)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _rows(now: datetime, specs: list[tuple[int, dict, int]], extra: dict | None = None) -> list[dict]:
    """specs: list of (minutes_before_now, {node: state}, duration_ms)."""
    out = []
    for mins_ago, nodes, ms in specs:
        t = now - timedelta(minutes=mins_ago)
        overall = "crit" if "crit" in nodes.values() else "warn" if "warn" in nodes.values() else (
            "unknown" if "unknown" in nodes.values() else "ok"
        )
        row = {"t": _iso(t), "overall": overall, "nodes": dict(nodes), "ms": ms}
        if extra:
            row.update(extra)
        out.append(row)
    return out


NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


class ProjectionEmptyAndCompatTests(unittest.TestCase):
    def test_empty_rows_returns_safe_defaults(self):
        proj = health_timeline.project_health_timeline([], now=NOW)
        self.assertEqual(proj["received_samples"], 0)
        self.assertEqual(proj["nodes"], {})
        self.assertEqual(proj["overall_current"], "unknown")

    def test_v1_rows_without_sample_version_still_project(self):
        specs = [(mins, {"charlie": "ok"}, 1000) for mins in (20, 15, 10, 5, 0)]
        rows = _rows(NOW, specs)  # no sample_version/issues/metrics keys at all
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        self.assertEqual(proj["nodes"]["charlie"]["current_state"], "ok")
        self.assertIsNone(proj["nodes"]["charlie"]["last_incident"])

    def test_v1_incident_cause_not_retained(self):
        specs = [(10, {"charlie": "ok"}, 1000), (5, {"charlie": "crit"}, 1000), (0, {"charlie": "ok"}, 1000)]
        rows = _rows(NOW, specs)
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        inc = proj["nodes"]["charlie"]["last_incident"]
        self.assertIsNotNone(inc)
        self.assertIsNone(inc["cause"])

    def test_v2_incident_cause_retained(self):
        specs = [(10, {"charlie": "ok"}, 1000), (5, {"charlie": "crit"}, 1000), (0, {"charlie": "ok"}, 1000)]
        rows = _rows(NOW, specs, extra={"sample_version": 2, "metrics": {}})
        rows[1]["issues"] = {"charlie": [{"kind": "disk", "health": "crit", "value": "97%",
                                          "method": "df", "error_class": "disk_crit"}]}
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        inc = proj["nodes"]["charlie"]["last_incident"]
        self.assertEqual(inc["cause"]["kind"], "disk")
        self.assertEqual(inc["cause"]["value"], "97%")


class IncidentGroupingTests(unittest.TestCase):
    def test_warn_then_crit_is_one_incident(self):
        specs = [
            (20, {"charlie": "ok"}, 1000), (15, {"charlie": "warn"}, 1000),
            (10, {"charlie": "crit"}, 1000), (5, {"charlie": "ok"}, 1000), (0, {"charlie": "ok"}, 1000),
        ]
        rows = _rows(NOW, specs)
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        node = proj["nodes"]["charlie"]
        self.assertEqual(node["incident_count"], 1)
        self.assertEqual(node["last_incident"]["peak_state"], "crit")
        self.assertTrue(node["last_incident"]["recovered"])

    def test_ongoing_incident_not_recovered(self):
        specs = [(10, {"charlie": "ok"}, 1000), (5, {"charlie": "warn"}, 1000), (0, {"charlie": "crit"}, 1000)]
        rows = _rows(NOW, specs)
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        inc = proj["nodes"]["charlie"]["last_incident"]
        self.assertFalse(inc["recovered"])
        self.assertIsNone(inc["recovered_at"])

    def test_current_streak_and_since(self):
        specs = [
            (20, {"charlie": "warn"}, 1000), (15, {"charlie": "ok"}, 1000),
            (10, {"charlie": "ok"}, 1000), (5, {"charlie": "ok"}, 1000), (0, {"charlie": "ok"}, 1000),
        ]
        rows = _rows(NOW, specs)
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        node = proj["nodes"]["charlie"]
        self.assertEqual(node["current_streak_samples"], 4)
        self.assertEqual(node["current_state_since"], rows[1]["t"])

    def test_healthy_pct_and_counts_by_state(self):
        specs = [(mins, {"charlie": s}, 1000) for mins, s in
                 [(15, "ok"), (10, "ok"), (5, "warn"), (0, "crit")]]
        rows = _rows(NOW, specs)
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        node = proj["nodes"]["charlie"]
        self.assertEqual(node["healthy_pct"], 50.0)
        self.assertEqual(node["counts_by_state"], {"ok": 2, "warn": 1, "crit": 1, "unknown": 0})


class CadenceAndDurationTests(unittest.TestCase):
    def test_gap_counted_when_samples_missed(self):
        rows = _rows(NOW, [(40, {"charlie": "ok"}, 1000), (35, {"charlie": "ok"}, 1000),
                            (5, {"charlie": "ok"}, 1000), (0, {"charlie": "ok"}, 1000)])
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        self.assertGreaterEqual(proj["gap_count"], 1)

    def test_no_gap_at_normal_cadence(self):
        rows = _rows(NOW, [(15, {"charlie": "ok"}, 1000), (10, {"charlie": "ok"}, 1000),
                            (5, {"charlie": "ok"}, 1000), (0, {"charlie": "ok"}, 1000)])
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        self.assertEqual(proj["gap_count"], 0)

    def test_duration_stats(self):
        durations = [1000, 1200, 1500, 2000, 16000]
        specs = [(20 - i * 5, {"charlie": "ok"}, ms) for i, ms in enumerate(durations)]
        rows = _rows(NOW, specs)
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        dm = proj["duration_ms"]
        self.assertEqual(dm["current"], 16000)
        self.assertEqual(dm["median"], 1500)
        self.assertEqual(dm["max"], 16000)
        self.assertEqual(dm["p95"], 16000)

    def test_scan_freshness(self):
        fresh_rows = _rows(NOW, [(0, {"charlie": "ok"}, 1000)])
        stale_rows = _rows(NOW, [(20, {"charlie": "ok"}, 1000)])
        fresh = health_timeline.project_health_timeline(fresh_rows, now=NOW, cadence_seconds=300)
        stale = health_timeline.project_health_timeline(stale_rows, now=NOW, cadence_seconds=300)
        self.assertTrue(fresh["scan_is_fresh"])
        self.assertFalse(stale["scan_is_fresh"])


class CorrelatedTransitionTests(unittest.TestCase):
    def test_two_nodes_entering_non_ok_same_sample_is_correlated(self):
        rows = _rows(NOW, [
            (10, {"charlie": "ok", "delta": "ok"}, 1000),
            (5, {"charlie": "crit", "delta": "crit"}, 1000),
            (0, {"charlie": "ok", "delta": "ok"}, 1000),
        ])
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        self.assertEqual(len(proj["correlated_incidents"]), 1)
        self.assertEqual(set(proj["correlated_incidents"][0]["nodes"]), {"charlie", "delta"})

    def test_single_node_transition_is_not_correlated(self):
        rows = _rows(NOW, [
            (10, {"charlie": "ok", "delta": "ok"}, 1000),
            (5, {"charlie": "crit", "delta": "ok"}, 1000),
            (0, {"charlie": "ok", "delta": "ok"}, 1000),
        ])
        proj = health_timeline.project_health_timeline(rows, now=NOW, cadence_seconds=300)
        self.assertEqual(proj["correlated_incidents"], [])


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class HealthTimelineEndpointTests(unittest.TestCase):
    def test_endpoint_never_reads_more_than_bounded_history(self):
        with patch.object(routes, "read_history", return_value=[]) as spy:
            resp = _client().get("/api/health-timeline", params={"hours": 24})
        self.assertEqual(resp.status_code, 200)
        self.assertLessEqual(spy.call_args.kwargs["limit"], routes.MAX_HISTORY_ROWS)
        self.assertIn("received_samples", resp.json())

    def test_hours_param_is_clamped(self):
        with patch.object(routes, "read_history", return_value=[]) as spy:
            resp_big = _client().get("/api/health-timeline", params={"hours": 99999})
            resp_small = _client().get("/api/health-timeline", params={"hours": 0})
            self.assertTrue(all(c.kwargs["limit"] <= routes.MAX_HISTORY_ROWS for c in spy.call_args_list))
        self.assertEqual(resp_big.json()["hours"], 168)
        self.assertEqual(resp_small.json()["hours"], 1)

    def test_api_history_contract_is_still_a_plain_list(self):
        resp = _client().get("/api/history", params={"limit": 5})
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), list)


class DashboardMarkupContractTests(unittest.TestCase):
    """String/regex contract checks on the actual template/CSS/JS files —
    deliberately NOT a byte-exact hash pin (this module's markup legitimately
    evolves), just the specific guarantees HEALTH-TIMELINE-2 promised."""

    def setUp(self):
        self.html = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        self.css = (REPO_ROOT / "static" / "dashboard.css").read_text()
        self.js = (REPO_ROOT / "static" / "dashboard.js").read_text()

    def test_subtitle_is_readable_not_rune_garbled(self):
        self.assertNotIn('class="sub rune">last 24h', self.html)
        self.assertIn("timelineSummary", self.html)
        self.assertIn("timelineStrip", self.html)

    def test_timeline_body_defaults_to_visible_opacity(self):
        self.assertRegex(self.css, r"\.timeline-strip\{[^}]*opacity:1")

    def test_timeline_strip_not_wired_to_scroll_linked_reveal(self):
        # panel-reveal's animation-timeline:view() backwards-fills opacity 0
        # before the scroll-linked entry point — never attach it to a
        # container this module replaces via innerHTML on every poll.
        self.assertNotIn('class="timeline-strip panel-reveal"', self.html)

    def test_narrow_viewport_rule_present(self):
        self.assertRegex(self.css, r"@media\s*\(max-width:\s*40\dpx\)")

    def test_timeline_load_is_only_driven_by_central_controller(self):
        # Exactly one assignment (the module's own IIFE); the central
        # polling controller elsewhere reads it back via timeline.load().
        self.assertEqual(self.js.count("window.__NEXUS_TIMELINE__ = {load: load};"), 1)
        self.assertIn("timeline.load(signal)", self.js)
        # No independent setInterval was introduced anywhere near the module.
        start = self.js.index("Health timeline: 24h fleet/per-node")
        end = self.js.index("window.__NEXUS_TIMELINE__ = {load: load};", start)
        self.assertNotIn("setInterval(", self.js[start:end])


if __name__ == "__main__":
    unittest.main()
