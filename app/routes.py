"""
HTTP surface.

JSON API is the machine contract (and what the dashboard's auto-refresh polls);
the dashboard route is the human surface. The manual-run endpoint is the first
hint of the OS direction: the dashboard can *act*, not only display.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import conformance, control_plane, detail_context, health_timeline, herospath, notify_store, push
from .activity import filter_cache, read_cache
from .semantic_index_watch import read_status as read_semantic_index_status
from .config import (
    settings,
    JOBS_PANEL_MAX,
    WORKER_ACTIVITY_PANEL_MAX,
    JOB_NONJOB_KINDS,
    JOB_KIND_VALUES,
)
from .events import hub, insert_event, newest_ts, read_events
from .heartbeat_runner import heartbeat_runner
from .model_usage_history import history_payload
from .model_usage_refresh import model_usage_refresh_runner
from .notify import notify as route_notify
from .scheduler import jobs_summary
from .seatboard import read_seat_board
from .shell_context import (
    FRAME_ACCENT, SHELL_ASSET_VERSION, app_chrome_context, central_header_stamp,
)
from .store import MAX_HISTORY_ROWS, read_history, read_snapshot
from .system_status import get_system_status
from .watchdogs_projection import get_projected_registry, projected_summary as watchdogs_summary

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

def _jsnum(value: Any) -> Any:
    """Mirror JS String(Number): a whole-number float drops its trailing '.0'."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


templates.env.filters["jsnum"] = _jsnum


def _central_header_stamp(value: Any) -> str:
    """Format a UTC-backed timestamp for the human dashboard in Central time."""
    return central_header_stamp(value)


templates.env.filters["central_header_stamp"] = _central_header_stamp
templates.env.globals["shell_asset_v"] = SHELL_ASSET_VERSION


def _compute_dashboard_asset_v() -> str:
    """Content-derived cache-busting token for the extracted dashboard CSS/JS,
    read once at startup — a stale copy here would mean a stale ?v= query on
    every page load until the next restart, so fail closed instead."""
    static_dir = Path(__file__).resolve().parent.parent / "static"
    try:
        css_bytes = (static_dir / "dashboard.css").read_bytes()
        js_bytes = (static_dir / "dashboard.js").read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"dashboard_asset_v: required static asset missing/unreadable: {exc}"
        ) from exc
    digest = hashlib.sha256()
    digest.update(b"dashboard.css\0")
    digest.update(css_bytes)
    digest.update(b"\0dashboard.js\0")
    digest.update(js_bytes)
    return digest.hexdigest()[:16]


templates.env.globals["dashboard_asset_v"] = _compute_dashboard_asset_v()


def _compute_nexus_css_v() -> str:
    """Content-derived cache-busting token for the shared nexus.css stylesheet,
    read once at startup — a stale copy here would mean a stale ?v= query on
    every page load until the next restart, so fail closed instead."""
    static_dir = Path(__file__).resolve().parent.parent / "static"
    try:
        css_bytes = (static_dir / "nexus.css").read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"nexus_css_v: required static asset missing/unreadable: {exc}"
        ) from exc
    digest = hashlib.sha256()
    digest.update(b"nexus.css\0")
    digest.update(css_bytes)
    return digest.hexdigest()[:16]


templates.env.globals["nexus_css_v"] = _compute_nexus_css_v()


def _compute_herospath_css_v() -> str:
    """Content-derived cache-busting token for Worker Activity transcript CSS."""
    static_dir = Path(__file__).resolve().parent.parent / "static"
    try:
        css_bytes = (static_dir / "herospath.css").read_bytes()
    except OSError as exc:
        raise RuntimeError(
            f"herospath_css_v: required static asset missing/unreadable: {exc}"
        ) from exc
    digest = hashlib.sha256()
    digest.update(b"herospath.css\0")
    digest.update(css_bytes)
    return digest.hexdigest()[:16]


templates.env.globals["herospath_css_v"] = _compute_herospath_css_v()


def _compute_activity_asset_v() -> str:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    digest = hashlib.sha256()
    for name in ("activity.css", "activity.js", "jobs.css"):
        digest.update(name.encode() + b"\0")
        digest.update((static_dir / name).read_bytes())
    return digest.hexdigest()[:16]


templates.env.globals["activity_asset_v"] = _compute_activity_asset_v()


def _compute_health_asset_v() -> str:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    css_bytes = (static_dir / "health.css").read_bytes()
    return hashlib.sha256(b"health.css\0" + css_bytes).hexdigest()[:16]


templates.env.globals["health_asset_v"] = _compute_health_asset_v()


def _compute_watchdogs_asset_v() -> str:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    css_bytes = (static_dir / "watchdogs.css").read_bytes()
    return hashlib.sha256(b"watchdogs.css\0" + css_bytes).hexdigest()[:16]


templates.env.globals["watchdogs_asset_v"] = _compute_watchdogs_asset_v()


def _compute_notifications_asset_v() -> str:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    css_bytes = (static_dir / "notifications.css").read_bytes()
    return hashlib.sha256(b"notifications.css\0" + css_bytes).hexdigest()[:16]


templates.env.globals["notifications_asset_v"] = _compute_notifications_asset_v()




def _compute_model_usage_asset_v() -> str:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    digest = hashlib.sha256()
    for name in ("model_usage.css", "model_usage.js"):
        digest.update(name.encode() + b"\0")
        digest.update((static_dir / name).read_bytes())
    return digest.hexdigest()[:16]


templates.env.globals["model_usage_asset_v"] = _compute_model_usage_asset_v()


def _compute_system_status_asset_v() -> str:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    digest = hashlib.sha256()
    for name in ("system_status.css", "system_status.js"):
        digest.update(name.encode() + b"\0")
        digest.update((static_dir / name).read_bytes())
    return digest.hexdigest()[:16]


templates.env.globals["system_status_asset_v"] = _compute_system_status_asset_v()

# --- PWA assets: served from the app behind the Cloudflare Access edge --------
# The service worker
# MUST live at the ROOT path so its default scope is "/" and it can intercept
# navigations; a /static/-mounted SW would only control /static/. The manifest
# gets its correct MIME type here (Python's mimetypes doesn't know .webmanifest).
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/manifest.webmanifest", include_in_schema=False)
async def web_manifest() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    # no-cache so a new SW is fetched promptly (a stale SW can't wedge the app,
    # but this keeps the update flow snappy); Service-Worker-Allowed broadens scope.
    return FileResponse(
        _STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


def _is_private_source(request: Request) -> bool:
    """True if the ingest peer is loopback/LAN/tailnet (not a public host).

    Belt-and-suspenders on top of the 0.0.0.0 bind: the port is only reachable on
    loopback, the LAN, and the tailnet anyway, but we reject a public source IP so
    a misrouted tunnel ingress can't turn /events into an open write endpoint.
    """
    client = request.client
    if client is None:
        return True  # no peer info (e.g. test transport) — don't hard-block ingest
    try:
        ip = ipaddress.ip_address(client.host)
    except ValueError:
        return True
    # 100.64/10 = Tailscale CGNAT range; is_private covers 10/8, 172.16/12,
    # 192.168/16, 127/8, and the IPv6 private/loopback space.
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")

# Job-card accents, keyed by job state. Templates map state to the shared
# static/nexus.css accent classes so /jobs and the live panel stay identical.
STATE_ACCENT = {
    "running": "developed", "stalled": "safelight", "failed": "overexposed",
    "done": "developed", "ended": "unexposed", "unknown": "unexposed",
}


async def _overlay_fresh_seats(snap) -> None:
    """Recompute the seat strip from cheap LOCAL reads and overlay it onto the
    cached fleet-probe snapshot IN PLACE, so a seat's state/kind/inline-progress/
    ETA is fresh on THIS request instead of waiting for the ~5-min fleet sweep.

    read_seat_board reads only local files (``from-{seat}/runs/<token>/`` metadata
    + ``heartbeats/inline/<seat>.json``) — NO SSH, no fleet probes on this path.
    The expensive SSH probes stay on the sweep and are left untouched in
    ``snap.nodes``. Run off-thread (mirrors the sweep) so a handful of small file
    reads can't stall the event loop. Fully isolated: read_seat_board never raises,
    and this last-resort guard means a broken read degrades to the sweep-baked
    ``snap.seats`` (last-good) — it never 500s the request or blanks the strip."""
    if snap is None:
        return
    try:
        snap.seats = await asyncio.to_thread(read_seat_board)
    except Exception:  # noqa: BLE001 — keep the sweep-baked seats, never break the page
        pass


@router.get("/api/status")
async def api_status() -> JSONResponse:
    snap = read_snapshot()
    if snap is None:
        return JSONResponse({"overall": "unknown", "nodes": []}, status_code=503)
    # Freshen ONLY the seat strip from local reads; snap.nodes stays the cached
    # sweep value (heavy SSH probes are not re-run here).
    await _overlay_fresh_seats(snap)
    return JSONResponse(snap.model_dump(mode="json"))


@router.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness probe for the Nexus's own internal checks
    (PANEL · LOOPBACK, SCHEDULER). Deliberately minimal: no IPs,
    fleet telemetry, or secrets — just proof the process is up and the
    heartbeat scheduler is still ticking."""
    snap = read_snapshot()
    now = time.time()
    if snap is None:
        scheduler_age_s = -1
        scheduler_fresh = False
    else:
        scheduler_age_s = int(now - snap.generated_at.timestamp())
        scheduler_fresh = scheduler_age_s < (settings.heartbeat_interval_seconds * 2)
    return JSONResponse({
        "ok": True,
        "ts": int(now),
        "scheduler_fresh": scheduler_fresh,
        "scheduler_age_s": scheduler_age_s,
    })


@router.get("/api/system-status")
async def api_system_status() -> JSONResponse:
    """One cache-only contract for every status-producing Nexus module."""
    return JSONResponse(await asyncio.to_thread(get_system_status))


@router.get("/system-status", response_class=HTMLResponse)
async def system_status_page(request: Request) -> HTMLResponse:
    # Force one fresh local/cache read for the drill-down.  This still launches
    # no probes; it only bypasses the registry's 15-second navigation cache.
    lens = request.query_params.get("tab", "overview").strip().lower()
    if lens not in {"overview", "systems", "governance", "services"}:
        return RedirectResponse("/system-status?tab=overview", status_code=302)
    status = await asyncio.to_thread(get_system_status, force=True)
    chrome = await app_chrome_context()
    chrome["chrome_system_status"] = status
    return templates.TemplateResponse(
        request,
        "system_status.html",
        {**chrome, "system_status": status, "status_lens": lens},
    )


@router.get("/api/activity")
async def api_activity(range: str = "all") -> JSONResponse:
    cache, error = await asyncio.to_thread(read_cache)
    if error or cache is None:
        return JSONResponse({"ok": False, "error": error, "stale": True}, status_code=503)
    try:
        payload = await asyncio.to_thread(filter_cache, cache, range)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    payload["ok"] = True
    generated = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
    payload["stale"] = (datetime.now(timezone.utc) - generated).total_seconds() > 1800
    return JSONResponse(payload)


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request) -> HTMLResponse:
    runs, jobs = await asyncio.gather(
        asyncio.to_thread(herospath.list_runs),
        asyncio.to_thread(_jobs_activity_rows),
    )
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            **(await app_chrome_context()),
            "runs": runs,
            "jobs": jobs,
            "state_accent": STATE_ACCENT,
            "initial_tab": request.query_params.get("tab", ""),
        },
    )


@router.get("/activity/workers")
async def worker_activity_index() -> RedirectResponse:
    """Canonical worker-run list lives as the Workers view of Activity."""
    return RedirectResponse("/activity?tab=workers", status_code=302)


@router.get("/activity/jobs")
async def jobs_activity_index() -> RedirectResponse:
    """Canonical job list lives as the Jobs view of Activity."""
    return RedirectResponse("/activity?tab=jobs", status_code=302)


@router.get("/api/conformance")
async def api_conformance() -> JSONResponse:
    data, error = await asyncio.to_thread(conformance.read_cache)
    if error or data is None:
        return JSONResponse({"ok": False, "error": error, "stale": True}, status_code=503)
    generated = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
    payload = dict(data)
    payload["ok"] = True
    payload["stale"] = (datetime.now(timezone.utc) - generated).total_seconds() > 2100
    return JSONResponse(payload)


async def _render_conformance(request: Request) -> HTMLResponse:
    data, error = await asyncio.to_thread(conformance.read_cache)
    history = await asyncio.to_thread(conformance.read_history)
    projection = conformance.project_conformance(data, error=error, history=history)
    return templates.TemplateResponse(
        request, "conformance.html",
        {**(await app_chrome_context()), "data": data, "error": error, "projection": projection},
    )


@router.get("/conformance")
async def conformance_page() -> RedirectResponse:
    """Compatibility route; Conformance is an Operations lens."""
    return RedirectResponse("/operations?tab=conformance", status_code=302)


@router.get("/api/control-plane")
async def api_control_plane() -> JSONResponse:
    data, error = await asyncio.to_thread(control_plane.read_cache)
    if error or data is None:
        return JSONResponse({"ok": False, "error": error, "stale": True}, status_code=503)
    projection = control_plane.project(data)
    return JSONResponse({**data, "ok": True, "stale": projection["is_stale"]})


async def _render_indexes(request: Request) -> HTMLResponse:
    data, error = await asyncio.to_thread(control_plane.read_cache)
    projection = control_plane.project(data, error)
    return templates.TemplateResponse(
        request, "control_plane.html",
        {**(await app_chrome_context()), "projection": projection},
    )


@router.get("/indexes")
async def indexes_page() -> RedirectResponse:
    """Compatibility route; Indexes is an Operations lens."""
    return RedirectResponse("/operations?tab=indexes", status_code=302)


@router.get("/control-plane", response_class=HTMLResponse)
async def legacy_control_plane_page() -> RedirectResponse:
    """Preserve old bookmarks while the user-facing destination is Indexes."""
    return RedirectResponse("/operations?tab=indexes", status_code=302)


@router.get("/api/model-usage/history")
async def api_model_usage_history(
    range: str = "30d", provider: str = "all"
) -> JSONResponse:
    try:
        payload = await asyncio.to_thread(
            history_payload,
            range,
            provider,
            settings.model_usage_history_db,
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    except Exception as exc:  # keep internal paths/details out of the response
        return JSONResponse(
            {"ok": False, "error": type(exc).__name__}, status_code=503
        )
    return JSONResponse(payload)


@router.get("/model-usage")
async def legacy_model_usage_page() -> RedirectResponse:
    """Model Usage now lives as the Models view of Activity."""
    return RedirectResponse("/activity?tab=models", status_code=302)


@router.get("/api/history")
async def api_history(limit: int = 288) -> JSONResponse:
    limit = max(1, min(int(limit), 2016))  # one week at 5-minute cadence
    return JSONResponse(read_history(limit=limit))


@router.get("/api/health-timeline")
async def api_health_timeline(hours: int = 24) -> JSONResponse:
    """Derived 24h (bounded to a week) fleet/per-node health projection —
    additive to /api/history, never a replacement. Cache/file-only: reads
    the same history.jsonl /api/history already serves and does no probing
    of its own, so this is safe to poll on any cadence."""
    hours = max(1, min(int(hours), 168))
    cadence = settings.heartbeat_interval_seconds
    limit = max(1, min(MAX_HISTORY_ROWS, round(hours * 3600 / cadence) + 24))
    rows = read_history(limit=limit)
    projection = health_timeline.project_health_timeline(
        rows, hours=hours, cadence_seconds=cadence
    )
    return JSONResponse(projection)


async def _render_health(request: Request) -> HTMLResponse:
    """Read-only fleet health from the existing cached sweep and history.

    This route does not launch probes. The heartbeat runner remains the sole
    collector; Health is only a more legible projection of its current and
    retained evidence.
    """
    snap = read_snapshot()
    cadence = settings.heartbeat_interval_seconds
    limit = max(1, min(MAX_HISTORY_ROWS, round(24 * 3600 / cadence) + 24))
    projection = health_timeline.project_health_timeline(
        read_history(limit=limit), hours=24, cadence_seconds=cadence
    )
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            **(await app_chrome_context()),
            "snap": snap,
            "projection": projection,
            "accent": FRAME_ACCENT,
        },
    )


@router.get("/health")
async def health_page() -> RedirectResponse:
    """Compatibility route; Health is the default Operations lens."""
    return RedirectResponse("/operations?tab=health", status_code=302)


@router.get("/api/scheduler")
async def api_scheduler() -> JSONResponse:
    """APScheduler's registered-task registry — the internal timers wired in
    register_jobs() (heartbeat, retention, watchers, self-test, ...). Distinct
    from POST /api/jobs/{job}/done|undone, which mutate heartbeat-derived
    detached-process job cards; the two share no data source."""
    return JSONResponse(jobs_summary())


@router.post("/api/run/heartbeat")
async def api_run_heartbeat() -> JSONResponse:
    """Fire a fleet sweep and a rate-limited model-usage refresh together."""
    result, usage_result = await asyncio.gather(
        heartbeat_runner.run(if_idle=True),
        model_usage_refresh_runner.run(),
    )
    snap = result.snap or read_snapshot()
    if snap is None:
        return JSONResponse(
            {"overall": "unknown", "nodes": [],
             "heartbeat": {"ran": False, "already_running": result.already_running}},
            status_code=503,
        )
    payload = snap.model_dump(mode="json")
    payload["heartbeat"] = {
        "ran": result.ran,
        "already_running": result.already_running,
        "started_at": result.started_at,
    }
    payload["model_usage"] = usage_result.as_dict()
    return JSONResponse(payload, status_code=202 if result.already_running else 200)


def _current_job_cards() -> dict[str, dict]:
    """The live job cards keyed by id, read from the CACHED snapshot (work.jobs)
    — the same data the dashboard renders. Deliberately NOT work.read_jobs(): we
    must not fire a fresh SSH probe in the request path. Empty if no snapshot."""
    snap = read_snapshot()
    jobs = (snap.work.get("jobs") if snap and snap.work else None) or []
    return {j["job"]: j for j in jobs if isinstance(j, dict) and j.get("job")}


@router.post("/api/jobs/{job}/done")
async def api_job_done(job: str) -> JSONResponse:
    """Manually mark a logical job `done` — close its open attempt AND mute the
    inferred adapter so it stops re-opening one every sweep the live PID is seen.
    Gated by the same read-gate as the rest of the app (off-box needs a valid
    Access JWT; on-box loopback exempt). Only a currently-known job id is
    accepted (404 otherwise). The mute auto-clears on a genuine relaunch
    (new pid / uptime reset); POST
    .../undone is the explicit un-mute hatch. Returns the stored mute row."""
    import time
    from . import job_history
    job_history.init_db()
    cards = _current_job_cards()
    if job not in cards:
        return JSONResponse(
            {"ok": False, "error": "unknown job", "known": sorted(cards)},
            status_code=404,
        )
    card = cards[job]
    pid = card.get("pid")
    et = card.get("uptime_s")
    now = time.time()
    started_epoch = (now - et) if et else None
    mute = job_history.mark_done(job, now, pid=pid, started_epoch=started_epoch)
    return JSONResponse({"ok": True, "job": job, "mute": mute})


@router.post("/api/jobs/{job}/undone")
async def api_job_undone(job: str) -> JSONResponse:
    """Un-mute a job (safety hatch for a misclicked "mark done"). The next sweep
    re-opens the attempt naturally if the PID is still alive. No known-job guard:
    clearing a stale/absent mute is a harmless no-op."""
    from . import job_history
    job_history.init_db()
    job_history.clear_mute(job)
    return JSONResponse({"ok": True, "job": job})


# --- live seat-activity collector (absorbed from delta's sessions-viewer) ---

@router.post("/events")
async def post_events(request: Request) -> JSONResponse:
    """Ingest one hook event. UNauthenticated (hooks carry no Access JWT) — the
    route additionally rejects non-private sources.
    Tolerant by design: never 500 on a bad body / transient db issue, so a hook
    POST can stay fire-and-forget and never slow a seat run."""
    if not _is_private_source(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    try:
        row = insert_event(body)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=200)
    await hub.broadcast(row)
    return JSONResponse({"ok": True, "id": row["id"]})


@router.get("/api/events")
async def api_events(since: int = 0, limit: int = 200) -> JSONResponse:
    """Backfill for the live timeline: rows id>since (or last N), oldest->newest."""
    return JSONResponse(read_events(since=since, limit=limit))


# --- Nexus notifications: Phase 0 stub (accept + log only, no send) ---------
# Bearer-gated (unlike /events): this is a deliberate write surface any seat/
# job/probe can call, not a hook fire-and-forget, so it fails closed on a bad
# or missing token rather than trusting source IP alone.

@router.post("/api/notify")
async def api_notify(request: Request) -> JSONResponse:
    """Routes through the Phase 3 auto-router (app/notify.py): normalize ->
    classify -> dedup -> render -> fan out. Any seat/job/probe that can curl
    lands here; the relay-outcome watcher (app/run_watcher.py) is the other,
    in-process caller of the same notify()."""
    expected = settings.notify_bearer_token
    if not expected:
        return JSONResponse({"ok": False, "error": "notify not configured"}, status_code=503)
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {expected}":
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    result = await route_notify(body)
    return JSONResponse({"ok": True, **result})


# --- Nexus notifications: Phase 2 push plumbing ------------------------------
# Browser-driven and protected at the Cloudflare Access edge — these are called
# from JS running inside an authenticated browser session, not by hooks/probes.

@router.get("/api/push/vapid-public-key")
async def api_push_vapid_key() -> JSONResponse:
    key = settings.vapid_public_key_b64url
    if not key:
        return JSONResponse({"ok": False, "error": "vapid key unavailable"}, status_code=503)
    return JSONResponse({"ok": True, "key": key})


@router.post("/api/push/subscribe")
async def api_push_subscribe(request: Request) -> JSONResponse:
    """Upsert a push_subscription on endpoint. Re-subscribes (the same
    endpoint POSTed again, e.g. by auto-resubscribe-on-open) are expected and
    idempotent — see notify_store.upsert_subscription."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    sub = body.get("subscription") if isinstance(body, dict) else None
    if not isinstance(sub, dict):
        return JSONResponse({"ok": False, "error": "missing subscription"}, status_code=400)
    endpoint = sub.get("endpoint")
    keys = sub.get("keys") or {}
    p256dh, auth_key = keys.get("p256dh"), keys.get("auth")
    if not endpoint or not p256dh or not auth_key:
        return JSONResponse({"ok": False, "error": "incomplete subscription"}, status_code=400)
    device_label = str(body.get("device_label") or "unknown")
    ua = request.headers.get("user-agent")
    row = await asyncio.to_thread(
        notify_store.upsert_subscription, endpoint, p256dh, auth_key, device_label, ua
    )
    return JSONResponse({"ok": True, "id": row.get("id")})


@router.post("/api/push/unsubscribe")
async def api_push_unsubscribe(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    if not endpoint:
        return JSONResponse({"ok": False, "error": "missing endpoint"}, status_code=400)
    await asyncio.to_thread(notify_store.deactivate_subscription, endpoint)
    return JSONResponse({"ok": True})


@router.post("/api/push/test")
async def api_push_test(request: Request) -> JSONResponse:
    """Send a test push to one device (body {endpoint}) or all active devices
    (no body / empty body) — used by Notification Preferences' enable flow."""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    endpoint = body.get("endpoint")
    note = {
        "event_key": f"test:{endpoint or 'all'}:{int(time.time())}",
        "channel": "nexus-post",
        "prio": 3,
        "title": "Nexus test push",
        "body": "If you can read this, push delivery works.",
        "navigate": "/notifications?nf=1",
        "tag": "nexus-test",
        "emoji": "🔔",
    }
    result = await push.send_push(note, endpoint=endpoint)
    if result["targeted"] == 0:
        return JSONResponse({"ok": False, "error": "no active subscriptions", **result}, status_code=200)
    return JSONResponse({"ok": True, **result})


@router.post("/api/notify/mark-all-read")
async def api_notify_mark_all_read() -> JSONResponse:
    """Same-session browser action (the feed's "mark all as read" control) —
    edge-gated only, like /api/push/*, not the bearer-gated /api/notify."""
    n = await asyncio.to_thread(notify_store.mark_all_read)
    return JSONResponse({"ok": True, "marked": n})


@router.get("/api/notify/unread-count")
async def api_notify_unread_count() -> JSONResponse:
    n = await asyncio.to_thread(notify_store.count_unread)
    return JSONResponse({"ok": True, "unread": n})


@router.post("/api/push/sub-health")
async def api_push_sub_health(request: Request) -> JSONResponse:
    """Phase 6a nag (design: visible fallback when silent auto-resubscribe-on-
    open can't fix it — permission revoked, or the server-side row was hard-
    deactivated). Body {endpoint}: the browser's OWN pushManager.getSubscription()
    endpoint, if it has one. `active: false`/missing here means the standalone
    client should show the re-enable nag; the existing subscribe flow (not this
    route) is what actually fixes it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    endpoint = body.get("endpoint") if isinstance(body, dict) else None
    if not endpoint:
        return JSONResponse({"ok": True, "active": False})
    row = await asyncio.to_thread(notify_store.get_subscription_by_endpoint, endpoint)
    if row is None:
        return JSONResponse({"ok": True, "active": False})
    return JSONResponse({
        "ok": True,
        "active": bool(row.get("active")),
        "consecutive_failures": row.get("consecutive_failures") or 0,
    })


# --- Nexus notifications: Phase 1 deep-linkable detail routes ---------------
# One shared context-builder per route (app/detail_context.py) drives BOTH the
# first-paint full page and the ?partial=1 fragment from the exact same dict —
# they cannot drift because they're the same render call. The touch-sheet
# presentation (pushState/drawer/gestures) is a deliberate follow-on; these
# routes just need to exist and be embeddable (panel-notifications-design.md
# §B, panel-touch-redesign.md §H.4/T3). All context builders are isolated
# (never raise) and read only cached/local state — no fresh SSH probes here.
#
# Touch T3.5: a cold/direct hit on one of these six routes (no ?partial, no
# ?standalone — e.g. a notification deep-link tapped with the PWA closed) no
# longer gets its own standalone-page shell. It gets the DASHBOARD, with the
# route's detail pre-rendered into the T3 sheet body and the sheet already
# marked .open — so a cold open and an in-app sheet-tap land on the same
# visual state. Three-way branch per route: ?partial=1 -> bare fragment (used
# by the in-app sheet's own fetch + polling) · ?standalone=1 -> the old
# standalone detail page (kept on disk as an explicit fallback) · neither ->
# cold dashboard-with-sheet-open. found=False degrades exactly the same way
# in all three branches, since all three render from the same context dict.

async def _dashboard_ctx() -> dict[str, Any]:
    """The exact context GET / builds, factored out so the cold-sheet path can
    reuse it verbatim instead of forking a second dashboard render."""
    snap = read_snapshot()
    # Server first-paint gets the same per-request-fresh seat strip as /api/status
    # (local reads only; fleet nodes remain the cached sweep value).
    await _overlay_fresh_seats(snap)
    now = time.time()
    _jobs_raw = (snap.work.get("jobs") if snap and snap.work else None) or []
    _jobs_raw = [j for j in _jobs_raw if not _is_watch_guard(j)]
    jobs_ordered = sorted(_jobs_raw, key=lambda j: _job_sort_key(j, now))
    return {
        "snap": snap,
        "jobs_ordered": jobs_ordered,
        "jobs": jobs_summary(),
        "accent": FRAME_ACCENT,
        # Render-only cap for the top live jobs panel. History recording sees
        # all jobs; only this panel's loop is sliced.
        "JOBS_PANEL_MAX": JOBS_PANEL_MAX,
        "WORKER_ACTIVITY_PANEL_MAX": WORKER_ACTIVITY_PANEL_MAX,
        # seat-tile inline-progress bar ages out at this age (client freshness
        # gate, mirrors seatboard's server-side gate).
        "job_stale_seconds": settings.job_stale_seconds,
        # Newest event ts for the scan-log's first-paint recency read; the
        # client refreshes this from the live list on its own cadence.
        "newest_event_ts": newest_ts(),
        # Header-bell first-paint count; refresh() keeps it live via
        # /api/notify/unread-count on the same cadence (must never drift from
        # this value's source, notify_store.count_unread()).
        "unread": await asyncio.to_thread(notify_store.count_unread),
        # Nexus-wide status includes fleet health plus every cache-backed
        # analytics/governance/delivery module. Briefly cached in-process so
        # ordinary dashboard navigation never reparses every source.
        "system_status": await asyncio.to_thread(get_system_status),
        # Semantic index module: local index/vault.db read only — the charlie
        # fleet-maint receipt is a separately-scheduled cached SSH probe,
        # never a fresh network call on this path.
        # See app/semantic_index_watch.py.
        "semantic_index": await asyncio.to_thread(read_semantic_index_status),
        # Fleet conformance is collector-owned cached evidence. This request
        # path never launches SSH or systemd probes.
        "conformance": conformance.project_conformance(
            *(await asyncio.to_thread(conformance.read_cache)),
            history=await asyncio.to_thread(conformance.read_history),
        ),
        # Five central indexes, parsed and validated only by the scheduled
        # collector. The request path reads the resulting local cache.
        "control_plane": control_plane.project(
            *(await asyncio.to_thread(control_plane.read_cache))
        ),
        # Static, point-in-time protective-mechanism inventory. The dashboard
        # module shows only bounded per-host counts and links to the full
        # accordion; it never mounts the 29 detail rows or launches probes.
        "watchdogs": watchdogs_summary(),
    }


async def _cold_sheet_response(
    request: Request, template_name: str, detail_ctx: dict[str, Any], route: str,
) -> HTMLResponse:
    """Render the dashboard (same context GET / uses) with `route`'s detail
    fragment pre-injected into the T3 sheet body, sheet+backdrop pre-marked
    .open, and body.cold-sheet set — a single server render, no client fetch."""
    sheet_html = templates.get_template(template_name).render({**detail_ctx, "partial": True})
    ctx = await _dashboard_ctx()
    ctx["cold_sheet"] = True
    ctx["cold_sheet_html"] = sheet_html
    ctx["cold_sheet_route"] = route
    return templates.TemplateResponse(request, "dashboard.html", ctx)


async def _confirm_if_nf(request: Request) -> None:
    """?nf=1 handling (design §B.2/§I.5): a notification deep-link tap lands
    here. Best-effort; never raises (see notify_store.confirm_navigate)."""
    if request.query_params.get("nf") == "1":
        await asyncio.to_thread(notify_store.confirm_navigate, request.url.path)


@router.get("/run/{token}", response_class=HTMLResponse)
async def run_detail(
    request: Request, token: str, partial: int = 0, standalone: int = 0,
) -> HTMLResponse:
    await _confirm_if_nf(request)
    ctx = await asyncio.to_thread(detail_context.build_run_context, token)
    if partial or standalone:
        ctx["partial"] = bool(partial)
        return templates.TemplateResponse(request, "detail_run.html", ctx)
    return await _cold_sheet_response(request, "detail_run.html", ctx, request.url.path)


# --- Jobs: dedicated full-list page (clone of hero-path's index) ------------
# Same panel as the dashboard's "jobs" section, uncapped (no JOBS_PANEL_MAX slice)
# and with running/stalled jobs ranked above finished ones.
_ACTIVE_JOB_STATES = ("running", "stalled")


def _parse_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None
    return None


def _job_sort_key(job: dict[str, Any], now: float) -> tuple[int, float]:
    """(0=active/1=finished, -recency) — active states first, then newest→oldest.
    A FINISHED job's recency prefers `ended_at` (real completion time) so a
    job that's been sitting done for days can't outrank one that just
    finished; falls back to `started`, then `beat_age_s` (last-heartbeat age)
    when both are absent — jobs with none of the three share one key so
    sorted()'s stability preserves their existing relative order instead of
    reshuffling them. Active jobs never use `ended_at` (they don't have one)."""
    active = 0 if job.get("state") in _ACTIVE_JOB_STATES else 1
    epoch = _parse_epoch(job.get("ended_at")) if active == 1 else None
    if epoch is None:
        epoch = _parse_epoch(job.get("started"))
    if epoch is None:
        beat_age_s = job.get("beat_age_s")
        if isinstance(beat_age_s, (int, float)) and not isinstance(beat_age_s, bool):
            epoch = now - beat_age_s
    return (active, -epoch if epoch is not None else 0.0)


def _is_watch_guard(job: dict[str, Any]) -> bool:
    """True for a heartbeat record that's a watch/guard sidecar, not a job —
    honors an explicit kind/type field (config.py JOB_NONJOB_KINDS) if a
    producer sets one. Applied ONLY when building Jobs UI contexts; the
    underlying snapshot/reader is untouched, so thermal_watch.py etc. keep
    seeing these records normally. Missing or unrecognized kind is
    job-compatible (not filtered) — the pre-kind-field legacy-id fallback
    was retired once every live producer either sets kind/type or was
    archived out of heartbeats/ (PANEL-2 final compat retirement,
    FLEET-WORKER2-BUILD-20260723-slate2-final-compat-retirement)."""
    kind = job.get("kind") or job.get("type")
    if isinstance(kind, str) and kind in JOB_KIND_VALUES:
        return kind in JOB_NONJOB_KINDS
    return False


def _jobs_activity_rows() -> list[dict[str, Any]]:
    """One prepared job list shared by Activity and the legacy route."""
    snap = read_snapshot()
    jobs = (snap.work.get("jobs") if snap and snap.work else None) or []
    jobs = [job for job in jobs if not _is_watch_guard(job)]
    return sorted(jobs, key=lambda job: _job_sort_key(job, time.time()))


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_index() -> RedirectResponse:
    """Legacy standalone index now belongs to Activity's Jobs lens."""
    return RedirectResponse("/activity?tab=jobs", status_code=302)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(
    request: Request, job_id: str, partial: int = 0, standalone: int = 0,
) -> HTMLResponse:
    await _confirm_if_nf(request)
    ctx = await asyncio.to_thread(detail_context.build_job_context, job_id)
    if partial or standalone:
        ctx["partial"] = bool(partial)
        return templates.TemplateResponse(request, "detail_jobs.html", ctx)
    return await _cold_sheet_response(request, "detail_jobs.html", ctx, request.url.path)


# --- Watchdogs: read-only inventory of watch/guard mechanisms (PANEL-4) ----
# A separate surface from Jobs: Jobs is live process state (heartbeat-derived,
# seconds-fresh); Watchdogs is a manifest of protective mechanisms (systemd/
# cron/APScheduler registrations, evidence sampled at recon time, overlaid
# with live cadence evidence by app/watchdogs_projection.py). Reads only the
# static registry plus local events.db receipts -- no systemctl/journalctl/
# ssh/subprocess call is ever made from either route below, and no POST/PUT/
# PATCH/DELETE verb is registered under /watchdogs or /api/watchdogs.

@router.get("/api/watchdogs")
async def api_watchdogs(host: str | None = None) -> JSONResponse:
    return JSONResponse({"rows": get_projected_registry(host), "summary": watchdogs_summary()})


async def _render_watchdogs(request: Request) -> HTMLResponse:
    """Host-grouped accordion. Initial DOM renders only the 4 host summary
    rows (counts below); each host's 1-11 detail rows mount client-side on
    expand and unmount on collapse (static/watchdogs.js) via GET
    /api/watchdogs -- no polling, no auto-refresh, no mutation control."""
    return templates.TemplateResponse(
        request, "watchdogs.html",
        {**(await app_chrome_context()), "wd_summary": watchdogs_summary()},
    )


@router.get("/watchdogs")
async def watchdogs_index() -> RedirectResponse:
    """Compatibility route; Watchdogs is an Operations lens."""
    return RedirectResponse("/operations?tab=watchdogs", status_code=302)


@router.get("/operations", response_class=HTMLResponse)
async def operations_index(request: Request) -> Any:
    """Canonical read-only workspace for fleet operational evidence."""
    tab = request.query_params.get("tab", "health").strip().lower()
    renderers = {
        "health": _render_health,
        "conformance": _render_conformance,
        "watchdogs": _render_watchdogs,
        "indexes": _render_indexes,
    }
    renderer = renderers.get(tab)
    if renderer is None:
        return RedirectResponse("/operations?tab=health", status_code=302)
    return await renderer(request)


@router.get("/queues/{queue}", response_class=HTMLResponse)
async def queue_detail(
    request: Request, queue: str, partial: int = 0, standalone: int = 0,
) -> HTMLResponse:
    await _confirm_if_nf(request)
    ctx = await asyncio.to_thread(detail_context.build_queue_context, queue)
    if partial or standalone:
        ctx["partial"] = bool(partial)
        return templates.TemplateResponse(request, "detail_queues.html", ctx)
    return await _cold_sheet_response(request, "detail_queues.html", ctx, request.url.path)


@router.get("/alerts/{alert_id}", response_class=HTMLResponse)
async def alert_detail(
    request: Request, alert_id: str, partial: int = 0, standalone: int = 0,
) -> HTMLResponse:
    await _confirm_if_nf(request)
    ctx = await asyncio.to_thread(detail_context.build_alert_context, alert_id)
    if partial or standalone:
        ctx["partial"] = bool(partial)
        return templates.TemplateResponse(request, "detail_alerts.html", ctx)
    return await _cold_sheet_response(request, "detail_alerts.html", ctx, request.url.path)


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request) -> HTMLResponse:
    """Canonical notification inbox and delivery-preferences workspace."""
    await _confirm_if_nf(request)
    ctx = await asyncio.to_thread(detail_context.build_feed_context)
    tab = request.query_params.get("tab", "inbox").strip().lower()
    if tab not in {"inbox", "preferences"}:
        return RedirectResponse("/notifications", status_code=302)
    group_keys = [str(group.get("key")) for group in ctx.get("groups", [])]
    requested_group = request.query_params.get("group", "").strip().lower()
    initial_group = requested_group if requested_group in group_keys else (
        group_keys[0] if group_keys else ""
    )
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            **(await app_chrome_context()),
            **ctx,
            "initial_tab": tab,
            "initial_group": initial_group,
        },
    )


@router.get("/feed")
async def legacy_feed_detail(request: Request) -> RedirectResponse:
    """The former feed sheet now belongs to the Notifications workspace."""
    suffix = "?nf=1" if request.query_params.get("nf") == "1" else ""
    return RedirectResponse(f"/notifications{suffix}", status_code=302)


@router.get("/settings")
async def legacy_settings_detail() -> RedirectResponse:
    """The retired settings page now lives under Notification Preferences."""
    return RedirectResponse("/notifications?tab=preferences", status_code=302)


@router.get("/approve/{token}", response_class=HTMLResponse)
async def approve_detail(
    request: Request, token: str, partial: int = 0, standalone: int = 0,
) -> HTMLResponse:
    """SHELL only: staged-build summary + INERT approve/deny placeholders.
    POST wiring is Phase 3/5, not this build."""
    ctx = await asyncio.to_thread(detail_context.build_approve_context, token)
    if partial or standalone:
        ctx["partial"] = bool(partial)
        return templates.TemplateResponse(request, "detail_approve.html", ctx)
    return await _cold_sheet_response(request, "detail_approve.html", ctx, request.url.path)


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    """Live stream of new events. Slow/dead clients are dropped without blocking
    ingest. Cloudflare Access authenticates public WebSocket upgrades before the
    tunnel forwards them to this handler."""
    await ws.accept()
    await hub.register(ws)
    try:
        while True:
            # We don't expect client messages; this keeps the socket open and
            # surfaces disconnects promptly.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.unregister(ws)


# --- Worker Activity: gated live session-transcript view (Tier 2) -----------
# Separate subsystem from the scan log — its own routes, its own WS channel, its
# own parser (app/herospath.py). Renders a headless run's full transcript as a
# conversation scroll and live-tails an in-flight run. Public access is protected
# by Cloudflare Access because transcripts carry raw tool output.

@router.get("/hero-path", response_class=HTMLResponse)
async def hero_path_index() -> RedirectResponse:
    """Legacy public name; preserve bookmarks while moving the UI to Activity."""
    return RedirectResponse("/activity?tab=workers", status_code=302)


@router.get("/activity/workers/{token}", response_class=HTMLResponse)
async def worker_activity_session(request: Request, token: str,
                                  limit: str = "") -> HTMLResponse:
    """The session scroll for one run. Path-safety lives in herospath (strict
    token charset + resolve inside a whitelisted transcript dir); a miss -> 404
    back to the run list. Default caps to the most-recent records; ?limit=all
    renders the full run (still bounded by HARD_EVENT_CAP)."""
    hit = herospath.resolve_transcript(token)
    if hit is None:
        runs = await asyncio.to_thread(herospath.list_runs)
        return templates.TemplateResponse(
            request, "activity.html",
            {**(await app_chrome_context()), "runs": runs, "not_found": token, "initial_tab": "workers"},
            status_code=404,
        )
    seat, path = hit
    cap = herospath.HARD_EVENT_CAP if limit == "all" else herospath.DEFAULT_EVENT_CAP
    data = await asyncio.to_thread(herospath.read_session, path, cap)
    state = await asyncio.to_thread(herospath.run_state_now, token) or "done"
    events_html = await asyncio.to_thread(herospath.render_events_html, data["events"])
    return templates.TemplateResponse(
        request, "hero_path_session.html",
        {
            **(await app_chrome_context()),
            "token": token, "seat": seat,
            "seat_class": herospath.SEAT_CLASS.get(seat, ""),
            "state": state, "events_html": events_html,
            "total": data["total"], "shown": data["shown"],
            "truncated_head": data["truncated_head"], "size": data["size"],
            "session": data["session"], "is_running": state == "running",
            "showing_all": limit == "all",
        },
    )


@router.get("/hero-path/{token}")
async def hero_path_session_legacy(token: str, limit: str = "") -> RedirectResponse:
    """Legacy transcript URL; retain the query contract during the rename."""
    destination = f"/activity/workers/{quote(token, safe='')}"
    if limit == "all":
        destination += "?limit=all"
    return RedirectResponse(destination, status_code=302)


async def _hero_tail_loop(ws: WebSocket, token: str, path: Path,
                          offset: int) -> None:
    """Poll the transcript for appended bytes, parse only COMPLETE records, and
    push rendered rows over the WS. Stops on a result record, or when the run is
    done/died and two consecutive polls saw no growth. One task per connection."""
    carry = ""
    idle = 0
    try:
        while True:
            res = await asyncio.to_thread(herospath.tail_since, path, offset, carry)
            offset, carry = res["offset"], res["carry"]
            if res["events"]:
                idle = 0
                html = await asyncio.to_thread(
                    herospath.render_events_html, res["events"])
                await ws.send_text(json.dumps({"type": "events", "html": html}))
            else:
                idle += 1
            if res["saw_result"]:
                await ws.send_text(json.dumps({"type": "end"}))
                return
            state = await asyncio.to_thread(herospath.run_state_now, token)
            if state in ("done", "died") and idle >= 2 and not carry:
                await ws.send_text(json.dumps({"type": "end"}))
                return
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    except Exception:
        return


@router.websocket("/activity/workers/ws")
@router.websocket("/hero-path/ws")
async def worker_activity_ws(ws: WebSocket) -> None:
    """Live tail of one in-flight run. Cloudflare Access authenticates public
    WebSocket upgrades before the tunnel forwards them to this handler.
    token is validated/path-safed here too; ?from=<byte offset> resumes exactly
    where the server-rendered page left off (no gap, no dup)."""
    token = ws.query_params.get("token", "")
    hit = herospath.resolve_transcript(token)
    await ws.accept()
    if hit is None:
        await ws.close(code=1008)
        return
    _seat, path = hit
    try:
        offset = int(ws.query_params.get("from", "0"))
    except (TypeError, ValueError):
        offset = 0
    if offset < 0:
        offset = 0
    tail_task = asyncio.create_task(_hero_tail_loop(ws, token, path, offset))
    try:
        # We don't expect client messages; this surfaces disconnects promptly.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        tail_task.cancel()
        try:
            await tail_task
        except Exception:
            pass


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    ctx = await _dashboard_ctx()
    return templates.TemplateResponse(request, "dashboard.html", ctx)
