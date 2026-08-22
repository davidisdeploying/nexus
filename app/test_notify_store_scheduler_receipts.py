"""
Focused tests for the scheduler_job_receipt table added to app/notify_store.py
(FLEET-AUTO-BUILD-20260802-panel-live-watchdog-evidence): idempotent schema
creation, one-latest-row-per-job-id upsert semantics, and graceful behavior
when the table has not been created yet (a bare test app with no lifespan).

Isolation mirrors app/test_health_watch.py: a temp-file-backed notify_store DB
via DB_PATH monkeypatching -- no real events.db is ever touched.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import notify_store


class SchedulerReceiptSchemaTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = Path(self._tmpdir.name) / "events.db"

    def tearDown(self):
        notify_store.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def test_init_db_is_idempotent(self):
        notify_store.init_db()
        notify_store.init_db()
        notify_store.init_db()
        conn = sqlite3.connect(notify_store.DB_PATH)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        finally:
            conn.close()
        self.assertIn("scheduler_job_receipt", names)

    def test_missing_table_yields_none_and_empty_list_not_an_exception(self):
        # DB file/table deliberately never created -- get_scheduler_receipt
        # must degrade gracefully, same discipline as list_notifications.
        self.assertIsNone(notify_store.get_scheduler_receipt("thermal-watch"))
        self.assertEqual(notify_store.list_scheduler_receipts(), [])
        self.assertIsNone(notify_store.get_last_selftest_receipt())


class SchedulerReceiptAccessorTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = Path(self._tmpdir.name) / "events.db"
        notify_store.init_db()

    def tearDown(self):
        notify_store.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def test_record_and_get_roundtrip(self):
        notify_store.record_scheduler_receipt(
            "thermal-watch", outcome="ok",
            completed_at="2026-08-02T10:00:00+00:00",
            scheduled_at="2026-08-02T09:59:00+00:00", detail="fine",
        )
        row = notify_store.get_scheduler_receipt("thermal-watch")
        self.assertEqual(row["job_id"], "thermal-watch")
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["completed_at"], "2026-08-02T10:00:00+00:00")
        self.assertEqual(row["detail"], "fine")

    def test_second_write_upserts_the_single_latest_row(self):
        notify_store.record_scheduler_receipt(
            "health-watch", outcome="ok", completed_at="2026-08-02T10:00:00+00:00",
        )
        notify_store.record_scheduler_receipt(
            "health-watch", outcome="error", completed_at="2026-08-02T10:01:00+00:00",
            detail="boom",
        )
        rows = [r for r in notify_store.list_scheduler_receipts() if r["job_id"] == "health-watch"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "error")
        self.assertEqual(rows[0]["detail"], "boom")

    def test_unknown_job_id_returns_none(self):
        self.assertIsNone(notify_store.get_scheduler_receipt("nonexistent-job"))

    def test_detail_is_bounded(self):
        notify_store.record_scheduler_receipt(
            "conformance-watch", outcome="ok",
            completed_at="2026-08-02T10:00:00+00:00", detail="x" * 5000,
        )
        row = notify_store.get_scheduler_receipt("conformance-watch")
        self.assertLessEqual(len(row["detail"]), notify_store._RECEIPT_DETAIL_LIMIT)

    def test_invalid_outcome_is_rejected(self):
        with self.assertRaises(ValueError):
            notify_store.record_scheduler_receipt(
                "deadman-ping", outcome="not-a-real-outcome",
                completed_at="2026-08-02T10:00:00+00:00",
            )

    def test_get_last_selftest_receipt_reads_latest_nexus_selftest_row(self):
        notify_store.insert_notification(
            event_key="selftest:2026-07-27", channel="nexus-selftest",
            prio=3, title="old", body=None,
        )
        row_id = notify_store.insert_notification(
            event_key="selftest:2026-08-03", channel="nexus-selftest",
            prio=3, title="new", body=None,
        )["id"]
        notify_store.update_notification_pwa(row_id, True)
        notify_store.update_notification_ntfy(row_id, True)
        receipt = notify_store.get_last_selftest_receipt()
        self.assertTrue(receipt["sent_pwa"])
        self.assertTrue(receipt["sent_ntfy"])


if __name__ == "__main__":
    unittest.main()
