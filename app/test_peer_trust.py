"""Regression tests for the peer-trust boundary (app/trust.py).

Context: Nexus has no in-process authentication by design — Cloudflare Access
authenticates public traffic at the edge. But Access only sees traffic that goes
through the tunnel, and Nexus also listened on 0.0.0.0, so any LAN peer could hit
the same routes directly. Among them:

    POST /api/control/workers  ->  worker_manager.launch(host, prompt, ...)

whose only guards are a known host name and a CLIENT-SUPPLIED `confirmed: true`
flag — a UI confirmation, not a credential. That made arbitrary prompt execution
as a fleet worker reachable from the LAN with no authentication at all.

These tests pin the boundary that closes it. They deliberately assert the
*rejections*: the failure mode being prevented is "a peer that should not be
trusted is", so the LAN and public cases matter more than the happy path.
"""
from __future__ import annotations

import unittest

import anyio

from . import trust


ALPHA_TAILNET = "100.64.0.10"
CHARLIE_TAILNET = "100.64.0.11"
ALPHA_TAILNET_V6 = "fd7a:115c:a1e0::7637:d25"


class ClassifyTests(unittest.TestCase):
    def test_loopback_is_trusted(self):
        """cloudflared dials the origin from loopback, so this IS the public path
        — it has already passed Access at the edge."""
        for addr in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"):
            self.assertEqual(trust.classify_peer_trust(addr), "loopback", addr)

    def test_tailnet_is_trusted(self):
        for addr in (ALPHA_TAILNET, CHARLIE_TAILNET, ALPHA_TAILNET_V6,
                     "100.64.0.0", "100.64.0.255"):
            self.assertEqual(trust.classify_peer_trust(addr), "tailnet", addr)

    def test_lan_is_not_trusted(self):
        """The regression that matters. 192.168/16 is where guest wifi and IoT
        devices live; it is NOT the WireGuard-authenticated tailnet."""
        for addr in ("192.0.2.66", "192.0.2.78", "198.51.100.1", "172.17.0.1"):
            self.assertIsNone(trust.classify_peer_trust(addr), addr)

    def test_public_is_not_trusted(self):
        for addr in ("8.8.8.8", "1.1.1.1"):
            self.assertIsNone(trust.classify_peer_trust(addr), addr)

    def test_cgnat_boundaries_hold(self):
        self.assertIsNone(trust.classify_peer_trust("100.63.255.255"))
        self.assertIsNone(trust.classify_peer_trust("100.128.0.0"))

    def test_unrelated_v6_ula_is_not_this_tailnet(self):
        self.assertIsNone(trust.classify_peer_trust("fd00:dead:beef::1"))

    def test_missing_or_malformed_peer_fails_closed(self):
        for addr in (None, "", "garbage", "999.999.999.999"):
            self.assertIsNone(trust.classify_peer_trust(addr), repr(addr))


class MiddlewareTests(unittest.TestCase):
    def _run(self, peer, kind="http", path="/api/control/workers"):
        reached = []

        async def downstream(_scope, _receive, _send):
            reached.append(True)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        events = []

        async def send(event):
            events.append(event)

        mw = trust.PeerTrustMiddleware(downstream)
        scope = {
            "type": kind,
            "path": path,
            "client": (peer, 12345) if peer else None,
            "headers": [],
        }
        anyio.run(mw, scope, receive, send)
        return bool(reached), events

    def test_lan_peer_cannot_reach_the_worker_launch_route(self):
        """The exact exposure: before this middleware a LAN peer got a real
        response from the control router (a 404 for an unknown job id, not a
        403), proving the route was live and ungated."""
        reached, events = self._run("192.0.2.78")
        self.assertFalse(reached, "a LAN peer must never reach the control router")
        self.assertEqual(events[0]["status"], 403)

    def test_public_peer_is_rejected(self):
        reached, events = self._run("8.8.8.8")
        self.assertFalse(reached)
        self.assertEqual(events[0]["status"], 403)

    def test_loopback_peer_passes(self):
        """Must not break the Cloudflare tunnel origin or the same-box hook."""
        reached, _ = self._run("127.0.0.1")
        self.assertTrue(reached)

    def test_tailnet_peer_passes(self):
        """Must not break the delta/charlie session hooks, whose COLLECTOR is
        http://100.64.0.10:8770/events."""
        reached, _ = self._run(CHARLIE_TAILNET, path="/events")
        self.assertTrue(reached)

    def test_websocket_upgrade_is_gated_too(self):
        """/control/ws drives the fixed-command strategy terminals; gating only
        HTTP would leave the more dangerous surface open."""
        reached, events = self._run("192.0.2.78", kind="websocket", path="/control/ws")
        self.assertFalse(reached)
        self.assertEqual(events[0]["type"], "websocket.close")
        self.assertEqual(events[0]["code"], 1008)

    def test_trusted_websocket_passes(self):
        reached, _ = self._run(CHARLIE_TAILNET, kind="websocket", path="/control/ws")
        self.assertTrue(reached)

    def test_missing_peer_fails_closed(self):
        reached, events = self._run(None)
        self.assertFalse(reached)
        self.assertEqual(events[0]["status"], 403)

    def test_lifespan_passes_through(self):
        """The gate must not interfere with startup/shutdown."""
        reached, _ = self._run("irrelevant", kind="lifespan")
        self.assertTrue(reached)


class ProxyHeaderTests(unittest.TestCase):
    """Regression: uvicorn's proxy_headers must stay OFF.

    It defaults to True, which rewrites scope["client"] from X-Forwarded-For
    whenever the immediate peer is in forwarded_allow_ips — whose default is
    127.0.0.1, i.e. exactly cloudflared. With it on, a phone loading
    nexus.example.com reached this gate as its own public IPv6 instead of as
    cloudflared's loopback and was rejected 403, locking the owner out of the
    dashboard within a minute of deploy.

    The principle: PeerTrustMiddleware is a TRANSPORT trust decision. It must see
    who actually opened the socket, never who a header claims to be. A gate whose
    input can be set by the client is not a gate."""

    def test_launcher_disables_proxy_headers(self):
        import inspect
        import run as nexus_run

        src = inspect.getsource(nexus_run.main)
        self.assertIn(
            "proxy_headers=False", src,
            "uvicorn defaults proxy_headers=True, which makes scope['client'] "
            "header-derived and breaks the trust gate for all tunnel traffic",
        )

    def test_a_forwarded_header_cannot_grant_trust(self):
        """Even if proxy_headers were re-enabled, an untrusted peer sending
        X-Forwarded-For: 127.0.0.1 must not be trusted by this middleware — the
        middleware reads scope['client'] only, never the headers."""
        reached = []

        async def downstream(_scope, _receive, _send):
            reached.append(True)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        events = []

        async def send(event):
            events.append(event)

        mw = trust.PeerTrustMiddleware(downstream)
        scope = {
            "type": "http",
            "path": "/api/control/workers",
            "client": ("192.0.2.78", 12345),
            "headers": [
                (b"x-forwarded-for", b"127.0.0.1"),
                (b"cf-connecting-ip", b"127.0.0.1"),
            ],
        }
        anyio.run(mw, scope, receive, send)
        self.assertFalse(reached, "headers must never grant transport trust")
        self.assertEqual(events[0]["status"], 403)


class BindAddressTests(unittest.TestCase):
    def test_launcher_defaults_to_loopback_only(self):
        import os
        from unittest import mock
        import run as nexus_run

        env = dict(os.environ)
        env.pop("NEXUS_BIND_ADDRESSES", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(nexus_run.bind_addresses(), ["127.0.0.1"])

    def test_launcher_never_defaults_to_wildcard(self):
        import run as nexus_run
        self.assertNotIn("0.0.0.0", nexus_run.bind_addresses())


if __name__ == "__main__":
    unittest.main()
