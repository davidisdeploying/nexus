"""
Focused stdlib tests for the generic file-heartbeat job card's terminal
display coherence (FLEET-WORKER2-BUILD-20260721-panel-gallery-terminal-semantics).

_job_from_file is the SHARED, job-agnostic reader (app/work.py's own docstring:
"no gallery-specific code on the read side") behind every heartbeats/<job>.json
card, so these tests exercise it directly rather than any one producer:
  - a terminal job's DISPLAYED age comes from `ended_at`, not the latest
    poll's `ts` (a steady terminal state must age normally, not read "just
    now" forever)
  - a `state: done` card always shows pct=100 at the card level, even when
    the raw done/total ratio intentionally sits under 100% (a non-additive
    aggregate, e.g. gallery's queue totals)
  - a RUNNING job's freshness is untouched — still driven by `ts`
  - a done job with no `ended_at` at all (some other producer) falls back to
    the existing ts-based age rather than breaking
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from . import work


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _job_from_hb(hb: dict, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "some-job.json"
        p.write_text(json.dumps(hb), encoding="utf-8")
        return work._job_from_file(p, now)


class TerminalAgeAndPctTests(unittest.TestCase):
    def test_signalled_stop_with_ended_at_is_terminal_not_stalled(self):
        now = time.time()
        hb = {
            "job": "temple-1080p-conversion", "state": "stopped",
            "message": "stopped (signalled)", "ts": now - 6 * 3600,
            "ended_at": _iso(now - 6 * 3600),
            "done": 22169, "total": 24789,
        }
        job = _job_from_hb(hb, now)
        self.assertEqual(job["state"], "ended")
        self.assertNotIn("no beat", job.get("detail", ""))

    def test_done_card_reads_100_pct_and_ages_from_ended_at(self):
        now = time.time()
        hb = {
            "job": "gallery-library-scan", "state": "done",
            "done": 515299, "total": 585811,          # ~88% aggregate, intentionally non-additive
            "ts": now,                                 # fresh — must NOT drive displayed age
            "ended_at": _iso(now - 7 * 86400),          # finished a week ago
            "run_id": "gallery-library-scan-123", "started": "2026-07-13T00:00:00Z",
        }
        job = _job_from_hb(hb, now)
        self.assertEqual(job["pct"], 100.0)
        self.assertGreater(job["beat_age_s"], 6 * 86400)
        self.assertIn("d ago", job["beat_age"])

    def test_failed_terminal_job_also_ages_from_ended_at(self):
        now = time.time()
        hb = {
            "job": "some-job", "state": "failed",
            "ts": now,
            "ended_at": _iso(now - 3 * 3600),
        }
        job = _job_from_hb(hb, now)
        self.assertGreater(job["beat_age_s"], 2 * 3600)
        self.assertIn("h ago", job["beat_age"])

    def test_running_job_freshness_still_driven_by_ts(self):
        now = time.time()
        hb = {
            "job": "some-job", "state": "running",
            "done": 10, "total": 100,
            "ts": now - 30,                            # 30s-old beat
        }
        job = _job_from_hb(hb, now)
        self.assertEqual(job["beat_age_s"], 30)
        self.assertEqual(job["beat_age"], "just now")
        # not terminal — the 100% coherence override must not apply
        self.assertEqual(job["pct"], 10.0)

    def test_done_job_without_ended_at_falls_back_to_ts_age(self):
        now = time.time()
        hb = {
            "job": "legacy-job", "state": "done",
            "done": 5, "total": 5,
            "ts": now - 120,
        }
        job = _job_from_hb(hb, now)
        self.assertEqual(job["beat_age_s"], 120)
        # still gets the coherence fix even without ended_at present
        self.assertEqual(job["pct"], 100.0)

    def test_done_job_pct_already_100_is_unaffected(self):
        now = time.time()
        hb = {"job": "clean-job", "state": "done", "done": 5, "total": 5, "ts": now}
        job = _job_from_hb(hb, now)
        self.assertEqual(job["pct"], 100.0)

    def test_terminal_state_and_ended_at_unaffected_by_kind_passthrough(self):
        """Regression guard for the PANEL-2 kind passthrough (H2): adding the
        new `kind` key to the parsed job dict must not disturb the existing
        terminal-state/ended_at-age fields these tests already pin."""
        now = time.time()
        hb = {
            "job": "some-job", "state": "failed", "kind": "job",
            "ts": now,
            "ended_at": _iso(now - 3 * 3600),
        }
        job = _job_from_hb(hb, now)
        self.assertEqual(job["state"], "failed")
        self.assertGreater(job["beat_age_s"], 2 * 3600)


class KindPassthroughTests(unittest.TestCase):
    def test_kind_in_raw_heartbeat_reaches_the_job_dict(self):
        """RED before app/work.py's kind passthrough (H2): _job_from_file
        never reads hb.get("kind") today, so this key never reaches the
        parsed job dict. GREEN once the passthrough lands."""
        now = time.time()
        hb = {"job": "some-watch", "state": "running", "kind": "watch", "ts": now}
        job = _job_from_hb(hb, now)
        self.assertEqual(job["kind"], "watch")

    def test_missing_kind_is_not_defaulted_at_the_reader_layer(self):
        """Absence is preserved, not defaulted to "job" here — the
        default-to-job behavior lives only in routes.py's _is_watch_guard
        classifier, not baked into the stored job dict."""
        now = time.time()
        hb = {"job": "some-job", "state": "running", "ts": now}
        job = _job_from_hb(hb, now)
        self.assertNotIn("kind", job)


if __name__ == "__main__":
    unittest.main()
