"""
Focused stdlib tests for send_ntfy's Unicode-title handling
(FLEET-AUTO-BUILD-20260802-panel-notification-health-repair).

httpx headers are ASCII-only -- httpx.Headers raises UnicodeEncodeError while
constructing the request if a header value (e.g. the weekly self-test's em
dash title) contains non-ASCII text. This pins the RFC 2047 encoded-word
fallback ntfy documents for exactly this case
(docs.ntfy.sh/publish/#e-mail-style-headers), using a real httpx.AsyncClient
routed through httpx.MockTransport so httpx's own header validation runs for
real -- no network I/O, no mocked-away proof.
"""
from __future__ import annotations

import unittest
from email.header import decode_header
from unittest.mock import patch

import httpx

from . import ntfy
from .ntfy import _header_value


def _decode(value: str) -> str:
    parts = decode_header(value)
    return "".join(
        chunk.decode(enc or "ascii") if isinstance(chunk, bytes) else chunk
        for chunk, enc in parts
    )


_RealAsyncClient = httpx.AsyncClient


def _mock_client_factory(handler):
    # Captures the real AsyncClient *before* patching -- app.ntfy.httpx and
    # this module's httpx are the same module object, so a naive reference
    # to `httpx.AsyncClient` inside this factory would resolve to the patch
    # itself once installed.
    def _make(*, timeout):
        return _RealAsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))
    return _make


class HeaderValueEncodingTests(unittest.TestCase):
    def test_ascii_only_value_passes_through_unchanged(self):
        self.assertEqual(_header_value("plain title, no surprises"), "plain title, no surprises")

    def test_unicode_value_is_rfc2047_encoded_and_round_trips(self):
        title = "Nexus notifications alive — weekly self-test"
        encoded = _header_value(title)
        self.assertNotEqual(encoded, title)
        self.assertEqual(_decode(encoded), title)


class SendNtfyUnicodeTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_unicode_title_is_sent_without_raising(self):
        title = "Nexus notifications alive — weekly self-test 🔔"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["title"] = request.headers.get("title")
            captured["body"] = request.content.decode("utf-8")
            return httpx.Response(200)

        mock_settings = patch("app.ntfy.settings")
        with mock_settings as settings:
            settings.ntfy_topic = "test-topic-do-not-log"
            settings.ntfy_base_url = "https://ntfy.sh"
            settings.public_origin = "https://nexus.example"
            with patch("app.ntfy.httpx.AsyncClient", new=_mock_client_factory(handler)):
                result = await ntfy.send_ntfy(
                    title=title, body="Both transports fired.", priority=3,
                    click="/notifications", tags="test_tube",
                )

        self.assertTrue(result)
        self.assertEqual(_decode(captured["title"]), title)
        self.assertEqual(captured["body"], "Both transports fired.")

    async def test_ascii_title_is_sent_verbatim(self):
        title = "Nexus test push"
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["title"] = request.headers.get("title")
            return httpx.Response(200)

        with patch("app.ntfy.settings") as settings:
            settings.ntfy_topic = "test-topic-do-not-log"
            settings.ntfy_base_url = "https://ntfy.sh"
            settings.public_origin = "https://nexus.example"
            with patch("app.ntfy.httpx.AsyncClient", new=_mock_client_factory(handler)):
                result = await ntfy.send_ntfy(title=title, body="body", priority=3)

        self.assertTrue(result)
        self.assertEqual(captured["title"], title)


if __name__ == "__main__":
    unittest.main()
