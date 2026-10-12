"""
Focused stdlib tests for the video-cull adapter's SSH-probe throttle
(FLEET-WORKER2-BUILD-20260723-slate5-video-cull-throttle).

Before this fix, `_adapter_video_cull` fired an unconditional ssh round-trip
to charlie every 300s heartbeat sweep, even when the file-source heartbeat for
'video-cull' already had a fresher, settled-terminal record (the file always
wins the merge in read_jobs() anyway, so that ssh call bought nothing). This
mirrors jobs/gallery.py's proven is_settled_terminal /
TERMINAL_POLL_INTERVAL_SECONDS throttle, scoped to this one adapter call:
  - settled-terminal file source: at most one real ssh probe per
    gpu_job_stale_seconds (900s), not every 300s tick
  - active/unsettled/missing file source: unchanged, polls every tick
  - a failed probe never starts (or extends) the throttle window — the very
    next normal sweep retries, it does not wait out another 900s
  - a process restart (module state reset to None) is always safe — at worst
    one extra early poll, never a stuck throttle

Pure stdlib unittest + unittest.mock, mirroring app/jobs/test_gallery.py's
AdaptivePollingTests conventions.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import work

JOB_ID = work.settings.gpu_job_name
STALE = work.settings.gpu_job_stale_seconds  # existing config number (900) — not invented here


def _settled(ended_at="2026-07-23T00:00:00Z", state="done"):
    return {"job": JOB_ID, "state": state, "ended_at": ended_at, "source": "file"}


def _active():
    return {"job": JOB_ID, "state": "running", "source": "file"}


class VideoCullThrottleTests(unittest.TestCase):
    def setUp(self):
        self._orig = work._video_cull_last_poll_ts
        work._video_cull_last_poll_ts = None

    def tearDown(self):
        work._video_cull_last_poll_ts = self._orig

    def _tick(self, now, by_id=None, ssh_result="ssh-output"):
        with patch.object(work, "_ssh_gpu_probe", return_value=ssh_result) as mock_ssh:
            job = work._adapter_video_cull(now, by_id)
        return job, mock_ssh

    # --- settled-terminal file source: throttled to <=1 ssh call / 900s ---

    def test_settled_terminal_three_ticks_within_900s_polls_once(self):
        by_id = {JOB_ID: _settled()}
        _, ssh1 = self._tick(1000.0, by_id)
        _, ssh2 = self._tick(1300.0, by_id)   # +300s
        _, ssh3 = self._tick(1600.0, by_id)   # +600s
        ssh1.assert_called_once()
        ssh2.assert_not_called()
        ssh3.assert_not_called()

    def test_tick_just_before_900s_does_not_poll(self):
        by_id = {JOB_ID: _settled()}
        self._tick(1000.0, by_id)
        _, ssh2 = self._tick(1000.0 + STALE - 1, by_id)
        ssh2.assert_not_called()

    def test_tick_at_or_after_900s_polls_again(self):
        by_id = {JOB_ID: _settled()}
        self._tick(1000.0, by_id)
        _, ssh2 = self._tick(1000.0 + STALE, by_id)
        ssh2.assert_called_once()

    def test_failed_state_also_counts_as_settled_terminal(self):
        by_id = {JOB_ID: _settled(state="failed")}
        self._tick(1000.0, by_id)
        _, ssh2 = self._tick(1300.0, by_id)
        ssh2.assert_not_called()

    # --- active/unsettled/missing file source: unchanged every-tick polling ---

    def test_active_file_source_polls_every_tick(self):
        by_id = {JOB_ID: _active()}
        _, ssh1 = self._tick(1000.0, by_id)
        _, ssh2 = self._tick(1300.0, by_id)
        _, ssh3 = self._tick(1600.0, by_id)
        ssh1.assert_called_once()
        ssh2.assert_called_once()
        ssh3.assert_called_once()

    def test_missing_file_source_polls_every_tick(self):
        _, ssh1 = self._tick(1000.0, {})
        _, ssh2 = self._tick(1300.0, {})
        ssh1.assert_called_once()
        ssh2.assert_called_once()

    def test_none_by_id_polls_every_tick(self):
        _, ssh1 = self._tick(1000.0, None)
        _, ssh2 = self._tick(1300.0, None)
        ssh1.assert_called_once()
        ssh2.assert_called_once()

    def test_blank_host_disables_adapter_without_ssh_or_job(self):
        with patch.object(work.settings, "gpu_job_ssh_host", ""):
            job, ssh_probe = self._tick(1000.0, {})
        self.assertEqual(job, {})
        ssh_probe.assert_not_called()

    def test_terminal_state_without_ended_at_is_not_trustworthy_polls_every_tick(self):
        by_id = {JOB_ID: {"job": JOB_ID, "state": "done", "source": "file"}}
        _, ssh1 = self._tick(1000.0, by_id)
        _, ssh2 = self._tick(1300.0, by_id)
        ssh1.assert_called_once()
        ssh2.assert_called_once()

    def test_stalled_state_polls_every_tick(self):
        by_id = {JOB_ID: {"job": JOB_ID, "state": "stalled", "ended_at": None, "source": "file"}}
        _, ssh1 = self._tick(1000.0, by_id)
        _, ssh2 = self._tick(1300.0, by_id)
        ssh1.assert_called_once()
        ssh2.assert_called_once()

    # --- failure handling: never masks recovery behind the 900s window ---

    def test_failed_probe_does_not_start_throttle_retries_next_sweep(self):
        by_id = {JOB_ID: _settled()}
        _, ssh1 = self._tick(1000.0, by_id, ssh_result=None)  # ssh unreachable
        ssh1.assert_called_once()
        self.assertIsNone(work._video_cull_last_poll_ts)
        # next normal 300s sweep — must retry immediately, not wait 900s
        _, ssh2 = self._tick(1300.0, by_id, ssh_result=None)
        ssh2.assert_called_once()
        # a later successful poll finally starts the throttle window
        _, ssh3 = self._tick(1600.0, by_id, ssh_result="ok")
        ssh3.assert_called_once()
        self.assertEqual(work._video_cull_last_poll_ts, 1600.0)
        _, ssh4 = self._tick(1900.0, by_id, ssh_result="ok")
        ssh4.assert_not_called()

    # --- process restart: module state resets safely ---

    def test_process_restart_resets_throttle_state_safely(self):
        by_id = {JOB_ID: _settled()}
        self._tick(1000.0, by_id)
        self.assertIsNotNone(work._video_cull_last_poll_ts)
        work._video_cull_last_poll_ts = None       # simulate a fresh process
        _, ssh2 = self._tick(1001.0, by_id)         # immediately after "restart"
        ssh2.assert_called_once()                   # safe: one extra early poll, never stuck

    # --- file-source-wins merge semantics are preserved ---

    def test_file_source_still_wins_merge_regardless_of_throttle(self):
        by_id = {JOB_ID: _settled()}
        merged = dict(by_id)
        for adapter in work._ADAPTERS:
            with patch.object(work, "_ssh_gpu_probe", return_value="ssh-output"):
                j = adapter(1000.0, merged)
            if j and j.get("job") and j["job"] not in merged:
                merged[j["job"]] = j
        self.assertEqual(merged[JOB_ID], by_id[JOB_ID])

    def test_adapter_result_used_when_no_file_source(self):
        merged: dict = {}
        for adapter in work._ADAPTERS:
            with patch.object(work, "_ssh_gpu_probe", return_value=None):
                j = adapter(1000.0, merged)
            if j and j.get("job") and j["job"] not in merged:
                merged[j["job"]] = j
        self.assertIn(JOB_ID, merged)
        self.assertEqual(merged[JOB_ID]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
