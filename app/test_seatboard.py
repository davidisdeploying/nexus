"""
Focused tests for the worker2-local dead-PID liveness check in seatboard._scan_seat
(FLEET-WORKER2-BUILD-20260722-panel-worker2-dead-pid).

Root cause (FLEET-WORKER2-RECON-20260722-tower-duplicate-run-forensics /
FLEET-WORKER2-RECON-20260722-tower-duplicate-collision-state): a worker2 run
whose process died without leaving a `done` sentinel kept reading as
`running` for up to ORPHAN_AFTER_S (6h) purely off heartbeat/mtime, pinning
the Worker2 seat card BUSY long after the process was gone and long after
newer worker2 runs had actually finished. worker2's run.sh and this Nexus
process share ONE host (alpha), so its status.json `pid` can be checked
against /proc directly here -- unlike the four genuinely remote seats.

These tests exercise both layers:
  - `_worker2_pid_state` / `_status_pid` directly (unit-level, real short-lived
    subprocesses for the alive/dead cases, so there's no dependency on
    guessing an unused pid number)
  - `_scan_seat` / `_tile` (seat-level, via a monkeypatched `RELAY`), which is
    where the BUSY badge actually gets decided
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from . import seatboard


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _spawn_marked(token: str, duration: float = 5.0) -> subprocess.Popen:
    """A live process that stays up for `duration` seconds with `token` sitting
    in its own argv (so /proc/<pid>/cmdline contains it verbatim). Runs the
    interpreter directly -- no shell wrapper -- so there's no exec-tail-call
    risk of the token disappearing from cmdline mid-test the way `sleep N
    <token>` would (coreutils sleep treats extra args as more durations and
    errors out near-instantly, which is NOT alive-with-matching-cmdline)."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({duration})", token]
    )


def _write_run(runs_dir: Path, token: str, *, lane: str = "recon",
               pid: int | None = None, started_ago: float = 60.0,
               mtime_ago: float = 60.0, done_code: str | None = None,
               provider: str | None = None,
               model: str | None = None) -> Path:
    """A synthetic from-{seat}/runs/<token>/ dir: status.json (+ optional pid),
    an optional `done` sentinel, and a directory mtime set `mtime_ago` seconds
    in the past (what _scan_seat's ORPHAN_AFTER_S check keys off)."""
    d = runs_dir / token
    d.mkdir(parents=True)
    sj = {"token": token, "lane": lane, "started_at": _iso(time.time() - started_ago)}
    if pid is not None:
        sj["pid"] = pid
    if provider is not None:
        sj["provider"] = provider
    if model is not None:
        sj["model"] = model
    (d / "status.json").write_text(json.dumps(sj))
    if done_code is not None:
        (d / "done").write_text(done_code)
    mt = time.time() - mtime_ago
    os.utime(d, (mt, mt))
    return d


class Worker2PidStateTests(unittest.TestCase):
    """Direct tests of the bounded liveness probe."""

    def test_reaped_pid_is_confirmed_dead(self):
        p = subprocess.Popen(["true"])
        dead_pid = p.pid
        p.wait()
        self.assertEqual(
            seatboard._worker2_pid_state(dead_pid, "some-token", Path("/tmp/some-run")),
            "dead",
        )

    def test_live_pid_with_matching_cmdline_is_alive(self):
        token = "seatboard-test-live-marker-abc123"
        p = _spawn_marked(token)
        try:
            self.assertEqual(
                seatboard._worker2_pid_state(p.pid, token, Path("/tmp/whatever")),
                "alive",
            )
        finally:
            p.terminate()
            p.wait()

    def test_live_pid_with_mismatched_cmdline_is_treated_as_reused_pid(self):
        # A live process that exists but whose cmdline names neither the token
        # nor the run dir isn't OUR run any more -- guards against PID reuse.
        p = _spawn_marked("some-other-unrelated-marker")
        try:
            self.assertEqual(
                seatboard._worker2_pid_state(
                    p.pid, "totally-unrelated-token", Path("/tmp/unrelated-run")
                ),
                "dead",
            )
        finally:
            p.terminate()
            p.wait()

    def test_permission_error_reading_cmdline_is_unknown_not_dead_or_alive(self):
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            state = seatboard._worker2_pid_state(os.getpid(), "tok", Path("/tmp/run"))
        self.assertEqual(state, "unknown")


class StatusPidParsingTests(unittest.TestCase):
    def _pid_for(self, payload) -> int | None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "status.json"
            p.write_text(json.dumps(payload))
            return seatboard._status_pid(p)

    def test_missing_pid_key_is_none(self):
        self.assertIsNone(self._pid_for({"token": "x"}))

    def test_non_integer_pid_is_none(self):
        self.assertIsNone(self._pid_for({"pid": "834805"}))

    def test_negative_or_zero_pid_is_none(self):
        self.assertIsNone(self._pid_for({"pid": 0}))
        self.assertIsNone(self._pid_for({"pid": -5}))

    def test_malformed_json_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "status.json"
            p.write_text("not json{{")
            self.assertIsNone(seatboard._status_pid(p))


class ScanSeatWorker2LivenessTests(unittest.TestCase):
    """Seat-level: this is what actually decides the BUSY badge."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.relay = Path(self._tmp.name)
        patcher = mock.patch.object(seatboard, "RELAY", self.relay)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._procs: list[subprocess.Popen] = []
        self.addCleanup(self._reap_spawned)

    def _reap_spawned(self):
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
            p.wait()

    def _spawn(self, token: str) -> subprocess.Popen:
        p = _spawn_marked(token)
        self._procs.append(p)
        return p

    def test_dead_worker2_pid_with_fresh_heartbeat_excluded_from_live(self):
        runs = self.relay / "from-worker2" / "runs"
        p = subprocess.Popen(["true"])
        dead_pid = p.pid
        p.wait()
        _write_run(runs, "FLEET-WORKER2-RECON-zombie", pid=dead_pid,
                   started_ago=300, mtime_ago=60)  # fresh -- well under ORPHAN_AFTER_S

        runs_out = seatboard._scan_seat("worker2", time.time())
        self.assertEqual(len(runs_out), 1)
        rec = runs_out[0]
        self.assertEqual(rec["state"], "died")
        self.assertFalse(rec["running"])

    def test_newer_completed_run_not_masked_by_older_dead_pid_zombie(self):
        runs = self.relay / "from-worker2" / "runs"
        p = subprocess.Popen(["true"])
        dead_pid = p.pid
        p.wait()
        _write_run(runs, "FLEET-WORKER2-RECON-zombie-old", pid=dead_pid,
                   started_ago=600, mtime_ago=500)
        _write_run(runs, "FLEET-WORKER2-BUILD-finished-new", pid=None,
                   started_ago=200, mtime_ago=100, done_code="0")

        tile = seatboard._tile("worker2", "alpha", "green", "Worker2", time.time())
        self.assertEqual(tile["state"], "done")
        self.assertEqual(tile["badge"], "FREE")
        self.assertEqual(tile["full_token"], "FLEET-WORKER2-BUILD-finished-new")

    def test_live_worker2_run_stays_busy_and_newest_concurrent_run_wins(self):
        runs = self.relay / "from-worker2" / "runs"
        token_a = "FLEET-WORKER2-RECON-concurrent-a"
        token_b = "FLEET-WORKER2-RECON-concurrent-b"
        p_a = self._spawn(token_a)
        p_b = self._spawn(token_b)
        _write_run(runs, token_a, pid=p_a.pid, started_ago=120, mtime_ago=5)
        _write_run(runs, token_b, pid=p_b.pid, started_ago=30, mtime_ago=5)

        tile = seatboard._tile("worker2", "alpha", "green", "Worker2", time.time())
        self.assertEqual(tile["state"], "busy")
        self.assertEqual(tile["badge"], "BUSY")
        self.assertEqual(tile["full_token"], token_b)  # later started_at wins

    def test_remote_seat_pid_never_checked_against_alpha_proc(self):
        runs = self.relay / "from-worker1" / "runs"
        # An obviously-nonexistent pid. If this were ever probed against
        # /proc (it must not be -- worker1 is remote, its pid means nothing on
        # alpha), os.kill would raise ProcessLookupError and wrongly mark
        # this run died despite the fresh heartbeat.
        _write_run(runs, "FLEET-WORKER1-BUILD-remote-example", pid=999999999,
                   started_ago=120, mtime_ago=30)

        runs_out = seatboard._scan_seat("worker1", time.time())
        self.assertEqual(len(runs_out), 1)
        rec = runs_out[0]
        self.assertEqual(rec["state"], "running")
        self.assertTrue(rec["running"])

    def test_active_relay_run_exposes_actual_model_badge(self):
        runs = self.relay / "from-worker1" / "runs"
        _write_run(
            runs,
            "FLEET-WORKER1-BUILD-model-badge",
            lane="prompts",
            provider="codex",
            model="gpt-5.6-terra",
            started_ago=30,
            mtime_ago=5,
        )
        tile = seatboard._tile("worker1", "delta", "cyan", "Worker1", time.time())
        self.assertEqual(tile["state"], "busy")
        self.assertEqual(
            tile["model_badge"],
            {"family": "codex", "label": "GPT-5.6 Terra", "mark": "◎"},
        )

    def test_unknown_probe_outcome_falls_back_to_heartbeat_mtime_both_ways(self):
        runs = self.relay / "from-worker2" / "runs"
        _write_run(runs, "FLEET-WORKER2-RECON-fresh-unknown", pid=4242,
                   started_ago=60, mtime_ago=60)  # fresh -- fallback keeps it running
        _write_run(runs, "FLEET-WORKER2-RECON-stale-unknown", pid=4243,
                   started_ago=8 * 3600, mtime_ago=8 * 3600)  # past ORPHAN_AFTER_S

        with mock.patch.object(seatboard, "_worker2_pid_state", return_value="unknown"):
            runs_out = {r["token"]: r for r in seatboard._scan_seat("worker2", time.time())}

        fresh = runs_out["FLEET-WORKER2-RECON-fresh-unknown"]
        stale = runs_out["FLEET-WORKER2-RECON-stale-unknown"]
        self.assertTrue(fresh["running"])
        self.assertEqual(fresh["state"], "running")
        self.assertFalse(stale["running"])
        self.assertEqual(stale["state"], "died")


class ControlWorkerBadgeTests(unittest.TestCase):
    def test_active_gemini_worker_drives_gemini_badge(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            relay = root / "relay"
            runs = root / "agy-runs"
            job = runs / "agy-delta-test"
            job.mkdir(parents=True)
            (job / "run.json").write_text(json.dumps({
                "job_id": "agy-delta-test",
                "host": "delta",
                "state": "running",
                "started_at": time.time() - 10,
                "timeout_seconds": 900,
                "provider": "gemini",
                "model": "gemini-3.6-flash-high",
            }))
            with (
                mock.patch.object(seatboard, "RELAY", relay),
                mock.patch.object(seatboard, "CONTROL_RUN_ROOTS", (runs,)),
            ):
                tile = seatboard._tile(
                    "worker1", "delta", "cyan", "Worker1", time.time()
                )
            self.assertEqual(tile["state"], "busy")
            self.assertIsNone(tile["full_token"])
            self.assertEqual(
                tile["model_badge"],
                {
                    "family": "gemini",
                    "label": "Gemini 3.6 Flash",
                    "mark": "✦",
                },
            )


class NodeCardSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.relay = Path(self._tmp.name)
        patcher = mock.patch.object(seatboard, "RELAY", self.relay)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_current_node_root_drives_current_task_and_model(self):
        token = "FLEET-CHARLIE-BUILD-node-card"
        _write_run(
            self.relay / "from-charlie" / "runs",
            token,
            provider="claude",
            model="sonnet",
            started_ago=20,
            mtime_ago=5,
        )
        tile = seatboard._tile(
            "charlie", "charlie", "amber", "charlie", time.time(),
            ("charlie", "worker3"),
        )
        self.assertEqual(tile["state"], "busy")
        self.assertEqual(tile["full_token"], token)
        self.assertEqual(
            tile["model_badge"],
            {"family": "claude", "label": "Claude Sonnet", "mark": "✳"},
        )

    def test_legacy_root_remains_a_read_only_fallback(self):
        token = "FLEET-WORKER3-BUILD-legacy-card"
        _write_run(
            self.relay / "from-worker3" / "runs",
            token,
            provider="codex",
            model="gpt-5.6-terra",
            started_ago=20,
            mtime_ago=5,
        )
        tile = seatboard._tile(
            "charlie", "charlie", "amber", "charlie", time.time(),
            ("charlie", "worker3"),
        )
        self.assertEqual(tile["full_token"], token)
        self.assertEqual(tile["model_badge"]["label"], "GPT-5.6 Terra")

    def test_duplicate_token_across_roots_is_counted_once(self):
        token = "FLEET-CHARLIE-BUILD-migrated-copy"
        current = _write_run(
            self.relay / "from-charlie" / "runs", token,
            started_ago=30, mtime_ago=3,
        )
        _write_run(
            self.relay / "from-worker3" / "runs", token,
            started_ago=300, mtime_ago=30,
        )
        with mock.patch.object(
            seatboard, "_median_duration", return_value=None
        ) as median:
            tile = seatboard._tile(
                "charlie", "charlie", "amber", "charlie", time.time(),
                ("charlie", "worker3"),
            )
        self.assertEqual(tile["full_token"], token)
        runs_seen = median.call_args.args[0]
        self.assertEqual([run["token"] for run in runs_seen].count(token), 1)
        self.assertEqual(runs_seen[0]["mtime"], current.stat().st_mtime)

    def test_localworker_reads_localworker_root_and_keeps_open_model_badge(self):
        token = "FLEET-LOCALWORKER-RECON-open-model"
        _write_run(
            self.relay / "from-localworker" / "runs", token,
            started_ago=20, mtime_ago=5,
        )
        tile = seatboard._tile(
            "localworker", "charlie", "emerald", "Localworker", time.time(),
            ("localworker",),
        )
        self.assertEqual(tile["full_token"], token)
        self.assertEqual(tile["model_badge"]["label"], "GPT-OSS 20B")


if __name__ == "__main__":
    unittest.main()
