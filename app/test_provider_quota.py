import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from . import seatboard

FIXED_NOW = datetime(
    2026, 7, 27, 16, 0, tzinfo=timezone.utc
).timestamp()


class ProviderQuotaTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(seatboard, "RELAY", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        (self.root / "heartbeats" / "quota").mkdir(parents=True)
        router = mock.Mock()
        router.recommend.return_value = {
            "ok": True,
            "provider": "gemini",
            "model": "gemini-3.6-flash-high",
            "state": "GREEN",
            "reason": "best protected quota",
            "candidates": [
                {
                    "provider": "codex",
                    "model": "gpt-5.6-terra",
                    "state": "YELLOW",
                    "score": 70,
                    "reason": "five_hour_unknown",
                },
                {
                    "provider": "claude",
                    "model": "sonnet-5",
                    "state": "GREEN",
                    "score": 65,
                    "reason": "within_dynamic_reserves",
                },
                {
                    "provider": "gemini",
                    "model": "gemini-3.6-flash-high",
                    "state": "GREEN",
                    "score": 85,
                    "reason": "within_dynamic_reserves",
                },
            ],
        }
        self.router_patch = mock.patch.object(seatboard, "quota_router", router)
        self.router_patch.start()
        self.addCleanup(self.router_patch.stop)

    def write_quota(self, seat: str, used: int, minutes: int = 10080) -> None:
        payload = {
            "ok": True,
            "generated_at": "2026-07-27T16:00:00Z",
            "rateLimits": {
                "primary": {
                    "usedPercent": used,
                    "windowDurationMins": minutes,
                    "resetsAt": datetime(
                        2026, 8, 2, 20, 32, tzinfo=timezone.utc
                    ).timestamp(),
                },
                "rateLimitReachedType": None,
                "spendControlReached": False,
            },
        }
        (self.root / "heartbeats" / "quota" / f"{seat}-codex.json").write_text(
            json.dumps(payload)
        )

    def test_worker_tiles_do_not_repeat_quota_information(self):
        tile = seatboard._tile("worker1", "delta", "cyan", "Worker1", time.time())
        self.assertEqual(tile["provider_line"], "")
        self.assertIsNone(tile["model_badge"])

    def test_node_card_labels_and_idle_localworker_model(self):
        with mock.patch.object(
            seatboard,
            "_model_usage_tile",
            return_value={"seat": "worker4", "usage_card": True},
        ):
            board = seatboard.read_seat_board(time.time())
        workers = board["seats"][:4]
        self.assertEqual(
            [(tile["seat"], tile["label"], tile["sub"]) for tile in workers],
            [
                ("charlie", "charlie", ""),
                ("delta", "delta", ""),
                ("alpha", "alpha", ""),
                ("localworker", "Localworker", ""),
            ],
        )
        self.assertEqual(
            workers[-1]["model_badge"],
            {"family": "local", "label": "GPT-OSS 20B", "mark": "⬡"},
        )

    def test_runtime_model_badges_are_normalized(self):
        self.assertEqual(
            seatboard._model_badge("claude", "sonnet"),
            {"family": "claude", "label": "Claude Sonnet", "mark": "✳"},
        )
        self.assertEqual(
            seatboard._model_badge("codex", "gpt-5.6-terra")["label"],
            "GPT-5.6 Terra",
        )
        self.assertEqual(
            seatboard._model_badge(None, "gpt-oss:20b")["label"],
            "GPT-OSS 20B",
        )
        self.assertEqual(
            seatboard._model_badge("gemini", "gemini-3.6-flash-high")["label"],
            "Gemini 3.6 Flash",
        )

    def test_retired_worker4_slot_is_three_provider_usage_card(self):
        self.write_quota("worker2", 47)
        payload = {
            "ok": True,
            "generated_at": "2026-07-27T16:00:00Z",
            "claude": {
                "ok": True,
                "windows": {
                    "weekly": {
                        "used_percent": 45,
                        "resets": "Jul30,4:59am(America/Chicago)",
                    },
                    "five_hour": {
                        "used_percent": 3,
                        "resets": "12:49pm(America/Chicago)",
                    },
                },
            },
            "gemini": {
                "ok": True,
                "windows": {
                    "weekly": {
                        "used_percent": 2.39,
                        "refreshes_in": "67h 24m",
                    },
                    "five_hour": {
                        "used_percent": 0.06,
                        "refreshes_in": "2h 31m",
                    },
                },
            },
        }
        (
            self.root / "heartbeats" / "quota" / "model-usage.json"
        ).write_text(json.dumps(payload))
        tile = seatboard._model_usage_tile(FIXED_NOW)
        self.assertEqual(tile["label"], "Model Usage")
        self.assertEqual(tile["badge"], "LIVE")
        self.assertEqual(tile["routing"]["worker"]["model"], "gemini-3.6-flash-high")
        self.assertEqual(
            [
                (candidate["provider"], candidate["state"])
                for candidate in tile["routing"]["candidates"]
            ],
            [
                ("gemini", "GREEN"),
                ("claude", "GREEN"),
                ("codex", "YELLOW"),
            ],
        )
        self.assertEqual(
            tile["provider_usage"],
            [
                {
                    "provider": "claude",
                    "label": "Claude",
                    "source": "claude internal usage",
                    "updated_ms": 1785168000000,
                    "five_hour_used": 3.0,
                    "weekly_used": 45.0,
                },
                {
                    "provider": "codex",
                    "label": "Codex",
                    "source": "codex app-server",
                    "updated_ms": 1785168000000,
                    "five_hour_used": None,
                    "weekly_used": 47.0,
                },
                {
                    "provider": "gemini",
                    "label": "Gemini",
                    "source": "cloudcode retrieveUserQuotaSummary",
                    "updated_ms": 1785168000000,
                    "five_hour_used": 0.06,
                    "weekly_used": 2.39,
                },
            ],
        )
        self.assertEqual(
            tile["usage_items"],
            [
                {
                    "usage": "Claude · 45% used wk · 3% used 5h",
                    "resets": (
                        "5h ↻ Mon Jul 27 · 12:49 PM CDT",
                        "wk ↻ Thu Jul 30 · 4:59 AM CDT",
                    ),
                },
                {
                    "usage": "Codex · 47% used wk",
                    "resets": (
                        "5h ↻ unavailable",
                        "wk ↻ Sun Aug 2 · 3:32 PM CDT",
                    ),
                },
                {
                    "usage": "Gemini · 2% used wk · <1% used 5h",
                    "resets": (
                        "5h ↻ ~Mon Jul 27 · 1:31 PM CDT",
                        "wk ↻ ~Thu Jul 30 · 6:24 AM CDT",
                    ),
                },
            ],
        )

    def test_reset_parsers_reject_malformed_values(self):
        self.assertIsNone(seatboard._claude_reset_epoch("not-a-time", FIXED_NOW))
        self.assertIsNone(seatboard._countdown_reset_epoch("", FIXED_NOW))

    def test_rpc_gemini_resets_are_exact_not_projected(self):
        self.write_quota("worker2", 47)
        payload = {
            "ok": True,
            "generated_at": "2026-07-27T16:00:00Z",
            "claude": {"ok": False},
            "gemini": {
                "ok": True,
                "source": "cloudcode retrieveUserQuotaSummary",
                "windows": {
                    "weekly": {
                        "used_percent": 3.57,
                        "resets_at": "2026-07-30T11:57:58Z",
                    },
                    "five_hour": {
                        "used_percent": 1.01,
                        "resets_at": "2026-07-28T05:37:31Z",
                    },
                },
            },
        }
        (
            self.root / "heartbeats" / "quota" / "model-usage.json"
        ).write_text(json.dumps(payload))
        tile = seatboard._model_usage_tile(FIXED_NOW)
        gemini = tile["usage_items"][2]
        self.assertEqual(
            gemini["resets"],
            (
                "5h ↻ Tue Jul 28 · 12:37 AM CDT",
                "wk ↻ Thu Jul 30 · 6:57 AM CDT",
            ),
        )
        self.assertNotIn("~", " ".join(gemini["resets"]))

    def test_newest_failed_codex_snapshot_selects_older_fresh_success(self):
        self.write_quota("worker1", 40)
        # Write a newer failed payload
        failed_payload = {
            "ok": False,
            "error": "rate limit backend timeout",
            "generated_at": "2026-07-27T15:59:00Z",
        }
        (self.root / "heartbeats" / "quota" / "worker2-codex.json").write_text(
            json.dumps(failed_payload)
        )
        info, gen, provider = seatboard._codex_usage(FIXED_NOW)
        self.assertIsNotNone(gen)
        self.assertIn("40% used wk", info["usage"])
        self.assertEqual(provider["weekly_used"], 40.0)

    def test_all_failed_codex_snapshots_render_unavailable(self):
        failed_payload = {
            "ok": False,
            "error": "rate limit backend timeout",
            "generated_at": "2026-07-27T15:59:00Z",
        }
        (self.root / "heartbeats" / "quota" / "worker2-codex.json").write_text(
            json.dumps(failed_payload)
        )
        info, gen, provider = seatboard._codex_usage(FIXED_NOW)
        self.assertIsNone(gen)
        self.assertEqual(info["usage"], "Codex · unavailable")
        self.assertEqual(info["resets"], ("5h ↻ unavailable", "wk ↻ unavailable"))
        self.assertIsNone(provider["weekly_used"])


if __name__ == "__main__":
    unittest.main()
