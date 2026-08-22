from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .activity import filter_cache, provider_for_surface, read_cache
from tools.collect_activity import successful_pushes


class ActivityTests(unittest.TestCase):
    def cache(self) -> dict:
        return {"version": 1, "generated_at": "2026-07-27T18:00:00Z", "commits": [{"timestamp": "2026-07-27T12:00:00Z", "repository": "nexus"}, {"timestamp": "2026-06-01T12:00:00Z", "repository": "old"}], "pushes": [{"finished_at": "2026-07-27T12:00:00Z"}], "assistant_turns": [{"timestamp": "2026-07-27T12:00:00Z", "provider": "OpenAI", "conversation_id": "x"}, {"timestamp": "2026-07-27T12:00:00Z", "provider": "OpenAI", "conversation_id": "x"}], "provider_coverage": {"OpenAI": {"records": 2, "assistant_records": 2}, "Google": {"records": 9, "assistant_records": 0}}}

    def test_range_filter(self) -> None:
        result = filter_cache(self.cache(), "7d", datetime(2026, 7, 27, tzinfo=timezone.utc))
        self.assertEqual(result["summary"]["commits"], 1)
        self.assertEqual(result["summary"]["peak_commit_hour"], "12:00 UTC")
        self.assertEqual(result["providers"]["OpenAI"]["sessions"], 1)
        self.assertEqual(result["providers"]["OpenAI"]["active_days"], 1)
        self.assertNotIn("assistant_turns", result)

    def test_missing_timestamp_does_not_create_active_day(self) -> None:
        cache = self.cache()
        cache["assistant_turns"].append(
            {"timestamp": "", "provider": "OpenAI", "conversation_id": "audit"}
        )
        result = filter_cache(cache, "all", datetime(2026, 7, 27, tzinfo=timezone.utc))
        self.assertEqual(result["providers"]["OpenAI"]["assistant_turns"], 3)
        self.assertEqual(result["providers"]["OpenAI"]["active_days"], 1)
        self.assertIsNone(result["providers"]["Google"]["assistant_turns"])
        self.assertFalse(result["providers"]["Google"]["comparable"])
        self.assertFalse(result["provider_comparison_complete"])

    def test_provider_mapping(self) -> None:
        self.assertEqual(provider_for_surface("claude-code"), "Claude")
        self.assertEqual(provider_for_surface("codex"), "OpenAI")
        self.assertEqual(provider_for_surface("gemini"), "Google")
        self.assertIsNone(provider_for_surface("unknown"))

    def test_pushed_not_up_to_date(self) -> None:
        events = successful_pushes([{"finished_at": "2026-07-27T00:00:00Z", "repositories": [{"name": "a", "status": "up_to_date"}, {"name": "b", "status": "pushed"}]}], "alpha")
        self.assertEqual([x["repository"] for x in events], ["b"])

    def test_partial_error_and_malformed_cache(self) -> None:
        result = filter_cache({**self.cache(), "host_errors": {"charlie": "timeout"}}, "all")
        self.assertIn("charlie", result["host_errors"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"; path.write_text("{")
            self.assertIsNotNone(read_cache(path)[1])


if __name__ == "__main__": unittest.main()
