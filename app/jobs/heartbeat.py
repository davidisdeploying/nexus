"""
The heartbeat job — job #1 of the OS scheduler.

Sweeps every node's probes concurrently, rolls up health, writes the snapshot
to the vault, then pushes an off-box heartbeat so an external service knows the
poller itself is alive. This is the "monitor can't fully watch its own host"
mitigation: worker2 can't report its own death, but the silence of this ping can.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..config import FLEET, settings
from ..models import NodeStatus, ProbeResult, StatusSnapshot
from ..probes import PROBE_DISPATCH
from ..seatboard import read_seat_board
from ..store import write_snapshot
from ..work import gather_work

log = logging.getLogger("nexus.heartbeat")


async def _run_probe(node, kind) -> ProbeResult:
    fn = PROBE_DISPATCH[kind.value]
    try:
        res = await fn(node, settings)
    except Exception as e:  # last-resort guard; probes shouldn't raise
        res = ProbeResult(node=node.name, kind=kind.value,
                          health="unknown", detail=f"probe crashed: {e}")
    # Per-node display-label override (config.Node.labels), keyed by the probe's
    # own kind. Purely cosmetic — lets worker2 render its HTTP tunnel check and its
    # RELAY lane read as named sub-rows ("tunnel · edge", "relay · archive")
    # while the probe logic (incl. the 401-is-OK http rule) stays untouched.
    res.kind = node.labels.get(res.kind, res.kind)
    return res


async def _push_dead_mans_switch(ok: bool) -> None:
    if not settings.heartbeat_ping_url:
        return
    url = settings.heartbeat_ping_url if ok else settings.heartbeat_ping_url.rstrip("/") + "/fail"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(url)
    except Exception as e:
        log.warning("dead-man's-switch ping failed: %s", e)


async def run_heartbeat() -> StatusSnapshot:
    start = time.perf_counter()

    # Fan out every (node, probe) pair concurrently.
    tasks, index = [], []
    for node in FLEET:
        for kind in node.kinds:
            tasks.append(_run_probe(node, kind))
            index.append(node.name)
    results = await asyncio.gather(*tasks)

    # Group results back under their node, preserving fleet order.
    by_node: dict[str, NodeStatus] = {
        n.name: NodeStatus(name=n.name, display_name=n.display_name) for n in FLEET
    }
    for name, res in zip(index, results):
        by_node[name].probes.append(res)

    snap = StatusSnapshot(nodes=list(by_node.values()))
    snap.recompute_rollups()

    # Fold in the vault-derived "the work" panels. gather_work is self-isolating
    # (each reader guarded; worst case an empty dict), and it runs off local
    # files, so a slow/blocking read can't stall the fleet probes — those already
    # completed above. A last-resort guard keeps even a catastrophic failure from
    # touching fleet health.
    try:
        snap.work = await asyncio.to_thread(gather_work)
    except Exception as e:
        log.warning("gather_work failed, shipping fleet-only snapshot: %s", e)
        snap.work = {}

    # Fold in the per-seat availability strip (Worker1/Worker5/Worker2 FREE/BUSY + ETA).
    # Same posture as work: derived off local from-{seat}/runs metadata in
    # this worker thread (never the probe path), fully isolated, off the rollup.
    # A total failure ships the snapshot without the strip, untouched.
    try:
        snap.seats = await asyncio.to_thread(read_seat_board)
    except Exception as e:
        log.warning("read_seat_board failed, shipping snapshot without seats: %s", e)
        snap.seats = {}

    snap.duration_ms = int((time.perf_counter() - start) * 1000)

    write_snapshot(snap)
    # "alive" = the poller ran; fleet-crit is shown on the dashboard and also
    # trips /fail so you get paged on a hard fleet problem, not just poller death.
    await _push_dead_mans_switch(ok=snap.overall.value != "crit")

    log.info("heartbeat: overall=%s in %dms", snap.overall.value, snap.duration_ms)
    return snap


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_heartbeat())
