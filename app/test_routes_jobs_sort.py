"""
Focused stdlib tests for the /jobs panel's terminal sort order
(FLEET-WORKER2-BUILD-20260721-panel-gallery-terminal-semantics).

Before this fix, a finished job's recency key fell back to `started` (or
beat_age_s), both of which the old gallery heartbeat re-stamped to "today" on
every tick — so a scan finished 9 days ago perpetually outranked jobs that
had genuinely finished more recently. These tests pin `_job_sort_key` to
prefer `ended_at` for terminal jobs, and confirm static/dashboard.js's
`sortJobsForPanel` mirror produces the identical order for the same fixture
(server/client parity), via a subprocess node --check + eval — no new
dependency, node ships in tools/_runtime/node per this repo's own bundled
runtime.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from . import routes

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_BIN = REPO_ROOT / "tools" / "_runtime" / "node" / "bin" / "node"


def _jobs_fixture():
    return [
        {"job": "old-done", "state": "done", "ended_at": "2026-07-13T03:58:38Z"},
        {"job": "recent-done", "state": "done", "ended_at": "2026-07-21T12:00:00Z"},
        {"job": "still-running", "state": "running", "started": "2026-07-01T00:00:00Z"},
        {"job": "stalled-job", "state": "stalled", "started": "2026-07-20T00:00:00Z"},
        {"job": "legacy-no-ended-at", "state": "done", "started": "2026-07-15T00:00:00Z"},
        {"job": "beat-age-only", "state": "failed", "beat_age_s": 3600},
    ]


class JobSortKeyTests(unittest.TestCase):
    def test_active_states_rank_before_terminal(self):
        jobs = _jobs_fixture()
        now = 1784800000.0
        ordered = sorted(jobs, key=lambda j: routes._job_sort_key(j, now))
        active_ids = {j["job"] for j in ordered[:2]}
        self.assertEqual(active_ids, {"still-running", "stalled-job"})

    def test_terminal_jobs_sort_newest_to_oldest_by_ended_at(self):
        jobs = _jobs_fixture()
        now = 1784800000.0
        ordered = sorted(jobs, key=lambda j: routes._job_sort_key(j, now))
        terminal_ids = [j["job"] for j in ordered if j["state"] not in ("running", "stalled")]
        recent_idx = terminal_ids.index("recent-done")
        old_idx = terminal_ids.index("old-done")
        self.assertLess(recent_idx, old_idx)

    def test_legacy_job_without_ended_at_falls_back_to_started(self):
        job = {"job": "legacy", "state": "done", "started": "2026-07-15T00:00:00Z"}
        active, neg_epoch = routes._job_sort_key(job, 1784800000.0)
        self.assertEqual(active, 1)
        self.assertLess(neg_epoch, 0)   # a real epoch was found, not the 0.0 no-data sentinel


class ServerClientSortParityTests(unittest.TestCase):
    """Feeds the exact same fixture through app/routes.py's _job_sort_key and
    static/dashboard.js's sortJobsForPanel and asserts identical resulting
    order — the server-render and the client's live-patch re-sort must never
    diverge."""

    def test_python_and_js_produce_the_same_order(self):
        if not NODE_BIN.exists():
            self.skipTest("bundled node runtime not present at tools/_runtime/node")
        jobs = _jobs_fixture()
        now = 1784800000.0
        py_order = [j["job"] for j in sorted(jobs, key=lambda j: routes._job_sort_key(j, now))]

        js_src = (REPO_ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
        # Pull just sortJobsForPanel's body out rather than loading the whole
        # browser-global file under node.
        start = js_src.index("function sortJobsForPanel")
        end = js_src.index("\n  }\n", start) + len("\n  }\n")
        fn_src = js_src[start:end]
        script = f"""
{fn_src}
const jobs = {json.dumps(jobs)};
const nowMs = {now * 1000};
const _origNow = Date.now;
Date.now = function() {{ return nowMs; }};
const order = sortJobsForPanel(jobs).map(j => j.job);
Date.now = _origNow;
console.log(JSON.stringify(order));
"""
        result = subprocess.run([str(NODE_BIN), "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        js_order = json.loads(result.stdout.strip())
        self.assertEqual(py_order, js_order)


if __name__ == "__main__":
    unittest.main()
