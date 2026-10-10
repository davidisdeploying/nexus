"""
App entry point.

Lifespan wiring: on startup, register jobs, kick one immediate heartbeat so the
dashboard isn't blank on first load, then start the scheduler for the recurring
cadence. On shutdown, stop it cleanly.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .gemini_remote import (
    router as control_router,
    terminal_manager as control_terminal_manager,
    worker_manager as control_worker_manager,
)
from .config import settings
from .events import init_db as init_events_db
from .heartbeat_runner import heartbeat_runner
from .model_usage_history import init_db as init_model_usage_history_db
from .notify_store import init_db as init_notify_db
from .routes import router
from .runtime_paths import GENERATED_STATE_DIR
from .run_watcher import scan_once as run_watcher_scan_once
from .scheduler import register_jobs, scheduler
from .trust import PeerTrustMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("nexus")

_STATE_DIR = GENERATED_STATE_DIR


def _cleanup_orphaned_state_tmp(state_dir: Path = _STATE_DIR) -> int:
    """Remove regular top-level *.tmp files left by interrupted atomic writes."""
    state_dir.mkdir(parents=True, exist_ok=True)
    removed = 0
    for tmp in state_dir.glob("*.tmp"):
        try:
            if tmp.is_symlink() or not tmp.is_file():
                continue
            tmp.unlink()
            removed += 1
        except OSError as exc:
            log.warning("state tmp cleanup failed for %s: %s", tmp, exc)
    if removed:
        log.info("removed %d orphaned state tmp file(s)", removed)
    return removed


@asynccontextmanager
async def lifespan(app: FastAPI):
    _cleanup_orphaned_state_tmp()
    init_events_db()          # live-timeline event store (SQLite, WAL)
    init_notify_db()          # push_subscription/notification_log/alerts (Phase 0)
    init_model_usage_history_db(settings.model_usage_history_db)
    register_jobs()
    # Immediate first sweep so the Nexus has something to show, but don't
    # block startup on it — let uvicorn come up and fill in a beat later.
    heartbeat_runner.create_startup_task()
    # Run one watcher tick synchronously (not backgrounded) so the NOW-watermark
    # is established before the recurring scheduler job's first 120s interval
    # elapses — a restart must never leave a window where new-since-boot run
    # dirs could be missed OR (if the process crashed right after boot) get
    # re-baselined. It's a handful of directory listings; the cost is trivial.
    await run_watcher_scan_once()
    scheduler.start()
    log.info("nexus dashboard up on %s:%d", settings.host, settings.port)
    yield
    await control_worker_manager.shutdown()
    await control_terminal_manager.shutdown()
    scheduler.shutdown(wait=False)
    log.info("scheduler stopped")


app = FastAPI(title="Nexus", lifespan=lifespan)
# Outermost gate: only loopback (== the Access-gated tunnel origin, plus same-box
# hooks) and the tailnet reach ANY route. Added before the routers so it wraps
# every path including /static and the /control/ws upgrade. Cloudflare Access
# remains the authentication for public traffic; this is the boundary for traffic
# that never touches Cloudflare. See app/trust.py for the full rationale.
app.add_middleware(PeerTrustMiddleware)
app.include_router(router)
app.include_router(control_router)
# Serve /static from the app's static/ dir (the runic-font slot lives at
# static/fonts/nexus.woff2 — a user-supplied asset; the build ships none).
# Public browser access is enforced at the Cloudflare Access edge.
_static_dir = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
