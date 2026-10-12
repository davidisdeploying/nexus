"""
Focused stdlib tests for the Jobs UI watch/guard classification
(PANEL-2 final compat retirement,
FLEET-WORKER2-BUILD-20260723-slate2-final-compat-retirement).

app/routes.py's `_is_watch_guard()` (mirrored by static/dashboard.js's
`isWatchGuardJob()`) decides whether a heartbeats/*.json record is a
watch/guard sidecar to hide from the Jobs panel, vs. a real job to show.

This build retires the pre-kind-field legacy-id fallback
(config.py's former JOB_NONJOB_LEGACY_IDS): every live producer now either
sets an explicit kind/type, or its pre-kind-field record was archived out of
heartbeats/ entirely (the two a1v2-watchdog* records, moved to
heartbeats/archive/). Classification is by explicit kind/type only — a
missing or unrecognized kind is job-compatible (not filtered), including for
the old legacy ids themselves, which get no special treatment anymore.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from . import routes

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_BIN = REPO_ROOT / "tools" / "_runtime" / "node" / "bin" / "node"


class IsWatchGuardTests(unittest.TestCase):
    def test_explicit_kind_watch_is_filtered(self):
        self.assertTrue(routes._is_watch_guard({"job": "some-job", "kind": "watch"}))

    def test_explicit_kind_guard_is_filtered(self):
        self.assertTrue(routes._is_watch_guard({"job": "some-job", "kind": "guard"}))

    def test_explicit_type_watch_is_filtered(self):
        self.assertTrue(routes._is_watch_guard({"job": "some-job", "type": "watch"}))

    def test_explicit_type_guard_is_filtered(self):
        self.assertTrue(routes._is_watch_guard({"job": "some-job", "type": "guard"}))

    def test_explicit_kind_job_is_not_filtered(self):
        self.assertFalse(routes._is_watch_guard({"job": "some-job", "kind": "job"}))

    def test_normal_job_no_kind_is_not_filtered(self):
        self.assertFalse(routes._is_watch_guard({"job": "gallery-library-scan"}))

    def test_missing_kind_is_not_filtered(self):
        """Missing kind is job-compatible now that the legacy-id fallback is
        gone — a record with no kind/type is never filtered on id alone."""
        self.assertFalse(routes._is_watch_guard({"job": "some-job"}))

    def test_unrecognized_kind_is_not_filtered(self):
        self.assertFalse(routes._is_watch_guard({"job": "some-job", "kind": "typo"}))

    def test_former_legacy_ids_get_no_special_treatment(self):
        """The old legacy ids (thermal-guard-charlie, a1v2-watchdog,
        a1v2-watchdog3) are no longer special-cased by id at all: with no
        kind set they're job-compatible like any other record. (The two
        a1v2 records were archived out of heartbeats/ as part of this build;
        thermal-guard-charlie's live producer already sets kind="guard", so
        it's still filtered — via the explicit-kind path, not the id.)"""
        for legacy_id in ("thermal-guard-charlie", "a1v2-watchdog", "a1v2-watchdog3"):
            with self.subTest(legacy_id=legacy_id):
                self.assertFalse(routes._is_watch_guard({"job": legacy_id}))

    def test_former_legacy_id_with_explicit_kind_guard_is_filtered(self):
        self.assertTrue(
            routes._is_watch_guard({"job": "thermal-guard-charlie", "kind": "guard"})
        )


def _classification_fixture():
    return [
        {"job": "some-job-a", "kind": "watch"},
        {"job": "some-job-b", "kind": "guard"},
        {"job": "some-job-c", "type": "watch"},
        {"job": "some-job-d", "type": "guard"},
        {"job": "thermal-guard-charlie", "kind": "guard"},
        {"job": "thermal-guard-charlie"},
        {"job": "a1v2-watchdog"},
        {"job": "a1v2-watchdog3"},
        {"job": "gallery-library-scan"},
        {"job": "a1v2-watchdog", "kind": "job"},
        {"job": "some-job-e", "kind": "typo"},
        {"job": "a1v2-watchdog3", "kind": "typo"},
    ]


class ServerClientClassificationParityTests(unittest.TestCase):
    """Feeds the same fixture through app/routes.py's _is_watch_guard and
    static/dashboard.js's isWatchGuardJob and asserts identical per-job
    filtering decisions — the third call site (/api/status live-refresh)
    must never diverge from the server-rendered /jobs and /  routes."""

    def test_python_and_js_agree_on_every_fixture_job(self):
        if not NODE_BIN.exists():
            self.skipTest("bundled node runtime not present at tools/_runtime/node")
        jobs = _classification_fixture()
        py_flags = [bool(routes._is_watch_guard(j)) for j in jobs]

        js_src = (REPO_ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
        const_start = js_src.index("const JOB_NONJOB_KINDS")
        fn_start = js_src.index("function isWatchGuardJob", const_start)
        fn_end = js_src.index("\n  }\n", fn_start) + len("\n  }\n")
        snippet = js_src[const_start:fn_end]
        script = f"""
{snippet}
const jobs = {json.dumps(jobs)};
const flags = jobs.map(isWatchGuardJob);
console.log(JSON.stringify(flags));
"""
        result = subprocess.run([str(NODE_BIN), "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        js_flags = json.loads(result.stdout.strip())
        self.assertEqual(py_flags, js_flags)


if __name__ == "__main__":
    unittest.main()
