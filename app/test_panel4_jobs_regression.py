"""
PANEL-4 Jobs-unchanged regression pin.

PANEL-4 (Watchdogs, a new/separate surface) must not alter Jobs
classification, routes, card data, timer ownership, polling, or mutation
behavior. This pins the exact byte hashes of Jobs-owned files. The baseline
was reconciled on 2026-07-27 after two separately authorized non-Jobs changes:
the committed Compendium probe registration in app/scheduler.py (c49f6a7) and
the unified model-usage/reset card, active worker model badges, and the
quota-aware strategy/worker recommendation inside the seat-board portion of
static/dashboard.js. Reconciled again on 2026-07-28 for the authorized
Model Usage history navigation link and its separate quota-event scheduler.
Reconciled again for the authorized worker-only tiered routing projection and
narrow quota reset/tracker-loss notification policy, plus the wide strip
four-peer-worker layout; Jobs behavior remains untouched.
Reconciled again for the authorized iOS-only routing recommendation text-size
guard; Jobs behavior remains untouched.
Reconciled again for PANEL-U's authorized consolidation of the Compendium
port probe into the existing heartbeat sweep; the separate scheduler entry
was removed while Jobs behavior remains untouched.
Reconciled again for the authorized worker-card alignment change that places
Worker3, Worker1, and Worker2 over their home nodes and Localworker over Temple; Jobs
behavior remains untouched.
Reconciled again for the authorized display-only Central-time header formatters;
the status API, stored timestamps, Jobs behavior, and polling remain UTC/unchanged.
Reconciled again for the authorized 12-hour AM/PM presentation adjustment to
those same display-only formatters; UTC plumbing and Jobs behavior remain unchanged.
Reconciled again to remove the redundant visible timezone suffix for mobile width;
Central conversion, AM/PM presentation, UTC plumbing, and Jobs behavior are unchanged.
Reconciled again to apply the same display-only peak-hour conversion to the
dashboard Activity module; its API bucket and Jobs behavior remain unchanged.
Reconciled again for the authorized CONFORMANCE-2 build
(FLEET-WORKER2-BUILD-20260730-conformance2-signal-durability), which
registered a new "conformance-watch" scheduler job (app/conformance_watch.py)
alongside the existing jobs; Jobs classification, routes, card data, timer
ownership, polling, and mutation behavior remain unchanged.
Reconciled again for the authorized FLEET-AUTO-BUILD-20260802-panel-live-
watchdog-evidence build, which added a scheduler-level EVENT_JOB_EXECUTED/
EVENT_JOB_ERROR/EVENT_JOB_MISSED listener (app/scheduler.py) that persists
per-job execution receipts to events.db, powering the watchdogs projection
layer; Jobs classification, routes, card data, timer ownership, polling, and
mutation behavior remain unchanged.
Reconciled again for the authorized HEALTH-TIMELINE-2 build
(FLEET-WORKER2-BUILD-20260730-health-timeline2), which rewrote the dashboard's
Health Timeline module (static/dashboard.js) — richer bounded history rows,
a 24h projection module, stepped/discrete per-node bands, and an expandable
per-node detail — entirely inside that module's own IIFE and CSS rules; no
Jobs classification, routes, card data, timer ownership, polling, or mutation
behavior was touched.
Reconciled again for the authorized Worker Activity information-architecture
rename; static/dashboard.js now generates `/activity/workers/<token>` links
instead of compatibility `/hero-path/<token>` links. Jobs behavior is unchanged.
Reconciled again for the authorized Model Usage move under Activity;
static/dashboard.js now generates `/activity?tab=models` links instead of the
compatibility `/model-usage` route. Jobs behavior is unchanged.
Reconciled again for the authorized Notifications workspace: the header bell
now performs normal navigation to `/notifications`, while the former feed and
settings sheet handlers and push-preferences controller moved out of
static/dashboard.js into the notification-owned controller. Jobs behavior is
unchanged.
Jobs' template, macro, CSS, behavior, and mutation routes remain unchanged.
(app/routes.py and app/config.py are intentionally excluded
here: PANEL-4 adds new routes/imports to routes.py by design, and its own
test suites -- test_routes_jobs_classification.py, test_api_scheduler_route.py,
test_routes_jobs_sort.py -- already pin _is_watch_guard/scheduler-route
BEHAVIOR directly, which is the actual regression surface that matters.)
Reconciled again on 2026-08-07 for four separately authorized non-Jobs changes
that landed between 2026-08-01 and 2026-08-06: the shared app-icon/eyebrow
consolidation (ba0755f, 37fb5a2), the design-token font migration in
static/jobs.css, the mobile shell control restructure and its multi-bell-count
rework in static/dashboard.js (79ba265, d6029da, 5948c08), and the additive
host_undervolt_alarm stat in templates/_job_row.html (02dc680). Verified before
re-baselining: app/scheduler.py still matched its pin, the diffs touch only
presentation and shared shell wiring, and the Jobs behaviour suites
(test_routes_jobs_classification, test_routes_jobs_sort, test_work_jobs -- 24
tests) pass. Jobs classification, routes, card data, timer ownership, polling,
and mutation behavior remain unchanged.
Reconciled again for the Nexus rename (2026-08-18), which dropped the
"Fleet" prefix from this project's own identity: templates/jobs.html picked up
the new page title, the /static/nexus.css stylesheet name, and the renamed
app_shell.nexus_eye() macro; static/jobs.css picked up the renamed stylesheet in
a comment; app/scheduler.py picked up the renamed "nexus.scheduler" logger.
All three changes are naming-only. Jobs classification, routes, card data, timer
ownership, polling, and mutation behavior remain unchanged.
Reconciled again for the Panel -> Nexus rename (2026-08-18). The dashboard and
control surface was renamed from Panel to Nexus, so these files picked up the new
product name in a page title, the /static/nexus.css stylesheet reference and its
cache-busting variable, the renamed app_shell.nexus_eye() macro, the "nexus.*"
logger namespace, and prose comments. The --panel CSS colour token is unrelated to
the product name and is unchanged. Historical FLEET-{SEAT} dispatch tokens
embedded in comments keep their original slugs and were explicitly restored after
an over-broad first pass rewrote them.
All changes are naming-only. Jobs classification, routes, card data, timer
ownership, polling, and mutation behavior remain unchanged.
Reconciled again for WORKER1-4 (2026-08-18), which renamed the Localworker GPU worker
to Localworker. static/dashboard.js picked up the new seat label, colour key and node
map entry. Naming-only; Jobs behaviour is unchanged.
Reconciled again for WORKER1-1 (2026-08-18), which retired seats in favour of nodes.
static/dashboard.js picked up a node-keyed colour, label, node and order map,
with the legacy seat keys retained so historical run records still render.
Naming-only; Jobs behaviour is unchanged.
Reconciled again for WORKER1-2 (2026-08-18), the node rename alpha->alpha,
delta->delta, charlie->charlie. Both files carry node names in comments and in
the dashboard's node board. Naming-only; Jobs behaviour is unchanged.
Reconciled again for the 2026-08-20 node-card repair: static/dashboard.js now
uses node ids consistently for snapshot and WebSocket reconciliation, removes
retired worker labels from the cards, and keeps Localworker's fixed open-model badge
visible while idle. Jobs behavior is unchanged.
"""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_EXPECTED_HASHES = {
    "templates/jobs.html": "6dd4b2170c427738d26e799036834097d40d55e3a2ae33ce2eabc4164066f3f1",
    "templates/_job_row.html": "13cd24f33cbb853be4b93a0244431e6251fc32d9d65f5b3ade28a9b0b7e47160",
    "static/jobs.css": "51f79ec23a72894f0017535398b5fd1384bb26d0379bed68abadc51264871931",
    "static/dashboard.js": "90af839d04da0993608d7e2a70bb1c39e50329c2891ee81332d162df34d525bb",
    "app/scheduler.py": "fda3fab067d5877b68917b73ef962c12110d926c83d430ef1e2b3d95267ea1b5",
}


class JobsFilesUnchangedTests(unittest.TestCase):
    def test_jobs_owned_files_byte_unchanged(self):
        for rel_path, expected in _EXPECTED_HASHES.items():
            with self.subTest(file=rel_path):
                actual = hashlib.sha256((REPO_ROOT / rel_path).read_bytes()).hexdigest()
                self.assertEqual(actual, expected, f"{rel_path} changed during PANEL-4")


if __name__ == "__main__":
    unittest.main()
