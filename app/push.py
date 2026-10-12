"""
Nexus notifications — Phase 2 send_push helper (design §C.4).

One function, called by the router only: fan a Note out to every active
push_subscription row via pywebpush, using the canonical DWP payload shape
(§C.2) so the SAME JSON is consumed declaratively on iOS 18.4+ and by the
fallback service worker (static/sw.js) everywhere else.

The notification_log row is always inserted FIRST — the feed must record an
event even when every push delivery fails or there are zero subscriptions.
"""
from __future__ import annotations

import os

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from pywebpush import WebPushException, webpush
from starlette.concurrency import run_in_threadpool

from . import notify_store
from .config import settings

log = logging.getLogger("nexus.push")

# Web Push requires a contact address. Deployment-specific: set NEXUS_VAPID_SUB
# in .env. The default is the RFC 2606 example domain.
VAPID_SUB = os.environ.get("NEXUS_VAPID_SUB", "mailto:admin@example.com")
TTL_SECONDS = 24 * 60 * 60

_URGENCY = {1: "low", 2: "low", 3: "normal", 4: "high", 5: "high"}
_HARD_FAIL_STATUSES = {403, 404, 410}

_VALID_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def _urgency_for_prio(prio: int) -> str:
    return _URGENCY.get(int(prio), "normal")


def topic_for_tag(tag: str) -> str:
    """Web Push Topic must be 1-32 URL-safe base64 characters (RFC 8030 §5.4).
    Raw notification tags (event keys) routinely contain colons and other
    punctuation, so pass an already-conformant tag through unchanged and
    derive a stable 32-char token from SHA-256 for anything else."""
    if tag and _VALID_TOPIC_RE.match(tag):
        return tag
    digest = hashlib.sha256((tag or "").encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:32]


def build_payload(note: dict, app_badge: int) -> dict:
    """The canonical DWP JSON shape (design §C.2). `title` gets the emoji
    prefix here so both the declarative display path and the fallback SW's
    showNotification() see one already-composed string."""
    title = note["title"]
    if note.get("emoji"):
        title = f"{note['emoji']} {title}"
    return {
        "web_push": 8030,
        "notification": {
            "title": title,
            "body": note.get("body") or "",
            "navigate": note.get("navigate") or "/notifications",
            "tag": note.get("tag") or note["event_key"],
            "app_badge": app_badge,
            "silent": bool(note.get("silent", False)),
        },
        "channel": note.get("channel", "nexus-post"),
        "prio": int(note.get("prio", 3)),
    }


def _send_one(sub: dict, payload: dict, prio: int) -> bool:
    """Runs in a worker thread (blocking `requests` call inside pywebpush).
    Returns True only on an accepted delivery, so the caller can persist a
    truthful per-notification sent_pwa outcome instead of inferring it later
    from a subscription row that other, unrelated sends keep mutating."""
    endpoint = sub["endpoint"]
    try:
        webpush(
            subscription_info={
                "endpoint": endpoint,
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=str(settings.vapid_private_key_path),
            vapid_claims={"sub": VAPID_SUB},
            ttl=TTL_SECONDS,
            headers={"Urgency": _urgency_for_prio(prio),
                     "Topic": topic_for_tag(payload["notification"]["tag"])},
            timeout=10,
        )
        notify_store.mark_subscription_sent(endpoint)
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in _HARD_FAIL_STATUSES:
            log.info("push: endpoint gone (status=%s) -> deactivating", status)
            notify_store.mark_subscription_gone(endpoint)
        else:
            log.warning("push: send failed (status=%s) -> bumping failure count", status)
            notify_store.bump_subscription_failure(endpoint)
        return False
    except Exception:  # noqa: BLE001 — one bad subscription must not sink the fan-out
        log.warning("push: unexpected send error", exc_info=True)
        notify_store.bump_subscription_failure(endpoint)
        return False


async def send_push(note: dict, endpoint: str | None = None) -> dict:
    """Fan a Note out to all active push_subscription rows (or just `endpoint`
    if given, for the settings-card single-device test). Note keys: event_key,
    channel, prio, title, body, navigate, tag, emoji, silent (see build_payload).

    Always logs to notification_log first, per design §C.4 — this holds even
    when there are zero active subscriptions."""
    row = notify_store.insert_notification(
        event_key=note["event_key"],
        channel=note.get("channel", "nexus-post"),
        prio=int(note.get("prio", 3)),
        title=note["title"],
        body=note.get("body"),
        navigate=note.get("navigate"),
        emoji=note.get("emoji"),
    )

    subs = notify_store.list_active_subscriptions()
    if endpoint:
        subs = [s for s in subs if s["endpoint"] == endpoint]

    app_badge = notify_store.count_unread()
    payload = build_payload(note, app_badge)

    accepted = False
    for sub in subs:
        if await run_in_threadpool(_send_one, sub, payload, int(note.get("prio", 3))):
            accepted = True

    notify_store.update_notification_pwa(row["id"], accepted)

    return {
        "log_id": row["id"],
        "targeted": len(subs),
        "app_badge": app_badge,
        "sent_pwa": accepted,
    }
