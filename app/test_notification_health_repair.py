"""
Focused regression tests for send_push's sent_pwa accounting
(FLEET-AUTO-BUILD-20260802-panel-notification-health-repair).

app/push.py used to never write notification_log.sent_pwa, so
system_status.py inferred old self-test delivery from the mutable
push_subscription.last_send_at column instead -- a later, unrelated push
overwrites that column and can turn a real PWA pass into a false "failed"
reading on the dashboard. These tests pin: a successful send persists
sent_pwa=1 on its own notification_log row; an all-target failure leaves it
0/false; the weekly self-test's row is durable against exactly that kind of
later, unrelated send; and a genuine repeat failure is not masked by it.
"""
from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from pywebpush import WebPushException

from . import notify_store, push, self_test, system_status


def _wrapped(value=None, error=None):
    return {"value": value, "error": error}


class NotifyStoreTempDbMixin:
    def setUp(self):
        super().setUp()
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "events.db"
        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = self.db_path
        self.addCleanup(setattr, notify_store, "DB_PATH", self._orig_db_path)
        notify_store.init_db()
        notify_store.upsert_subscription(
            endpoint="https://push.example.com/dev-1",
            p256dh="p256dh-key", auth="auth-key",
            device_label="phone", ua="test-ua",
        )

    def _selftest_row(self) -> dict:
        return next(
            row for row in notify_store.list_notifications(50)
            if row["channel"] == "nexus-selftest"
        )


class SendPushSentPwaAccountingTests(NotifyStoreTempDbMixin, unittest.IsolatedAsyncioTestCase):
    async def test_accepted_delivery_persists_sent_pwa_true(self):
        note = {"event_key": "k1", "channel": "nexus-post", "prio": 3, "title": "hi"}
        with patch("app.push.webpush", return_value=None):
            result = await push.send_push(note)

        self.assertTrue(result["sent_pwa"])
        row = notify_store.list_notifications(1)[0]
        self.assertEqual(row["id"], result["log_id"])
        self.assertEqual(row["sent_pwa"], 1)

    async def test_all_target_failure_persists_sent_pwa_false(self):
        note = {"event_key": "k2", "channel": "nexus-post", "prio": 3, "title": "hi"}
        with patch("app.push.webpush", side_effect=WebPushException("boom")):
            result = await push.send_push(note)

        self.assertFalse(result["sent_pwa"])
        row = notify_store.list_notifications(1)[0]
        self.assertEqual(row["sent_pwa"], 0)

    async def test_zero_targeted_subscriptions_persists_sent_pwa_false(self):
        notify_store.deactivate_subscription("https://push.example.com/dev-1")
        note = {"event_key": "k3", "channel": "nexus-post", "prio": 3, "title": "hi"}
        with patch("app.push.webpush") as mock_webpush:
            result = await push.send_push(note)

        mock_webpush.assert_not_called()
        self.assertFalse(result["sent_pwa"])
        self.assertEqual(notify_store.list_notifications(1)[0]["sent_pwa"], 0)


class SelfTestDurabilityTests(NotifyStoreTempDbMixin, unittest.IsolatedAsyncioTestCase):
    async def test_pwa_success_persists_on_the_selftest_row(self):
        with patch("app.push.webpush", return_value=None), \
             patch("app.self_test.ntfy.send_ntfy", new=AsyncMock(return_value=True)):
            await self_test.run_self_test()

        row = self._selftest_row()
        self.assertEqual(row["sent_pwa"], 1)
        self.assertEqual(row["sent_ntfy"], 1)

    async def test_all_target_pwa_failure_stays_false_through_self_test(self):
        with patch("app.push.webpush", side_effect=WebPushException("boom")), \
             patch("app.self_test.ntfy.send_ntfy", new=AsyncMock(return_value=True)):
            await self_test.run_self_test()

        row = self._selftest_row()
        self.assertEqual(row["sent_pwa"], 0)
        self.assertEqual(row["sent_ntfy"], 1)

        sources = {"notifications": _wrapped(system_status._read_notifications())}
        result = system_status.build_system_status(sources, now=datetime.now(timezone.utc))
        notifications = next(m for m in result["modules"] if m["id"] == "notifications")
        canary = notifications["checks"][-1]
        self.assertEqual(canary["value"], "one transport failed")
        self.assertIn("PWA failed", canary["detail"])

    async def test_later_unrelated_send_cannot_flip_a_persisted_pass(self):
        with patch("app.push.webpush", return_value=None), \
             patch("app.self_test.ntfy.send_ntfy", new=AsyncMock(return_value=True)):
            await self_test.run_self_test()
        selftest_created_at = self._selftest_row()["created_at"]

        # A later, unrelated push (e.g. an ordinary notification) moves
        # push_subscription.last_send_at hours past the self-test's own
        # timestamp -- well outside build_system_status's +-180s proximity
        # window that the old fallback-only logic depended on.
        far_future = (
            datetime.fromisoformat(selftest_created_at) + timedelta(hours=6)
        ).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE push_subscription SET last_send_at=?", (far_future,))
        conn.commit()
        conn.close()

        now = datetime.fromisoformat(far_future) + timedelta(minutes=5)
        sources = {"notifications": _wrapped(system_status._read_notifications())}
        result = system_status.build_system_status(sources, now=now)
        notifications = next(m for m in result["modules"] if m["id"] == "notifications")
        canary = notifications["checks"][-1]

        self.assertEqual(canary["status"], "ok")
        self.assertEqual(canary["value"], "PWA + ntfy passed")


if __name__ == "__main__":
    unittest.main()
