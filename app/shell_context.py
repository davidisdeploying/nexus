"""Shared live context for the persistent Nexus application chrome."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import notify_store
from .models import Health
from .store import read_snapshot
from .system_status import get_system_status


CENTRAL_DISPLAY_ZONE = ZoneInfo("America/Chicago")

FRAME_ACCENT = {
    Health.OK: "developed",
    Health.WARN: "safelight",
    Health.CRIT: "overexposed",
    Health.UNKNOWN: "unexposed",
}


def _shell_asset_version() -> str:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    digest = hashlib.sha256()
    for name in ("nexus.css", "app_shell.js", "notifications.js", "system_status.js"):
        digest.update(name.encode() + b"\0")
        digest.update((static_dir / name).read_bytes())
    return digest.hexdigest()[:16]


SHELL_ASSET_VERSION = _shell_asset_version()


def central_header_stamp(value: Any) -> str:
    """Format a UTC-backed timestamp for the shared chrome in Central time."""
    if not isinstance(value, datetime):
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CENTRAL_DISPLAY_ZONE).strftime("%Y-%m-%d %-I:%M %p")


async def app_chrome_context() -> dict[str, Any]:
    """Read only the local cached state required by the global app shell."""
    snap = read_snapshot()
    unread, system_status = await asyncio.gather(
        asyncio.to_thread(notify_store.count_unread),
        asyncio.to_thread(get_system_status),
    )
    return {
        "chrome_snap": snap,
        "chrome_accent": FRAME_ACCENT,
        "chrome_unread": unread,
        "chrome_stamp": central_header_stamp(snap.generated_at if snap else None),
        "chrome_system_status": system_status,
    }
