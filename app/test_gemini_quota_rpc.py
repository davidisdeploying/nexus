from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from . import gemini_quota_rpc as rpc


FAKE_TOKEN = "FAKE-ACCESS-TOKEN-DO-NOT-USE-0000"
FAKE_REFRESH = "FAKE-REFRESH-TOKEN-0000"
VALID_TOKEN_STORE = {
    "auth_method": "consumer",
    "token": {
        "access_token": FAKE_TOKEN,
        "refresh_token": FAKE_REFRESH,
        "token_type": "Bearer",
        "expiry": "2099-01-01T00:00:00.123456789+00:00",
    },
}
EXPIRED_TOKEN_STORE = {
    "auth_method": "consumer",
    "token": {
        "access_token": FAKE_TOKEN,
        "refresh_token": FAKE_REFRESH,
        "expiry": "2000-01-01T00:00:00Z",
    },
}


def _response(
    weekly: float = 0.9642777,
    five_hour: float = 0.9899,
) -> dict:
    return {
        "groups": [{
            "displayName": "Gemini Models",
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "window": "weekly",
                    "resetTime": "2026-07-30T11:57:58Z",
                    "remainingFraction": weekly,
                },
                {
                    "bucketId": "gemini-5h",
                    "window": "5h",
                    "resetTime": "2026-07-28T05:37:31Z",
                    "remainingFraction": five_hour,
                },
            ],
        }],
    }


def _make_home(
    root: str,
    *,
    marked: bool = True,
    token_store: dict | None = VALID_TOKEN_STORE,
) -> Path:
    home = Path(root)
    if marked:
        (home / rpc.MARKER).touch()
    cli = home / ".gemini" / "gemini-cli"
    cli.mkdir(parents=True)
    if token_store is not None:
        (cli / "gemini-oauth-token").write_text(
            json.dumps(token_store)
        )
    return home


class _BytesResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, *_args) -> bytes:
        return self.payload

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False


class GeminiQuotaRpcTests(unittest.TestCase):
    def test_success_computes_used_and_exact_resets(self) -> None:
        result = rpc.parse_quota_summary(_response())
        self.assertEqual(result["source"], rpc.SOURCE)
        self.assertEqual(result["windows"]["weekly"]["used_percent"], 3.57)
        self.assertEqual(
            result["windows"]["five_hour"]["used_percent"], 1.01
        )
        self.assertEqual(
            result["windows"]["five_hour"]["resets_at"],
            "2026-07-28T05:37:31Z",
        )

    def test_full_buckets_keep_exact_reset(self) -> None:
        result = rpc.parse_quota_summary(_response(1.0, 1.0))
        self.assertEqual(result["windows"]["weekly"]["used_percent"], 0.0)
        self.assertEqual(result["windows"]["five_hour"]["used_percent"], 0.0)
        self.assertIn("resets_at", result["windows"]["five_hour"])

    def test_missing_reset_is_valid(self) -> None:
        payload = _response()
        payload["groups"][0]["buckets"][0].pop("resetTime")
        result = rpc.parse_quota_summary(payload)
        self.assertNotIn("resets_at", result["windows"]["weekly"])

    def test_invalid_reset_is_rejected(self) -> None:
        payload = _response()
        payload["groups"][0]["buckets"][0]["resetTime"] = "not-a-time"
        with self.assertRaises(rpc.GeminiSchemaError):
            rpc.parse_quota_summary(payload)

    def test_missing_required_window_is_rejected(self) -> None:
        payload = _response()
        payload["groups"][0]["buckets"].pop()
        with self.assertRaises(rpc.GeminiSchemaError):
            rpc.parse_quota_summary(payload)

    def test_missing_gemini_group_is_rejected(self) -> None:
        with self.assertRaises(rpc.GeminiSchemaError):
            rpc.parse_quota_summary({"groups": []})

    def test_bool_fraction_is_rejected(self) -> None:
        payload = _response()
        for bucket in payload["groups"][0]["buckets"]:
            bucket["remainingFraction"] = True
        with self.assertRaises(rpc.GeminiSchemaError):
            rpc.parse_quota_summary(payload)

    def test_non_object_and_missing_groups_are_rejected(self) -> None:
        for payload in ([], {"other": []}):
            with self.subTest(payload=payload):
                with self.assertRaises(rpc.GeminiSchemaError):
                    rpc.parse_quota_summary(payload)

    def test_unmarked_and_live_homes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rpc.GeminiHomeError):
                rpc._marked_home(_make_home(tmp, marked=False))
        with self.assertRaises(rpc.GeminiHomeError):
            rpc._marked_home(rpc.LIVE_HOME)

    def test_valid_expired_and_missing_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                rpc._read_access_token(_make_home(tmp)), FAKE_TOKEN
            )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rpc.GeminiAuthError):
                rpc._read_access_token(
                    _make_home(tmp, token_store=EXPIRED_TOKEN_STORE)
                )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(rpc.GeminiAuthError):
                rpc._read_access_token(
                    _make_home(tmp, token_store={"token": {}})
                )

    def test_http_403_hides_response_body(self) -> None:
        error = urllib.error.HTTPError(
            rpc.QUOTA_URL,
            403,
            "Forbidden",
            {},
            _BytesResponse(
                b'{"error":{"message":"secret@example.com"}}'
            ),
        )
        with patch.object(
            rpc.urllib.request, "urlopen", side_effect=error
        ), self.assertRaises(rpc.GeminiAuthError) as caught:
            rpc.fetch_quota_summary(FAKE_TOKEN)
        self.assertEqual(str(caught.exception), "quota RPC HTTP 403")
        self.assertNotIn("secret@example.com", str(caught.exception))
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_http_500_is_network_not_auth(self) -> None:
        error = urllib.error.HTTPError(
            rpc.QUOTA_URL, 500, "Internal", {}, _BytesResponse(b"private")
        )
        with patch.object(
            rpc.urllib.request, "urlopen", side_effect=error
        ), self.assertRaises(rpc.GeminiNetworkError):
            rpc.fetch_quota_summary(FAKE_TOKEN)

    def test_timeout_and_url_error_are_network_errors(self) -> None:
        for error in (socket.timeout(), urllib.error.URLError("no route")):
            with self.subTest(error=type(error).__name__):
                with patch.object(
                    rpc.urllib.request, "urlopen", side_effect=error
                ), self.assertRaises(rpc.GeminiNetworkError):
                    rpc.fetch_quota_summary(FAKE_TOKEN)

    def test_success_output_never_contains_token(self) -> None:
        def urlopen(request, timeout=0):
            self.assertEqual(
                request.headers["Authorization"], f"Bearer {FAKE_TOKEN}"
            )
            self.assertEqual(request.headers["User-agent"], "gemini")
            return _BytesResponse(json.dumps(_response()).encode())

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            rpc.urllib.request, "urlopen", side_effect=urlopen
        ):
            result = rpc.collect_gemini_rpc(_make_home(tmp))
        serialized = json.dumps(result)
        self.assertNotIn(FAKE_TOKEN, serialized)
        self.assertNotIn("Bearer", serialized)


if __name__ == "__main__":
    unittest.main()
