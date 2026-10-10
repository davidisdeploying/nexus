"""
Focused stdlib tests for the gallery-library-scan heartbeat's terminal
identity/recency semantics and adaptive polling
(FLEET-WORKER2-BUILD-20260721-panel-gallery-terminal-semantics).

Before this fix, `_build_heartbeat()` re-derived `run_id`/`started` from
wall-clock "now"/"today" on every one of its perpetual 60s ticks, so a scan
finished days ago looked freshly restarted every day. These tests pin the
replacement contract: run_id/started/ended_at are STABLE across repeated
samples of the same attempt, `ended_at` is stamped once on the terminal
transition and held fixed thereafter, a genuinely new attempt (terminal ->
active, a progress reset, or queue reactivation) mints a fresh identity, and
the poller stops SSH-ing every 60s once settled in a terminal state.

Pure stdlib unittest + unittest.mock, no pytest dependency, mirroring
app/test_probes.py's conventions.
"""
from __future__ import annotations

import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import gallery
from .gallery import _build_heartbeat, _is_new_attempt, run_gallery_heartbeat


def _sample(total=100, metadata_done=100, bull_depths=None, ml_errors=0, **extra):
    s = {
        "library_assets": total,
        "metadata_done": metadata_done,
        "faces_done": metadata_done,
        "ocr_done": metadata_done,
        "smart_done": metadata_done,
        "thumbnails_done": metadata_done,
        "video_total": 0,
        "video_done": 0,
        "recent_ml_errors": ml_errors,
        "recent_metadata_timeouts": 0,
        "ml_health_status": "healthy",
        "ml_health_rc": 0,
        "containers": "gallery_server\ngallery_machine_learning",
        "db_rc": 0,
        "library_name": "test-lib",
        "gpus": [],
        "bull_depths": bull_depths or {},
    }
    s.update(extra)
    return s


DONE_SAMPLE = _sample(total=100, metadata_done=100)          # state -> done
RUNNING_SAMPLE = _sample(total=100, metadata_done=50)         # state -> running


def _ticking_clock(base=1_700_000_000.0, step=1.0):
    """time.time() patched to strictly increasing values — _build_heartbeat's
    identity minting is second-precision-ish (ms), so two calls made back to
    back in a fast test can otherwise land in the same instant and defeat a
    'must differ' assertion by pure timing luck rather than by logic."""
    return patch("time.time", side_effect=itertools.count(base, step))


class BuildHeartbeatIdentityTests(unittest.TestCase):
    def test_stable_run_id_started_ended_at_across_repeated_done_samples(self):
        with _ticking_clock():
            first = _build_heartbeat(DONE_SAMPLE, prev=None)
            second = _build_heartbeat(DONE_SAMPLE, prev=first)
            third = _build_heartbeat(DONE_SAMPLE, prev=second)
        self.assertEqual(first["state"], "done")
        self.assertIsNotNone(first["ended_at"])

        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["run_id"], third["run_id"])
        self.assertEqual(first["started"], second["started"])
        self.assertEqual(second["started"], third["started"])
        self.assertEqual(first["ended_at"], second["ended_at"])
        self.assertEqual(second["ended_at"], third["ended_at"])
        # ts is the one field allowed to keep moving — it's an observation
        # timestamp, not identity/recency.
        self.assertIn("ts", third)

    def test_running_to_done_transition_stamps_ended_at_once(self):
        with _ticking_clock():
            running = _build_heartbeat(RUNNING_SAMPLE, prev=None)
            done = _build_heartbeat(DONE_SAMPLE, prev=running)
            done_again = _build_heartbeat(DONE_SAMPLE, prev=done)
        self.assertEqual(running["state"], "running")
        self.assertIsNone(running["ended_at"])
        self.assertEqual(done["state"], "done")
        self.assertIsNotNone(done["ended_at"])
        # identity carries over — this is the SAME attempt, just progressed
        self.assertEqual(running["run_id"], done["run_id"])
        self.assertEqual(running["started"], done["started"])
        self.assertEqual(done_again["ended_at"], done["ended_at"])
        self.assertEqual(done_again["run_id"], done["run_id"])

    def test_terminal_to_active_transition_mints_new_attempt(self):
        with _ticking_clock():
            done = _build_heartbeat(DONE_SAMPLE, prev=None)
            restarted = _build_heartbeat(RUNNING_SAMPLE, prev=done)
        self.assertEqual(restarted["state"], "running")
        self.assertIsNone(restarted["ended_at"])
        self.assertNotEqual(restarted["run_id"], done["run_id"])
        self.assertNotEqual(restarted["started"], done["started"])

    def test_progress_reset_while_terminal_mints_new_attempt(self):
        with _ticking_clock():
            done = _build_heartbeat(DONE_SAMPLE, prev=None)
            # a smaller library replacing the old one — done count goes
            # backwards even though the top-level state still computes as "done"
            reset_sample = _sample(total=10, metadata_done=10)
            reset = _build_heartbeat(reset_sample, prev=done)
        self.assertNotEqual(reset["run_id"], done["run_id"])
        self.assertIsNotNone(reset["ended_at"])   # still terminal, freshly stamped

    def test_queue_reactivation_while_state_stays_done_mints_new_attempt(self):
        idle_depths = {
            "metadataExtraction": {"waiting": 0, "active": 0},
            "facialRecognition": {"waiting": 0, "active": 0},
            "ocr": {"waiting": 0, "active": 0},
            "smartSearch": {"waiting": 0, "active": 0},
            "thumbnailGeneration": {"waiting": 0, "active": 0},
            "videoConversion": {"waiting": 0, "active": 0},
        }
        with _ticking_clock():
            done = _build_heartbeat(_sample(total=100, metadata_done=100, bull_depths=idle_depths), prev=None)
            reactivated_depths = dict(idle_depths)
            reactivated_depths["facialRecognition"] = {"waiting": 3, "active": 1}
            reactivated_sample = _sample(total=100, metadata_done=100, bull_depths=reactivated_depths)
            reactivated = _build_heartbeat(reactivated_sample, prev=done)
        self.assertEqual(done["state"], "done")
        # metadata_done >= total still holds, so top-level state reads "done"
        # again this same tick — but queue reactivation from a fully idle
        # terminal state is itself evidence of a new attempt.
        self.assertEqual(reactivated["state"], "done")
        self.assertNotEqual(reactivated["run_id"], done["run_id"])

    def test_transient_failure_fallback_prev_does_not_trigger_false_reactivation(self):
        """A heartbeat written by run_gallery_heartbeat's exception fallback has
        no `queues` key. _is_new_attempt must not read that absence as 'every
        queue was idle' and falsely mint a new attempt the next time a real
        sample succeeds and shows normal in-progress queue activity."""
        fallback_prev = {"job": gallery.JOB_ID, "state": "failed", "ended_at": "2026-07-20T00:00:00Z",
                         "run_id": "gallery-library-scan-1700000000", "started": "2026-07-19T00:00:00Z"}
        active_depths = {"facialRecognition": {"waiting": 2, "active": 1}}
        recovered = _build_heartbeat(_sample(total=100, metadata_done=100, bull_depths=active_depths),
                                     prev=fallback_prev)
        self.assertEqual(recovered["run_id"], fallback_prev["run_id"])
        self.assertEqual(recovered["started"], fallback_prev["started"])


class IsNewAttemptTests(unittest.TestCase):
    def test_no_prev_is_new(self):
        self.assertTrue(_is_new_attempt(None, "done", 100, []))

    def test_prev_running_never_forces_new_attempt(self):
        prev = {"state": "running", "done": 50, "queues": []}
        self.assertFalse(_is_new_attempt(prev, "running", 60, []))
        self.assertFalse(_is_new_attempt(prev, "done", 100, []))


class _FakeSettings:
    def __init__(self, heartbeats_dir):
        self.heartbeats_dir = heartbeats_dir


class AdaptivePollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_last_poll = gallery._last_poll_ts
        gallery._last_poll_ts = None
        self._tmpdir = tempfile.TemporaryDirectory()
        self._path = Path(self._tmpdir.name) / f"{gallery.JOB_ID}.json"
        # settings.heartbeats_dir is a read-only pydantic-computed property —
        # swap the whole module-level `settings` name for a plain fake instead
        # of trying to setattr through the property.
        self._settings_patch = patch.object(gallery, "settings", _FakeSettings(Path(self._tmpdir.name)))
        self._settings_patch.start()

    def tearDown(self):
        self._settings_patch.stop()
        self._tmpdir.cleanup()
        gallery._last_poll_ts = self._orig_last_poll

    async def _tick(self, now, sample=DONE_SAMPLE):
        with patch("time.time", return_value=now), \
             patch.object(gallery, "_run_remote", return_value=sample) as mock_remote:
            await run_gallery_heartbeat()
        return mock_remote

    async def test_first_tick_always_polls(self):
        mock_remote = await self._tick(1_000_000.0)
        mock_remote.assert_called_once()
        self.assertTrue(self._path.exists())

    async def test_skipped_terminal_tick_does_not_poll_or_rewrite_before_15_minutes(self):
        await self._tick(1_000_000.0)                       # settles into done
        mtime_before = self._path.stat().st_mtime
        content_before = self._path.read_text()

        mock_remote = await self._tick(1_000_000.0 + 60)    # one normal 60s tick later
        mock_remote.assert_not_called()
        self.assertEqual(self._path.stat().st_mtime, mtime_before)
        self.assertEqual(self._path.read_text(), content_before)

    async def test_discovery_poll_fires_after_15_minutes(self):
        await self._tick(1_000_000.0)
        mock_remote = await self._tick(1_000_000.0 + gallery.TERMINAL_POLL_INTERVAL_SECONDS + 1)
        mock_remote.assert_called_once()

    async def test_active_scan_resumes_1min_polling_immediately(self):
        await self._tick(1_000_000.0)                       # done, settles
        await self._tick(1_000_000.0 + gallery.TERMINAL_POLL_INTERVAL_SECONDS + 1,
                         sample=RUNNING_SAMPLE)               # discovery poll finds it running again
        # next tick just 60s later — must poll again immediately, no throttle
        mock_remote = await self._tick(
            1_000_000.0 + gallery.TERMINAL_POLL_INTERVAL_SECONDS + 61, sample=RUNNING_SAMPLE)
        mock_remote.assert_called_once()

    async def test_transient_ssh_failure_keeps_polling_at_normal_cadence(self):
        await self._tick(1_000_000.0)                        # settles into done
        # discovery-poll boundary hasn't been reached, but a raw connectivity
        # failure (no prior settled read) must not itself start a 15m throttle
        with patch("time.time", return_value=1_000_000.0 + gallery.TERMINAL_POLL_INTERVAL_SECONDS + 1), \
             patch.object(gallery, "_run_remote", side_effect=RuntimeError("ssh timeout")):
            await run_gallery_heartbeat()
        written = json.loads(self._path.read_text())
        self.assertEqual(written["state"], "failed")
        # a bare connectivity failure isn't a "settled" terminal sample (no
        # queues) so the very next tick must poll again, not wait 15 more min.
        mock_remote = await self._tick(
            1_000_000.0 + gallery.TERMINAL_POLL_INTERVAL_SECONDS + 61, sample=DONE_SAMPLE)
        mock_remote.assert_called_once()


class BuildHeartbeatKindTests(unittest.TestCase):
    """PANEL-2 kind-taxonomy build (H2): gallery is never watch/guard, so
    _build_heartbeat always stamps kind="job", regardless of running/done
    state — no state-dependent branching needed."""

    def test_done_sample_has_kind_job(self):
        self.assertEqual(_build_heartbeat(DONE_SAMPLE, prev=None)["kind"], "job")

    def test_running_sample_has_kind_job(self):
        self.assertEqual(_build_heartbeat(RUNNING_SAMPLE, prev=None)["kind"], "job")


if __name__ == "__main__":
    unittest.main()
