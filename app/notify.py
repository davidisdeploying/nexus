"""
Nexus notifications — Phase 3 auto-router (design panel-notifications-design.md
§D, §A.2, §A.3).

`notify(event)` is the SINGLE entrypoint into notifications: normalize ->
classify -> dedup -> render -> fan_out. Both `POST /api/notify` (routes.py)
and the relay-outcome watcher (app/run_watcher.py) call this — there is
exactly one path in, per design §D.2.

Channel -> transport (design §A.1, §D.2, §E):
  nexus-log     -> feed + badge only (notification_log row), NEVER a push
  nexus-post    -> send_push (Phase 2); never send_ntfy
  nexus-approve -> send_push + ntfy tap-through mirror, prio 4 (design D-2) —
                   no one-tap HTTP Approve action over ntfy
  nexus-alarm   -> send_ntfy is the primary transport, prio 5 (design §E,
                   D-3); send_push is a best-effort PWA duplicate (design D-4).
                   Both attempted; neither's failure blocks the other.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from . import notify_store, ntfy, push

log = logging.getLogger("nexus.notify")

TOKEN_RE = re.compile(
    r"^FLEET-(?:(?P<seat>[A-Z]+)-)?(?P<kind>RECON|BUILD)-(?P<date>\d{8})-(?P<slug>[a-z0-9-]+)$"
)

# Health condition -> (channel, prio), per design §A.2. Routed by condition
# string ONLY, never by token (design §A.3 rule 5).
HEALTH_CONDITIONS: dict[str, tuple[str, int]] = {
    "thermal_critical": ("nexus-alarm", 5),
    "gpu_xid_fault": ("nexus-alarm", 5),
    "service_down": ("nexus-alarm", 5),
    "disk_warn": ("nexus-post", 4),
    "disk_critical": ("nexus-alarm", 5),
    "backup_stale": ("nexus-post", 4),
    "heartbeat_stale": ("nexus-post", 4),
    # Phase 4a (app/health_watch.py) recovery edges — DELIBERATELY distinct
    # condition strings from their alarm counterparts (mirrors thermal_recovery
    # vs thermal_halt below). event_key is derived from `condition` alone
    # (_event_key), so a recovery reusing its alarm's condition string would
    # collide with the alarm's own notification_log row and get suppressed by
    # the 30-min health dedup window — exactly the bug a same-key recovery hit
    # in this build's isolated verify pass.
    "disk_warn_recovery": ("nexus-post", 4),
    "disk_critical_recovery": ("nexus-post", 4),
    "backup_stale_recovery": ("nexus-post", 4),
    "service_down_recovery": ("nexus-post", 4),
    "heartbeat_stale_recovery": ("nexus-post", 4),
    # Thermal-halt slice (app/thermal_watch.py), targeted Phase-4 wiring —
    # charlie's thermal-guard hard-halts gallery at 110C; these are the ONLY
    # conditions currently routed through notify() from a probe/scheduler
    # sweep (every other HEALTH_CONDITIONS entry above is still unwired).
    "thermal_halt": ("nexus-alarm", 5),
    "thermal_recovery": ("nexus-post", 4),
    "thermal_approaching": ("nexus-alarm", 5),
    "thermal_guard_dark": ("nexus-post", 4),
    # Phase-4c: Worker5's guard caps auto-restarts and stays down (thermal
    # runaway / cooling failure needing manual intervention) — distinct from
    # thermal_halt/thermal_recovery above so its own 30-min dedup window
    # never collides with a concurrent halt/recovery episode.
    "thermal_holdoff": ("nexus-alarm", 5),
    "thermal_holdoff_recovery": ("nexus-post", 4),
    # Model quota history (app/model_usage_watch.py). User-visible pushes are
    # intentionally narrow: confirmed 5-hour/weekly rollovers and aggregate
    # all-provider tracker loss. Every other quota event remains history-only.
    "model_quota_window_rolled_over": ("nexus-post", 3),
    "model_quota_tracker_unavailable": ("nexus-post", 4),
    # CONFORMANCE-2 part D (app/conformance_watch.py): fleet conformance
    # transition watcher. Per-check drift/recovery and cache stale/fresh are
    # deliberately distinct condition strings (mirrors the thermal/disk
    # alarm-vs-recovery split above) so a recovery never collides with its own
    # alarm's dedup window; the watcher also sets an explicit, timestamped
    # event_key per edge, so this catalog entry only governs channel/prio.
    "conformance_check_drift": ("nexus-post", 4),
    "conformance_check_recovery": ("nexus-post", 4),
    "conformance_cache_stale": ("nexus-post", 4),
    "conformance_cache_recovery": ("nexus-post", 4),
    # Compendium (library on alpha). David does not want reminder or
    # calendar delivery here — his iPhone's own apps do that, and Compendium
    # only syncs them for one dashboard. These are the things Apple cannot tell
    # him. Alarm/recovery pairs use distinct strings, per the split above.
    #
    # The approval is the reason this catalog entry exists at all: a vault
    # agent-request expires in five minutes, so nexus-post prio 3 (the default
    # for an unknown condition) is not good enough. nexus-approve adds the ntfy
    # tap-through mirror, which is what makes a five-minute window actionable
    # when the PWA is not already open.
    # PRODUCER CONTRACT: this one MUST send an explicit per-request event_key
    # (e.g. "compendium-agent-request:<uuid>"). Without it _event_key derives
    # "alert:compendium_agent_request:<host>" for every request alike, and the
    # 30-minute health dedup window would swallow the second approval inside
    # half an hour — each of which is separately actionable and expires in five
    # minutes. The catalog cannot enforce this; the caller must.
    "compendium_agent_request": ("nexus-approve", 4),
    "compendium_bridge_profile_expiring": ("nexus-post", 4),
    "compendium_sync_stale": ("nexus-post", 4),
    "compendium_sync_stale_recovery": ("nexus-post", 4),
    "compendium_backup_failed": ("nexus-post", 4),
}

_EMOJI = {
    "recon_success": "🔍✅",
    "build_success": "🛠️✅",
    "failure": "❌",
    "abort_restore": "⛑️",
    "collision": "⚠️",
    "turn_end_death": "💀",
    "blocked_awaiting_approval": "✋",
    "unrouted": "❓",
}

# Health alerts re-fire inside this window (design §D.2/§E.3); run outcomes
# and milestones are exact-once forever (window=None).
_HEALTH_DEDUP_WINDOW_SECONDS = 30 * 60


def parse_token(token: str | None) -> dict | None:
    """Lenient parse (design §A.3): SEAT is optional, existing tokens in the
    wild omit it."""
    if not token:
        return None
    m = TOKEN_RE.match(token)
    if not m:
        return None
    return {
        "seat": m.group("seat"),
        "kind": m.group("kind"),
        "date": m.group("date"),
        "slug": m.group("slug"),
    }


def normalize(event: dict) -> dict:
    ev = dict(event)
    ev.setdefault("received_at", datetime.now(timezone.utc).isoformat())
    token = ev.get("token")
    ev["_parsed"] = parse_token(token) if token else None
    if ev["_parsed"] and not ev.get("seat"):
        ev["seat"] = ev["_parsed"].get("seat")
    exit_code = ev.get("exit_code")
    if exit_code is not None:
        try:
            ev["exit_code"] = int(exit_code)
        except (TypeError, ValueError):
            ev["exit_code"] = None
    return ev


def classify(ev: dict) -> dict:
    """Routing rules, first match wins (design §A.3). Returns
    {channel, prio, template_id}."""
    condition = ev.get("condition")
    if condition:
        channel, prio = HEALTH_CONDITIONS.get(condition, ("nexus-post", 3))
        return {"channel": channel, "prio": prio, "template_id": f"health:{condition}"}

    parsed = ev.get("_parsed")
    outcome = ev.get("outcome")
    kind = (parsed or {}).get("kind")

    if parsed:
        if outcome == "blocked_awaiting_approval":
            return {"channel": "nexus-approve", "prio": 4,
                     "template_id": "blocked_awaiting_approval"}
        if outcome in {"failure", "abort_restore", "collision", "turn_end_death"}:
            return {"channel": "nexus-post", "prio": 4, "template_id": outcome}
        if outcome == "success" and kind == "BUILD":
            return {"channel": "nexus-post", "prio": 3, "template_id": "build_success"}
        if outcome == "success" and kind == "RECON":
            return {"channel": "nexus-log", "prio": 2, "template_id": "recon_success"}

    # Rule 6: unparseable token, or a parsed token with no recognized outcome —
    # never dropped silently.
    return {"channel": "nexus-post", "prio": 3, "template_id": "unrouted"}


def _event_key(ev: dict, cls: dict) -> str:
    if ev.get("event_key"):
        return str(ev["event_key"])
    condition = ev.get("condition")
    if condition:
        return f"alert:{condition}:{ev.get('host') or ''}"
    token = ev.get("token")
    if token:
        outcome = ev.get("outcome") or cls["template_id"]
        return f"run:{token}:{outcome}"
    return f"unrouted:{ev.get('source') or 'unknown'}:{ev.get('received_at')}"


def render(ev: dict, cls: dict) -> dict:
    """Title/emoji/body/navigate from the catalog (design §A.2)."""
    template_id = cls["template_id"]
    token = ev.get("token")
    parsed = ev.get("_parsed") or {}
    seat = ev.get("seat") or parsed.get("seat") or "seat"
    slug = parsed.get("slug") or token or "unknown"
    kind = parsed.get("kind") or "run"
    emoji = ev.get("emoji") or _EMOJI.get(template_id, "🔔")
    body = ev.get("body")

    if template_id == "recon_success":
        title, navigate = f"{seat} recon done — {slug}", f"/run/{token}"
    elif template_id == "build_success":
        title, navigate = f"{seat} build done — {slug}", f"/run/{token}"
    elif template_id == "failure":
        title = f"{seat} {kind.lower()} FAILED ({ev.get('exit_code')}) — {slug}"
        navigate = f"/run/{token}#log"
    elif template_id == "abort_restore":
        title, navigate = f"{seat} build rolled back — {slug}", f"/run/{token}#gate"
    elif template_id == "collision":
        title, navigate = f"Token collision — {token}", f"/run/{token}#collision"
    elif template_id == "turn_end_death":
        title, navigate = f"{seat} run died — {slug}", f"/run/{token}#log"
    elif template_id == "blocked_awaiting_approval":
        title, navigate = f"Approve? {seat} build — {slug}", f"/approve/{token}"
    elif template_id.startswith("health:"):
        condition = template_id.split(":", 1)[1]
        title = ev.get("title") or f"{condition.replace('_', ' ')} — {ev.get('host') or ''}"
        # Callers may target a more informative existing route (design note,
        # thermal-halt slice) than the generic per-alert view; fall back to
        # the original /alerts/{alert_id} when none is supplied.
        navigate = ev.get("navigate") or f"/alerts/{ev.get('alert_id') or ''}"
    else:
        title = "Unrouted event"
        if not body:
            raw = {k: v for k, v in ev.items() if not k.startswith("_")}
            body = json.dumps(raw, default=str)[:500]
        navigate = f"/run/{token}" if token else "/notifications"

    return {
        "event_key": _event_key(ev, cls),
        "channel": cls["channel"],
        "prio": cls["prio"],
        "title": title,
        "body": body,
        "navigate": navigate,
        "emoji": emoji,
        "tag": token or _event_key(ev, cls),
    }


def _dedup_window_seconds(template_id: str) -> int | None:
    return _HEALTH_DEDUP_WINDOW_SECONDS if template_id.startswith("health:") else None


async def fan_out(note: dict) -> dict:
    """channel -> transport (design §D.2). Always logs notification_log FIRST
    — send_push does this internally; the nexus-log branch does it directly."""
    channel = note["channel"]
    if channel == "nexus-log":
        row = notify_store.insert_notification(
            event_key=note["event_key"], channel=channel, prio=note["prio"],
            title=note["title"], body=note.get("body"), navigate=note.get("navigate"),
            emoji=note.get("emoji"),
        )
        return {"log_id": row["id"], "targeted": 0, "pushed": False}

    if channel == "nexus-approve":
        result = await push.send_push(note)
        # D-2: tap-through mirror only — no one-tap HTTP Approve action, the
        # native app just opens the approve page like the PWA push does.
        sent = await ntfy.send_ntfy(
            title=note["title"], body=note.get("body") or "", priority=4,
            click=f"{note['navigate']}?nf=1", tags=note.get("emoji"),
        )
        if result.get("log_id"):
            notify_store.update_notification_ntfy(result["log_id"], sent)
        return {**result, "pushed": True, "sent_ntfy": int(sent)}

    if channel == "nexus-post":
        result = await push.send_push(note)
        return {**result, "pushed": True}

    if channel == "nexus-alarm":
        # send_ntfy is the primary transport (design §E, D-3 — reliable
        # critical wake-me); send_push is a best-effort PWA duplicate (D-4).
        # Both attempted; neither's failure blocks the other.
        result = await push.send_push(note)
        sent = await ntfy.send_ntfy(
            title=note["title"], body=note.get("body") or "", priority=5,
            click=note.get("navigate"), tags=note.get("emoji"),
        )
        if result.get("log_id"):
            notify_store.update_notification_ntfy(result["log_id"], sent)
        return {**result, "pushed": True, "sent_ntfy": int(sent)}

    # Unreachable in practice — classify() only ever emits the three channels
    # above — but fail safe rather than raise, per design rule 6 (never drop).
    row = notify_store.insert_notification(
        event_key=note["event_key"], channel=channel, prio=note["prio"],
        title=note["title"], body=note.get("body"), navigate=note.get("navigate"),
        emoji=note.get("emoji"),
    )
    return {"log_id": row["id"], "targeted": 0, "pushed": False}


async def notify(event: dict) -> dict:
    """The one path into notifications. Returns a small result dict describing
    what happened (never raises — a bad event renders as 'unrouted' rather
    than failing the caller, per design rule 6)."""
    ev = normalize(event)
    cls = classify(ev)
    note = render(ev, cls)
    window = _dedup_window_seconds(cls["template_id"])

    if notify_store.notification_exists(note["event_key"], since_seconds=window):
        log.info("notify: suppressed duplicate event_key=%s", note["event_key"])
        return {
            "suppressed": True, "event_key": note["event_key"],
            "channel": cls["channel"], "prio": cls["prio"],
        }

    result = await fan_out(note)
    log.info("notify: routed event_key=%s channel=%s prio=%s",
              note["event_key"], cls["channel"], cls["prio"])
    return {"suppressed": False, "event_key": note["event_key"],
            "channel": cls["channel"], "prio": cls["prio"], **result}
