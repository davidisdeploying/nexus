"""
Focused tests for the PANEL-4 static watchdogs registry (app/watchdogs_registry.py).

Covers: schema completeness, unique stable ids, exact per-host counts
(per-host counts come from wd._EXPECTED_HOST_COUNTS, never a literal here), the explicit alpha/delta Tower
primary-vs-orphaned-secondary distinction, the two PANEL-2-repaired
host-probe rows reflecting REPAIRED (not stale/orphaned) status, and
retired-by-design semantics for the one-shot gallery-faces-resume-watcher.
"""
from __future__ import annotations

import unittest

from . import watchdogs_registry as wd


class RegistrySchemaTests(unittest.TestCase):
    def test_total_row_count_matches_expected_host_counts(self):
        self.assertEqual(len(wd.REGISTRY), sum(wd._EXPECTED_HOST_COUNTS.values()))

    def test_exact_per_host_counts(self):
        counts = {}
        for row in wd.REGISTRY:
            counts[row["host"]] = counts.get(row["host"], 0) + 1
        # Assert against the module constant, not a second copy of the numbers.
        # Those two copies drifted apart: the module read 9/9 while this read
        # 8/10, so the guard that should have caught a moved service was itself
        # broken. _validate() enforces the constant against the data at import,
        # so the constant remains the one place a human edits.
        self.assertEqual(counts, wd._EXPECTED_HOST_COUNTS)

    def test_ids_are_unique(self):
        ids = [row["id"] for row in wd.REGISTRY]
        self.assertEqual(len(ids), len(set(ids)))

    def test_all_kinds_are_watch_or_guard(self):
        for row in wd.REGISTRY:
            with self.subTest(id=row["id"]):
                self.assertIn(row["kind"], {"watch", "guard"})

    def test_all_statuses_are_recognized(self):
        for row in wd.REGISTRY:
            with self.subTest(id=row["id"]):
                self.assertIn(row["status"], wd.WATCHDOG_STATUS_VALUES)

    def test_every_row_has_all_required_fields_nonempty(self):
        required = (
            "id", "kind", "owner", "host", "label", "source", "protected_target",
            "cadence_timeout", "last_check_evidence", "last_action_evidence",
            "status", "status_detail", "source_of_truth", "evidence_as_of",
        )
        for row in wd.REGISTRY:
            for field in required:
                with self.subTest(id=row["id"], field=field):
                    self.assertTrue(row.get(field), f"{row['id']}.{field} is empty/missing")

    def test_duplicate_id_is_rejected_by_validator(self):
        bad = wd.ALPHA_ROWS[:1] + wd.ALPHA_ROWS[:1]
        with self.assertRaises(ValueError):
            wd._validate(bad)

    def test_wrong_host_count_is_rejected_by_validator(self):
        with self.assertRaises(ValueError):
            wd._validate(wd.ALPHA_ROWS)  # 11 rows, all host=alpha -> fails full-count check


class TowerDistinctionTests(unittest.TestCase):
    """The two alpha/delta tower.service rows must stay separate,
    non-deduped, and clearly labeled primary vs. orphaned secondary."""

    def test_both_tower_rows_present_and_distinct_ids(self):
        ids = {row["id"] for row in wd.REGISTRY}
        self.assertIn("alpha-systemd-tower", ids)
        self.assertIn("delta-systemd-tower", ids)

    def test_alpha_tower_labeled_primary(self):
        row = next(r for r in wd.REGISTRY if r["id"] == "alpha-systemd-tower")
        self.assertIn("primary", row["label"].lower())

    def test_delta_tower_labeled_retired_secondary_no_ingress(self):
        row = next(r for r in wd.REGISTRY if r["id"] == "delta-systemd-tower")
        self.assertIn("retired", row["label"].lower())
        self.assertIn("no ingress", row["label"].lower())
        self.assertEqual(row["status"], "retired")
        self.assertIn("delta", row["last_action_evidence"].lower())
        self.assertIn("tower-retirement-20260802t225000z", row["last_action_evidence"].lower())


class RepairedHostProbeTests(unittest.TestCase):
    """PANEL-2 repaired both hosts' dead-path host-probe findings same-day;
    the registry must reflect the repaired state, not the original recon's
    stale/orphaned finding."""

    def test_charlie_host_probe_reflects_repair_not_dead_path(self):
        row = next(r for r in wd.REGISTRY if r["id"] == "charlie-fleet-host-probe")
        self.assertEqual(row["status"], "active")
        self.assertNotIn("library", row["source"])
        self.assertIn("homelab-vault", row["source"])
        self.assertIn("repaired", row["last_action_evidence"].lower())

    def test_delta_host_probe_is_a_supervised_unit_not_cron(self):
        """2026-08-03: the cron @reboot mechanism was retired. The process it
        was credited with supervising turned out to be an unsupervised manual
        leftover in a closing login session, and once a unit existed the cron
        line would have started a second writer on the same file at boot."""
        row = next(r for r in wd.REGISTRY if r["id"] == "delta-systemd-host-probe")
        self.assertEqual(row["status"], "active")
        self.assertIn("fleet-host-probe.service", row["source"])
        self.assertNotIn("crontab @reboot", row["source"])
        self.assertNotIn("library", row["source"])

    def test_no_host_probe_row_still_claims_a_cron_mechanism(self):
        for row in wd.REGISTRY:
            if "host-probe" in row["id"] or "host_probe" in row.get("source", ""):
                self.assertNotIn("crontab @reboot", row["source"])


class RetiredByDesignTests(unittest.TestCase):
    def test_gallery_faces_resume_watcher_is_retired_not_failed(self):
        row = next(r for r in wd.REGISTRY if r["id"] == "charlie-gallery-faces-resume-watcher")
        self.assertEqual(row["status"], "retired")
        self.assertIn("by design", row["status_detail"].lower())


class TempleLinkForensicsTests(unittest.TestCase):
    def test_three_link_forensics_mechanisms_are_registered_active(self):
        ids = {
            "charlie-temple-link-observer",
            "charlie-temple-link-watch-ensure",
            "temple-local-link-forensics",
        }
        rows = {row["id"]: row for row in wd.REGISTRY if row["id"] in ids}
        self.assertEqual(set(rows), ids)
        self.assertTrue(all(row["status"] == "active" for row in rows.values()))
        self.assertIn("carrier_changes", rows["temple-local-link-forensics"]["last_check_evidence"])
        self.assertIn("pid 21036", rows["charlie-temple-link-watch-ensure"]["last_action_evidence"].lower())


class RegistryAccessTests(unittest.TestCase):
    def test_get_registry_no_filter_returns_all(self):
        self.assertEqual(len(wd.get_registry()), sum(wd._EXPECTED_HOST_COUNTS.values()))

    def test_get_registry_host_filter(self):
        for host, count in wd._EXPECTED_HOST_COUNTS.items():
            self.assertEqual(len(wd.get_registry(host)), count, host)

    def test_get_registry_unknown_host_returns_empty(self):
        self.assertEqual(wd.get_registry("nonexistent-host"), [])

    def test_get_registry_returns_a_copy_not_the_live_list(self):
        rows = wd.get_registry()
        rows.append({"id": "injected"})
        self.assertEqual(len(wd.REGISTRY), sum(wd._EXPECTED_HOST_COUNTS.values()))

    def test_summary_hosts_and_total(self):
        s = wd.summary()
        self.assertEqual(s["total"], sum(wd._EXPECTED_HOST_COUNTS.values()))
        by_host = {h["host"]: h["count"] for h in s["hosts"]}
        self.assertEqual(by_host, wd._EXPECTED_HOST_COUNTS)


if __name__ == "__main__":
    unittest.main()
