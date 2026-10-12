"""
Focused stdlib test that a STABLE run_id (the gallery heartbeat fix in
app/jobs/gallery.py) makes milestone_watch's one-shot scan-complete
notification fire at most once, instead of re-firing on every UTC-midnight
run_id rollover the way the old date-derived run_id did
(FLEET-WORKER2-BUILD-20260721-panel-gallery-terminal-semantics).

Per the recon and build prompt, milestone_watch.py itself is NOT hand-patched
— its existing one-shot-per-run_id watermark logic is correct; it only ever
misbehaved because the run_id it was keyed on used to change every day. This
test proves the watermark logic alone is sufficient once run_id is stable, so
no change to milestone_watch.py was warranted.

Uses a real temp-file-backed notify_store (isolated from the live events.db
via DB_PATH monkeypatching) so the actual watermark SQL is exercised; only
the single outward call — app.milestone_watch.notify — is mocked, since a
real notify() would classify/render/dedup/push against live tables far
outside this fix's scope.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import notify_store
from .milestone_watch import evaluate_heartbeat

STABLE_RUN_ID = "gallery-library-scan-1784678400000"


class MilestoneStableRunIdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = Path(self._tmpdir.name) / "events.db"
        notify_store.init_db()

    def tearDown(self):
        notify_store.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    async def test_stable_run_id_fires_scan_complete_at_most_once(self):
        running_hb = {"run_id": STABLE_RUN_ID, "state": "running", "queues": []}
        done_hb = {"run_id": STABLE_RUN_ID, "state": "done", "queues": []}

        with patch("app.milestone_watch.notify", new=AsyncMock(return_value={"suppressed": False})) as mock_notify:
            # first-ever evaluation: not complete yet -> seeds "pending", no fire
            r1 = await evaluate_heartbeat(running_hb)
            self.assertFalse(r1["scan_complete"]["fired"])
            self.assertEqual(mock_notify.call_count, 0)

            # genuine completion edge, SAME run_id (the fix) -> fires exactly once
            r2 = await evaluate_heartbeat(done_hb)
            self.assertTrue(r2["scan_complete"]["fired"])
            self.assertEqual(mock_notify.call_count, 1)

            # 25 hours later in wall-clock terms, still the SAME run_id (a
            # stable identity no longer rolls at UTC midnight) and still done
            # -> must NOT fire again
            r3 = await evaluate_heartbeat(done_hb)
            self.assertFalse(r3["scan_complete"]["fired"])
            self.assertEqual(mock_notify.call_count, 1)

    async def test_old_daily_run_id_rollover_would_have_refired(self):
        """Documents the bug this replaces: WITHOUT a stable run_id (i.e. a
        fresh run_id per day, as the old gallery.py minted), the same
        already-done scan re-fires on every new run_id — this is the daily
        notification-spam bug the recon found live in events.db. The
        cold-start day seeds (WATERMARK-FROM-NOW) without firing; each
        SUBSEQUENT day's fresh run_id resets the watermark and fires again."""
        with patch("app.milestone_watch.notify", new=AsyncMock(return_value={"suppressed": False})) as mock_notify:
            await evaluate_heartbeat({"run_id": "gallery-library-scan-20260720", "state": "done", "queues": []})
            await evaluate_heartbeat({"run_id": "gallery-library-scan-20260721", "state": "done", "queues": []})
            await evaluate_heartbeat({"run_id": "gallery-library-scan-20260722", "state": "done", "queues": []})
        self.assertEqual(mock_notify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
