"""
The status.json contract.

Everything the dashboard renders comes from a StatusSnapshot. Keeping this a
real schema (not an ad-hoc dict) means the poller and the dashboard can evolve
independently as long as they agree on this file. This is the seam.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field
from typing import Any


class Health(str, Enum):
    """Four exposures on the Nexus."""
    OK = "ok"            # developed — green frame
    WARN = "warn"        # safelight — amber frame
    CRIT = "crit"        # red frame
    UNKNOWN = "unknown"  # unexposed — dark frame (unreachable / not run)

    @property
    def rank(self) -> int:
        return {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}[self.value]


# Card / overall rollup severity: a fault outranks a gap-in-knowledge.
# Distinct from Health.rank (which sorts unknown above ok for history math).
_SEVERITY = {"crit": 3, "warn": 2, "ok": 1, "unknown": 0}


class ProbeResult(BaseModel):
    node: str
    kind: str
    health: Health = Health.UNKNOWN
    value: str | None = None          # human-readable measurement ("71%", "1000/Full")
    detail: str | None = None         # one-line explanation, esp. on failure
    latency_ms: int | None = None
    # Backend-observation metadata. Existing UI consumers can ignore
    # these fields, but API callers can now distinguish method/path
    # instead of inferring too much from the display label.
    source_host: str | None = None
    target: str | None = None
    method: str | None = None
    timeout_ms: int | None = None
    error_class: str | None = None


# History v2 (HEALTH-TIMELINE-2): a non-OK probe's kind/health/value/method/
# error_class is retained per node, bounded, so the 24h projection can show a
# real cause instead of just a color. `detail`/`target`/`source_host` are
# deliberately excluded from history — those can carry raw addresses, paths,
# or command output, which has no place in an on-disk/API history file.
HISTORY_SAMPLE_VERSION = 2
HISTORY_MAX_ISSUES_PER_NODE = 6
HISTORY_MAX_METRICS_PER_NODE = 8
_HISTORY_FIELD_MAX_LEN = 60
_METRIC_PCT_KINDS = {"disk", "mem"}


def _bounded(value: str | None, limit: int = _HISTORY_FIELD_MAX_LEN) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= limit else s[:limit]


def _probe_metric(p: ProbeResult) -> dict | None:
    """One deterministic numeric signal per probe, if any: latency_ms takes
    priority (present on the reachability-style probes), else a parsed
    disk/mem percentage. GPU temperature and process RSS are deliberately
    left out — both only exist today baked into a probe's free-form `value`
    display string, not a structured field, so parsing them here would mean
    guessing at a display format rather than reading real data."""
    if p.latency_ms is not None:
        return {"label": _bounded(p.kind), "ms": p.latency_ms}
    if p.kind in _METRIC_PCT_KINDS and p.value and p.value.endswith("%"):
        try:
            pct = int(p.value[:-1])
        except ValueError:
            return None
        return {"label": _bounded(p.kind), "pct": pct}
    return None


class NodeStatus(BaseModel):
    name: str
    display_name: str | None = None   # cosmetic card-heading override; `name` stays the key
    health: Health = Health.UNKNOWN   # worst of the node's probes
    probes: list[ProbeResult] = Field(default_factory=list)


class StatusSnapshot(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    overall: Health = Health.UNKNOWN  # worst node
    duration_ms: int = 0
    nodes: list[NodeStatus] = Field(default_factory=list)
    # Vault-derived "the work" panels. Free-form by design: the poller folds in
    # whatever it could read this sweep; a reader that failed simply omits its
    # key and the dashboard renders that panel as a clean n/a. Never load-bearing
    # for fleet health, so it stays off the rollup and out of history_line.
    work: dict[str, Any] = Field(default_factory=dict)
    # Per-seat availability strip (Worker1/Worker5/Worker2): FREE/BUSY + what each seat last
    # did / is doing, with a historical-median ETA for a live run. Same free-form,
    # never-load-bearing contract as `work`: the poller folds in whatever
    # seatboard.read_seat_board could derive from the from-{seat}/runs metadata this
    # sweep; a failure ships an empty dict and the strip renders n/a. Off the rollup
    # and out of history_line — live status flair, not fleet health.
    seats: dict[str, Any] = Field(default_factory=dict)

    def recompute_rollups(self) -> None:
        """Roll a node up to its CARD color, and the fleet up to overall.

        Card severity is ``crit > warn > ok > unknown``: an unknown sub-probe is
        a gap in knowledge, not a fault, so it must not drag an otherwise-healthy
        card down. A node with >=1 probe that *succeeded* (returned anything but
        unknown) reads as the worst of its succeeded probes — the unknown ones
        keep their own dim "?" inline. A node reads node-level UNKNOWN only when
        NO probe succeeded at all. Overall applies the same ordering across nodes,
        so one unreadable node can't paint the whole fleet dark.
        """
        for node in self.nodes:
            succeeded = [p.health for p in node.probes if p.health != Health.UNKNOWN]
            if succeeded:
                node.health = max(succeeded, key=lambda h: _SEVERITY[h.value])
            elif node.probes:
                node.health = Health.UNKNOWN
        if self.nodes:
            self.overall = max(
                (n.health for n in self.nodes), key=lambda h: _SEVERITY[h.value]
            )

    def history_line(self) -> dict:
        """One compact row for history.jsonl (sparklines, uptime math).

        v1 fields (t/overall/nodes/ms) are the load-bearing contract every
        existing /api/history consumer parses — never rename, retype, or
        drop them. `sample_version`/`issues`/`metrics` are additive (HEALTH-
        TIMELINE-2): a bounded, non-sensitive slice of *why* a node was
        non-OK this sample, so the 24h projection and detail views can show
        a retained cause instead of just a color. Old rows written before
        this change simply lack these keys; readers must treat their
        absence as "cause not retained", never as an error.
        """
        issues: dict[str, list[dict]] = {}
        metrics: dict[str, list[dict]] = {}
        for n in self.nodes:
            node_issues = []
            node_metrics = []
            for p in n.probes:
                if p.health != Health.OK and len(node_issues) < HISTORY_MAX_ISSUES_PER_NODE:
                    node_issues.append({
                        "kind": _bounded(p.kind),
                        "health": p.health.value,
                        "value": _bounded(p.value),
                        "method": _bounded(p.method),
                        "error_class": _bounded(p.error_class),
                    })
                if len(node_metrics) < HISTORY_MAX_METRICS_PER_NODE:
                    metric = _probe_metric(p)
                    if metric is not None:
                        node_metrics.append(metric)
            if node_issues:
                issues[n.name] = node_issues
            if node_metrics:
                metrics[n.name] = node_metrics
        return {
            "t": self.generated_at.isoformat(),
            "overall": self.overall.value,
            "nodes": {n.name: n.health.value for n in self.nodes},
            "ms": self.duration_ms,
            "sample_version": HISTORY_SAMPLE_VERSION,
            "issues": issues,
            "metrics": metrics,
        }
