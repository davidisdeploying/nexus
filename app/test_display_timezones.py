"""Human time displays stay Central while API/storage plumbing remains UTC."""
from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timezone
import os
from pathlib import Path

from .routes import _central_header_stamp

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_JS = REPO_ROOT / "static" / "dashboard.js"
ACTIVITY_JS = REPO_ROOT / "static" / "activity.js"
NODE_BIN = Path(os.environ.get("NEXUS_NODE_BIN")) if os.environ.get("NEXUS_NODE_BIN") else REPO_ROOT / "tools" / "_runtime" / "node" / "bin" / "node"
# The runtime is vendored, not committed. Without it these cross-checks
# cannot run at all; that is a missing tool, not a failing assertion.
requires_node = unittest.skipUnless(
    NODE_BIN.exists(), f"no Node runtime at {NODE_BIN}; set NEXUS_NODE_BIN to run"
)
DASHBOARD_BEGIN = "// === BEGIN Central display-time formatters ==="
DASHBOARD_END = "// === END Central display-time formatters ==="
ACTIVITY_BEGIN = "// === BEGIN Activity Central display-time formatter ==="
ACTIVITY_END = "// === END Activity Central display-time formatter ==="


def extract(source: str, begin: str, end: str) -> str:
    start = source.index(begin)
    finish = source.index(end, start) + len(end)
    return source[start:finish]


class ServerFirstPaintTests(unittest.TestCase):
    def test_summer_uses_daylight_offset(self):
        value = datetime(2026, 7, 29, 11, 52, tzinfo=timezone.utc)
        self.assertEqual(_central_header_stamp(value), "2026-07-29 6:52 AM")

    def test_winter_uses_standard_offset(self):
        value = datetime(2026, 1, 29, 12, 52, tzinfo=timezone.utc)
        self.assertEqual(_central_header_stamp(value), "2026-01-29 6:52 AM")

    def test_naive_input_is_treated_as_utc(self):
        value = datetime(2026, 7, 29, 11, 52)
        self.assertEqual(_central_header_stamp(value), "2026-07-29 6:52 AM")


@requires_node
class BrowserDisplayTests(unittest.TestCase):
    def test_fixed_summer_and_winter_examples(self):
        source = DASHBOARD_JS.read_text(encoding="utf-8")
        self.assertIn(
            '? formatCentralHourBucket(summary[key])',
            source,
        )
        formatter_source = extract(source, DASHBOARD_BEGIN, DASHBOARD_END)
        script = (
            formatter_source
            + "\nconsole.log(JSON.stringify({"
            + 'summerStamp:formatCentralStamp(new Date("2026-07-29T11:52:16Z")),'
            + 'summerClock:formatCentralClock(new Date("2026-07-29T11:52:16Z")),'
            + 'winterStamp:formatCentralStamp(new Date("2026-01-29T12:52:16Z")),'
            + 'winterClock:formatCentralClock(new Date("2026-01-29T12:52:16Z")),'
            + 'summerPeak:formatCentralHourBucket("02:00 UTC",new Date("2026-07-29T12:00:00Z")),'
            + 'winterPeak:formatCentralHourBucket("02:00 UTC",new Date("2026-01-29T12:00:00Z"))'
            + "}));"
        )
        result = subprocess.run(
            [str(NODE_BIN), "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(result.stdout),
            {
                "summerStamp": "2026-07-29 6:52 AM",
                "summerClock": "6:52:16 AM",
                "winterStamp": "2026-01-29 6:52 AM",
                "winterClock": "6:52:16 AM",
                "summerPeak": "9:00 PM",
                "winterPeak": "8:00 PM",
            },
        )


@requires_node
class ActivityPeakHourTests(unittest.TestCase):
    def test_peak_hour_uses_current_central_offset(self):
        source = ACTIVITY_JS.read_text(encoding="utf-8")
        formatter_source = extract(source, ACTIVITY_BEGIN, ACTIVITY_END)
        script = (
            formatter_source
            + "\nconsole.log(JSON.stringify({"
            + 'summer:formatPeakHourCentral("02:00 UTC",new Date("2026-07-29T12:00:00Z")),'
            + 'winter:formatPeakHourCentral("02:00 UTC",new Date("2026-01-29T12:00:00Z")),'
            + 'missing:formatPeakHourCentral(null,new Date("2026-07-29T12:00:00Z"))'
            + "}));"
        )
        result = subprocess.run(
            [str(NODE_BIN), "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(result.stdout),
            {
                "summer": "9:00 PM",
                "winter": "8:00 PM",
                "missing": "—",
            },
        )


if __name__ == "__main__":
    unittest.main()
