"""
Nexus notifications — Phase 6a weekly self-test (design: "if it stops arriving
on a transport, that transport died").

Apple's push service returns success for a dead PWA subscription (iOS silently
drops it) — nothing in send_push's own error handling can ever catch that. The
only defense is a canary: fire a LOW-priority notification through BOTH
transports on a schedule and let the human notice if one goes quiet. This
bypasses notify()'s classify/routing entirely (there is no HEALTH_CONDITIONS
entry for "it's Sunday") and calls push.send_push + ntfy.send_ntfy directly,
same as routes.py's /api/push/test.

Distinct channel `nexus-selftest` on the notification_log row IS the "last
self-test time" record (notify_store.get_last_selftest_at reads it back) —
no new table needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import notify_store, ntfy, push

log = logging.getLogger("nexus.self_test")

# Weekly cadence — Sunday 09:00 Central (David's wall clock, not server UTC).
# Kept as its own CronTrigger timezone override rather than a Settings field,
# mirroring health_watch.py's REMIND_AFTER_HOURS-style local constants.
SELF_TEST_DAY_OF_WEEK = "sun"
SELF_TEST_HOUR = 9
SELF_TEST_MINUTE = 0
SELF_TEST_TIMEZONE = "America/Chicago"

_CHANNEL = "nexus-selftest"
_NTFY_PRIORITY = 3  # ntfy's own scale: default, NOT 5 — a weekly canary must not wake anyone.


async def run_self_test() -> dict:
    """Fires the canary through both transports. Never raises — a bad send on
    either transport logs and degrades that half of the result rather than
    sinking the job (same never-drop contract as push.send_push/ntfy.send_ntfy,
    which already never raise on their own)."""
    today = datetime.now(timezone.utc).date().isoformat()
    note = {
        "event_key": f"selftest:{today}",
        "channel": _CHANNEL,
        "prio": 3,  # push.build_payload's urgency map: 3 -> "normal" (design: PWA normal urgency)
        "title": "Nexus notifications alive — weekly self-test",
        "body": "Both transports fired on schedule. No action needed.",
        "navigate": "/notifications",
        "tag": "nexus-selftest",
        "emoji": "🔔",
    }

    push_result = await push.send_push(note)
    ntfy_sent = await ntfy.send_ntfy(
        title=note["title"], body=note["body"], priority=_NTFY_PRIORITY,
        click=note["navigate"], tags="test_tube",
    )
    if push_result.get("log_id"):
        notify_store.update_notification_ntfy(push_result["log_id"], ntfy_sent)

    log.info("self_test: fired (targeted=%s, sent_ntfy=%s)",
              push_result.get("targeted"), ntfy_sent)
    return {**push_result, "sent_ntfy": int(ntfy_sent), "channel": _CHANNEL}
