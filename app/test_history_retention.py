"""
Focused stdlib tests for bounded history.jsonl retention
(FLEET-WORKER2-BUILD-20260721-panel-bounded-retention).

history.jsonl was append-only and unbounded (4,600+ lines live). These pin:
trim below threshold is a no-op, trim above threshold keeps exactly the
newest MAX_HISTORY_ROWS rows in chronological order, malformed lines are
tolerated (dropped, not counted toward `keep`), file mode/owner survive the
atomic rewrite, an interrupted rewrite leaves the original file intact, and
running trim twice in a row (idempotence) changes nothing on the second pass.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import store


def _write_lines(path: Path, n: int, start: int = 0) -> None:
    with open(path, "w") as f:
        for i in range(start, start + n):
            f.write(json.dumps({"ts": i}, separators=(",", ":")) + "\n")


class TrimHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "history.jsonl"

    def test_missing_file_is_a_noop(self):
        result = store.trim_history(self.path)
        self.assertEqual(result, {"before": 0, "kept": 0, "malformed": 0, "trimmed": False})

    def test_below_threshold_is_unchanged(self):
        _write_lines(self.path, 100)
        before_text = self.path.read_text()

        result = store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)

        self.assertFalse(result["trimmed"])
        self.assertEqual(result["before"], 100)
        self.assertEqual(self.path.read_text(), before_text)

    def test_above_threshold_trims_to_newest_keep_in_order(self):
        total = store.MAX_HISTORY_ROWS + 500
        _write_lines(self.path, total)

        result = store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)

        self.assertTrue(result["trimmed"])
        self.assertEqual(result["before"], total)
        self.assertEqual(result["kept"], store.MAX_HISTORY_ROWS)

        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), store.MAX_HISTORY_ROWS)
        rows = [json.loads(ln) for ln in lines]
        expected_first = total - store.MAX_HISTORY_ROWS
        self.assertEqual(rows[0]["ts"], expected_first)
        self.assertEqual(rows[-1]["ts"], total - 1)
        # strictly ascending -> chronological order preserved
        self.assertEqual([r["ts"] for r in rows], sorted(r["ts"] for r in rows))

    def test_file_ends_with_trailing_newline_after_trim(self):
        _write_lines(self.path, store.MAX_HISTORY_ROWS + 10)
        store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)
        self.assertTrue(self.path.read_text().endswith("\n"))

    def test_malformed_rows_are_dropped_not_counted_toward_keep(self):
        with open(self.path, "w") as f:
            for i in range(store.MAX_HISTORY_ROWS + 10):
                f.write(json.dumps({"ts": i}, separators=(",", ":")) + "\n")
            f.write("{not json\n")
            f.write("\n")  # blank line

        result = store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)

        self.assertTrue(result["trimmed"])
        self.assertEqual(result["malformed"], 1)
        self.assertEqual(result["kept"], store.MAX_HISTORY_ROWS)
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), store.MAX_HISTORY_ROWS)
        for ln in lines:
            json.loads(ln)  # every surviving line is valid JSON

    def test_read_history_after_trim_returns_newest_rows_chronologically(self):
        total = store.MAX_HISTORY_ROWS + 200
        _write_lines(self.path, total)
        store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)

        fake_settings = type("FakeSettings", (), {"history_file": self.path})()
        with patch.object(store, "settings", fake_settings):
            rows = store.read_history(limit=50)

        self.assertEqual(len(rows), 50)
        self.assertEqual([r["ts"] for r in rows], sorted(r["ts"] for r in rows))
        self.assertEqual(rows[-1]["ts"], total - 1)

    def test_file_mode_and_owner_preserved_across_rewrite(self):
        _write_lines(self.path, store.MAX_HISTORY_ROWS + 50)
        os.chmod(self.path, 0o664)
        before_stat = self.path.stat()

        store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)

        after_stat = self.path.stat()
        self.assertEqual(stat.S_IMODE(after_stat.st_mode), 0o664)
        self.assertEqual(after_stat.st_uid, before_stat.st_uid)
        self.assertEqual(after_stat.st_gid, before_stat.st_gid)

    def test_atomic_failure_leaves_original_intact(self):
        _write_lines(self.path, store.MAX_HISTORY_ROWS + 50)
        before_text = self.path.read_text()

        with patch.object(store, "_atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)

        self.assertEqual(self.path.read_text(), before_text)

    def test_idempotent_second_trim_is_a_noop(self):
        _write_lines(self.path, store.MAX_HISTORY_ROWS + 500)
        first = store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)
        after_first_text = self.path.read_text()

        second = store.trim_history(self.path, keep=store.MAX_HISTORY_ROWS)

        self.assertTrue(first["trimmed"])
        self.assertFalse(second["trimmed"])
        self.assertEqual(self.path.read_text(), after_first_text)


class MaybeTrimHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "history.jsonl"

    def test_below_slack_threshold_does_not_trigger_rewrite(self):
        _write_lines(self.path, store.HISTORY_TRIM_THRESHOLD_ROWS)
        before_text = self.path.read_text()

        store._maybe_trim_history(self.path)

        self.assertEqual(self.path.read_text(), before_text)

    def test_above_slack_threshold_triggers_rewrite_to_keep(self):
        _write_lines(self.path, store.HISTORY_TRIM_THRESHOLD_ROWS + 1)

        store._maybe_trim_history(self.path)

        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), store.MAX_HISTORY_ROWS)


if __name__ == "__main__":
    unittest.main()
