"""Regression checks for fixed-height dashboard summary modules."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class DashboardModuleFitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = (ROOT / "templates" / "dashboard.html").read_text()
        cls.css = (ROOT / "static" / "dashboard.css").read_text()
        cls.js = (ROOT / "static" / "dashboard.js").read_text()
        cls.config = (ROOT / "app" / "config.py").read_text()

    def test_requested_summaries_use_the_non_scrolling_body_policy(self) -> None:
        self.assertIn('"jh-semantic-index", none, "jh-module-fit"', self.template)
        self.assertIn('"jh-path", "/activity?tab=workers", "jh-module-fit"', self.template)
        self.assertIn('"jh-module-fit ops-module-body", "healthModule"', self.template)
        self.assertIn(".jh-module-fit{height:var(--module-h)", self.css)
        self.assertIn("overflow:hidden", self.css)

    def test_worker_activity_cap_is_shared_by_first_paint_and_refresh(self) -> None:
        self.assertIn("WORKER_ACTIVITY_PANEL_MAX = 5", self.config)
        self.assertIn("['runs'][:WORKER_ACTIVITY_PANEL_MAX]", self.template)
        self.assertIn('"workerActivityPanelMax": WORKER_ACTIVITY_PANEL_MAX', self.template)
        self.assertIn("const WORKER_ACTIVITY_PANEL_MAX", self.js)
        self.assertIn("runs.slice(0, WORKER_ACTIVITY_PANEL_MAX)", self.js)

    def test_health_projection_removes_expanding_detail_controls(self) -> None:
        self.assertIn(".ops-health .ht-band{height:10px}", self.css)
        self.assertIn(
            ".ops-health .ht-ticks,.ops-health .ht-details-toggle{display:none}",
            self.css,
        )

    def test_stacked_layout_returns_both_body_policies_to_natural_height(self) -> None:
        self.assertIn(".jh-scroll,.jh-module-fit{height:auto", self.css)
        self.assertIn(".ops-module-body.jh-module-fit{overflow:visible}", self.css)


if __name__ == "__main__":
    unittest.main()
