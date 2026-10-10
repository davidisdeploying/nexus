"""Structured Gemini/Gemini rolling-quota adapter.

The installed Gemini client uses an authenticated private Cloud Code RPC
for the same weekly and five-hour model-group windows shown by ``/usage``.
This adapter reads the existing OAuth access token only from a marked isolated
collector HOME and normalizes the minimal response fields Nexus needs.

Private contracts are expected to drift. Failures are deliberately bounded and
credential-free so ``app.model_usage`` can fall back to the interactive
Gemini ``/usage`` panel without exposing token or response-body material.
"""

from __future__ import annotations

import os

import datetime as dt
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


QUOTA_URL = (
    "https://cloudcode-pa.googleapis.com/"
    "v1internal:retrieveUserQuotaSummary"
)
MARKER = ".nexus-model-usage-home"
LIVE_HOME = Path(os.path.expanduser("~")).resolve()
SOURCE = "cloudcode retrieveUserQuotaSummary"
CLIENT_USER_AGENT = "gemini"


class GeminiQuotaError(RuntimeError):
    """Base for adapter failures; never carries credential material."""


class GeminiHomeError(GeminiQuotaError):
    """Collector HOME failed the marker or live-home guard."""


class GeminiAuthError(GeminiQuotaError):
    """Token is missing, expired, or rejected by the quota RPC."""


class GeminiNetworkError(GeminiQuotaError):
    """The quota RPC failed outside authentication/schema handling."""


class GeminiSchemaError(GeminiQuotaError):
    """The quota RPC response did not match the required minimal shape."""


def _marked_home(home: Path) -> Path:
    if not (home / MARKER).is_file():
        raise GeminiHomeError(f"refusing unmarked collector HOME: {home}")
    if home.resolve() == LIVE_HOME:
        raise GeminiHomeError("refusing live user HOME")
    return home


def _parse_rfc3339(value: str) -> dt.datetime | None:
    """Parse RFC3339 while tolerating Go nanosecond precision."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                rest = tail[len(digits):]
                break
        else:
            rest = ""
        text = f"{head}.{digits[:6]}{rest}"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _read_access_token(home: Path) -> str:
    token_file = (
        home / ".gemini" / "gemini-cli" / "gemini-oauth-token"
    )
    try:
        blob = json.loads(token_file.read_text())
    except (OSError, ValueError) as exc:
        raise GeminiAuthError(
            f"token store unreadable: {type(exc).__name__}"
        ) from None
    token_obj = blob.get("token")
    token = (
        token_obj.get("access_token") if isinstance(token_obj, dict) else None
    )
    if not isinstance(token, str) or not token:
        raise GeminiAuthError("Gemini OAuth access token unavailable")
    expiry = token_obj.get("expiry") if isinstance(token_obj, dict) else None
    if isinstance(expiry, str) and expiry:
        parsed = _parse_rfc3339(expiry)
        if parsed is not None:
            now = dt.datetime.now(parsed.tzinfo or dt.timezone.utc)
            if parsed <= now:
                raise GeminiAuthError("Gemini OAuth access token expired")
    return token


def fetch_quota_summary(token: str, timeout: float = 10) -> dict[str, Any]:
    """Fetch the private quota summary without surfacing response bodies."""
    request = urllib.request.Request(
        QUOTA_URL,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": CLIENT_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        # Error bodies may echo account identity. Never read or report them.
        error_type = GeminiAuthError if exc.code in (401, 403) else GeminiNetworkError
        raise error_type(f"quota RPC HTTP {exc.code}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise GeminiNetworkError(
            f"quota RPC transport: {type(exc).__name__}"
        ) from None
    if not isinstance(payload, dict):
        raise GeminiSchemaError("quota summary is not an object")
    return payload


def parse_quota_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the exact Gemini weekly and five-hour rolling windows."""
    if not isinstance(payload, dict):
        raise GeminiSchemaError("quota summary is not an object")
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise GeminiSchemaError("quota summary missing groups")
    gemini = next(
        (
            group for group in groups
            if isinstance(group, dict)
            and str(group.get("displayName", "")).casefold()
            == "gemini models"
        ),
        None,
    )
    if gemini is None:
        raise GeminiSchemaError("quota summary missing Gemini group")

    windows: dict[str, Any] = {}
    for bucket in gemini.get("buckets") or []:
        if not isinstance(bucket, dict):
            continue
        fraction = bucket.get("remainingFraction")
        if isinstance(fraction, bool) or not isinstance(
            fraction, (int, float)
        ):
            continue
        clamped = max(0.0, min(1.0, float(fraction)))
        entry: dict[str, Any] = {
            "used_percent": round(100.0 * (1.0 - clamped), 2),
            "remaining_percent": round(100.0 * clamped, 2),
        }
        reset = bucket.get("resetTime")
        if isinstance(reset, str) and reset:
            if _parse_rfc3339(reset) is None:
                raise GeminiSchemaError("quota summary has invalid reset time")
            entry["resets_at"] = reset
        window = str(
            bucket.get("window") or bucket.get("bucketId") or ""
        ).casefold()
        if "weekly" in window:
            windows["weekly"] = entry
        elif "5h" in window or "five" in window:
            windows["five_hour"] = entry

    if set(windows) != {"weekly", "five_hour"}:
        raise GeminiSchemaError("quota summary missing required window")
    return {
        "ok": True,
        "source": SOURCE,
        "group": "Gemini models",
        "windows": windows,
    }


def collect_gemini_rpc(home: Path, timeout: float = 10) -> dict[str, Any]:
    """Marked HOME -> token -> RPC -> strict normalized quota result."""
    home = _marked_home(home)
    token = _read_access_token(home)
    return parse_quota_summary(fetch_quota_summary(token, timeout))
