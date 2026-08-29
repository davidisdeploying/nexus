from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .gemini_quota_rpc import GeminiAuthError
from .model_usage import (
    collect_gemini,
    collect_claude,
    collect_claude_direct,
    parse_gemini_usage,
    parse_claude_api_usage,
    parse_claude_usage,
)


class ModelUsageParserTests(unittest.TestCase):
    def test_gemini_gemini_windows(self) -> None:
        text = """
        GEMINI MODELS
        Weekly Limit
        [bar] 97.61%
        98% remaining · Refreshes in 68h 26m
        Five Hour Limit
        [bar] 99.94%
        100% remaining · Refreshes in 3h 33m
        CLAUDE AND GPT MODELS
        """
        parsed = parse_gemini_usage(text)
        self.assertEqual(parsed["windows"]["weekly"]["used_percent"], 2.39)
        self.assertEqual(parsed["windows"]["five_hour"]["used_percent"], 0.06)

    def test_gemini_full_five_hour_window(self) -> None:
        text = """
        GEMINI MODELS
        Weekly Limit
        [bar] 97.31%
        97% remaining · Refreshes in 59h 22m
        Five Hour Limit
        [bar] 100.00%
        Quota available
        CLAUDE AND GPT MODELS
        """
        parsed = parse_gemini_usage(text)
        self.assertEqual(parsed["windows"]["weekly"]["used_percent"], 2.69)
        self.assertEqual(parsed["windows"]["five_hour"]["used_percent"], 0.0)
        self.assertEqual(
            parsed["windows"]["five_hour"]["remaining_percent"],
            100,
        )
        self.assertIsNone(
            parsed["windows"]["five_hour"]["refreshes_in"],
        )

    def test_gemini_full_weekly_and_five_hour_windows(self) -> None:
        text = """
        GEMINI MODELS
        Weekly Limit
        [bar] 100.00%
        Quota available
        Five Hour Limit
        [bar] 100.00%
        Quota available
        CLAUDE AND GPT MODELS
        """
        parsed = parse_gemini_usage(text)
        self.assertEqual(parsed["windows"]["weekly"]["used_percent"], 0.0)
        self.assertEqual(parsed["windows"]["five_hour"]["used_percent"], 0.0)

    def test_claude_windows(self) -> None:
        text = """
        Current session
        bar 3% used
        Resets 12:49pm (America/Chicago)
        Current week (all models)
        bar 45% used
        Resets Jul 30, 4:59am (America/Chicago)
        Current week (Fable)
        bar 9% used
        Resets Jul 30, 4:59am (America/Chicago)
        Usage credits are off
        """
        parsed = parse_claude_usage(text)
        self.assertEqual(parsed["windows"]["five_hour"]["used_percent"], 3)
        self.assertEqual(parsed["windows"]["weekly"]["used_percent"], 45)
        self.assertEqual(parsed["windows"]["fable_weekly"]["used_percent"], 9)

    def test_claude_structured_api_windows(self) -> None:
        parsed = parse_claude_api_usage({
            "five_hour": {
                "utilization": 3.0,
                "resets_at": "2026-07-27T17:50:00+00:00",
            },
            "seven_day": {
                "utilization": 45.0,
                "resets_at": "2026-07-30T10:00:00+00:00",
            },
            "limits": [{
                "kind": "weekly_scoped",
                "percent": 9,
                "resets_at": "2026-07-30T10:00:00+00:00",
                "scope": {"model": {"display_name": "Fable"}},
            }],
        })
        self.assertEqual(parsed["source"], "claude internal usage")
        self.assertEqual(parsed["windows"]["five_hour"]["used_percent"], 3.0)
        self.assertEqual(
            parsed["windows"]["weekly"]["resets_at"],
            "2026-07-30T10:00:00+00:00",
        )
        self.assertEqual(parsed["windows"]["fable_weekly"]["used_percent"], 9.0)

    def test_claude_structured_api_rejects_missing_window(self) -> None:
        with self.assertRaises(ValueError):
            parse_claude_api_usage({"five_hour": {}})

    def test_claude_falls_back_to_cli_without_leaking_error_text(self) -> None:
        fallback = {"ok": True, "source": "claude /usage", "windows": {}}
        with patch(
            "app.model_usage.collect_claude_direct",
            side_effect=RuntimeError("secret-bearing diagnostic"),
        ), patch(
            "app.model_usage.collect_claude_cli",
            return_value=fallback,
        ):
            result = collect_claude(None, "claude")
        self.assertEqual(result["source"], "claude /usage")
        self.assertEqual(result["fallback_from"], "claude internal usage")
        self.assertEqual(result["direct_error"], "RuntimeError")
        self.assertNotIn("secret-bearing", str(result))

    def test_gemini_falls_back_to_cli_without_leaking_error_text(
        self,
    ) -> None:
        fallback = {"ok": True, "source": "agy /usage", "windows": {}}
        with patch(
            "app.model_usage.collect_gemini_rpc",
            side_effect=GeminiAuthError("quota RPC HTTP 403"),
        ), patch(
            "app.model_usage.collect_gemini_cli",
            return_value=fallback,
        ):
            result = collect_gemini(None, "agy")
        self.assertEqual(result["source"], "agy /usage")
        self.assertEqual(
            result["fallback_from"], "cloudcode retrieveUserQuotaSummary"
        )
        self.assertEqual(result["direct_error"], "GeminiAuthError")
        self.assertNotIn("403", str(result))

    def test_gemini_prefers_rpc_without_launching_cli(self) -> None:
        direct = {
            "ok": True,
            "source": "cloudcode retrieveUserQuotaSummary",
            "windows": {},
        }
        with patch(
            "app.model_usage.collect_gemini_rpc",
            return_value=direct,
        ), patch("app.model_usage.collect_gemini_cli") as fallback:
            result = collect_gemini(None, "agy")
        self.assertEqual(result, direct)
        fallback.assert_not_called()

    def test_claude_direct_reads_marked_home_and_never_returns_token(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "five_hour": {
                        "utilization": 3,
                        "resets_at": "2026-07-27T17:50:00Z",
                    },
                    "seven_day": {
                        "utilization": 45,
                        "resets_at": "2026-07-30T10:00:00Z",
                    },
                }).encode()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".nexus-model-usage-home").touch()
            (home / ".claude").mkdir()
            (home / ".claude" / ".credentials.json").write_text(json.dumps({
                "claudeAiOauth": {"accessToken": "test-secret-token"}
            }))
            with patch(
                "app.model_usage.urllib.request.urlopen",
                return_value=Response(),
            ) as urlopen:
                result = collect_claude_direct(home)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.headers["Authorization"], "Bearer test-secret-token"
        )
        self.assertEqual(result["source"], "claude internal usage")
        self.assertNotIn("test-secret-token", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
