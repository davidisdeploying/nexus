import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import routes
from .models import Health, StatusSnapshot


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class NotificationsPageTests(unittest.TestCase):
    def chrome(self) -> dict:
        snap = StatusSnapshot(
            generated_at=datetime(2026, 8, 2, 4, 0, tzinfo=timezone.utc),
            overall=Health.OK,
            duration_ms=100,
            nodes=[],
        )
        return {
            "chrome_snap": snap,
            "chrome_accent": routes.FRAME_ACCENT,
            "chrome_unread": 1,
            "chrome_stamp": "2026-08-01 11:00 PM",
        }

    @patch.object(routes, "app_chrome_context", new_callable=AsyncMock)
    @patch.object(routes.detail_context, "build_feed_context")
    def test_notifications_inbox_is_canonical_full_page(self, build_feed, chrome) -> None:
        chrome.return_value = self.chrome()
        build_feed.return_value = {
            "count": 1,
            "rows": [{
                "title": "Fleet scan complete",
                "body": "All systems nominal.",
                "channel": "nexus-log",
                "prio": 2,
                "created_at": "2026-08-02T04:00:00+00:00",
                "event_key": "scan:complete",
                "navigate": "/operations",
                "emoji": "✅",
                "unread": True,
                "read_at": None,
            }],
            "groups": [{
                "key": "operations",
                "label": "Fleet operations",
                "description": "Health, conformance, jobs, scans, and protective signals",
                "icon": "operations",
                "count": 1,
                "unread_count": 1,
                "items": [{
                    "friendly_title": "Fleet scan complete",
                    "friendly_body": "All systems nominal.",
                    "category_label": "Job or scan",
                    "source_label": "Job monitor",
                    "created_label": "Just now",
                    "created_exact": "Aug 1, 2026 at 11:00:00 PM CDT",
                    "created_at": "2026-08-02T04:00:00+00:00",
                    "read_label": "Time unavailable",
                    "channel": "nexus-log",
                    "prio": 2,
                    "event_key": "scan:complete",
                    "navigate": "/operations",
                    "emoji": "✅",
                    "tone": "good",
                    "unread": True,
                    "read_at": None,
                    "body": "All systems nominal.",
                }],
            }],
        }

        response = _client().get("/notifications")

        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>notifications · Nexus</title>", response.text)
        # Notifications moved from the header bell into primary navigation in
        # the 2026-08-02 mobile restructure; the active marker moved with it.
        self.assertIn("nexus-app-link--desktop-notifications is-active", response.text)
        self.assertIn('href="/notifications" aria-label="Notifications" aria-current="page"', response.text)
        self.assertIn("<h2>fleet notifications</h2>", response.text)
        self.assertIn("Fleet scan complete", response.text)
        self.assertIn("Fleet operations", response.text)
        self.assertIn("Job monitor", response.text)
        self.assertIn("Just now", response.text)
        self.assertIn("Event details", response.text)
        self.assertIn('role="tablist"', response.text)
        self.assertIn('class="nexus-lens-tabs nexus-lens-tabs--four notification-group-index"', response.text)
        self.assertIn('data-notification-group="operations"', response.text)
        self.assertIn('aria-selected="true"', response.text)
        self.assertIn('data-notification-panel="operations"', response.text)
        self.assertNotIn('href="#notification-group-', response.text)
        self.assertIn('id="markAllRead"', response.text)
        self.assertIn('id="clearNotifications"', response.text)
        self.assertIn('aria-label="Clear all notifications"', response.text)
        self.assertNotIn("notification overview", response.text)
        self.assertNotIn('id="settingsCog"', response.text)
        self.assertNotIn("sheet-backdrop", response.text)

    @patch.object(routes, "app_chrome_context", new_callable=AsyncMock)
    @patch.object(routes.detail_context, "build_feed_context")
    def test_preferences_live_inside_notifications(self, build_feed, chrome) -> None:
        chrome.return_value = self.chrome()
        build_feed.return_value = {
            "count": 0,
            "rows": [],
            "groups": [
                {"key": "workers", "label": "Worker activity", "description": "Worker runs", "icon": "workers", "count": 0, "unread_count": 0, "items": []},
                {"key": "models", "label": "Model usage", "description": "Provider capacity", "icon": "usage", "count": 0, "unread_count": 0, "items": []},
                {"key": "operations", "label": "Fleet operations", "description": "Fleet signals", "icon": "operations", "count": 0, "unread_count": 0, "items": []},
                {"key": "updates", "label": "Other updates", "description": "Additional events", "icon": "notifications", "count": 0, "unread_count": 0, "items": []},
            ],
        }

        response = _client().get("/notifications?tab=preferences")

        self.assertEqual(response.status_code, 200)
        self.assertIn('<h3 id="notificationLensTitle">preferences</h3>', response.text)
        self.assertIn('class="notification-header-control" href="/notifications"', response.text)
        self.assertIn('aria-label="Return to notification inbox"', response.text)
        self.assertIn('id="clearNotifications"', response.text)
        self.assertIn("<h2>fleet notifications</h2>", response.text)
        self.assertEqual(response.text.count('data-notification-group="'), 4)
        self.assertIn('id="push-status"', response.text)
        self.assertIn('data-device="iphone"', response.text)
        self.assertIn('data-device="ipad"', response.text)
        self.assertIn('data-device="macbook"', response.text)

    @patch.object(routes, "app_chrome_context", new_callable=AsyncMock)
    @patch.object(routes.detail_context, "build_feed_context")
    def test_group_query_selects_one_panel_without_anchor_navigation(self, build_feed, chrome) -> None:
        chrome.return_value = self.chrome()
        build_feed.return_value = {
            "count": 1,
            "rows": [{"id": 1}],
            "groups": [
                {
                    "key": "workers", "label": "Worker activity", "description": "Worker runs",
                    "icon": "workers", "count": 1, "unread_count": 0, "items": [],
                },
                {
                    "key": "models", "label": "Model usage", "description": "Provider capacity",
                    "icon": "usage", "count": 1, "unread_count": 0, "items": [],
                },
            ],
        }

        response = _client().get("/notifications?group=models")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.text,
            r'class="nexus-lens-tab notification-group-picker is-active"[\s\S]+?data-notification-group="models"',
        )
        self.assertRegex(
            response.text,
            r'data-notification-panel="workers" hidden',
        )
        self.assertRegex(
            response.text,
            r'data-notification-panel="models">',
        )

    def test_group_picker_replaces_the_view_without_page_scrolling(self) -> None:
        source = (Path(routes.__file__).resolve().parent.parent / "static" / "notifications.js").read_text()
        self.assertIn("panel.hidden = panel.dataset.notificationPanel !== groupKey", source)
        self.assertIn("window.history.replaceState", source)
        self.assertIn("preventScroll: true", source)
        self.assertIn("behavior: \"instant\"", source)
        self.assertNotIn("scrollIntoView", source)

    def test_clear_control_reconciles_badge_and_dismisses_delivered_notifications(self) -> None:
        source = (Path(routes.__file__).resolve().parent.parent / "static" / "notifications.js").read_text()
        self.assertIn('event.target.closest("#markAllRead, #clearNotifications")', source)
        self.assertIn('fetch("/api/notify/mark-all-read", {method: "POST"})', source)
        self.assertIn("navigator.clearAppBadge()", source)
        self.assertIn("registration.getNotifications()", source)
        self.assertIn("notification.close()", source)
        self.assertIn("reconcileUnreadCount();", source)

    @patch.object(routes, "app_chrome_context", new_callable=AsyncMock)
    @patch.object(routes.detail_context, "build_feed_context")
    def test_clear_control_is_available_when_nothing_is_unread(self, build_feed, chrome) -> None:
        chrome.return_value = {**self.chrome(), "chrome_unread": 0}
        build_feed.return_value = {
            "count": 0,
            "rows": [],
            "groups": [],
        }

        response = _client().get("/notifications")

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="clearNotifications"', response.text)
        self.assertNotIn('id="markAllRead"', response.text)

    def test_mobile_group_picker_shows_all_four_groups_without_scrolling(self) -> None:
        root = Path(routes.__file__).resolve().parent.parent
        template = (root / "templates" / "_app_shell.html").read_text()
        route_css = (root / "static" / "notifications.css").read_text()
        shared_css = (root / "static" / "nexus.css").read_text()
        self.assertIn("nexus-lens-tabs nexus-lens-tabs--four notification-group-index", template)
        self.assertIn("grid-template-columns:repeat(4,minmax(0,1fr))", shared_css)
        self.assertNotIn("scroll-snap-type:x", route_css)

    def test_legacy_feed_redirect_preserves_notification_confirmation(self) -> None:
        response = _client().get("/feed?nf=1", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/notifications?nf=1")

    def test_legacy_settings_redirects_to_preferences(self) -> None:
        response = _client().get("/settings", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/notifications?tab=preferences")

    def test_unknown_notification_tab_returns_to_inbox(self) -> None:
        response = _client().get("/notifications?tab=retired", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/notifications")

    def test_new_push_fallbacks_target_notifications(self) -> None:
        app_dir = Path(routes.__file__).resolve().parent
        push_source = (app_dir / "push.py").read_text()
        notify_source = (app_dir / "notify.py").read_text()
        self_test_source = (app_dir / "self_test.py").read_text()
        for source in (push_source, notify_source, self_test_source):
            self.assertIn("/notifications", source)
            self.assertNotIn('"/feed"', source)


if __name__ == "__main__":
    unittest.main()
