import re
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from .models import Health


class AppShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        templates = Path(__file__).resolve().parent.parent / "templates"
        cls.module = Environment(loader=FileSystemLoader(templates)).get_template(
            "_app_shell.html"
        ).make_module()

    def test_primary_navigation_order_and_current_page(self) -> None:
        html = str(self.module.app_nav("operations"))
        markers = [
            'href="/activity"',
            'href="/operations"',
            'id="notifyBell"',
            'class="nexus-app-link nexus-app-link--desktop-status',
        ]
        positions = [html.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(re.findall(r'<a[^>]+aria-current="page"', html)), 1)
        self.assertIn('href="/operations" aria-current="page"', html)

    def test_every_document_uses_the_shared_icon_metadata(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        templates = repo_root / "templates"
        document_templates = [
            path for path in templates.glob("*.html")
            if "<head>" in path.read_text()
        ]

        self.assertEqual(len(document_templates), 18)
        for path in document_templates:
            source = path.read_text()
            self.assertIn('import "_app_shell.html" as app_shell', source, path.name)
            self.assertEqual(source.count("app_shell.app_icons()"), 1, path.name)

        icon_html = str(self.module.app_icons())
        for asset in (
            "nexus-eye.svg",
            "nexus-eye-mask.svg",
            "nexus-eye-180.png",
            "nexus-eye-192.png",
            "nexus-eye-512.png",
        ):
            self.assertIn(f"/static/icons/{asset}", icon_html)
        self.assertNotIn("apple-touch-icon-180.png", icon_html)
        self.assertNotIn("/static/icons/icon-", icon_html)

    def test_manifest_and_service_worker_own_the_new_icon_generation(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        manifest = json.loads((repo_root / "static" / "manifest.webmanifest").read_text())
        sources = [icon["src"] for icon in manifest["icons"]]
        self.assertEqual(sources, [
            "/static/icons/nexus-eye-192.png",
            "/static/icons/nexus-eye-512.png",
            "/static/icons/nexus-eye-maskable-512.png",
        ])
        for source in sources:
            self.assertTrue((repo_root / "static" / source.removeprefix("/static/")).is_file(), source)

        compatibility_icons = {
            "apple-touch-icon-180.png": "nexus-eye-180.png",
            "icon-192.png": "nexus-eye-192.png",
            "icon-512.png": "nexus-eye-512.png",
            "icon-maskable-512.png": "nexus-eye-maskable-512.png",
        }
        for legacy, current in compatibility_icons.items():
            self.assertEqual(
                (repo_root / "static" / "icons" / legacy).read_bytes(),
                (repo_root / "static" / "icons" / current).read_bytes(),
                legacy,
            )

        service_worker = (repo_root / "static" / "sw.js").read_text()
        self.assertIn("const CACHE_GENERATION = 'v21'", service_worker)

        # The node-matrix mark's own bounding box is x 0..88, y -100..-12.
        # It is set at 0.56 rather than the old eye's 0.76: a solid block grid
        # carries far more weight on the plate than that linework did.
        mark_source = (repo_root / "static" / "icons" / "nexus-eye.svg").read_text()
        transform = re.search(
            r'transform="translate\(([\d.]+) ([\d.]+)\) scale\(([\d.]+)\)"',
            mark_source,
        )
        self.assertIsNotNone(transform)
        tx, ty, scale = map(float, transform.groups())
        self.assertAlmostEqual(scale * (88.0 - 0.0), 512 * 0.56, places=3)
        self.assertAlmostEqual(tx + scale * ((0.0 + 88.0) / 2), 256, places=3)
        self.assertAlmostEqual(ty + scale * ((-100.0 + -12.0) / 2), 256, places=3)

    def test_legacy_page_headers_use_the_shared_supplied_eye(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        templates = repo_root / "templates"
        legacy_headers = (
            "detail_alerts.html", "detail_approve.html", "detail_jobs.html",
            "detail_queues.html", "detail_run.html", "hero_path.html",
            "jobs.html",
        )
        for filename in legacy_headers:
            source = (templates / filename).read_text()
            self.assertIn("app_shell.nexus_eye()", source, filename)
            self.assertNotIn('viewBox="0 0 64 64"', source, filename)

        eye_html = str(self.module.nexus_eye())
        # Every drawn shape must carry .nexus-eye-mark: that class is what
        # --eye-col fills, so a shape without it would not follow status.
        shapes = re.findall(r"<(?:path|rect)\b", eye_html)
        self.assertEqual(len(shapes), 4)
        self.assertEqual(eye_html.count('class="nexus-eye-mark"'), len(shapes))
        self.assertIn('viewBox="0 0 88 88"', eye_html)

    def test_primary_navigation_centers_above_mobile_width(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()
        self.assertIn(
            "@media(min-width:641px){\n  .nexus-app-nav__track{justify-content:center}",
            shared_css,
        )

    def test_ipad_and_desktop_use_one_centered_header_row(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()

        self.assertIn("@media(min-width:768px){", shared_css)
        self.assertIn(
            "grid-template-columns:minmax(0,1fr) auto auto;",
            shared_css,
        )
        self.assertIn(
            ".nexus-sticky-shell > .nexus-dashboard-chrome{display:contents}",
            shared_css,
        )
        self.assertIn(
            ".nexus-dashboard-chrome .nexus-brand{grid-column:1;grid-row:1;display:flex;",
            shared_css,
        )
        self.assertIn(".nexus-dashboard-chrome h1{white-space:nowrap}", shared_css)
        self.assertIn(
            ".nexus-dashboard-chrome .header-controls{grid-column:2;grid-row:1;display:flex;",
            shared_css,
        )
        self.assertIn(
            ".nexus-sticky-shell > .nexus-app-nav{grid-column:3;grid-row:1;display:flex;align-items:center;",
            shared_css,
        )

    def test_header_groups_brand_and_orders_desktop_actions(self) -> None:
        health = Health.OK
        snap = SimpleNamespace(
            overall=health, nodes=[],
            work=SimpleNamespace(jobs=[]), seats=SimpleNamespace(seats=[]),
        )
        html = str(
            self.module.dashboard_chrome(
                "dashboard", snap, {health: "developed"}, 0,
                "2026-08-02 12:45 PM",
            )
        )

        self.assertIn('class="nexus-brand"', html)
        brand_start = html.index('class="nexus-brand"')
        brand_end = html.index('class="overall', brand_start)
        self.assertIn('id="overall-eye"', html[brand_start:brand_end])
        self.assertIn('<h1 class="nexus-wordmark">', html[brand_start:brand_end])
        self.assertIn(
            '<span class="nexus-wordmark-name">Nexus</span>',
            html[brand_start:brand_end],
        )
        self.assertNotIn("FLEET RUNE - SCANNING", html)
        nav_start = html.index('class="nexus-app-nav"')
        nav = html[nav_start:]
        markers = [
            'href="/activity"',
            'href="/operations"',
            'id="notifyBell"',
            'class="nexus-app-link nexus-app-link--desktop-status',
        ]
        positions = [nav.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        controls_start = html.index('class="header-controls"')
        controls = html[controls_start:html.index('class="warnticker"', controls_start)]
        self.assertNotIn('id="notifyBell"', controls)
        utility_markers = [
            'aria-label="Dashboard"',
            'aria-label="CLI"',
            'aria-label="Refresh fleet status"',
        ]
        utility_positions = [controls.index(marker) for marker in utility_markers]
        self.assertEqual(utility_positions, sorted(utility_positions))
        self.assertIn('class="scan-control mobile-refresh-control"', controls)

    def test_desktop_navigation_is_neutral_until_current_or_refreshing(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()

        self.assertIn(
            ".nexus-app-link{min-height:44px;padding:0 12px;border-radius:8px;",
            shared_css,
        )
        self.assertIn(
            "border-color:var(--line);background:transparent;color:var(--ink-dim);box-shadow:none;",
            shared_css,
        )
        self.assertIn(
            ".nexus-app-link.is-active{color:var(--cyan);border-color:var(--cyan);\n"
            "    background:color-mix(in srgb,var(--cyan) 15%,var(--stone));"
            "box-shadow:0 0 12px -8px var(--cyan)}",
            shared_css,
        )
        self.assertIn(
            ".nexus-dashboard-chrome .mobile-dashboard-control,",
            shared_css,
        )
        self.assertIn("display:inline-flex;width:44px;height:44px;min-height:44px;", shared_css)
        self.assertIn(
            ".nexus-dashboard-chrome .mobile-dashboard-control.is-active,\n"
            "  .nexus-dashboard-chrome .mobile-cli-control.is-active,\n"
            "  .nexus-dashboard-chrome .scan-control.is-scanning{color:var(--cyan);",
            shared_css,
        )
        self.assertNotIn(".nexus-app-link--desktop-status.is-active{color:var(--ink-dim);", shared_css)

    def test_ipad_and_desktop_hide_both_header_timestamps(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()

        self.assertIn(
            ".nexus-dashboard-chrome .overall .stamp,\n"
            "  .nexus-dashboard-chrome .overall .clock{display:none}",
            shared_css,
        )

    def test_primary_navigation_uses_shared_icons_not_emoji(self) -> None:
        html = str(self.module.app_nav("dashboard"))
        self.assertEqual(html.count('class="nexus-icon"'), 6)
        for glyph in ("🛡️", "⚙️", "🔔", "◫", "◔", "◇", "⌘"):
            self.assertNotIn(glyph, html)

    def test_mobile_moves_dashboard_and_cli_to_header_and_adds_status_to_nav(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()
        nav = str(self.module.app_nav("notifications", 3, "critical"))

        self.assertNotIn("nexus-app-link--desktop-dashboard", nav)
        self.assertNotIn("nexus-app-link--desktop-control", nav)
        self.assertIn("nexus-app-link--mobile-notifications is-active", nav)
        self.assertIn("nexus-app-link--mobile-status", nav)
        self.assertIn('data-system-state="critical"', nav)
        self.assertIn('<span data-system-status-label>Status</span>', nav)
        self.assertIn('data-bell-count>3</span>', nav)
        self.assertIn(".nexus-app-link--mobile-notifications,.nexus-app-link--mobile-status{display:none}", shared_css)
        mobile = shared_css.split("@media(max-width:640px){", 1)[1]
        self.assertIn(".nexus-dashboard-chrome .mobile-dashboard-control{display:inline-flex;order:3}", mobile)
        self.assertIn(".nexus-dashboard-chrome .mobile-cli-control{display:inline-flex;order:5}", mobile)
        self.assertIn(".nexus-app-link--desktop-notifications,.nexus-app-link--desktop-status{display:none}", mobile)
        self.assertIn(".nexus-app-link--mobile-notifications,.nexus-app-link--mobile-status{display:inline-flex}", mobile)

    def test_primary_navigation_does_not_shift_the_mobile_track(self) -> None:
        html = str(self.module.app_nav("control"))
        self.assertNotIn("scrollLeft", html)
        self.assertNotIn("offsetLeft", html)

    def test_operations_header_owns_four_ordered_lenses(self) -> None:
        html = str(self.module.operations_header("watchdogs", "29 mechanisms"))
        labels = ["Health", "Conformance", "Watchdogs", "Indexes"]
        positions = [html.index(f"<span>{label}</span>") for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(re.findall(r'<a[^>]+aria-current="page"', html)), 1)
        self.assertIn('href="/operations?tab=watchdogs" aria-current="page"', html)
        self.assertIn('class="nexus-lens-tabs nexus-lens-tabs--four"', html)
        self.assertIn("<h2>fleet operations</h2>", html)
        self.assertIn("<h3>watchdogs</h3>", html)

    def test_status_header_uses_the_shared_four_lens_fleet_structure(self) -> None:
        html = str(self.module.status_header(
            "governance", "14 modules · 3 flagged checks", "statusSummary",
        ))
        labels = ["Overview", "Systems", "Governance", "Services"]
        positions = [html.index(f"<span>{label}</span>") for label in labels]

        self.assertEqual(positions, sorted(positions))
        self.assertEqual(len(re.findall(r'<a[^>]+aria-current="page"', html)), 1)
        self.assertIn("<h2>fleet status</h2>", html)
        self.assertIn('class="nexus-lens-tabs nexus-lens-tabs--four"', html)
        self.assertIn('href="/system-status?tab=governance" aria-current="page"', html)
        self.assertIn('<h3 id="statusLensTitle">governance</h3>', html)

    def test_activity_header_uses_the_shared_lens_primitive(self) -> None:
        html = str(self.module.activity_header("models", "loading history", "activityStatus"))
        labels = ["Commits", "Models", "Workers", "Jobs"]
        positions = [html.index(f"<span>{label}</span>") for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(html.count('<button type="button" class="nexus-lens-tab'), 4)
        self.assertEqual(html.count('aria-selected="true"'), 1)
        self.assertIn('data-tab="models"', html)
        self.assertIn('<h3 id="activityLensTitle">models</h3>', html)
        self.assertIn('id="activityStatus"', html)

    def test_shared_content_headers_do_not_render_decorative_rune_eyebrows(self) -> None:
        html = str(self.module.content_header("fleet activity", "ready", "activityStatus"))

        self.assertIn("<h2>fleet activity</h2>", html)
        self.assertNotIn("nexus-content-eyebrow", html)
        self.assertNotIn('class="rune"', html)

        repo_root = Path(__file__).resolve().parent.parent
        template_source = "\n".join(
            path.read_text() for path in (repo_root / "templates").glob("*.html")
        )
        stylesheet_source = "\n".join(
            path.read_text() for path in (repo_root / "static").glob("*.css")
        )
        self.assertNotRegex(template_source, r'class="[^"]*\brune\b')
        self.assertNotIn("font-family:'nexus'", stylesheet_source)

    def test_dashboard_chrome_has_one_product_h1_and_global_nav(self) -> None:
        health = Health.OK
        snap = SimpleNamespace(
            overall=health, nodes=[],
            work=SimpleNamespace(jobs=[]), seats=SimpleNamespace(seats=[]),
        )
        html = str(
            self.module.dashboard_chrome(
                "activity", snap, {health: "developed"}, 3, "2026-08-01 7:55 PM"
            )
        )
        self.assertEqual(html.count("<h1"), 1)
        self.assertEqual(html.count('class="nexus-sticky-shell"'), 1)
        self.assertIn('<h1 class="nexus-wordmark">', html)
        self.assertIn('<span class="nexus-wordmark-name">Nexus</span>', html)
        self.assertNotIn('id="settingsCog"', html)
        self.assertIn('id="notifyBell"', html)
        self.assertIn('href="/notifications"', html)
        self.assertIn('id="bellCount" data-bell-count>3</span>', html)
        self.assertIn('href="/activity" aria-current="page"', html)

    def test_notifications_header_uses_the_four_lens_fleet_structure(self) -> None:
        groups = [
            {"key": "workers", "label": "Worker activity", "icon": "workers"},
            {"key": "models", "label": "Model usage", "icon": "usage"},
            {"key": "operations", "label": "Fleet operations", "icon": "operations"},
            {"key": "updates", "label": "Other updates", "icon": "notifications"},
        ]
        html = str(self.module.notifications_header(
            groups, "models", "inbox", "1 unread · 100 recent", "notificationStatus", 1,
        ))

        self.assertIn("<h2>fleet notifications</h2>", html)
        self.assertIn('class="nexus-lens-tabs nexus-lens-tabs--four notification-group-index"', html)
        self.assertEqual(html.count('data-notification-group="'), 4)
        self.assertIn("nexus-lens-tab notification-group-picker is-active", html)
        self.assertIn('<h3 id="notificationLensTitle">Model usage</h3>', html)
        self.assertIn('class="notification-header-control" href="/notifications?tab=preferences"', html)
        self.assertIn('aria-label="Notification preferences"', html)
        self.assertIn('id="clearNotifications"', html)
        self.assertIn('aria-label="Clear all notifications"', html)
        self.assertNotIn("<span>Preferences</span>", html)
        self.assertIn('id="markAllRead"', html)

    def test_notifications_page_marks_the_bell_active(self) -> None:
        health = Health.OK
        snap = SimpleNamespace(
            overall=health, nodes=[],
            work=SimpleNamespace(jobs=[]), seats=SimpleNamespace(seats=[]),
        )
        html = str(
            self.module.dashboard_chrome(
                "notifications", snap, {health: "developed"}, 1, "2026-08-01 11:10 PM"
            )
        )
        self.assertIn('class="nexus-app-link nexus-app-link--desktop-notifications is-active"', html)
        self.assertIn('href="/notifications" aria-label="Notifications" aria-current="page"', html)
        self.assertNotIn('id="settingsCog"', html)

    def test_system_status_uses_shared_navigation_treatment_and_eye_renders_live_severity(self) -> None:
        health = Health.OK
        snap = SimpleNamespace(
            overall=health, nodes=[],
            work=SimpleNamespace(jobs=[]), seats=SimpleNamespace(seats=[]),
        )
        status = SimpleNamespace(overall="critical")
        html = str(
            self.module.dashboard_chrome(
                "system-status", snap, {health: "developed"}, 0,
                "2026-08-02 11:45 AM", False, status,
            )
        )
        self.assertIn('href="/system-status" data-system-status', html)
        self.assertIn('class="eye overexposed" id="overall-eye"', html)
        self.assertIn('viewBox="0 0 88 88"', html)
        self.assertIn('class="nexus-eye-mark"', html)
        self.assertNotIn('id="app-eye-clip"', html)
        self.assertIn('class="nexus-app-link nexus-app-link--desktop-status is-active"', html)
        self.assertIn('data-system-state="critical"', html)
        self.assertIn('aria-label="System status: critical"', html)
        self.assertIn('<span data-system-status-label>Status</span>', html)
        self.assertNotIn('status-critical', html)
        self.assertIn('src="/static/system_status.js?v=', html)
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()
        dashboard_css = (repo_root / "static" / "dashboard.css").read_text()
        self.assertIn("#overall-eye .nexus-eye-mark{fill:var(--eye-col)}", shared_css)
        self.assertIn("#overall-eye .nexus-eye-mark{fill:var(--eye-col)}", dashboard_css)

    def test_dashboard_calls_the_same_shared_chrome_as_subpages(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        dashboard = (repo_root / "templates" / "dashboard.html").read_text()
        self.assertIn('app_shell.dashboard_chrome("dashboard"', dashboard)
        self.assertNotIn('<header>\n  <svg class="eye', dashboard)

        adopted = (
            "activity.html", "conformance.html",
            "control_plane.html", "watchdogs.html", "health.html", "gemini.html",
            "hero_path_session.html", "notifications.html",
        )
        for name in adopted:
            source = (repo_root / "templates" / name).read_text()
            self.assertIn("app_shell.dashboard_chrome(", source, name)
            self.assertIn("nexus-app-frame", source, name)
            self.assertNotIn("app_shell.page_header(", source, name)

    def test_sticky_shell_uses_glass_and_preserves_document_scrolling(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()
        dashboard_css = (repo_root / "static" / "dashboard.css").read_text()

        self.assertIn(".nexus-sticky-shell{position:sticky;top:0;z-index:30", shared_css)
        self.assertIn("backdrop-filter:blur(18px) saturate(135%)", shared_css)
        self.assertIn(".nexus-dashboard-chrome{display:flex", shared_css)
        self.assertIn(".nexus.nexus-app-frame", shared_css)
        self.assertIn(".screen{position:relative; overflow:clip", shared_css)
        self.assertIn(".screen{position:relative; overflow:clip", dashboard_css)
        self.assertIn("html,body{overflow-x:clip}", dashboard_css)
        self.assertNotIn("html,body{overflow-x:hidden}", dashboard_css)

    def test_mobile_shell_fits_primary_and_lens_navigation_without_auto_scrolling(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()

        self.assertIn(
            ".nexus-app-nav__track{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))",
            shared_css,
        )
        self.assertIn(
            ".nexus-lens-tabs--four,.activity-tabs{display:grid;",
            shared_css,
        )
        self.assertIn(".nexus-lens-tabs--two{display:grid;", shared_css)
        self.assertIn(".nexus-content-status{width:100%;max-width:none", shared_css)
        self.assertIn(
            ".nexus-dashboard-chrome .scan-control{position:relative;",
            shared_css,
        )
        self.assertIn(
            ".nexus-dashboard-chrome .overall .clock{display:none}",
            shared_css,
        )

    def test_mobile_header_orders_dashboard_cli_and_refresh_glyphs(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()

        self.assertIn(
            ".nexus-dashboard-chrome .overall,\n"
            "  .nexus-dashboard-chrome .header-controls{display:contents}",
            shared_css,
        )
        self.assertIn(
            ".nexus-dashboard-chrome{justify-content:center;gap:4px;row-gap:8px;padding:12px 6px;",
            shared_css,
        )
        self.assertIn(
            ".nexus-dashboard-chrome h1{font-size:clamp(17px,4.6vw,19px)}",
            shared_css,
        )
        self.assertIn(".nexus-dashboard-chrome .mobile-dashboard-control{display:inline-flex;order:3}", shared_css)
        self.assertIn(".nexus-dashboard-chrome .mobile-cli-control{display:inline-flex;order:5}", shared_css)
        self.assertIn(
            ".nexus-dashboard-chrome .scan-control{position:relative;\n"
            "    display:inline-flex;order:6;width:42px;height:42px;min-height:42px",
            shared_css,
        )
        self.assertIn(
            ".nexus-dashboard-chrome .warnticker:has(.wt-ok){display:none}",
            shared_css,
        )

    def test_mobile_header_utilities_are_neutral_until_their_view_is_current(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        shared_css = (repo_root / "static" / "nexus.css").read_text()
        mobile = shared_css.split("@media(max-width:640px){", 1)[1]

        for selector in (
            ".nexus-dashboard-chrome .mobile-dashboard-control:hover",
            ".nexus-dashboard-chrome .mobile-cli-control:hover",
            ".nexus-dashboard-chrome .scan-control:hover",
            ".nexus-dashboard-chrome .scan-control:disabled",
        ):
            self.assertIn(selector, mobile)
        self.assertIn(
            "color:var(--ink);border-color:var(--line);background:transparent;"
            "box-shadow:none;opacity:1}",
            mobile,
        )
        self.assertIn(
            ".nexus-dashboard-chrome .mobile-dashboard-control.is-active,\n"
            "  .nexus-dashboard-chrome .mobile-cli-control.is-active{",
            mobile,
        )
        self.assertIn(
            "color:var(--cyan);border-color:color-mix(in srgb,var(--cyan) 55%,var(--line));",
            mobile,
        )

    def test_dashboard_and_cli_controls_expose_current_page_state(self) -> None:
        health = Health.OK
        snap = SimpleNamespace(
            overall=health, nodes=[],
            work=SimpleNamespace(jobs=[]), seats=SimpleNamespace(seats=[]),
        )

        for current, control_class, label in (
            ("dashboard", "mobile-dashboard-control", "Dashboard"),
            ("control", "mobile-cli-control", "CLI"),
        ):
            with self.subTest(current=current):
                html = str(self.module.dashboard_chrome(
                    current, snap, {health: "developed"}, 0, "—", False,
                ))
                self.assertIn(f'{control_class} is-active', html)
                self.assertRegex(
                    html,
                    rf'class="[^"]*{control_class} is-active[^"]*"[^>]*aria-label="{label}"[^>]*aria-current="page"',
                )
                self.assertEqual(len(re.findall(r'<a[^>]+aria-current="page"', html)), 1)

    def test_shared_refresh_control_preserves_its_icon_while_running(self) -> None:
        health = Health.OK
        snap = SimpleNamespace(
            overall=health, nodes=[],
            work=SimpleNamespace(jobs=[]), seats=SimpleNamespace(seats=[]),
        )
        html = str(self.module.dashboard_chrome(
            "dashboard", snap, {health: "developed"}, 0, "—", True,
        ))
        repo_root = Path(__file__).resolve().parent.parent
        dashboard_js = (repo_root / "static" / "dashboard.js").read_text()
        shell_js = (repo_root / "static" / "app_shell.js").read_text()

        self.assertEqual(html.count("data-refresh-control"), 1)
        self.assertIn('class="scan-control mobile-refresh-control"', html)
        self.assertEqual(html.count('onclick="developNow(event)"'), 1)
        self.assertNotIn('>Scan<', html)
        self.assertIn('control.classList.add("is-scanning")', dashboard_js)
        self.assertIn('control.classList.remove("is-scanning")', dashboard_js)
        self.assertIn('control.classList.toggle("is-scanning", isRefreshing)', shell_js)
        self.assertIn('setRefreshState(false)', shell_js)
        self.assertNotIn('btn.textContent = "Scanning', dashboard_js)
        self.assertNotIn('control.textContent = "Scanning', shell_js)

    def test_system_status_script_exclusively_updates_eye_color(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        status_js = (repo_root / "static" / "system_status.js").read_text()
        dashboard_js = (repo_root / "static" / "dashboard.js").read_text()

        self.assertIn('const EYE_ACCENT = {', status_js)
        self.assertIn('eye.classList.add(EYE_ACCENT[overall])', status_js)
        self.assertNotIn('eye.setAttribute("class", "eye " + oa)', dashboard_js)


if __name__ == "__main__":
    unittest.main()
