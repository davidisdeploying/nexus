"""
Runs the PANEL-4 watchdogs bounded-DOM harness (tools/watchdogs-dom-cap/
dom_cap_test.js) as part of the Python test suite -- mirrors
app/test_routes_jobs_classification.py's node-subprocess pattern. Skips if
the bundled node runtime is absent.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_BIN = REPO_ROOT / "tools" / "_runtime" / "node" / "bin" / "node"
HARNESS = REPO_ROOT / "tools" / "watchdogs-dom-cap" / "dom_cap_test.js"


class WatchdogsDomCapTests(unittest.TestCase):
    def test_mount_unmount_bounded_dom_harness_passes(self):
        if not NODE_BIN.exists():
            self.skipTest("bundled node runtime not present at tools/_runtime/node")
        result = subprocess.run(
            [str(NODE_BIN), str(HARNESS)], capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
