"""
Nexus Notifications — Phase 1 shared context builders.

One build_*() per detail route (run/jobs/queues/alerts/approve), plus the
Notifications inbox context. Each
function returns a plain dict; the route then renders EITHER the full page or
the ?partial=1 fragment from the exact same context, so first-paint and a
future touch-sheet refresh can never drift (panel-notifications-design.md §B).

Hard rule mirrored from work.py/seatboard.py: every builder is isolated and
degrades to a graceful found=False / empty-state dict rather than raising —
these routes must 200 on missing data, never 500.

Data sources are REUSED, not reinvented:
  - run-state    -> from-{seat}/runs/<token>/ (mirrors seatboard._scan_seat /
                     work.read_relay_runs), + herospath for the transcript tail.
  - job/queue    -> the CACHED snapshot's work.jobs (same data the dashboard
                     renders; no fresh SSH probe in the request path, mirrors
                     routes._current_job_cards).
  - alerts/notifications -> notify_store (events.db).
  - approve      -> the run dir's own prompt.txt (the staged prompt actually
                     used for that run), falling back to token metadata alone.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import herospath, notify_store, seatboard
from .config import settings
from .seats import RUN_SOURCE_IDS, RUN_SOURCE_TO_CARD
from .store import read_snapshot

# from-{seat}/runs lives under the relay root, split from vault_root
# (loupe-vault, heartbeats only) on 2026-07-10.
RELAY = settings.relay_root
# Current node roots and legacy aliases come from app/seats.py.

# Same orphan window as seatboard/work: no `done` sentinel for this long with no
# touch = treat as died, not running forever.
ORPHAN_AFTER_S = 6 * 3600
# Small tail for embedding in the run-detail page — the full hero's-path scroll
# is a different, deliberately heavier view.
RUN_TAIL_CAP = 60


def _rel_age(mtime: float) -> str:
    secs = max(0, time.time() - mtime)
    if secs < 90:
        return "just now"
    mins = secs / 60
    if mins < 90:
        return f"{int(mins)}m ago"
    hrs = mins / 60
    if hrs < 36:
        return f"{int(hrs)}h ago"
    return f"{int(hrs / 24)}d ago"


def _fmt_ts(epoch: float | None) -> str | None:
    """Epoch -> 'YYYY-MM-DD HH:MM UTC', matching the fleet's UTC-first stamp
    convention. None passes through as None (renders as an em dash)."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return None


def _fmt_dur(secs: float | None) -> str | None:
    if secs is None:
        return None
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# --------------------------------------------------------------------------- #
# /run/{token} — path-safety mirrors herospath.resolve_transcript exactly:
# strict charset, then resolve()+relative_to() strictly inside a whitelisted
# from-{seat}/runs dir. Never accepts a caller-supplied path.
# --------------------------------------------------------------------------- #
def _resolve_run_dir(token: str) -> tuple[str | None, Path | None]:
    if not herospath.valid_token(token):
        return None, None
    for source in RUN_SOURCE_IDS:
        base = (RELAY / f"from-{source}" / "runs").resolve()
        candidate = base / token
        try:
            resolved = candidate.resolve()
            resolved.relative_to(base)
        except Exception:
            continue
        if resolved.is_dir():
            return RUN_SOURCE_TO_CARD[source], resolved
    return None, None


def _find_conflicts(d: Path) -> list[str]:
    """Syncthing *.sync-conflict-* forks inside this run dir, if any — the
    known fleet gotcha (concurrent seat writes forking vault files)."""
    try:
        return sorted(p.name for p in d.glob("*sync-conflict*"))
    except Exception:
        return []


def build_run_context(token: str) -> dict[str, Any]:
    """Run detail: seat/kind/slug/date, lane, timestamps, exit code, sentinel
    state, transcript tail. found=False (still 200) when the token has no
    matching run dir in any seat's from-{seat}/runs/."""
    now = time.time()
    ctx: dict[str, Any] = {"token": token, "found": False}
    seat, d = _resolve_run_dir(token)
    if d is None:
        return ctx

    ctx["found"] = True
    ctx["seat"] = seat
    ctx["seat_class"] = herospath.SEAT_CLASS.get(seat, "")
    started_epoch, lane = seatboard._started_epoch(d / "status.json")
    ctx["lane"] = lane
    ctx["kind"] = seatboard._kind_of(token, lane)
    ctx["slug"] = seatboard._short_token(token)
    dm = re.search(r"-(\d{4})(\d{2})(\d{2})-", token)
    ctx["date"] = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else None

    try:
        dir_mtime = d.stat().st_mtime
    except Exception:
        dir_mtime = now

    done_f = d / "done"
    exit_code: str | None = None
    ended_epoch: float | None = None
    if done_f.exists():
        try:
            exit_code = done_f.read_text(errors="replace")[:32].strip()
        except Exception:
            exit_code = ""
        state = "done" if exit_code in ("", "0") else "died"
        try:
            ended_epoch = done_f.stat().st_mtime
        except Exception:
            ended_epoch = dir_mtime
    elif (now - dir_mtime) > ORPHAN_AFTER_S:
        state = "died"
        ended_epoch = dir_mtime
    else:
        state = "running"

    ctx["state"] = state
    ctx["exit_code"] = exit_code
    ctx["started_epoch"] = started_epoch
    ctx["started_str"] = _fmt_ts(started_epoch)
    ctx["ended_epoch"] = ended_epoch
    ctx["ended_str"] = _fmt_ts(ended_epoch)
    dur = (ended_epoch - started_epoch) if (ended_epoch and started_epoch) else None
    ctx["duration_s"] = int(dur) if dur is not None else None
    ctx["duration_str"] = _fmt_dur(dur)
    ctx["age"] = _rel_age(ended_epoch or dir_mtime)

    prompt_path = d / "prompt.txt"
    ctx["has_prompt"] = prompt_path.is_file()
    ctx["conflicts"] = _find_conflicts(d)

    hit = herospath.resolve_transcript(token)
    if hit is not None:
        _, tpath = hit
        data = herospath.read_session(tpath, cap=RUN_TAIL_CAP)
        ctx["has_transcript"] = True
        ctx["tail_html"] = herospath.render_events_html(data["events"])
        ctx["tail_total"] = data["total"]
        ctx["tail_shown"] = data["shown"]
        ctx["tail_truncated"] = data["total"] - data["shown"]
    else:
        ctx["has_transcript"] = False
        ctx["tail_html"] = ""
        ctx["tail_total"] = ctx["tail_shown"] = ctx["tail_truncated"] = 0
    return ctx


# --------------------------------------------------------------------------- #
# /jobs/{job_id} and /queues/{queue} — read the CACHED snapshot only (same
# data the dashboard renders); never fire a fresh SSH probe in a request path.
# --------------------------------------------------------------------------- #
def _cached_jobs() -> list[dict[str, Any]]:
    snap = read_snapshot()
    jobs = (snap.work.get("jobs") if snap and snap.work else None) or []
    return [j for j in jobs if isinstance(j, dict) and j.get("job")]


def build_job_context(job_id: str) -> dict[str, Any]:
    """Expanded job card: the SAME job dict the dashboard's jobs panel
    renders (phases/%/ETA, queues[], gpus[], metrics, heartbeat freshness) —
    just the whole thing instead of the panel's truncated view."""
    ctx: dict[str, Any] = {"job_id": job_id, "found": False}
    job = next((j for j in _cached_jobs() if j.get("job") == job_id), None)
    if job is None:
        return ctx
    ctx["found"] = True
    ctx["job"] = job
    return ctx


def build_queue_context(queue: str) -> dict[str, Any]:
    """Per-queue detail, sourced from its owning job's queues[] (a queue has
    no identity of its own outside a job — e.g. 'faces' lives on
    gallery-library-scan). Collects every job that currently owns a queue by
    this name (normally exactly one) so a name collision is visible rather
    than silently picking one."""
    ctx: dict[str, Any] = {"queue": queue, "found": False}
    matches: list[dict[str, Any]] = []
    for j in _cached_jobs():
        for q in j.get("queues") or []:
            if isinstance(q, dict) and q.get("name") == queue:
                matches.append({
                    "job_id": j.get("job"), "job_state": j.get("state"),
                    "job_host": j.get("host"), "row": q,
                })
    if not matches:
        return ctx
    ctx["found"] = True
    ctx["matches"] = matches
    ctx["primary"] = matches[0]
    return ctx


# --------------------------------------------------------------------------- #
# /alerts/{alert_id} and /notifications — notify_store (events.db). Table is empty as
# of Phase 0, so "not found"/"no rows yet" is the expected common case, not an
# error — must still 200 with a clean empty-state.
# --------------------------------------------------------------------------- #
def build_alert_context(alert_id: str) -> dict[str, Any]:
    ctx: dict[str, Any] = {"alert_id": alert_id, "found": False}
    try:
        aid = int(alert_id)
    except (TypeError, ValueError):
        return ctx
    notify_store.init_db()
    row = notify_store.get_alert(aid)
    if row is None:
        return ctx
    ctx["found"] = True
    ctx["alert"] = row
    ctx["resolved"] = bool(row.get("resolved_at"))
    return ctx


_CENTRAL = ZoneInfo("America/Chicago")
_NOTIFICATION_GROUPS = {
    "workers": {
        "label": "Worker activity",
        "description": "Completed builds, recons, and worker runs",
        "icon": "workers",
    },
    "models": {
        "label": "Model usage",
        "description": "Quota windows and provider capacity",
        "icon": "usage",
    },
    "operations": {
        "label": "Fleet operations",
        "description": "Health, conformance, jobs, scans, and protective signals",
        "icon": "operations",
    },
    "updates": {
        "label": "Other updates",
        "description": "Additional Nexus events",
        "icon": "notifications",
    },
}


def _notification_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _human_notification_time(value: Any, now: datetime) -> tuple[str, str]:
    parsed = _notification_datetime(value)
    if parsed is None:
        return "Time unavailable", str(value or "")
    local = parsed.astimezone(_CENTRAL)
    local_now = now.astimezone(_CENTRAL)
    seconds = max(0, int((now - parsed).total_seconds()))
    if seconds < 60:
        relative = "Just now"
    elif seconds < 3600:
        minutes = seconds // 60
        relative = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    elif seconds < 6 * 3600:
        hours = seconds // 3600
        relative = f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif local.date() == local_now.date():
        relative = f"Today at {local.strftime('%-I:%M %p')}"
    elif (local_now.date() - local.date()).days == 1:
        relative = f"Yesterday at {local.strftime('%-I:%M %p')}"
    elif seconds < 7 * 86400:
        relative = f"{local.strftime('%A')} at {local.strftime('%-I:%M %p')}"
    else:
        relative = local.strftime("%b %-d at %-I:%M %p")
    exact = local.strftime("%b %-d, %Y at %-I:%M:%S %p %Z")
    return relative, exact


def _humanize_slug(value: str) -> str:
    text = re.sub(r"[_:-]+", " ", value).strip()
    text = re.sub(r"\b20\d{6,12}\b", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ·—-")
    return text[:1].upper() + text[1:] if text else "Worker run"


def _human_sentence(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""
    text = text[:1].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _strip_signal_emoji(value: str) -> str:
    # Writers sometimes prepend a status emoji to the title even though the
    # notification row already has a dedicated signal glyph.
    return re.sub(r"^[^\w]+", "", value, flags=re.UNICODE).strip()


def _notification_category(row: dict[str, Any]) -> str:
    key = str(row.get("event_key") or "").lower()
    title = str(row.get("title") or "").lower()
    unread = row.get("read_at") is None
    is_recovery = "recovery" in key or "recovered" in title or "again" in title
    is_problem = any(word in f"{key} {title}" for word in (
        "alarm", "drift", "failed", "failure", "unavailable", "service_down",
        "stalled", "critical",
    ))
    if unread and not is_recovery and (int(row.get("prio") or 0) >= 4 or is_problem):
        return "operations"
    if key.startswith("run:"):
        return "workers"
    if key.startswith("model-usage-event:") or "quota" in title:
        return "models"
    if key.startswith(("conformance-", "alert:")) or any(
        word in f"{key} {title}" for word in ("health", "watchdog", "thermal", "deadman")
    ):
        return "operations"
    if key.startswith(("job:", "queue:", "scan:", "milestone:")) or " scan " in f" {title} ":
        return "operations"
    return "updates"


def _humanize_notification(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    item = dict(row)
    key = str(item.get("event_key") or "")
    key_lower = key.lower()
    raw_title = _strip_signal_emoji(str(item.get("title") or "Notification"))
    body = str(item.get("body") or "").strip()
    title = raw_title[:1].upper() + raw_title[1:] if raw_title else "Notification"
    source = "Nexus"
    category_label = "Update"
    tone = "neutral"

    run_match = re.match(
        r"^(?P<seat>[a-z0-9_-]+)\s+(?P<kind>build|recon|worker)\s+"
        r"(?P<state>done|success|failed|died)\s*[—-]\s*(?P<slug>.+)$",
        raw_title,
        flags=re.IGNORECASE,
    )
    if key_lower.startswith("run:"):
        source = "Worker relay"
        category_label = "Worker run"
        if run_match:
            seat = run_match.group("seat").capitalize()
            kind = run_match.group("kind").lower()
            state = run_match.group("state").lower()
            title = f"{seat} {'completed' if state in ('done', 'success') else 'failed'} a {kind}"
            body = _humanize_slug(run_match.group("slug"))
            tone = "good" if state in ("done", "success") else "danger"
        elif ":success" in key_lower:
            tone = "good"
    elif key_lower.startswith("model-usage-event:") or "quota" in raw_title.lower():
        source = "Model usage"
        category_label = "Quota window"
        tone = "info"
        title = re.sub(r"five-hour\s+", "", title, flags=re.IGNORECASE)
        if body.lower().startswith("new window ends "):
            body = "Next five-hour window ends " + body[len("New window ends "):]
    elif key_lower.startswith("conformance-"):
        source = "Fleet conformance"
        category_label = "Conformance"
        if "recovery" in key_lower or "fresh again" in raw_title.lower():
            title = "Conformance recovered"
            body = body or "The affected fleet check is passing again."
            tone = "good"
        else:
            title = "Conformance warning"
            subject = re.split(r"\s+[—-]\s+", raw_title, maxsplit=1)
            body = (_humanize_slug(subject[-1]) + ". " if len(subject) > 1 else "") + (
                "A declared fleet check no longer matches its expected state."
            )
            tone = "danger"
    elif key_lower.startswith("alert:"):
        source = "Health monitor"
        category_label = "Health"
        tone = "good" if any(word in key_lower for word in ("recovery", "resolved")) else "danger"
        if body.lower() == "tailnet ping ok":
            body = "Tailnet connectivity is responding normally."
        title = re.sub(r"\breachable again\b", "is reachable again", title, flags=re.IGNORECASE)
    elif key_lower.startswith(("job:", "queue:", "scan:", "milestone:")):
        source = "Job monitor"
        category_label = "Job or scan"
        tone = "good" if any(word in key_lower for word in ("success", "complete", "done")) else "neutral"

    body = _human_sentence(body)

    created_label, created_exact = _human_notification_time(item.get("created_at"), now)
    read_label, read_exact = _human_notification_time(item.get("read_at"), now)
    item.update({
        "unread": item.get("read_at") is None,
        "friendly_title": title,
        "friendly_body": body,
        "source_label": source,
        "category_label": category_label,
        "tone": tone,
        "created_label": created_label,
        "created_exact": created_exact,
        "read_label": read_label,
        "read_exact": read_exact,
    })
    return item


def build_feed_context(limit: int = 100, now: datetime | None = None) -> dict[str, Any]:
    notify_store.init_db()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    rows = [_humanize_notification(row, current) for row in notify_store.list_notifications(limit=limit)]
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in _NOTIFICATION_GROUPS}
    for row in rows:
        buckets[_notification_category(row)].append(row)
    groups = []
    for key, metadata in _NOTIFICATION_GROUPS.items():
        items = buckets[key]
        groups.append({
            "key": key,
            **metadata,
            "items": items,
            "count": len(items),
            "unread_count": sum(1 for item in items if item["unread"]),
        })
    return {"rows": rows, "groups": groups, "count": len(rows)}


# --------------------------------------------------------------------------- #
# /approve/{token} — SHELL only (Phase 1 scope): staged-build summary +
# INERT Approve/Deny placeholders. POST wiring is Phase 3/5.
# --------------------------------------------------------------------------- #
def build_approve_context(token: str) -> dict[str, Any]:
    ctx: dict[str, Any] = {"token": token}
    seat, d = _resolve_run_dir(token)
    ctx["run_found"] = d is not None
    ctx["seat"] = seat
    lane = None
    prompt_text = None
    started_epoch = None
    if d is not None:
        started_epoch, lane = seatboard._started_epoch(d / "status.json")
        prompt_path = d / "prompt.txt"
        if prompt_path.is_file():
            try:
                prompt_text = prompt_path.read_text(errors="replace")[:20000]
            except Exception:
                prompt_text = None
    ctx["lane"] = lane
    ctx["started_epoch"] = started_epoch
    ctx["started_str"] = _fmt_ts(started_epoch)
    ctx["prompt_text"] = prompt_text
    ctx["kind"] = seatboard._kind_of(token, lane)
    ctx["slug"] = seatboard._short_token(token)
    dm = re.search(r"-(\d{4})(\d{2})(\d{2})-", token)
    ctx["date"] = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else None
    return ctx
