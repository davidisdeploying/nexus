"""
Focused tests for the APScheduler execution-receipt listener added to
app/scheduler.py (FLEET-AUTO-BUILD-20260802-panel-live-watchdog-evidence):
EVENT_JOB_EXECUTED/EVENT_JOB_ERROR/EVENT_JOB_MISSED classification into
ok/error/missed, the "ok unless the return explicitly says ok=False" default,
persistence-failure isolation (a broken write must not crash the scheduler),
and idempotent listener registration.

Events are constructed directly (apscheduler.events.JobExecutionEvent) and
fed straight to the module-level _on_job_event callback -- no real
AsyncIOScheduler tick is needed to exercise the classification/persistence
logic, mirroring app/test_conformance_watch.py's isolation style.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED, JobExecutionEvent

from . import notify_store, scheduler


def _event(code, job_id="thermal-watch", retval=None, exception=None):
    return JobExecutionEvent(
        code, job_id, "default",
        datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        retval=retval, exception=exception,
    )


class ReceiptClassificationAndPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = Path(self._tmpdir.name) / "events.db"
        notify_store.init_db()

    def tearDown(self):
        notify_store.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    def test_executed_with_no_structured_signal_is_ok(self):
        scheduler._on_job_event(_event(EVENT_JOB_EXECUTED, retval=None))
        row = notify_store.get_scheduler_receipt("thermal-watch")
        self.assertEqual(row["outcome"], "ok")

    def test_executed_with_dict_retval_and_no_ok_key_is_ok(self):
        scheduler._on_job_event(_event(EVENT_JOB_EXECUTED, retval={"fired": False}))
        row = notify_store.get_scheduler_receipt("thermal-watch")
        self.assertEqual(row["outcome"], "ok")

    def test_executed_with_explicit_ok_false_is_error(self):
        scheduler._on_job_event(_event(
            EVENT_JOB_EXECUTED, job_id="deadman-ping",
            retval={"ok": False, "detail": "non-2xx response"},
        ))
        row = notify_store.get_scheduler_receipt("deadman-ping")
        self.assertEqual(row["outcome"], "error")
        self.assertIn("non-2xx", row["detail"])

    def test_executed_with_explicit_ok_true_is_ok(self):
        scheduler._on_job_event(_event(
            EVENT_JOB_EXECUTED, job_id="deadman-ping", retval={"ok": True},
        ))
        row = notify_store.get_scheduler_receipt("deadman-ping")
        self.assertEqual(row["outcome"], "ok")

    def test_job_error_event_is_error(self):
        secret = "super-secret-token"
        scheduler._on_job_event(_event(
            EVENT_JOB_ERROR, job_id="health-watch",
            exception=RuntimeError(f"failed https://private.example/{secret}"),
        ))
        row = notify_store.get_scheduler_receipt("health-watch")
        self.assertEqual(row["outcome"], "error")
        self.assertIn("RuntimeError", row["detail"])
        self.assertNotIn(secret, row["detail"])
        self.assertNotIn("private.example", row["detail"])

    def test_job_missed_event_is_missed(self):
        scheduler._on_job_event(_event(EVENT_JOB_MISSED, job_id="conformance-watch"))
        row = notify_store.get_scheduler_receipt("conformance-watch")
        self.assertEqual(row["outcome"], "missed")

    def test_scheduled_run_time_is_persisted_as_utc_iso(self):
        scheduler._on_job_event(_event(EVENT_JOB_EXECUTED, job_id="thermal-watch"))
        row = notify_store.get_scheduler_receipt("thermal-watch")
        self.assertEqual(row["scheduled_at"], "2026-08-02T10:00:00+00:00")

    def test_persistence_failure_does_not_raise(self):
        with patch.object(
            notify_store, "record_scheduler_receipt", side_effect=RuntimeError("db locked"),
        ):
            try:
                scheduler._on_job_event(_event(EVENT_JOB_EXECUTED, job_id="thermal-watch"))
            except Exception as exc:  # pragma: no cover - failure path under test
                self.fail(f"_on_job_event raised despite persistence failure: {exc!r}")


class ListenerRegistrationTests(unittest.TestCase):
    def setUp(self):
        scheduler._receipt_listener_registered = False
        self._orig_listeners = list(scheduler.scheduler._listeners)
        scheduler.scheduler._listeners = []

    def tearDown(self):
        scheduler.scheduler._listeners = self._orig_listeners

    def test_registering_twice_adds_exactly_one_listener(self):
        scheduler._register_receipt_listener()
        scheduler._register_receipt_listener()
        scheduler._register_receipt_listener()
        self.assertEqual(len(scheduler.scheduler._listeners), 1)

    def test_register_jobs_registration_path_is_also_idempotent(self):
        # register_jobs() itself calls add_job with fixed ids -- exercising
        # it twice would raise ConflictingIdError unrelated to the listener,
        # so this pins _register_receipt_listener's own idempotence directly,
        # which is the actual duplicate-registration risk named in the build
        # spec (test/startup registration paths both calling it).
        scheduler._register_receipt_listener()
        scheduler._register_receipt_listener()
        self.assertEqual(len(scheduler.scheduler._listeners), 1)


if __name__ == "__main__":
    unittest.main()
