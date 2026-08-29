"""
Nexus notifications — Phase 5 send_ntfy (design §E, D-3).

Reliable critical transport for nexus-alarm (native ntfy app, not the flaky PWA
push path) plus a tap-through mirror for nexus-approve (D-2). Public ntfy.sh,
topic-as-password (D-3) — no self-hosting. The topic (settings.ntfy_topic) is
the ONLY secret here and is never logged or included in any return value.
"""
from __future__ import annotations

import logging
from email.header import Header

import httpx

from .config import settings

log = logging.getLogger("nexus.ntfy")

_TIMEOUT_SECONDS = 8


def _header_value(value: str) -> str:
    """HTTP headers are ASCII-only (httpx raises UnicodeEncodeError otherwise).
    ntfy's documented fallback for non-ASCII header content is RFC 2047
    encoded-word syntax (docs.ntfy.sh/publish/#e-mail-style-headers), which
    its server decodes back to the original Unicode text. ASCII-only values
    pass through untouched so the common case never gets encoded-word noise."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode()


async def send_ntfy(
    title: str, body: str, priority: int,
    click: str | None = None, tags: str | None = None,
) -> bool:
    """POST one message to the configured ntfy.sh topic. Never raises — a
    provisioning gap or a network failure logs and returns False rather than
    sinking the caller (same never-drop-the-caller contract as send_push)."""
    topic = settings.ntfy_topic
    if not topic:
        log.info("ntfy: no topic provisioned -> no-op")
        return False

    headers = {"Title": _header_value(title), "Priority": str(priority)}
    if click:
        headers["Click"] = _header_value(f"{settings.public_origin}{click}")
    if tags:
        headers["Tags"] = _header_value(tags)

    url = f"{settings.ntfy_base_url}/{topic}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, content=body.encode("utf-8"), headers=headers)
        if 200 <= resp.status_code < 300:
            return True
        log.warning("ntfy: send failed (status=%s)", resp.status_code)
        return False
    except Exception:  # noqa: BLE001 — a bad send must not sink notify()
        log.warning("ntfy: unexpected send error", exc_info=True)
        return False
