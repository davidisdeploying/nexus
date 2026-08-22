"""Safe cache reader and local range filtering for the Activity surface."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .runtime_paths import GENERATED_STATE_DIR

ACTIVITY_CACHE = GENERATED_STATE_DIR / "activity.json"
VALID_RANGES = {"all", "30d", "7d"}


def provider_for_surface(surface: object) -> str | None:
    value = str(surface or "").lower()
    if "claude" in value:
        return "Claude"
    if "codex" in value or "openai" in value:
        return "OpenAI"
    if "gemini" in value or "gemini" in value:
        return "Google"
    return None


def read_cache(path: Path = ACTIVITY_CACHE) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "cache unavailable or malformed"
    if not isinstance(data, dict) or data.get("version") != 1:
        return None, "cache has an unsupported schema"
    if not isinstance(data.get("generated_at"), str):
        return None, "cache is missing generated_at"
    return data, None


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _in_range(value: object, cutoff: datetime | None) -> bool:
    return cutoff is None or ((_parse_utc(value) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff)


def filter_cache(cache: dict[str, Any], selected_range: str, now: datetime | None = None) -> dict[str, Any]:
    if selected_range not in VALID_RANGES:
        raise ValueError("range must be all, 30d, or 7d")
    now = now or datetime.now(timezone.utc)
    cutoff = None if selected_range == "all" else now - timedelta(days=30 if selected_range == "30d" else 7)
    result = dict(cache)
    commits = [x for x in cache.get("commits", []) if _in_range(x.get("timestamp"), cutoff)]
    pushes = [x for x in cache.get("pushes", []) if _in_range(x.get("finished_at"), cutoff)]
    turns = [x for x in cache.get("assistant_turns", []) if _in_range(x.get("timestamp"), cutoff)]
    result.update({"range": selected_range, "commits": commits, "pushes": pushes})
    daily: dict[str, dict[str, int]] = {}
    for commit in commits:
        day = str(commit.get("timestamp", ""))[:10]
        if day:
            daily.setdefault(day, {"commits": 0, "Claude": 0, "OpenAI": 0, "Google": 0})["commits"] += 1
    providers = {"Claude": 0, "OpenAI": 0, "Google": 0}
    sessions: dict[str, set[str]] = {key: set() for key in providers}
    provider_days: dict[str, set[str]] = {key: set() for key in providers}
    for turn in turns:
        provider = turn.get("provider")
        if provider in providers:
            providers[provider] += 1
            sessions[provider].add(str(turn.get("conversation_id", "")))
            stamp = _parse_utc(turn.get("timestamp"))
            if stamp:
                day = stamp.date().isoformat()
                provider_days[provider].add(day)
                daily.setdefault(day, {"commits": 0, "Claude": 0, "OpenAI": 0, "Google": 0})[provider] += 1
    active_days = sorted(day for day, values in daily.items() if values["commits"])
    streak = longest = run = 0
    previous = None
    for day in active_days:
        date = datetime.fromisoformat(day).date()
        run = run + 1 if previous and (date - previous).days == 1 else 1
        longest = max(longest, run); previous = date
    if active_days:
        today = now.date(); current = 0; cursor = today
        active = set(active_days)
        while cursor.isoformat() in active:
            current += 1; cursor -= timedelta(days=1)
    repo_counts: dict[str, int] = {}
    hour_counts: dict[str, int] = {}
    for commit in commits:
        repo = str(commit.get("repository", "unknown")); repo_counts[repo] = repo_counts.get(repo, 0) + 1
        stamp = _parse_utc(commit.get("timestamp"));
        if stamp: hour_counts[f"{stamp.hour:02d}:00 UTC"] = hour_counts.get(f"{stamp.hour:02d}:00 UTC", 0) + 1
    coverage = cache.get("provider_coverage", {})
    comparable = {
        name: bool(coverage.get(name, {}).get("assistant_records"))
        for name in providers
    }
    total_turns = sum(count for name, count in providers.items() if comparable[name])
    result["daily"] = [{"date": d, **daily[d]} for d in sorted(daily)]
    result["summary"] = {"commits": len(commits), "successful_pushes": len(pushes), "repositories_touched": len(repo_counts), "active_days": len(active_days), "current_streak": current if active_days else 0, "longest_streak": longest, "peak_commit_hour": max(hour_counts, key=hour_counts.get) if hour_counts else None, "top_repository": max(repo_counts, key=repo_counts.get) if repo_counts else None}
    result["providers"] = {
        name: {
            "assistant_turns": count if comparable[name] else None,
            "share": round((count / total_turns * 100) if total_turns else 0, 1) if comparable[name] else None,
            "sessions": len(sessions[name]) if comparable[name] else None,
            "active_days": len(provider_days[name]) if comparable[name] else None,
            "comparable": comparable[name],
            "normalized_records": int(coverage.get(name, {}).get("records", 0)),
            "coverage_reason": None if comparable[name] else "normalized evidence has no assistant-role labels",
        }
        for name, count in providers.items()
    }
    comparable_counts = {name: count for name, count in providers.items() if comparable[name]}
    result["most_used_provider"] = max(comparable_counts, key=comparable_counts.get) if total_turns else None
    result["provider_comparison_complete"] = all(comparable.values())
    result["recent_events"] = sorted([*commits, *pushes], key=lambda x: str(x.get("timestamp") or x.get("finished_at") or ""), reverse=True)[:100]
    # The cache retains normalized metadata for re-filtering, but the API never
    # exposes every evidence record; the UI only needs aggregates.
    result.pop("assistant_turns", None)
    return result
