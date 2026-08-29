import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import routes


REPO_ROOT = Path(__file__).resolve().parent.parent


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


class ActivityModelUsageTests(unittest.TestCase):
    def test_legacy_model_usage_route_redirects_to_models_lens(self) -> None:
        response = _client().get("/model-usage", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/activity?tab=models")

    def test_models_lens_combines_quota_and_historical_activity(self) -> None:
        template = (REPO_ROOT / "templates" / "activity.html").read_text()
        activity_js = (REPO_ROOT / "static" / "activity.js").read_text()
        usage_js = (REPO_ROOT / "static" / "model_usage.js").read_text()

        self.assertIn('id="models" class="tab-panel models-usage-panel"', template)
        self.assertIn('id="usageBody"', template)
        self.assertIn('id="modelActivityBody"', template)
        self.assertIn('id="usageEvents"', template)
        self.assertIn('data-model-range="all"', template)
        self.assertIn('$("#modelActivityBody").innerHTML', activity_js)
        self.assertNotIn('$("#models").innerHTML', activity_js)
        self.assertIn('document.querySelector("#models.models-usage-panel")', usage_js)

    def test_activity_template_uses_shared_lens_chrome_above_the_body(self) -> None:
        template = (REPO_ROOT / "templates" / "activity.html").read_text()

        self.assertIn("app_shell.activity_header(", template)
        self.assertIn('aria-labelledby="activityLensTitle"', template)
        self.assertNotIn('class="tabs"', template)
        self.assertNotIn('aria-label="Activity view"', template)

    def test_primary_nav_and_generated_links_use_activity_destination(self) -> None:
        shell = (REPO_ROOT / "templates" / "_app_shell.html").read_text()
        dashboard = (REPO_ROOT / "templates" / "dashboard.html").read_text()
        dashboard_js = (REPO_ROOT / "static" / "dashboard.js").read_text()
        watch = (REPO_ROOT / "app" / "model_usage_watch.py").read_text()

        self.assertNotIn('<span>model usage</span>', shell)
        for source in (dashboard, dashboard_js, watch):
            self.assertIn("/activity?tab=models", source)
            self.assertNotIn('href="/model-usage"', source)
            self.assertNotIn('"navigate": "/model-usage"', source)

    def test_design_contract_records_models_as_canonical_home(self) -> None:
        design = (REPO_ROOT / "design-index.md").read_text()
        standard = (REPO_ROOT / "PANEL-STANDARDIZATION.md").read_text()
        self.assertIn("`/activity?tab=models`", design)
        self.assertIn("`/model-usage` is a compatibility redirect", design)
        self.assertIn("`/activity?tab=models`", standard)


if __name__ == "__main__":
    unittest.main()
