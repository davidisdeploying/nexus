"""
Focused tests for the structured, non-secret result contract added to
app/deadman.py's deadman_ping_once (FLEET-AUTO-BUILD-20260802-panel-live-
watchdog-evidence). Only an HTTP 2xx response counts as ok; a missing ping URL
is a neutral unprovisioned no-op, never a fabricated success; the function
must never raise regardless of transport outcome.

Mirrors app/test_ntfy.py's httpx.MockTransport pattern -- a real
httpx.AsyncClient routed through a mock transport, no network I/O.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from . import deadman

_RealAsyncClient = httpx.AsyncClient


def _mock_client_factory(handler):
    def _make(*, timeout):
        return _RealAsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))
    return _make


class UnprovisionedTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_url_is_neutral_not_a_fabricated_success(self):
        with patch.object(deadman, "_read_ping_url", return_value=None):
            result = await deadman.deadman_ping_once()
        self.assertEqual(result["provisioned"], False)
        self.assertIsNone(result["ok"])
        self.assertNotEqual(result["ok"], True)


class ProvisionedOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_2xx_response_is_ok(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        with patch.object(deadman, "_read_ping_url", return_value="https://hc.example/ping/abc"), \
             patch.object(deadman, "_self_health", return_value=True), \
             patch("app.deadman.httpx.AsyncClient", _mock_client_factory(handler)):
            result = await deadman.deadman_ping_once()
        self.assertEqual(result["provisioned"], True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)

    async def test_non_2xx_response_is_not_ok(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        with patch.object(deadman, "_read_ping_url", return_value="https://hc.example/ping/abc"), \
             patch.object(deadman, "_self_health", return_value=True), \
             patch("app.deadman.httpx.AsyncClient", _mock_client_factory(handler)):
            result = await deadman.deadman_ping_once()
        self.assertEqual(result["provisioned"], True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 500)

    async def test_unhealthy_self_check_pings_fail_suffix(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200)

        with patch.object(deadman, "_read_ping_url", return_value="https://hc.example/ping/abc"), \
             patch.object(deadman, "_self_health", return_value=False), \
             patch("app.deadman.httpx.AsyncClient", _mock_client_factory(handler)):
            result = await deadman.deadman_ping_once()
        self.assertTrue(seen["url"].endswith("/fail"))
        self.assertFalse(result["healthy"])

    async def test_transport_exception_never_raises_and_reports_not_ok(self):
        secret = "super-secret-token"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"connection refused for {request.url}", request=request)

        with patch.object(deadman, "_read_ping_url", return_value=f"https://hc.example/ping/{secret}"), \
             patch.object(deadman, "_self_health", return_value=True), \
             patch("app.deadman.httpx.AsyncClient", _mock_client_factory(handler)), \
             self.assertLogs("nexus.deadman", level="WARNING") as captured:
                result = await deadman.deadman_ping_once()
        self.assertEqual(result["provisioned"], True)
        self.assertFalse(result["ok"])
        self.assertIn("ConnectError", result["detail"])
        self.assertNotIn(secret, str(result))
        self.assertNotIn("hc.example", str(result))
        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertNotIn("hc.example", "\n".join(captured.output))

    async def test_result_carries_no_url_or_secret(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        with patch.object(deadman, "_read_ping_url", return_value="https://hc.example/ping/super-secret-token"), \
             patch.object(deadman, "_self_health", return_value=True), \
             patch("app.deadman.httpx.AsyncClient", _mock_client_factory(handler)):
            result = await deadman.deadman_ping_once()
        self.assertNotIn("super-secret-token", str(result))
        self.assertNotIn("hc.example", str(result))


if __name__ == "__main__":
    unittest.main()
