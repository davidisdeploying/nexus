import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from .model_usage_history import (
    backfill_routing,
    history_payload,
    init_db,
    provider_records,
    record_snapshot,
)


class ModelUsageHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "history.sqlite3"
        self.quota = self.root / "quota"
        self.quota.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, generated="2026-07-28T10:00:00Z", reset=2_000_000_000):
        return {
            "generated_at": generated,
            "claude": {
                "ok": True,
                "source": "claude internal usage",
                "windows": {
                    "five_hour": {"used_percent": 10, "resets_at": reset},
                    "weekly": {"used_percent": 25, "resets_at": reset + 1000},
                },
            },
            "gemini": {
                "ok": True,
                "source": "gemini quota RPC",
                "windows": {
                    "five_hour": {"remaining_percent": 80, "resets_at": reset},
                    "weekly": {"remaining_percent": 70, "resets_at": reset + 1000},
                },
            },
        }

    def write_codex(self, generated="2026-07-28T10:00:00Z", reset=2_000_000_000):
        (self.quota / "macbook-codex.json").write_text(json.dumps({
            "ok": True,
            "generated_at": generated,
            "rateLimits": {
                "planType": "pro",
                "limitId": "codex",
                "primary": {
                    "usedPercent": 15,
                    "windowDurationMins": 300,
                    "resetsAt": reset,
                },
                "secondary": {
                    "usedPercent": 35,
                    "windowDurationMins": 10080,
                    "resetsAt": reset + 1000,
                },
            },
        }))

    def test_schema_and_secret_free_error(self):
        init_db(self.db)
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)
        records = provider_records(
            {
                "generated_at": "2026-07-28T10:00:00Z",
                "claude": {"ok": False, "error": "Bearer secret-value"},
                "gemini": {"ok": False, "error": "private response tail"},
            },
            self.quota,
            1_775_000_000,
        )
        self.assertEqual(records["claude"]["error_class"], "Unavailable")
        self.assertNotIn("secret", json.dumps(records))

    def test_records_all_providers_and_filters(self):
        self.write_codex()
        inserted = record_snapshot(
            self.payload(), self.quota, self.db, captured_at=1_775_000_000
        )
        self.assertEqual(inserted, 3)
        data = history_payload("all", "all", self.db)
        self.assertEqual(data["summary"]["samples"], 3)
        self.assertEqual(data["summary"]["providers"], 3)
        self.assertEqual({x["provider"] for x in data["latest"]},
                         {"claude", "codex", "gemini"})
        claude = history_payload("all", "claude", self.db)
        self.assertEqual(claude["summary"]["samples"], 1)
        self.assertTrue(all(row["provider"] == "claude"
                            for row in claude["series"]))

    def test_detects_early_reset_reanchor(self):
        first = self.payload(reset=2_000_000_000)
        second = self.payload(reset=2_000_100_000)
        record_snapshot(first, self.quota, self.db, captured_at=1_775_000_000)
        record_snapshot(second, self.quota, self.db, captured_at=1_775_000_300)
        data = history_payload("all", "claude", self.db)
        kinds = {event["event_type"] for event in data["events"]}
        self.assertIn("window_reanchored_early", kinds)
        self.assertGreaterEqual(data["summary"]["early_reanchors"], 1)

    def test_backfills_routing_evidence_idempotently(self):
        run = self.root / "from-worker1" / "runs" / "token"
        run.mkdir(parents=True)
        (run / "routing.json").write_text(json.dumps({
            "generated_at": "2026-07-27T12:00:00Z",
            "candidates": [{
                "provider": "gemini",
                "source": "router snapshot",
                "remaining": {"five_hour": 90, "weekly": 95},
                "resets_at": {
                    "five_hour": "2026-07-27T15:00:00Z",
                    "weekly": "2026-08-02T12:00:00Z",
                },
            }],
        }))
        self.assertEqual(backfill_routing(self.root, self.db), 1)
        self.assertEqual(backfill_routing(self.root, self.db), 0)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM usage_windows"
            ).fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
