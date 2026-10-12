from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from jinja2 import Environment, FileSystemLoader

from app import heartbeat_runner as heartbeat_module
from app.models import Health, StatusSnapshot


REPO_ROOT = Path(__file__).resolve().parent.parent


class ModuleShellTests(unittest.TestCase):
    def test_dashboard_uses_shared_shell_for_all_standard_modules(self):
        source = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        self.assertIn('import "_module_shell.html" as modules', source)
        self.assertEqual(source.count("modules.module_shell("), 9)

    def test_operations_modules_form_an_ordered_peer_grid(self):
        source = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        titles = (
            'module_shell("indexes"',
            'module_shell("watchdogs"',
            'module_shell("health"',
            'module_shell("conformance"',
        )
        self.assertIn('class="operations-dashboard-grid panel-reveal"', source)
        positions = [source.index(title) for title in titles]
        self.assertEqual(positions, sorted(positions))

        css = (REPO_ROOT / "static" / "dashboard.css").read_text()
        self.assertIn(
            ".activity-dashboard-grid,.operations-dashboard-grid{display:grid; "
            "grid-template-columns:repeat(2,minmax(0,1fr))",
            css,
        )
        self.assertIn(".ops-module-body{padding:10px 12px", css)

    def test_shell_renders_optional_link_and_body_attributes(self):
        env = Environment(loader=FileSystemLoader(REPO_ROOT / "templates"))
        rendered = env.from_string(
            '{% import "_module_shell.html" as modules %}'
            # 37fb5a2 ("Remove decorative rune eyebrows") dropped the subtitle
            # parameter; the macro now takes 6, not 7.
            '{% call modules.module_shell("title", "kind", "/more", '
            '"jh-scroll body", "body-id", "polite") %}content{% endcall %}'
        ).render()
        self.assertIn('class="jh-col kind"', rendered)
        self.assertIn('href="/more"', rendered)
        self.assertIn('class="jh-scroll body" id="body-id" aria-live="polite"', rendered)
        self.assertIn("content", rendered)


class SemanticIndexProbeConsolidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_completed_heartbeat_refreshes_semantic_index_cache(self):
        snapshot = StatusSnapshot(
            generated_at="2026-07-28T00:00:00+00:00",
            duration_ms=1,
            overall=Health.OK,
            nodes=[],
        )
        runner = heartbeat_module.HeartbeatRunner()
        with (
            patch.object(heartbeat_module, "run_heartbeat", AsyncMock(return_value=snapshot)),
            patch.object(heartbeat_module, "probe_maint_once", AsyncMock()) as probe,
        ):
            result = await runner.run()
        self.assertIs(result.snap, snapshot)
        probe.assert_awaited_once_with()

    def test_scheduler_has_no_separate_semantic_index_job(self):
        source = (REPO_ROOT / "app" / "scheduler.py").read_text()
        self.assertNotIn('id="semantic-index-probe"', source)
        self.assertNotIn("compendium_probe_once", source)


if __name__ == "__main__":
    unittest.main()
