"""Regression tests for the semantic index receipt probe and health rules."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest.mock import patch

from . import semantic_index_watch as siw


HEALTHY_PROBE = (
    "RESULT=success\n"
    "EXEC_STATUS=0\n"
    "EXIT_EPOCH=1785745281\n"
    "SUMMARY=[2026-08-03T08:21:21Z] SUMMARY: reindex_rc=0 purged=0 tpurged=0\n"
)

HEALTHY_INDEX = {
    "ok": True,
    "markdown_docs": 7086,
    "markdown_chunks": 11719,
    "transcript_docs": 5572,
    "transcript_chunks": 414578,
    "latest_document_indexed_at": "2026-08-03T08:18:44",
    "database_bytes": 2613612544,
    "published_at": "2026-08-03T08:21:19Z",
    "published_at_source": "database_file_mtime",
    "authoritative_build_timestamp_available": False,
}


def _cache(exit_epoch=None, result="success", exec_status=0, reindex_rc=0):
    return {
        "result": result,
        "exec_status": exec_status,
        "exit_epoch": exit_epoch if exit_epoch is not None else time.time() - 3600,
        "reindex_rc": reindex_rc,
        "summary": "SUMMARY: reindex_rc=%s purged=0 tpurged=0" % reindex_rc,
    }


class ProbeParserTests(unittest.TestCase):
    def test_default_index_path_uses_normalized_state_root(self):
        self.assertEqual(
            siw.INDEX_PATH,
            Path.home() / ".local" / "share" / "tower" / "index" / "vault.db",
        )

    def test_parses_healthy_probe(self):
        parsed = siw._parse_probe(HEALTHY_PROBE)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["result"], "success")
        self.assertEqual(parsed["exec_status"], 0)
        self.assertEqual(parsed["reindex_rc"], 0)
        self.assertEqual(parsed["exit_epoch"], 1785745281.0)

    def test_rejects_output_missing_required_fields(self):
        self.assertIsNone(siw._parse_probe("SUMMARY=nothing useful\n"))

    def test_never_ran_yields_null_epoch(self):
        parsed = siw._parse_probe("RESULT=success\nEXEC_STATUS=0\nEXIT_EPOCH=\nSUMMARY=\n")
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed["exit_epoch"])
        self.assertIsNone(parsed["reindex_rc"])

    def test_nonzero_reindex_rc_is_extracted(self):
        parsed = siw._parse_probe(
            "RESULT=success\nEXEC_STATUS=0\nEXIT_EPOCH=1785745281\n"
            "SUMMARY=[ts] SUMMARY: reindex_rc=1 purged=0 tpurged=0\n"
        )
        self.assertEqual(parsed["reindex_rc"], 1)

    def test_probe_targets_user_scope(self):
        """Regression: the system scope reports success for a unit that does
        not exist there, which would render a permanently green tile."""
        self.assertIn("--user", siw._REMOTE_SCRIPT)
        self.assertIn("XDG_RUNTIME_DIR", siw._REMOTE_SCRIPT)


class HealthTests(unittest.TestCase):
    def _read(self, cache_data, cache_error=None, index=None):
        with patch.dict(siw._maint_cache,
                        {"data": cache_data, "error": cache_error, "checked_at": time.time()}), \
             patch.object(siw, "_read_index_counts", return_value=index or dict(HEALTHY_INDEX)):
            return siw.read_status()

    def test_green_when_unit_and_both_collections_healthy(self):
        status = self._read(_cache())
        self.assertEqual(status["health"], "GREEN")
        self.assertFalse(status["no_receipt"])
        self.assertTrue(status["markdown_ok"])
        self.assertTrue(status["transcript_ok"])
        self.assertIsNone(status["detail"])

    def test_unprobed_cache_is_red_not_green(self):
        status = self._read(None, cache_error=None)
        self.assertEqual(status["health"], "RED")
        self.assertTrue(status["no_receipt"])

    def test_never_ran_is_red(self):
        cache = _cache()
        cache["exit_epoch"] = None
        status = self._read(cache)
        self.assertEqual(status["health"], "RED")
        self.assertTrue(status["no_receipt"])

    def test_stale_beyond_amber_threshold(self):
        status = self._read(_cache(exit_epoch=time.time() - 27 * 3600))
        self.assertEqual(status["health"], "AMBER")

    def test_stale_beyond_red_threshold(self):
        status = self._read(_cache(exit_epoch=time.time() - 31 * 3600))
        self.assertEqual(status["health"], "RED")

    def test_failed_unit_is_red(self):
        status = self._read(_cache(result="exit-code", exec_status=1))
        self.assertEqual(status["health"], "RED")
        self.assertIn("unit", status["detail"])

    def test_nonzero_reindex_rc_is_red_even_when_unit_succeeded(self):
        """The unit also purges .trash, so unit success alone is not proof the
        reindex ran."""
        status = self._read(_cache(reindex_rc=1))
        self.assertEqual(status["health"], "RED")
        self.assertIn("reindex_rc=1", status["detail"])

    def test_missing_summary_receipt_is_red(self):
        status = self._read(_cache(reindex_rc=None))
        self.assertEqual(status["health"], "RED")
        self.assertIn("no SUMMARY receipt", status["detail"])

    def test_empty_transcript_collection_is_red(self):
        index = dict(HEALTHY_INDEX, transcript_docs=0)
        status = self._read(_cache(), index=index)
        self.assertEqual(status["health"], "RED")
        self.assertIn("transcript collection empty", status["detail"])
        self.assertFalse(status["transcript_ok"])

    def test_empty_markdown_collection_is_red(self):
        index = dict(HEALTHY_INDEX, markdown_docs=0)
        status = self._read(_cache(), index=index)
        self.assertEqual(status["health"], "RED")
        self.assertIn("markdown index empty", status["detail"])

    def test_unreadable_index_is_red(self):
        status = self._read(_cache(), index={"ok": False, "error": "index unavailable: OSError"})
        self.assertEqual(status["health"], "RED")
        self.assertIn("index unavailable", status["detail"])

    def test_published_at_is_labelled_as_file_mtime(self):
        """The index has no authoritative build timestamp; mtime must never be
        presented as one."""
        status = self._read(_cache())
        self.assertEqual(status["published_at_source"], "database_file_mtime")


if __name__ == "__main__":
    unittest.main()
