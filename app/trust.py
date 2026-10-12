"""Peer-trust boundary for every request that does NOT come through Cloudflare.

Nexus has no in-process authentication and is not getting any: public traffic is
authenticated by Cloudflare Access at the edge before the tunnel forwards it, and
that is the deliberate design (see test_cloudflare_access_boundary.py).

The gap this closes: Access only sees traffic that actually goes through the
tunnel. Nexus also listened on 0.0.0.0, so anything on the LAN could reach the
same routes directly and skip Access entirely — including
`POST /api/control/workers`, whose only guards are a known host name and a
CLIENT-SUPPLIED `confirmed: true` flag. That is a UI "are you sure", not a
credential. A LAN peer could therefore execute an arbitrary prompt as a fleet
worker, and Gemini fleet workers run with tool auto-approval.

Two layers now close it, either of which is sufficient:
  1. the unit binds 127.0.0.1 and the tailnet address EXPLICITLY, never 0.0.0.0,
     so the LAN cannot open a socket at all;
  2. this middleware rejects any peer that is not loopback or tailnet, so a
     future bind change cannot silently re-open the hole.

Trust model — deliberately the NARROW one (mirrors
breadcrumbs/history/server/requestTrust.js and tower/server.py):

  loopback  127/8, ::1  — cloudflared dials the origin from here, so this IS the
                          public path; it has already passed Access at the edge.
                          Also same-box hooks.
  tailnet   100.64/10, fd7a:115c:a1e0:  — WireGuard-authenticated devices. The
                          delta/charlie session hooks post here.
  anything else          — rejected, including all of RFC1918. The LAN is NOT
                          the tailnet: guest wifi and IoT devices live there.

Note this is narrower than the older `_is_private_source` helper in routes.py,
which accepted any RFC1918 address. That helper stays as belt-and-suspenders on
/events but is no longer the outermost check.
"""
from __future__ import annotations

import ipaddress
import logging

log = logging.getLogger("nexus.trust")

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6_PREFIX = "fd7a:115c:a1e0:"


def normalize_peer(address: object) -> str:
    """Lowercase, drop an IPv6 %zone, and unwrap ::ffff: v4-mapped addresses.

    A dual-stack listener reports loopback as '::ffff:127.0.0.1'; without the
    unwrap a legitimate same-box caller would be classified untrusted."""
    normalized = str(address or "").strip().lower()
    zone = normalized.find("%")
    if zone != -1:
        normalized = normalized[:zone]
    if normalized.startswith("::ffff:"):
        normalized = normalized[7:]
    return normalized


def _parsed(address: object):
    try:
        return ipaddress.ip_address(normalize_peer(address))
    except ValueError:
        return None


def is_loopback_peer(address: object) -> bool:
    """Whole 127/8 block and ::1 — not just the literal 127.0.0.1."""
    ip = _parsed(address)
    return ip is not None and ip.is_loopback


def is_tailnet_peer(address: object) -> bool:
    """Tailscale CGNAT range, or this tailnet's IPv6 ULA prefix."""
    normalized = normalize_peer(address)
    ip = _parsed(normalized)
    if ip is None:
        return False
    if ip.version == 6:
        return normalized.startswith(TAILNET_V6_PREFIX)
    return ip in TAILNET_V4


def classify_peer_trust(address: object) -> str | None:
    """Name the channel, or None if the peer is not trusted.

    Returning a NAME rather than a bool keeps the decision legible in the log:
    which door a request came in by is recorded, not inferred."""
    if is_loopback_peer(address):
        return "loopback"
    if is_tailnet_peer(address):
        return "tailnet"
    return None


class PeerTrustMiddleware:
    """Pure-ASGI gate. Rejects any peer that is neither loopback nor tailnet.

    Pure ASGI rather than BaseHTTPMiddleware so it does not buffer streaming
    responses and so it can also gate WebSocket upgrades — /control/ws drives the
    fixed-command strategy terminals and must not be reachable from the LAN
    either. Fails CLOSED: a missing or unparseable peer is rejected."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)     # lifespan et al
            return
        client = scope.get("client")
        peer = client[0] if client else None
        channel = classify_peer_trust(peer)
        if channel:
            await self.app(scope, receive, send)
            return
        log.warning(
            "peer-trust: rejected %s %s from untrusted peer %s",
            kind, scope.get("path", "?"), normalize_peer(peer) or "<none>",
        )
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"error":"trusted network or Cloudflare Access required"}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
