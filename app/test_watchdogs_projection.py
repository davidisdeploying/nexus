"""
Focused tests for the cadence-aware watchdog projection layer
(app/watchdogs_projection.py, FLEET-AUTO-BUILD-20260802-panel-live-
watchdog-evidence): cadence aging thresholds for the four scheduler-receipt-
backed rows, explicit non-ok/missing receipts, weekly self-test pass/fail/
stale evidence, retired-row exemption from overlay, and non-overlaid static
rows passing through unchanged.

Isolation mirrors app/test_health_watch.py: a temp-file-backed notify_store DB
via DB_PATH monkeypatching -- no real events.db is ever touched.
"""
from __future__ import annotations

import tempfile
import unittest

from . import watchdogs_registry as _wdreg
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notify_store, watchdogs_projection as wp
from .watchdogs_registry import REGISTRY


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class ProjectionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = Path(self._tmpdir.name) / "events.db"
        notify_store.init_db()
        wp.get_projected_registry(force=True)  # warm/reset cache against new DB

    def tearDown(self):
        notify_store.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def _row(self, row_id: str) -> dict:
        rows = wp.get_projected_registry(force=True)
        return next(r for r in rows if r["id"] == row_id)


class CadenceOverlayTests(ProjectionTestCase):
    def test_thermal_watch_ok_receipt_within_3min_is_active(self):
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "thermal-watch", outcome="ok", completed_at=_iso(now - timedelta(seconds=30)),
        )
        row = self._row("alpha-aps-thermal-watch")
        self.assertEqual(row["status"], "active")

    def test_thermal_watch_ok_receipt_over_3min_is_stale(self):
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "thermal-watch", outcome="ok", completed_at=_iso(now - timedelta(minutes=4)),
        )
        row = self._row("alpha-aps-thermal-watch")
        self.assertEqual(row["status"], "stale_evidence")
        self.assertIn("240", row["status_detail"])

    def test_health_watch_ok_receipt_within_3min_is_active(self):
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "health-watch", outcome="ok", completed_at=_iso(now - timedelta(seconds=90)),
        )
        row = self._row("alpha-aps-health-watch")
        self.assertEqual(row["status"], "active")

    def test_conformance_watch_ok_receipt_within_15min_is_active(self):
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "conformance-watch", outcome="ok", completed_at=_iso(now - timedelta(minutes=10)),
        )
        row = self._row("alpha-aps-conformance-watch")
        self.assertEqual(row["status"], "active")

    def test_conformance_watch_ok_receipt_over_15min_is_stale(self):
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "conformance-watch", outcome="ok", completed_at=_iso(now - timedelta(minutes=20)),
        )
        row = self._row("alpha-aps-conformance-watch")
        self.assertEqual(row["status"], "stale_evidence")

    def test_deadman_ping_ok_receipt_within_15min_is_active(self):
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "deadman-ping", outcome="ok", completed_at=_iso(now - timedelta(minutes=5)),
        )
        row = self._row("alpha-aps-deadman-ping")
        self.assertEqual(row["status"], "active")

    def test_missing_receipt_is_stale_not_a_crash(self):
        row = self._row("alpha-aps-deadman-ping")
        self.assertEqual(row["status"], "stale_evidence")
        self.assertIn("No scheduler execution receipt", row["status_detail"])

    def test_explicit_non_ok_receipt_is_stale_regardless_of_age(self):
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "health-watch", outcome="error", completed_at=_iso(now),
            detail="collector raised TypeError",
        )
        row = self._row("alpha-aps-health-watch")
        self.assertEqual(row["status"], "stale_evidence")
        self.assertIn("non-ok", row["status_detail"])
        self.assertIn("collector raised TypeError", row["status_detail"])

    def test_missed_receipt_is_stale(self):
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "thermal-watch", outcome="missed", completed_at=_iso(now),
        )
        row = self._row("alpha-aps-thermal-watch")
        self.assertEqual(row["status"], "stale_evidence")

    def test_no_fired_edge_is_not_evidence_of_failure(self):
        """The whole point of this build: a receipt whose payload shows no
        alarm/action edge fired is STILL 'ok' -- edge-triggered watchers are
        healthy by default when they complete without error."""
        now = datetime.now(timezone.utc)
        notify_store.record_scheduler_receipt(
            "conformance-watch", outcome="ok", completed_at=_iso(now),
            detail="checks=0, cache=fresh, no transitions",
        )
        row = self._row("alpha-aps-conformance-watch")
        self.assertEqual(row["status"], "active")


class SelftestOverlayTests(ProjectionTestCase):
    def _seed_selftest(self, *, age: timedelta, sent_pwa: bool, sent_ntfy: bool):
        row_id = notify_store.insert_notification(
            event_key="selftest:test", channel="nexus-selftest",
            prio=3, title="t", body=None,
        )["id"]
        notify_store.update_notification_pwa(row_id, sent_pwa)
        notify_store.update_notification_ntfy(row_id, sent_ntfy)
        conn = notify_store._db()
        try:
            conn.execute(
                "UPDATE notification_log SET created_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - age).isoformat(), row_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_recent_pass_both_transports_is_active(self):
        self._seed_selftest(age=timedelta(days=1), sent_pwa=True, sent_ntfy=True)
        row = self._row("alpha-aps-nexus-selftest")
        self.assertEqual(row["status"], "active")

    def test_recent_but_one_transport_failed_is_stale(self):
        self._seed_selftest(age=timedelta(days=1), sent_pwa=True, sent_ntfy=False)
        row = self._row("alpha-aps-nexus-selftest")
        self.assertEqual(row["status"], "stale_evidence")
        self.assertIn("ntfy=FAILED", row["status_detail"])

    def test_older_than_8_days_is_stale(self):
        self._seed_selftest(age=timedelta(days=9), sent_pwa=True, sent_ntfy=True)
        row = self._row("alpha-aps-nexus-selftest")
        self.assertEqual(row["status"], "stale_evidence")

    def test_no_receipt_at_all_is_stale(self):
        row = self._row("alpha-aps-nexus-selftest")
        self.assertEqual(row["status"], "stale_evidence")
        self.assertIn("No weekly self-test", row["status_detail"])


class RetiredAndPassthroughTests(ProjectionTestCase):
    def test_retired_delta_tower_row_never_flagged(self):
        row = self._row("delta-systemd-tower")
        self.assertEqual(row["status"], "retired")
        summary = wp.projected_summary()
        delta = next(h for h in summary["hosts"] if h["host"] == "delta")
        self.assertEqual(delta["flagged"], 0)

    def test_retired_gallery_faces_resume_watcher_never_flagged(self):
        row = self._row("charlie-gallery-faces-resume-watcher")
        self.assertEqual(row["status"], "retired")

    def test_non_overlaid_static_row_passes_through_unchanged(self):
        static_row = next(r for r in REGISTRY if r["id"] == "charlie-thermal-governor")
        projected_row = self._row("charlie-thermal-governor")
        self.assertEqual(projected_row, static_row)

    def test_all_rows_still_present(self):
        rows = wp.get_projected_registry(force=True)
        self.assertEqual(len(rows), sum(_wdreg._EXPECTED_HOST_COUNTS.values()))

    def test_host_filter_applies_after_projection(self):
        rows = wp.get_projected_registry(host="alpha", force=True)
        self.assertEqual(len(rows), _wdreg._EXPECTED_HOST_COUNTS["alpha"])
        self.assertTrue(all(r["host"] == "alpha" for r in rows))


if __name__ == "__main__":
    unittest.main()
