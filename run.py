"""Launcher that binds each address EXPLICITLY instead of the 0.0.0.0 wildcard.

Nexus needs to be reachable from exactly two places:

  * loopback           — cloudflared dials the origin here, so this is the public
                         (Cloudflare-Access-authenticated) path, plus same-box
                         session hooks;
  * alpha's tailnet IP — the delta/charlie session hooks POST /events here
                         (verified: their COLLECTOR is http://<this-node-tailnet-address>:8770/events).

The unit previously passed `--host 0.0.0.0` with the comment "so delta/charlie
hooks can POST /events over the trusted tailnet". The intent was the tailnet; the
wildcard just happens to hand the same routes to the LAN as well — where nothing
authenticates them, because Cloudflare Access only sees traffic that actually
goes through the tunnel. uvicorn's CLI --host takes a single address, which is
why this launcher exists rather than a longer ExecStart.

PeerTrustMiddleware independently rejects untrusted peers, so this is the second
of two layers, not the only one. Either alone closes the hole.
"""
from __future__ import annotations

import logging
import os
import socket

import uvicorn

from app.main import app

log = logging.getLogger("nexus.run")

DEFAULT_BIND = "127.0.0.1"


def bind_addresses() -> list[str]:
    raw = (os.environ.get("NEXUS_BIND_ADDRESSES")
           or os.environ.get("PANEL_BIND_ADDRESSES")
           or os.environ.get("FLEET_NEXUS_BIND_ADDRESSES", "")).strip()
    addrs = [a.strip() for a in raw.split(",") if a.strip()]
    return addrs or [DEFAULT_BIND]


def listen_sockets(addresses: list[str], port: int) -> list[socket.socket]:
    """One socket per address; a per-address failure is a warning, not fatal.

    tailscaled may not be up yet after a reboot. Losing the tailnet listener
    degrades the remote hooks; taking the whole dashboard down with it would be
    strictly worse, since loopback still carries the tunnel origin."""
    socks: list[socket.socket] = []
    for addr in addresses:
        try:
            info = socket.getaddrinfo(
                addr, port, proto=socket.IPPROTO_TCP, flags=socket.AI_PASSIVE
            )[0]
            s = socket.socket(info[0], info[1])
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if info[0] == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind(info[4])
            s.listen()
            s.set_inheritable(True)
            socks.append(s)
            log.info("nexus: listening on %s:%d", addr, port)
        except OSError as exc:
            log.warning(
                "nexus: could not bind %s:%d (%s) — continuing without it",
                addr, port, type(exc).__name__,
            )
    return socks


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("NEXUS_PORT")
               or os.environ.get("PANEL_PORT")
               or os.environ.get("FLEET_PORT", "8770"))
    addrs = bind_addresses()
    socks = listen_sockets(addrs, port)
    if not socks:
        raise SystemExit(f"nexus: no listening socket could be bound from {addrs}")
    # proxy_headers MUST stay off. uvicorn defaults it to True, which rewrites
    # scope["client"] from X-Forwarded-For whenever the immediate peer is in
    # forwarded_allow_ips (default 127.0.0.1 — i.e. exactly cloudflared). That
    # turns the peer address into a CLIENT-INFLUENCED value, and PeerTrustMiddleware
    # is a *transport* trust decision: it must see who actually opened the socket,
    # never who a header claims to be.
    #
    # Leaving it on broke real access within a minute of deploy: a phone loading
    # nexus.example.com arrived at the gate as its own public IPv6 rather than as
    # cloudflared's loopback, and was rejected. Access had already authenticated
    # that request at the edge; the gate should never have been judging it.
    #
    # Cost: Nexus sees cloudflared's 127.0.0.1 instead of the visitor's IP. Nothing
    # here needs the visitor IP — the sole consumer is routes.py's _is_private_source
    # on /events, and hook traffic arrives directly rather than through the proxy.
    config = uvicorn.Config(app, log_level="info", proxy_headers=False)
    uvicorn.Server(config).run(sockets=socks)


if __name__ == "__main__":
    main()
