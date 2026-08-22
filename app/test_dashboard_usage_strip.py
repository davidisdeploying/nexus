"""Dashboard contract for the wide quota strip and four peer worker cards."""
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent


class DashboardUsageStripTests(unittest.TestCase):
    def test_usage_is_a_separate_wide_projection(self):
        template = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        self.assertIn('id="modelUsageStrip"', template)
        self.assertIn('class="model-usage-grid"', template)
        self.assertIn("usage.tile.provider_usage", template)
        self.assertIn("('charlie', 'delta', 'alpha', 'localworker')", template)
        self.assertIn(
            '{% if s.sub %}<span class="seat-sub">{{ s.sub }}</span>{% endif %}',
            template,
        )
        self.assertIn("<b>worker routing</b>", template)
        self.assertNotIn("('strategy', 'worker')", template)

    def test_live_reconcile_excludes_compatibility_usage_seat(self):
        script = (REPO_ROOT / "static" / "dashboard.js").read_text()
        self.assertIn(
            'var ORDER  = ["charlie","delta","alpha","localworker"];',
            script,
        )
        self.assertIn("renderUsageStrip();", script)
        self.assertIn('effective("worker4")', script)
        self.assertIn("(routing.candidates||[])", script)
        self.assertIn("<b>worker routing</b>", script)

    def test_node_labels_and_localworker_idle_model_are_explicit(self):
        script = (REPO_ROOT / "static" / "dashboard.js").read_text()
        self.assertIn(
            'var LABELS = {charlie:"charlie", delta:"delta", alpha:"alpha", localworker:"Localworker"',
            script,
        )
        self.assertIn('if(!m && t.seat==="localworker")', script)
        self.assertIn('label:"GPT-OSS 20B"', script)
        self.assertIn('if(t.seat!=="localworker"', script)
        self.assertIn('return "alpha";', script)
        self.assertIn('return "charlie";', script)
        self.assertIn('return "delta";', script)

    def test_desktop_columns_align_workers_over_nodes(self):
        css = (REPO_ROOT / "static" / "dashboard.css").read_text()
        self.assertIn(
            'grid-template-areas:"charlie delta alpha localworker"',
            css,
        )
        self.assertIn(
            ".model-usage-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))}",
            css,
        )

    def test_worker_model_badge_fits_full_model_name(self):
        css = (REPO_ROOT / "static" / "dashboard.css").read_text()
        self.assertIn(
            ".seat-tile.has-model .seat-head{padding-right:142px}",
            css,
        )
        self.assertIn("width:fit-content; max-width:136px", css)
        self.assertIn(
            ".seat-tile.has-model .seat-head{padding-right:124px}",
            css,
        )
        self.assertIn("gap:3px; max-width:118px", css)

    def test_worker_routes_align_with_quota_rows(self):
        css = (REPO_ROOT / "static" / "dashboard.css").read_text()
        self.assertIn(
            ".model-route-list{display:grid;gap:7px;margin-top:10px}",
            css,
        )

    def test_evidence_footers_are_small_complete_and_bottom_anchored(self):
        template = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        script = (REPO_ROOT / "static" / "dashboard.js").read_text()
        css = (REPO_ROOT / "static" / "dashboard.css").read_text()
        recommendation_class = 'class="model-route-recommendation"'
        self.assertIn(recommendation_class, template)
        self.assertIn(recommendation_class, script)
        self.assertIn(
            ".model-route-summary,.model-provider-summary{min-width:0;"
            "padding:12px 14px 13px;\n"
            "    display:flex;flex-direction:column}",
            css,
        )
        self.assertIn(
            ".model-route-summary p,.model-provider-summary p{"
            "margin:auto 0 0;padding-top:10px;",
            css,
        )
        self.assertIn("white-space:normal;overflow-wrap:anywhere", css)
        self.assertIn("font-family:var(--mono);font-size:8px;", css)
        self.assertIn("-webkit-text-size-adjust:none;text-size-adjust:none", css)


if __name__ == "__main__":
    unittest.main()
