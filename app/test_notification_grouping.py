import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from . import detail_context


class NotificationGroupingTests(unittest.TestCase):
    def test_groups_by_user_intent_and_humanizes_primary_copy(self) -> None:
        rows = [
            {
                "event_key": "conformance-check:path:alpha:alarm:1",
                "channel": "nexus-post",
                "prio": 4,
                "title": "⚠️ Fleet conformance drift — path:alpha:README.md",
                "body": "state=error expected=file exists actual=exit 1",
                "navigate": "/operations?tab=conformance",
                "emoji": "⚠️",
                "created_at": "2026-08-02T04:58:00+00:00",
                "read_at": None,
            },
            {
                "event_key": "run:FLEET-AUTO-BUILD-20260802-standby-premise-heading:success",
                "channel": "nexus-post",
                "prio": 3,
                "title": "worker2 build done — standby-premise-heading",
                "body": None,
                "navigate": "/activity?tab=workers",
                "emoji": "🛠️✅",
                "created_at": "2026-08-02T04:30:00+00:00",
                "read_at": None,
            },
            {
                "event_key": "model-usage-event:1066",
                "channel": "nexus-post",
                "prio": 3,
                "title": "Claude five-hour quota reset",
                "body": "New window ends Sun Aug 2, 3:40 AM CDT.",
                "navigate": "/activity?tab=models",
                "emoji": "🔄",
                "created_at": "2026-08-02T03:49:00+00:00",
                "read_at": "2026-08-02T04:15:00+00:00",
            },
            {
                "event_key": "alert:service_down_recovery:delta",
                "channel": "nexus-log",
                "prio": 4,
                "title": "✅📡 delta reachable again",
                "body": "tailnet ping ok",
                "navigate": "/operations",
                "emoji": "✅",
                "created_at": "2026-08-01T23:00:00+00:00",
                "read_at": None,
            },
            {
                "event_key": "scan:complete",
                "channel": "nexus-log",
                "prio": 2,
                "title": "Fleet scan complete",
                "body": "All systems nominal.",
                "navigate": "/operations",
                "emoji": "✅",
                "created_at": "2026-07-31T20:00:00+00:00",
                "read_at": None,
            },
        ]
        now = datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc)

        with patch.object(detail_context.notify_store, "init_db"), patch.object(
            detail_context.notify_store, "list_notifications", return_value=rows
        ):
            context = detail_context.build_feed_context(now=now)

        self.assertEqual(
            [group["key"] for group in context["groups"]],
            ["workers", "models", "operations", "updates"],
        )
        operations = next(group for group in context["groups"] if group["key"] == "operations")
        self.assertEqual(operations["count"], 3)
        worker = next(row for row in context["rows"] if row["event_key"].startswith("run:"))
        self.assertEqual(worker["friendly_title"], "Worker2 completed a build")
        self.assertEqual(worker["friendly_body"], "Standby premise heading.")
        self.assertEqual(worker["source_label"], "Worker relay")
        self.assertEqual(worker["created_label"], "30 minutes ago")

        model = next(row for row in context["rows"] if row["event_key"].startswith("model-usage"))
        self.assertEqual(model["friendly_title"], "Claude quota reset")
        self.assertTrue(model["friendly_body"].startswith("Next five-hour window ends"))
        self.assertEqual(model["category_label"], "Quota window")

        recovery = next(row for row in context["rows"] if row["event_key"].startswith("alert:"))
        self.assertEqual(recovery["friendly_title"], "Delta is reachable again")
        self.assertEqual(recovery["friendly_body"], "Tailnet connectivity is responding normally.")
        self.assertEqual(recovery["tone"], "good")

    def test_read_warning_stays_with_operations_instead_of_attention(self) -> None:
        row = {
            "event_key": "conformance-cache:alarm:20260802T010000Z",
            "channel": "nexus-post",
            "prio": 4,
            "title": "Fleet conformance cache stale",
            "body": "unsupported schema",
            "navigate": "/operations?tab=conformance",
            "emoji": "⚠️",
            "created_at": "2026-08-02T01:00:00+00:00",
            "read_at": "2026-08-02T01:05:00+00:00",
        }
        with patch.object(detail_context.notify_store, "init_db"), patch.object(
            detail_context.notify_store, "list_notifications", return_value=[row]
        ):
            context = detail_context.build_feed_context(
                now=datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc)
            )

        operations = next(group for group in context["groups"] if group["key"] == "operations")
        self.assertEqual(operations["unread_count"], 0)
        self.assertEqual(context["rows"][0]["created_label"], "4 hours ago")


if __name__ == "__main__":
    unittest.main()
