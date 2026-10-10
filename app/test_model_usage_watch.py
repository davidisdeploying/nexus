import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import model_usage_watch, notify, notify_store
from .model_usage_history import init_db


class ModelUsageWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.usage_db = self.root / "usage.sqlite3"
        self.notify_db = self.root / "events.db"
        init_db(self.usage_db)
        self.old_notify_db = notify_store.DB_PATH
        notify_store.DB_PATH = self.notify_db
        notify_store.init_db()

    def tearDown(self):
        notify_store.DB_PATH = self.old_notify_db
        self.temp.cleanup()

    def add_event(self, event_type="window_rolled_over"):
        with sqlite3.connect(self.usage_db) as conn:
            cursor = conn.execute(
                """INSERT INTO usage_events(
                   captured_at,provider,window,event_type,severity,
                   previous_used_percent,used_percent,
                   previous_resets_at,resets_at,previous_source,source
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    1_775_000_000, "codex", "weekly", event_type, "warn",
                    75, 4, 2_000_000_000, 2_000_100_000,
                    "old source", "new source",
                ),
            )
            return cursor.lastrowid

    def add_capture(self, captured_at: int, ok: bool) -> None:
        with sqlite3.connect(self.usage_db) as conn:
            for provider in ("claude", "codex", "gemini"):
                conn.execute(
                    """INSERT INTO usage_samples(
                       captured_at,provider,ok,source
                       ) VALUES(?,?,?,?)""",
                    (captured_at, provider, int(ok), "test source"),
                )

    async def test_first_tick_baselines_without_backfill(self):
        event_id = self.add_event()
        with patch.object(
            model_usage_watch, "notify", new=AsyncMock()
        ) as mock_notify:
            result = await model_usage_watch.scan_once(self.usage_db)
        self.assertTrue(result["seeded"])
        self.assertEqual(result["watermark"], event_id)
        mock_notify.assert_not_awaited()

    async def test_new_event_notifies_once_and_advances(self):
        await model_usage_watch.scan_once(self.usage_db)
        event_id = self.add_event()
        with patch.object(
            model_usage_watch,
            "notify",
            new=AsyncMock(return_value={"suppressed": False}),
        ) as mock_notify:
            first = await model_usage_watch.scan_once(self.usage_db)
            second = await model_usage_watch.scan_once(self.usage_db)
        self.assertEqual(first["fired"], 1)
        self.assertEqual(first["watermark"], event_id)
        self.assertEqual(second["seen"], 0)
        mock_notify.assert_awaited_once()
        payload = mock_notify.await_args.args[0]
        self.assertEqual(payload["event_key"], f"model-usage-event:{event_id}")
        self.assertEqual(payload["navigate"], "/activity?tab=models")

    async def test_unknown_event_is_silently_consumed(self):
        await model_usage_watch.scan_once(self.usage_db)
        event_id = self.add_event("future_event")
        with patch.object(
            model_usage_watch, "notify", new=AsyncMock()
        ) as mock_notify:
            result = await model_usage_watch.scan_once(self.usage_db)
        self.assertEqual(result["watermark"], event_id)
        self.assertEqual(result["fired"], 0)
        mock_notify.assert_not_awaited()

    async def test_only_confirmed_window_resets_notify(self):
        await model_usage_watch.scan_once(self.usage_db)
        for event_type in (
            "window_reanchored_early",
            "availability_lost",
            "availability_restored",
            "fallback_used",
            "source_changed",
            "usage_dropped",
        ):
            self.add_event(event_type)
        with patch.object(
            model_usage_watch, "notify", new=AsyncMock()
        ) as mock_notify:
            result = await model_usage_watch.scan_once(self.usage_db)
        self.assertEqual(result["fired"], 0)
        mock_notify.assert_not_awaited()

    async def test_tracker_loss_notifies_once_and_recovery_is_silent(self):
        now = int(time.time())
        self.add_capture(now, True)
        await model_usage_watch.scan_once(self.usage_db)
        self.add_capture(now + 60, False)
        with patch.object(
            model_usage_watch,
            "notify",
            new=AsyncMock(return_value={"suppressed": False}),
        ) as mock_notify, patch.object(
            model_usage_watch.time, "time", return_value=now + 60
        ):
            first = await model_usage_watch.scan_once(self.usage_db)
            second = await model_usage_watch.scan_once(self.usage_db)
        self.assertEqual(first["fired"], 1)
        self.assertFalse(first["tracker_available"])
        self.assertEqual(second["fired"], 0)
        mock_notify.assert_awaited_once()
        payload = mock_notify.await_args.args[0]
        self.assertEqual(
            payload["condition"], "model_quota_tracker_unavailable"
        )

        self.add_capture(now + 120, True)
        with patch.object(
            model_usage_watch, "notify", new=AsyncMock()
        ) as recovery_notify, patch.object(
            model_usage_watch.time, "time", return_value=now + 120
        ):
            recovered = await model_usage_watch.scan_once(self.usage_db)
        self.assertTrue(recovered["tracker_available"])
        recovery_notify.assert_not_awaited()

    def test_routing_contract_and_explicit_event_key(self):
        event = {
            "source": "model_usage_watch",
            "condition": "model_quota_window_rolled_over",
            "host": "codex:weekly",
            "event_key": "model-usage-event:42",
            "title": "changed",
            "navigate": "/activity?tab=models",
        }
        normalized = notify.normalize(event)
        classified = notify.classify(normalized)
        rendered = notify.render(normalized, classified)
        self.assertEqual(classified["channel"], "nexus-post")
        self.assertEqual(classified["prio"], 3)
        self.assertEqual(rendered["event_key"], "model-usage-event:42")
        rollover = notify.classify(notify.normalize({
            "condition": "model_quota_window_rolled_over"
        }))
        self.assertEqual(rollover["channel"], "nexus-post")


if __name__ == "__main__":
    unittest.main()
