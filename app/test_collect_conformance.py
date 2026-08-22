"""Focused tests for CONFORMANCE-2's collector additions
(tools/collect_conformance.py): per-check transition metadata and the
sanitized fleet-git-push receipt probe. Pure-function tests -- no subprocess,
ssh, or systemd call is ever made; `_read_remote_file` is monkeypatched for
the receipt tests so behavior is deterministic and network-free.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tools import collect_conformance as cc


def _row(state: str, checked_at: str = "2026-07-30T10:00:00Z") -> dict:
    return {"id": "unit:alpha:x", "category": "service", "host": "alpha",
            "state": state, "expected": "active", "actual": state, "checked_at": checked_at}


class TransitionMetadataTests(unittest.TestCase):
    def test_new_check_ok_seeds_without_transition(self):
        row = _row("ok")
        cc.apply_transition_metadata(row, {})
        self.assertIsNone(row["previous_state"])
        self.assertEqual(row["state_changed_at"], row["checked_at"])
        self.assertEqual(row["consecutive_non_ok_scans"], 0)
        self.assertIsNone(row["first_non_ok_at"])
        self.assertIsNone(row["last_recovered_at"])

    def test_new_check_non_ok_seeds_without_transition(self):
        row = _row("error")
        cc.apply_transition_metadata(row, {})
        self.assertIsNone(row["previous_state"])
        self.assertEqual(row["consecutive_non_ok_scans"], 1)
        self.assertEqual(row["first_non_ok_at"], row["checked_at"])
        self.assertIsNone(row["last_recovered_at"])

    def test_unchanged_ok_state_stays_quiet(self):
        old = _row("ok", checked_at="2026-07-30T09:00:00Z")
        cc.apply_transition_metadata(old, {})
        row = _row("ok", checked_at="2026-07-30T10:00:00Z")
        cc.apply_transition_metadata(row, old)
        self.assertEqual(row["previous_state"], "ok")
        self.assertEqual(row["state_changed_at"], "2026-07-30T09:00:00Z")  # carried forward
        self.assertEqual(row["consecutive_non_ok_scans"], 0)
        self.assertIsNone(row["first_non_ok_at"])

    def test_unchanged_non_ok_state_increments(self):
        old = _row("error", checked_at="2026-07-30T09:00:00Z")
        cc.apply_transition_metadata(old, {})  # seeds: consecutive=1
        row = _row("error", checked_at="2026-07-30T09:05:00Z")
        cc.apply_transition_metadata(row, old)
        self.assertEqual(row["consecutive_non_ok_scans"], 2)
        self.assertEqual(row["first_non_ok_at"], "2026-07-30T09:00:00Z")
        self.assertEqual(row["state_changed_at"], "2026-07-30T09:00:00Z")

    def test_ok_to_non_ok_starts_failure_metadata(self):
        old = _row("ok", checked_at="2026-07-30T09:00:00Z")
        cc.apply_transition_metadata(old, {})
        row = _row("error", checked_at="2026-07-30T09:05:00Z")
        cc.apply_transition_metadata(row, old)
        self.assertEqual(row["previous_state"], "ok")
        self.assertEqual(row["consecutive_non_ok_scans"], 1)
        self.assertEqual(row["first_non_ok_at"], "2026-07-30T09:05:00Z")
        self.assertEqual(row["state_changed_at"], "2026-07-30T09:05:00Z")
        self.assertIsNone(row["last_recovered_at"])

    def test_non_ok_substate_change_continues_incrementing(self):
        old = _row("warning", checked_at="2026-07-30T09:00:00Z")
        old["first_non_ok_at"] = "2026-07-30T08:55:00Z"
        old["consecutive_non_ok_scans"] = 3
        old["last_recovered_at"] = None
        row = _row("error", checked_at="2026-07-30T09:05:00Z")
        cc.apply_transition_metadata(row, old)
        self.assertEqual(row["previous_state"], "warning")
        self.assertEqual(row["consecutive_non_ok_scans"], 4)
        self.assertEqual(row["first_non_ok_at"], "2026-07-30T08:55:00Z")  # preserved
        self.assertEqual(row["state_changed_at"], "2026-07-30T09:05:00Z")  # literal value changed

    def test_non_ok_to_ok_records_recovery_and_clears_failure_fields(self):
        old = _row("error", checked_at="2026-07-30T09:00:00Z")
        old["first_non_ok_at"] = "2026-07-30T08:00:00Z"
        old["consecutive_non_ok_scans"] = 5
        old["last_recovered_at"] = None
        row = _row("ok", checked_at="2026-07-30T09:05:00Z")
        cc.apply_transition_metadata(row, old)
        self.assertEqual(row["previous_state"], "error")
        self.assertEqual(row["consecutive_non_ok_scans"], 0)
        self.assertIsNone(row["first_non_ok_at"])
        self.assertEqual(row["last_recovered_at"], "2026-07-30T09:05:00Z")
        self.assertEqual(row["state_changed_at"], "2026-07-30T09:05:00Z")

    def test_last_recovered_at_persists_through_later_ok_scans(self):
        old = _row("ok", checked_at="2026-07-30T09:05:00Z")
        old["previous_state"] = "error"
        old["last_recovered_at"] = "2026-07-30T09:05:00Z"
        row = _row("ok", checked_at="2026-07-30T09:10:00Z")
        cc.apply_transition_metadata(row, old)
        self.assertEqual(row["last_recovered_at"], "2026-07-30T09:05:00Z")


class GitPushReceiptCheckTests(unittest.TestCase):
    def _patched(self, rc, out, err=""):
        return patch.object(cc, "_read_remote_file", return_value=(rc, out, err))

    def test_valid_fresh_matching_receipt_is_ok(self):
        finished = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        receipt = json.dumps({"ok": True, "host": "charlie", "finished_at": finished})
        with self._patched(0, receipt):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["category"], "receipts")
        self.assertIsNone(row["failure_class"])
        self.assertNotIn("path.json", row["actual"])  # sanitized: no raw receipt content

    def test_local_reader_preserves_valid_receipt_larger_than_four_kib(self):
        receipt = json.dumps({"ok": True, "padding": "x" * 5000})
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
            handle.write(receipt)
            handle.flush()
            rc, out, err = cc._read_remote_file("alpha", handle.name)
        self.assertEqual((rc, err), (0, ""))
        self.assertGreater(len(out), 4096)
        self.assertEqual(json.loads(out)["ok"], True)

    def test_timeout_is_unknown(self):
        with self._patched(124, "", "probe exceeded 12s"):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["failure_class"], "probe_failure")

    def test_exec_failure_is_unknown(self):
        with self._patched(127, "", "no such command"):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["failure_class"], "probe_failure")

    def test_missing_file_is_error(self):
        with self._patched(1, "", "cat: No such file or directory"):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertEqual(row["state"], "error")
        self.assertEqual(row["failure_class"], "drift")

    def test_malformed_json_is_error(self):
        with self._patched(0, "{not json"):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertEqual(row["state"], "error")
        self.assertIn("malformed", row["actual"])

    def test_failed_receipt_ok_false_is_error(self):
        finished = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        receipt = json.dumps({"ok": False, "host": "charlie", "finished_at": finished})
        with self._patched(0, receipt):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertEqual(row["state"], "error")

    def test_wrong_host_receipt_is_error(self):
        finished = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        receipt = json.dumps({"ok": True, "host": "delta", "finished_at": finished})
        with self._patched(0, receipt):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertEqual(row["state"], "error")

    def test_stale_receipt_is_error(self):
        finished = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat().replace("+00:00", "Z")
        receipt = json.dumps({"ok": True, "host": "charlie", "finished_at": finished})
        with self._patched(0, receipt):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertEqual(row["state"], "error")

    def test_sanitized_evidence_never_includes_raw_receipt_fields(self):
        finished = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        receipt = json.dumps({
            "ok": True, "host": "charlie", "finished_at": finished,
            "commit": "deadbeefcafe", "secret_token": "sh0uld-never-appear",
        })
        with self._patched(0, receipt):
            row = cc.git_push_receipt_check("charlie", "/some/path.json")
        self.assertNotIn("deadbeefcafe", row["actual"])
        self.assertNotIn("sh0uld-never-appear", row["actual"])


if __name__ == "__main__":
    unittest.main()
