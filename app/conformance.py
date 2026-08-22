"""Cache-only reader and display projection helper for the fleet conformance surface."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .runtime_paths import GENERATED_STATE_DIR

CACHE = GENERATED_STATE_DIR / "conformance.json"
HISTORY = GENERATED_STATE_DIR / "conformance-history.jsonl"
CENTRAL_TZ = ZoneInfo("America/Chicago")
STALE_THRESHOLD_SECONDS = 2100
HISTORY_STRIP_SAMPLES = 96
# "Recently recovered" is a display convenience, not a durability guarantee —
# a check that recovered longer ago than this simply stops showing in that
# section; last_recovered_at itself is retained in the cache indefinitely.
RECENTLY_RECOVERED_WINDOW_SECONDS = 24 * 60 * 60
_DISPLAY_CATEGORY_TITLES = {"governance": "Indexes (governance)"}

CATEGORY_DEFS: list[dict[str, str]] = [
    {
        "key": "agents",
        "title": "Operating contract (agents)",
        "short_title": "Contract",
        "description": "Verification of bounded agent contract hashes across target hosts.",
    },
    {
        "key": "ssh",
        "title": "Worker access (ssh)",
        "short_title": "SSH",
        "description": "Direct inter-host SSH accessibility and credential health.",
    },
    {
        "key": "service",
        "title": "Required services (service)",
        "short_title": "Services",
        "description": "Systemd services and timer units required across fleet hosts, including both live active state and startup enablement.",
    },
    {
        "key": "path",
        "title": "Required files (path)",
        "short_title": "Files",
        "description": "Presence and accessibility of critical configuration files and manifests.",
    },
    {
        "key": "receipts",
        "title": "Automation receipts (receipts)",
        "short_title": "Receipts",
        "description": "Structured daily fleet Git-push success receipts, one per host, verified for freshness and host match.",
    },
    {
        "key": "fresh",
        "title": "Snapshot freshness (fresh)",
        "short_title": "Snapshots",
        "description": "Age and timestamp verification for control plane backup archives.",
    },
    {
        "key": "mirror",
        "title": "Vault mirror (mirror)",
        "short_title": "Mirror",
        "description": "Git HEAD alignment between local Vaults and remote backup mirrors.",
    },
    {
        "key": "governance",
        "title": "Control plane (governance)",
        "short_title": "Governance",
        "description": "Central index integrity and authoritative parity, routing, lint, and adoption validators.",
    },
    {
        "key": "other",
        "title": "Unclassified checks",
        "short_title": "Other",
        "description": "Declared checks whose category is not recognized by this Nexus version.",
    },
]


def read_cache(path: Path = CACHE) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "conformance cache unavailable or malformed"
    if not isinstance(data, dict) or data.get("version") not in {1, 2}:
        return None, "unsupported conformance cache schema"
    if not isinstance(data.get("generated_at"), str):
        return None, "conformance cache is missing generated_at"
    return data, None


def read_history(path: Path = HISTORY, limit: int = 1000) -> list[dict[str, Any]]:
    """Read the bounded conformance history WITHOUT mutating it. Returns an
    oldest-first list of {generated_at, overall, counts, collector_error}
    rows; any unparseable line is skipped rather than failing the whole read."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def format_central(iso_str: str | None) -> str:
    """Format an ISO timestamp into Central Time (America/Chicago)."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        central_dt = dt.astimezone(CENTRAL_TZ)
        return central_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError):
        return str(iso_str)


def _categorize_check(ch: dict[str, Any]) -> str:
    cid = str(ch.get("id", ""))
    cat = str(ch.get("category", ""))
    if cid.startswith("agents:") or cat == "contract":
        return "agents"
    if cid.startswith("ssh:") or cat == "ssh":
        return "ssh"
    if cid.startswith("unit:") or cat == "service":
        return "service"
    if cid.startswith("path:") or cat == "path":
        return "path"
    if cid.startswith("enabled:"):
        return "service"
    if cid.startswith("fresh:"):
        return "fresh"
    if cid.startswith("mirror:"):
        return "mirror"
    if cid.startswith("receipt:") or cat == "receipts":
        return "receipts"
    if cid.startswith("governance:") or cat == "governance":
        return "governance"
    if cat == "backup":
        if "fresh" in cid:
            return "fresh"
        if "mirror" in cid:
            return "mirror"
    return "other"


def _format_human_title(ch: dict[str, Any]) -> str:
    cid = str(ch.get("id", ""))
    host = str(ch.get("host", ""))
    if cid.startswith("agents:"):
        target = cid.split(":", 1)[1] if ":" in cid else host
        return f"Agent Contract ({target})"
    elif cid.startswith("ssh:"):
        parts = cid.split(":")
        if len(parts) == 3:
            return f"SSH Access ({parts[1]} → {parts[2]})"
        return f"SSH Access ({host})"
    elif cid.startswith("unit:"):
        parts = cid.split(":", 2)
        unit = parts[2] if len(parts) == 3 else cid
        return f"Service Active ({unit} on {host})"
    elif cid.startswith("enabled:"):
        parts = cid.split(":", 2)
        unit = parts[2] if len(parts) == 3 else cid
        return f"Service Enabled at boot ({unit} on {host})"
    elif cid.startswith("receipt:"):
        return f"Git Push Receipt ({host})"
    elif cid.startswith("path:"):
        parts = cid.split(":", 2)
        p = parts[2] if len(parts) == 3 else cid
        fname = Path(p).name if p else cid
        return f"Required Path ({fname} on {host})"
    elif cid.startswith("fresh:"):
        parts = cid.split(":", 2)
        p = parts[2] if len(parts) == 3 else cid
        fname = Path(p).name if p else cid
        return f"Snapshot Freshness ({fname} on {host})"
    elif cid.startswith("mirror:"):
        return f"Vault Mirror ({host})"
    elif cid.startswith("governance:"):
        return str(ch.get("title") or f"Governance ({cid.split(':', 1)[-1]})")
    return f"Check ({cid})"


def derive_stability(
    history: list[dict[str, Any]], now: datetime | None = None
) -> dict[str, Any]:
    """Derive consecutive overall-state scan count, stable-since time, the
    last transition visible in the retained window (if any), and an honest
    "at least" qualifier for when the ENTIRE retained window shares one
    state (we can't see further back than the window, so we can't claim more
    than "at least this long"). `history` must be oldest-first."""
    if not history:
        return {
            "current_state": "unknown",
            "consecutive_scans": 0,
            "stable_since": None,
            "stable_since_central": "—",
            "at_least_qualifier": False,
            "last_transition": None,
        }
    latest_state = history[-1].get("overall", "unknown")
    latest_policy = history[-1].get("check_set_sha256")
    consecutive = 0
    stable_since_iso = None
    idx = len(history) - 1
    while (
        idx >= 0
        and history[idx].get("overall", "unknown") == latest_state
        and (not latest_policy or history[idx].get("check_set_sha256") == latest_policy)
    ):
        consecutive += 1
        stable_since_iso = history[idx].get("generated_at")
        idx -= 1
    at_least_qualifier = idx < 0  # ran off the front of the retained window
    last_transition = None
    if idx >= 0:
        policy_changed = bool(
            latest_policy
            and history[idx].get("check_set_sha256") != latest_policy
        )
        last_transition = {
            "from": history[idx].get("overall", "unknown"),
            "to": latest_state,
            "at": history[idx + 1].get("generated_at"),
            "at_central": format_central(history[idx + 1].get("generated_at")),
            "kind": "policy_change" if policy_changed else "state_change",
        }
    return {
        "current_state": latest_state,
        "consecutive_scans": consecutive,
        "stable_since": stable_since_iso,
        "stable_since_central": format_central(stable_since_iso),
        "at_least_qualifier": at_least_qualifier,
        "last_transition": last_transition,
        "manifest_revision": history[-1].get("manifest_revision"),
    }


def history_strip(
    history: list[dict[str, Any]], count: int = HISTORY_STRIP_SAMPLES
) -> list[dict[str, Any]]:
    """The most recent `count` samples (oldest-first) for a compact ~24h
    conformance strip, each reduced to just what the strip needs to render."""
    recent = history[-count:] if count > 0 else []
    return [
        {
            "state": row.get("overall", "unknown"),
            "generated_at": row.get("generated_at"),
            "generated_at_central": format_central(row.get("generated_at")),
            "manifest_revision": row.get("manifest_revision"),
            "policy_changed": bool(row.get("policy_changed")),
        }
        for row in recent
    ]


def _recently_recovered(
    checks: list[dict[str, Any]],
    now: datetime,
    window_seconds: int = RECENTLY_RECOVERED_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    """Enhanced check rows whose last_recovered_at (additive cache metadata)
    falls inside the trailing window, newest recovery first."""
    rows = []
    for ch in checks:
        recovered_at = ch.get("last_recovered_at")
        if not recovered_at:
            continue
        try:
            dt = datetime.fromisoformat(str(recovered_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        age = (now - dt).total_seconds()
        if 0 <= age <= window_seconds:
            rows.append(ch)
    rows.sort(key=lambda c: c.get("last_recovered_at") or "", reverse=True)
    return rows


def project_conformance(
    data: dict[str, Any] | None,
    error: str | None = None,
    history: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Derive display-ready human projection structure without mutating raw
    cache or history. `history` (oldest-first) and `now` are both
    dependency-injectable so tests never touch the real state/ files or wall
    clock."""
    now = now or datetime.now(timezone.utc)
    history = history if history is not None else []
    if error or not data:
        category_defs = CATEGORY_DEFS[:-1]
        empty_categories = [
            {
                "key": cat["key"],
                "title": cat["title"],
                "short_title": cat["short_title"],
                "description": cat["description"],
                "count": 0,
                "ok_count": 0,
                "non_ok_count": 0,
                "checks": [],
            }
            for cat in category_defs
        ]
        return {
            "available": False,
            "error": error or "conformance cache unavailable",
            "overall": "unknown",
            "generated_at": None,
            "generated_at_central": "—",
            "is_stale": True,
            "age_seconds": None,
            "scan_duration": "0.00",
            "total_checks": 0,
            "counts": {"ok": 0, "warning": 0, "error": 0, "unknown": 0},
            "non_ok_count": 0,
            "outcome_headline": "Conformance cache unavailable",
            "categories": empty_categories,
            "grouped_checks": {cat["key"]: [] for cat in category_defs},
            "history_strip": history_strip(history),
            "stability": derive_stability(history, now=now),
            "recently_recovered": [],
        }

    generated_at_iso = data.get("generated_at", "")
    generated_at_central = format_central(generated_at_iso)

    # Calculate staleness
    is_stale = True
    age_seconds = 0.0
    try:
        gen_dt = datetime.fromisoformat(generated_at_iso.replace("Z", "+00:00"))
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (now - gen_dt).total_seconds())
        is_stale = age_seconds > STALE_THRESHOLD_SECONDS
    except Exception:
        pass

    raw_checks = data.get("checks", [])
    if not isinstance(raw_checks, list):
        raw_checks = []

    raw_category_defs = data.get("categories") if data.get("version") == 2 else None
    fallback_defs = CATEGORY_DEFS[:7] if data.get("version") == 1 else CATEGORY_DEFS[:-1]
    category_defs = [cat for cat in raw_category_defs or fallback_defs
                     if isinstance(cat, dict) and cat.get("key")]
    categorized_checks: dict[str, list[dict[str, Any]]] = {
        str(cat["key"]): [] for cat in category_defs
    }
    all_enhanced: list[dict[str, Any]] = []

    total_checks = len(raw_checks)
    counts = dict(data.get("counts", {"ok": 0, "warning": 0, "error": 0, "unknown": 0}))

    for raw_ch in raw_checks:
        if not isinstance(raw_ch, dict):
            continue
        cat_key = _categorize_check(raw_ch)
        enhanced_ch = dict(raw_ch)  # copy to avoid mutating raw cache object
        enhanced_ch["category_key"] = cat_key
        enhanced_ch["human_title"] = _format_human_title(raw_ch)
        enhanced_ch["checked_at_central"] = format_central(raw_ch.get("checked_at"))
        enhanced_ch["last_ok_at_central"] = format_central(raw_ch.get("last_ok_at"))
        enhanced_ch["state_changed_at_central"] = format_central(raw_ch.get("state_changed_at"))
        enhanced_ch["first_non_ok_at_central"] = format_central(raw_ch.get("first_non_ok_at"))
        enhanced_ch["last_recovered_at_central"] = format_central(raw_ch.get("last_recovered_at"))
        all_enhanced.append(enhanced_ch)

        if cat_key in categorized_checks:
            categorized_checks[cat_key].append(enhanced_ch)
        else:
            categorized_checks.setdefault(cat_key, []).append(enhanced_ch)

    category_list = []
    known_keys = {str(cat["key"]) for cat in category_defs}
    if categorized_checks.get("other") and "other" not in known_keys:
        category_defs.append(next(cat for cat in CATEGORY_DEFS if cat["key"] == "other"))
    for cat_def in category_defs:
        key = cat_def["key"]
        cat_checks = categorized_checks.get(key, [])
        cat_total = len(cat_checks)
        cat_ok = sum(1 for c in cat_checks if c.get("state") == "ok")
        cat_non_ok = cat_total - cat_ok
        category_list.append(
            {
                "key": key,
                "title": _DISPLAY_CATEGORY_TITLES.get(key, cat_def["title"]),
                "short_title": cat_def["short_title"],
                "description": cat_def["description"],
                "count": cat_total,
                "ok_count": cat_ok,
                "non_ok_count": cat_non_ok,
                "checks": cat_checks,
            }
        )

    ok_cnt = counts.get("ok", 0)
    warn_cnt = counts.get("warning", 0)
    err_cnt = counts.get("error", 0)
    unk_cnt = counts.get("unknown", 0)
    non_ok_count = warn_cnt + err_cnt + unk_cnt

    if total_checks > 0 and ok_cnt == total_checks and non_ok_count == 0:
        outcome_headline = f"All {total_checks} declared checks pass"
    elif total_checks > 0:
        issue_parts = []
        if err_cnt > 0:
            issue_parts.append(f"{err_cnt} error{'s' if err_cnt > 1 else ''}")
        if warn_cnt > 0:
            issue_parts.append(f"{warn_cnt} warning{'s' if warn_cnt > 1 else ''}")
        if unk_cnt > 0:
            issue_parts.append(f"{unk_cnt} unknown")
        breakdown = ", ".join(issue_parts) if issue_parts else "issues"
        outcome_headline = f"{ok_cnt} of {total_checks} declared checks pass ({breakdown})"
    else:
        outcome_headline = "No declared checks recorded"

    duration_val = data.get("duration_seconds")
    scan_duration_str = f"{duration_val:.2f}" if isinstance(duration_val, (int, float)) else "—"

    return {
        "available": True,
        "version": data.get("version", 1),
        "overall": data.get("overall", "unknown"),
        "generated_at": generated_at_iso,
        "generated_at_central": generated_at_central,
        "is_stale": is_stale,
        "age_seconds": age_seconds,
        "scan_duration": scan_duration_str,
        "total_checks": total_checks,
        "counts": counts,
        "non_ok_count": non_ok_count,
        "outcome_headline": outcome_headline,
        "categories": category_list,
        "grouped_checks": categorized_checks,
        "error": None,
        "history_strip": history_strip(history),
        "stability": derive_stability(history, now=now),
        "recently_recovered": _recently_recovered(all_enhanced, now=now),
    }
