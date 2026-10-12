"""Cache-only reader for the five-index fleet control-plane projection."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .runtime_paths import GENERATED_STATE_DIR

CACHE = GENERATED_STATE_DIR / "control-plane.json"
CENTRAL = ZoneInfo("America/Chicago")
STALE_SECONDS = 2100


def read_cache(path: Path = CACHE) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "control-plane cache unavailable or malformed"
    if not isinstance(data, dict) or data.get("version") != 1:
        return None, "unsupported control-plane cache schema"
    if not isinstance(data.get("generated_at"), str):
        return None, "control-plane cache is missing generated_at"
    return data, None


def _central(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(CENTRAL).strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(value)


def project(data: dict[str, Any] | None, error: str | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if error or not data:
        return {"available": False, "error": error or "control-plane cache unavailable",
                "overall": "unknown", "cards": [], "is_stale": True,
                "generated_at_central": "—"}
    generated = str(data.get("generated_at", ""))
    try:
        age = max(0.0, (now - datetime.fromisoformat(generated.replace("Z", "+00:00"))).total_seconds())
    except (ValueError, TypeError):
        age = float("inf")
    cards = [dict(card) for card in data.get("cards", []) if isinstance(card, dict)]
    counts = {state: sum(card.get("status") == state for card in cards)
              for state in ("ok", "warning", "error")}
    return {**data, "available": True, "error": None, "cards": cards,
            "counts": counts, "is_stale": age > STALE_SECONDS,
            "age_seconds": age, "generated_at_central": _central(generated)}
