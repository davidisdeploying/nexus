"""Regression tests for end-to-end semantic-index health."""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from . import semantic_index_watch as siw


def primary(build_id="build-1", ok=True, manifest_match=True):
    return {
        "ok": ok,
        "build_id": build_id,
        "manifest_matches_database": manifest_match,
        "document_count": 8000,
        "transcript_document_count": 9000,
    }


def cache(build_id="build-1", **overrides):
    result = {
        "result": "success",
        "exec_status": 0,
        "exit_epoch": time.time() - 3600,
        "reindex_rc": 0,
        "summary": "SUMMARY: reindex_rc=0 purged=0 tpurged=0",
        "primary": primary(build_id),
    }
    result.update(overrides)
    return result


def fallback(build_id="build-1", **overrides):
    result = {
        "ok": True,
        "markdown_docs": 8000,
        "markdown_chunks": 12000,
        "transcript_docs": 9000,
        "transcript_chunks": 700000,
        "build_id": build_id,
        "manifest_matches_database": True,
        "database_bytes": 4_000_000_000,
        "published_at": "2026-08-23T08:30:00Z",
        "published_at_source": "index_meta.build_completed_at",
        "latest_document_indexed_at": "2026-08-23T08:29:00",
        "syncthing": {
            "ok": True, "state": "idle", "need_files": 0, "need_bytes": 0,
        },
    }
    result.update(overrides)
    return result


class ProbeTests(unittest.TestCase):
    def test_default_path_is_normalized(self):
        self.assertEqual(
            siw.INDEX_PATH,
            Path.home() / ".local" / "share" / "tower" / "index" / "vault.db",
        )

    def test_probe_requires_user_scope_and_primary_canary(self):
        self.assertIn("--user", siw._REMOTE_SCRIPT)
        self.assertIn("XDG_RUNTIME_DIR", siw._REMOTE_SCRIPT)
        self.assertIn("PRIMARY_JSON", siw._REMOTE_SCRIPT)

    def test_parse_includes_primary_generation(self):
        payload = json.dumps(primary("generation-a"), separators=(",", ":"))
        parsed = siw._parse_probe(
            "RESULT=success\nEXEC_STATUS=0\nEXIT_EPOCH=1785745281\n"
            "SUMMARY=SUMMARY: reindex_rc=0 purged=0 tpurged=0\n"
            f"PRIMARY_JSON={payload}\n"
        )
        self.assertEqual(parsed["primary"]["build_id"], "generation-a")
        self.assertEqual(parsed["reindex_rc"], 0)


class HealthTests(unittest.TestCase):
    def read(self, cached=None, local=None):
        with (
            patch.dict(siw._maint_cache, {
                "data": cache() if cached is None else cached,
                "error": None,
                "checked_at": time.time(),
            }),
            patch.object(siw, "_read_index_counts", return_value=fallback() if local is None else local),
        ):
            return siw.read_status()

    def test_green_requires_matching_primary_fallback_and_idle_transport(self):
        status = self.read()
        self.assertEqual(status["health"], "GREEN")
        self.assertTrue(status["fallback_matches_primary"])
        self.assertIsNone(status["detail"])

    def test_false_green_regression_generation_mismatch_is_red(self):
        status = self.read(local=fallback("old-build"))
        self.assertEqual(status["health"], "RED")
        self.assertIn("differs", status["detail"])

    def test_syncthing_path_or_need_error_is_red(self):
        local = fallback(syncthing={
            "ok": False, "state": "error", "error": "folder path missing",
            "need_files": 1, "need_bytes": 4_000_000_000,
        })
        status = self.read(local=local)
        self.assertEqual(status["health"], "RED")
        self.assertIn("folder path missing", status["detail"])

    def test_primary_manifest_mismatch_is_red(self):
        cached = cache(primary=primary(manifest_match=False))
        status = self.read(cached=cached)
        self.assertEqual(status["health"], "RED")
        self.assertIn("Charlie manifest", status["detail"])

    def test_nonzero_reindex_is_red(self):
        status = self.read(cached=cache(reindex_rc=3))
        self.assertEqual(status["health"], "RED")
        self.assertIn("reindex_rc=3", status["detail"])

    def test_unprobed_is_red(self):
        with (
            patch.dict(siw._maint_cache, {"data": None, "error": None, "checked_at": None}),
            patch.object(siw, "_read_index_counts", return_value=fallback()),
        ):
            status = siw.read_status()
        self.assertEqual(status["health"], "RED")


if __name__ == "__main__":
    unittest.main()
