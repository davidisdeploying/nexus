"""
24h fleet/per-node health-timeline projection (HEALTH-TIMELINE-2).

Pure, deterministic, and testable: takes the rows store.read_history() already
returns (oldest-first, v1 or v2 shape) plus a window size, and derives every
number the dashboard's Health Timeline module needs — cadence coverage,
duration stats, per-node incident/streak/healthy-% math, and correlated
transition candidates. Never reads a file or launches a probe itself; the
route layer owns I/O, this module owns math, same split as conformance.py.

A v1 row (no `sample_version`) is fully usable for state/timing math; it just
carries no retained cause for its issues, so an incident whose worst sample
predates HEALTH-TIMELINE-2 reports cause=None ("cause not retained"), never a
guess and never an error.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

CENTRAL_TZ = ZoneInfo("America/Chicago")

DEFAULT_CADENCE_SECONDS = 300           # settings.heartbeat_interval_seconds, mirrored so this
                                         # module has no import-time dependency on app.config
GAP_MULTIPLIER = 1.5                    # a gap between samples wider than this many cadences
                                         # is a missed beat, not just sampling jitter
FRESH_MULTIPLIER = 2                    # mirrors routes.healthz's scheduler_fresh rule
_STATE_ORDER = ("ok", "warn", "crit", "unknown")
_INCIDENT_PEAK_RANK = {"crit": 3, "warn": 2, "unknown": 1}   # "ok" never appears in a run


def format_central(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(CENTRAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile — no interpolation, no numpy dependency, and
    the same answer every time for the same input (deterministic/testable)."""
    n = len(sorted_values)
    if n == 0:
        return None
    idx = max(0, min(n - 1, int(round(pct / 100.0 * n + 0.5)) - 1))
    return sorted_values[idx]


def _empty_projection(hours: int, cadence_seconds: int, now: datetime) -> dict:
    return {
        "hours": hours,
        "generated_at": now.isoformat(),
        "generated_at_central": format_central(now),
        "cadence_seconds": cadence_seconds,
        "expected_samples": max(1, round(hours * 3600 / cadence_seconds)),
        "received_samples": 0,
        "cadence_coverage_pct": 0.0,
        "gap_count": 0,
        "overall_current": "unknown",
        "overall_healthy_pct": None,
        "nodes_healthy_now": 0,
        "nodes_total": 0,
        "scan_generated_at": None,
        "scan_generated_at_central": "—",
        "scan_age_seconds": None,
        "scan_is_fresh": False,
        "duration_ms": {"current": None, "median": None, "p95": None, "max": None},
        "nodes": {},
        "correlated_incidents": [],
    }


def _node_states(parsed: list[tuple[datetime, dict]], name: str) -> list[str]:
    return [str((row.get("nodes") or {}).get(name, "unknown")) for _dt, row in parsed]


def _cause_for(row: dict, name: str) -> dict | None:
    """First retained issue for `name` in this sample, or None when the row
    predates sample_version 2 (or simply retained nothing for this node)."""
    if row.get("sample_version") is None:
        return None
    issues = (row.get("issues") or {}).get(name)
    if not issues:
        return None
    first = issues[0]
    return {
        "kind": first.get("kind"),
        "health": first.get("health"),
        "value": first.get("value"),
        "method": first.get("method"),
        "error_class": first.get("error_class"),
    }


def _node_incidents(parsed: list[tuple[datetime, dict]], states: list[str]) -> list[dict]:
    incidents = []
    run_start = None
    run_rows: list[int] = []
    for i, state in enumerate(states):
        if state != "ok":
            if run_start is None:
                run_start = i
            run_rows.append(i)
            continue
        if run_start is not None:
            incidents.append(_close_incident(parsed, states, run_rows, recovered_at=i))
            run_start, run_rows = None, []
    if run_start is not None:
        incidents.append(_close_incident(parsed, states, run_rows, recovered_at=None))
    return incidents


def _close_incident(parsed, states, run_rows: list[int], recovered_at: int | None) -> dict:
    peak_idx = max(run_rows, key=lambda i: _INCIDENT_PEAK_RANK.get(states[i], 0))
    start_dt = parsed[run_rows[0]][0]
    end_dt = parsed[run_rows[-1]][0]
    duration_s = (end_dt - start_dt).total_seconds()
    return {
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "duration_seconds": duration_s,
        "peak_state": states[peak_idx],
        "recovered": recovered_at is not None,
        "recovered_at": parsed[recovered_at][0].isoformat() if recovered_at is not None else None,
        "cause": _cause_for(parsed[peak_idx][1], parsed[peak_idx][1].get("__node_name__", "")),
    }


def project_health_timeline(
    rows: list[dict],
    hours: int = 24,
    now: datetime | None = None,
    cadence_seconds: int | None = None,
) -> dict:
    """Derive the 24h (or `hours`) fleet/per-node summary from history rows.

    `rows` is whatever store.read_history() returns — oldest-first, each row
    at minimum {"t","overall","nodes","ms"}, optionally v2's
    {"sample_version","issues","metrics"}. Malformed/unparseable rows are
    skipped, never raised on — one bad line must not blank the whole module.
    """
    cadence = cadence_seconds or DEFAULT_CADENCE_SECONDS
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)

    parsed: list[tuple[datetime, dict]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dt = _parse_iso(row.get("t"))
        if dt is None or dt < window_start or dt > now:
            continue
        parsed.append((dt, row))
    parsed.sort(key=lambda pair: pair[0])

    if not parsed:
        return _empty_projection(hours, cadence, now)

    node_names: set[str] = set()
    for _dt, row in parsed:
        node_names |= set((row.get("nodes") or {}).keys())
    node_names = sorted(node_names)

    received = len(parsed)
    expected = max(1, round(hours * 3600 / cadence))
    coverage_pct = round(min(100.0, received / expected * 100), 1)

    gap_threshold = cadence * GAP_MULTIPLIER
    gap_count = sum(
        1 for (a, _), (b, _) in zip(parsed, parsed[1:])
        if (b - a).total_seconds() > gap_threshold
    )

    last_dt, last_row = parsed[-1]
    scan_age = (now - last_dt).total_seconds()
    scan_fresh = scan_age < cadence * FRESH_MULTIPLIER

    durations = sorted(
        float(row["ms"]) for _dt, row in parsed
        if isinstance(row.get("ms"), (int, float))
    )
    duration_stats = {
        "current": (float(last_row["ms"]) if isinstance(last_row.get("ms"), (int, float)) else None),
        "median": _percentile(durations, 50),
        "p95": _percentile(durations, 95),
        "max": (durations[-1] if durations else None),
    }

    nodes_out: dict[str, Any] = {}
    correlated_incidents: list[dict] = []

    # Correlated transitions: two or more nodes newly entering non-OK on the
    # SAME sample. "Shared event" / "correlated transition" — a same-tick
    # coincidence worth flagging, never asserted as one proven root cause.
    for i in range(1, len(parsed)):
        newly_bad = []
        for name in node_names:
            cur = str((parsed[i][1].get("nodes") or {}).get(name, "unknown"))
            prev = str((parsed[i - 1][1].get("nodes") or {}).get(name, "unknown"))
            if cur != "ok" and prev == "ok":
                newly_bad.append(name)
        if len(newly_bad) >= 2:
            correlated_incidents.append({
                "t": parsed[i][0].isoformat(),
                "t_central": format_central(parsed[i][0]),
                "nodes": newly_bad,
                "label": "correlated transition",
            })

    for name in node_names:
        states = _node_states(parsed, name)
        counts_by_state = {s: 0 for s in _STATE_ORDER}
        for s in states:
            counts_by_state[s if s in counts_by_state else "unknown"] += 1
        healthy_pct = round(counts_by_state["ok"] / len(states) * 100, 1) if states else None

        # Stamp the node name onto each row's dict view so _close_incident's
        # cause lookup (which needs to know WHICH node this run belongs to)
        # doesn't require threading an extra parameter through every helper.
        tagged = [(dt, {**row, "__node_name__": name}) for dt, row in parsed]
        incidents = _node_incidents(tagged, states)

        current_state = states[-1]
        streak = 1
        for i in range(len(states) - 2, -1, -1):
            if states[i] == current_state:
                streak += 1
            else:
                break
        current_state_since = parsed[len(states) - streak][0].isoformat()

        last_incident = incidents[-1] if incidents else None
        recovered_incidents = [inc for inc in incidents if inc["recovered"]]
        last_recovery_at = recovered_incidents[-1]["recovered_at"] if recovered_incidents else None

        nodes_out[name] = {
            "current_state": current_state,
            "current_state_since": current_state_since,
            "current_streak_samples": streak,
            "healthy_pct": healthy_pct,
            "counts_by_state": counts_by_state,
            "incident_count": len(incidents),
            "last_incident": last_incident,
            "last_recovery_at": last_recovery_at,
            "sample_versions_seen": sorted({
                row.get("sample_version") for _dt, row in parsed
                if row.get("sample_version") is not None
            }) or None,
        }

    nodes_healthy_now = sum(
        1 for name in node_names
        if str((last_row.get("nodes") or {}).get(name, "unknown")) == "ok"
    )
    overall_ok = sum(1 for _dt, row in parsed if str(row.get("overall", "unknown")) == "ok")
    overall_healthy_pct = round(overall_ok / received * 100, 1)

    return {
        "hours": hours,
        "generated_at": now.isoformat(),
        "generated_at_central": format_central(now),
        "cadence_seconds": cadence,
        "expected_samples": expected,
        "received_samples": received,
        "cadence_coverage_pct": coverage_pct,
        "gap_count": gap_count,
        "overall_current": str(last_row.get("overall", "unknown")),
        "overall_healthy_pct": overall_healthy_pct,
        "nodes_healthy_now": nodes_healthy_now,
        "nodes_total": len(node_names),
        "scan_generated_at": last_dt.isoformat(),
        "scan_generated_at_central": format_central(last_dt),
        "scan_age_seconds": scan_age,
        "scan_is_fresh": scan_fresh,
        "duration_ms": duration_stats,
        "nodes": nodes_out,
        "correlated_incidents": correlated_incidents,
    }
