"""
Gallery library/ML heartbeat for the Nexus jobs panel.

This is deliberately read-only against Charlie: it samples Gallery containers,
Postgres job tables, recent ML errors, and GPU ownership, then writes one generic
heartbeat file that the existing jobs panel already knows how to render.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings

log = logging.getLogger("nexus.gallery")

JOB_ID = "gallery-library-scan"
HOST = "charlie"
REMOTE_TIMEOUT_SECONDS = 25

# While the last-written heartbeat is terminal (done/failed), a real Gallery
# scan can only be detected again by actually SSH-ing in and sampling — so we
# can't stop polling outright without going blind to a future scan. Instead
# we throttle to one discovery poll per this many seconds; a genuine active
# scan (state flips off terminal) resumes the normal 60s cadence immediately
# on the very next tick, since the terminal check below no longer matches.
TERMINAL_POLL_INTERVAL_SECONDS = 15 * 60

# Process-lifetime cache of the last time we actually SSH'd (not persisted —
# a restart just costs one extra early discovery poll, never a correctness
# problem). None means "poll now" (covers process start).
_last_poll_ts: float | None = None

# Dual-GPU split on charlie, keyed by nvidia-smi uuid prefix (2026-07-09):
# the ML GPU (GPU0) does ML inference, the video GPU (GPU1) does video nvenc.
def _gpu_roles() -> dict[str, str]:
    """uuid-prefix -> role, from NEXUS_GPU_ROLES.

    Format: "GPU-abcd1234=ML,GPU-ef567890=video". Unset is fine — roles
    then fall back to the device name nvidia-smi reports.
    """
    roles: dict[str, str] = {}
    for item in os.environ.get("NEXUS_GPU_ROLES", "").split(","):
        prefix, sep, role = item.partition("=")
        if sep and prefix.strip():
            roles[prefix.strip()] = role.strip()
    return roles


_GPU_ROLES: dict[str, str] = _gpu_roles()

# The role whose metrics feed the back-compat scalar fields below.
_PRIMARY_GPU_ROLE = os.environ.get("NEXUS_PRIMARY_GPU_ROLE", "ML")

# Module-global rolling-window sample cache for per-queue rate/eta, keyed by
# queue name -> deque of (ts, done), maxlen=6 (~5 min at the 60s scheduler
# cadence). Persists across ticks within this process; reset on process
# restart (rate/eta report null until >=2 samples are in the window).
_QUEUE_WINDOWS: dict[str, collections.deque[tuple[float, int]]] = {}

# Bull queue name for each dashboard queue, verified against a live redis
# --scan on charlie (gallery_bull:<name>:meta) on 2026-07-09. If Gallery renames
# a queue this mapping goes stale silently, so it's an explicit dict rather
# than a derived transform.
_BULL_QUEUE_NAMES: dict[str, str] = {
    "metadata": "metadataExtraction",
    "faces": "facialRecognition",
    "ocr": "ocr",
    "smart": "smartSearch",
    "thumbnails": "thumbnailGeneration",
    "video": "videoConversion",
}


REMOTE_SCRIPT = r"""
import json
import subprocess
import time


def run(args, timeout=8):
    try:
        p = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        return {"rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()}
    except Exception as e:
        return {"rc": 255, "out": "", "err": type(e).__name__ + ": " + str(e)}


def first_int(text):
    try:
        return int(float(str(text).strip()))
    except Exception:
        return None


data = {"ts": time.time()}

ps = run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"], timeout=6)
data["docker_ps_rc"] = ps["rc"]
data["containers"] = ps["out"]

gpu = run([
    "nvidia-smi",
    "--query-gpu=index,name,uuid,utilization.gpu,utilization.encoder,utilization.decoder,memory.used,memory.total,temperature.gpu",
    "--format=csv,noheader,nounits",
], timeout=6)
data["gpu_rc"] = gpu["rc"]
gpus = []
for line in gpu["out"].splitlines():
    parts = [p.strip() for p in line.split(",")]
    if len(parts) >= 9:
        gpus.append({
            "index": first_int(parts[0]),
            "name": parts[1],
            "uuid": parts[2],
            "util": first_int(parts[3]),
            "enc": first_int(parts[4]),
            "dec": first_int(parts[5]),
            "mem_used": first_int(parts[6]),
            "mem_total": first_int(parts[7]),
            "temp": first_int(parts[8]),
        })
data["gpus"] = gpus
# Back-compat scalar fields — mapped to the primary GPU so existing metric
# tiles keep working for dashboards that have not picked up gpus[].
_primary_prefixes = [p for p, r in _GPU_ROLES.items() if r == _PRIMARY_GPU_ROLE]
ml_gpu = next(
    (g for g in gpus if any(g["uuid"].startswith(p) for p in _primary_prefixes)),
    gpus[0] if gpus else None,
)
if ml_gpu:
    data["gpu_util_pct"] = ml_gpu["util"]
    data["gpu_mem_mb"] = ml_gpu["mem_used"]
    data["gpu_total_mb"] = ml_gpu["mem_total"]
    data["gpu_temp_c"] = ml_gpu["temp"]

apps = run([
    "nvidia-smi",
    "--query-compute-apps=pid,process_name,used_memory",
    "--format=csv,noheader,nounits",
], timeout=6)
data["gpu_apps"] = apps["out"]
ml_vram = 0
for line in apps["out"].splitlines():
    if "python" in line.lower():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            ml_vram = max(ml_vram, first_int(parts[2]) or 0)
data["gallery_ml_vram_mb"] = ml_vram

# CUDA/onnxruntime providers are fixed at container startup, so re-running an
# onnxruntime inference smoke-test every 60s just to read them back was pure
# GPU/CPU load with no new information. gallery_machine_learning already ships
# a Docker HEALTHCHECK (healthcheck.py) that hits its own /ping every ~30s and
# is cached by the docker engine, so reading .State.Health.Status is a free
# read of a check that's already running rather than a new one.
health = run([
    "docker", "inspect", "-f", "{{.State.Health.Status}}", "gallery_machine_learning",
], timeout=6)
data["ml_health_rc"] = health["rc"]
data["ml_health_status"] = health["out"].strip()

sql = "\n".join([
    'with latest_library as (',
    '  select id, name from library order by "updatedAt" desc nulls last limit 1',
    '), base as (',
    '  select a.id from asset a join latest_library l on a."libraryId" = l.id',
    '  where a."deletedAt" is null',
    ')',
    "select 'library_name', (select name from latest_library)",
    "union all select 'library_assets', count(*)::text from base",
    'union all select \'metadata_done\', count(*)::text from base b join asset_job_status s on s."assetId" = b.id where s."metadataExtractedAt" is not null',
    'union all select \'faces_done\', count(*)::text from base b join asset_job_status s on s."assetId" = b.id where s."facesRecognizedAt" is not null',
    'union all select \'ocr_done\', count(*)::text from base b join asset_job_status s on s."assetId" = b.id where s."ocrAt" is not null',
    'union all select \'smart_done\', count(distinct ss."assetId")::text from base b join smart_search ss on ss."assetId" = b.id',
    'union all select \'thumbnails_done\', count(distinct af."assetId")::text from base b join asset_file af on af."assetId" = b.id and af.type = \'thumbnail\'',
    'union all select \'video_total\', count(*)::text from base b join asset a on a.id = b.id where a.type = \'VIDEO\'',
    'union all select \'video_done\', count(distinct af."assetId")::text from base b join asset a on a.id = b.id join asset_file af on af."assetId" = b.id and af.type = \'encoded_video\' where a.type = \'VIDEO\'',
    'union all select \'total_assets\', count(*)::text from asset where "deletedAt" is null;',
])
db = run(["docker", "exec", "gallery_postgres", "psql", "-U", "postgres", "-d", "gallery", "-Atc", sql], timeout=12)
data["db_rc"] = db["rc"]
data["db_err"] = db["err"][:300]
for line in db["out"].splitlines():
    if "|" not in line:
        continue
    k, v = line.split("|", 1)
    data[k] = v

logs = run([
    "docker", "logs", "--since", "30m", "gallery_server",
], timeout=8)
recent = logs["out"] + "\n" + logs["err"]
data["recent_ml_errors"] = recent.count("Machine learning request") + recent.count("Unable to run job handler")
data["recent_metadata_timeouts"] = recent.count("Error reading exif data")

bull_scan = run(["docker", "exec", "gallery_redis", "redis-cli", "--scan", "--pattern", "gallery_bull:*:meta"], timeout=8)
data["bull_scan_rc"] = bull_scan["rc"]
bull_queue_names = sorted({
    line.split(":", 2)[1]
    for line in bull_scan["out"].splitlines()
    if line.startswith("gallery_bull:") and line.endswith(":meta")
})
data["bull_queue_names"] = bull_queue_names

bull_depths = {}
for qname in bull_queue_names:
    waiting = run(["docker", "exec", "gallery_redis", "redis-cli", "LLEN", f"gallery_bull:{qname}:wait"], timeout=6)
    active = run(["docker", "exec", "gallery_redis", "redis-cli", "LLEN", f"gallery_bull:{qname}:active"], timeout=6)
    bull_depths[qname] = {
        "waiting": first_int(waiting["out"]) if waiting["rc"] == 0 else None,
        "active": first_int(active["out"]) if active["rc"] == 0 else None,
    }
data["bull_depths"] = bull_depths

print(json.dumps(data, separators=(",", ":")))
"""


def _num(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_prev_heartbeat(path: Path) -> dict[str, Any] | None:
    """The last-written heartbeat, or None on first-ever run / any read/parse
    problem — the identity logic below treats None exactly like "no prior
    attempt", so a missing/corrupt file just mints a fresh attempt rather than
    raising."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _is_new_attempt(prev: dict[str, Any] | None, state: str, agg_done: int,
                    queues: list[dict[str, Any]]) -> bool:
    """True only when there's actual evidence this is a DIFFERENT scan attempt
    than the one `prev` recorded, not just another sample of the same one:
      - no prior heartbeat at all (cold start — nothing to carry forward)
      - the prior attempt was terminal (done/failed) and this sample is running
        again (terminal -> active transition)
      - the prior attempt was terminal and the done-count has gone backwards
        (a real reset, not just noise — counts never legitimately decrease)
      - the prior attempt was terminal with every queue idle, and this sample
        shows queue activity again (idle -> active reactivation), even if the
        top-level `state` still happens to compute as "done" this tick (the
        metadata-only done condition can hold while other queues are re-fed)
    A prior heartbeat that came from the exception fallback (no `queues` key)
    can't support the reset/reactivation checks, so those are skipped rather
    than guessed — a transient SSH hiccup must never masquerade as a new scan.
    """
    if not prev:
        return True
    prev_state = str(prev.get("state") or "")
    if prev_state not in ("done", "failed"):
        return False
    if state == "running":
        return True
    prev_done = _num(prev.get("done"))
    if prev_done is not None and agg_done < prev_done:
        return True
    prev_queues = prev.get("queues")
    if isinstance(prev_queues, list):
        prev_activity = sum(
            (q.get("waiting") or 0) + (q.get("active") or 0)
            for q in prev_queues if isinstance(q, dict)
        )
        curr_activity = sum((q.get("waiting") or 0) + (q.get("active") or 0) for q in queues)
        if prev_activity == 0 and curr_activity > 0:
            return True
    return False


def _queue_entry(
    name: str,
    done: int,
    total: int,
    now: float,
    bull_depths: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    done = max(0, done)
    total = max(0, total)
    pct = round(100.0 * done / total, 1) if total else None
    remaining = max(total - done, 0)

    window = _QUEUE_WINDOWS.setdefault(name, collections.deque(maxlen=6))
    window.append((now, done))

    rate: float | None = None
    eta: float | None = None
    if len(window) >= 2:
        oldest_ts, oldest_done = window[0]
        newest_ts, newest_done = window[-1]
        dt_min = (newest_ts - oldest_ts) / 60.0
        if dt_min > 0:
            rate = max(0.0, (newest_done - oldest_done) / dt_min)

    if remaining == 0:
        rate = 0.0
        eta = None
    elif rate:
        eta = remaining / rate

    entry = {
        "name": name,
        "done": done,
        "total": total,
        "pct": pct,
        "remaining": remaining,
        "rate": round(rate, 2) if rate is not None else None,
        "eta": round(eta, 1) if eta is not None else None,
        "waiting": None,
        "active": None,
    }

    bull_name = _BULL_QUEUE_NAMES.get(name)
    depths = bull_depths.get(bull_name) if bull_depths and bull_name else None
    if depths:
        entry["waiting"] = depths.get("waiting")
        entry["active"] = depths.get("active")

    return entry


def _run_remote() -> dict[str, Any]:
    encoded_script = base64.b64encode(REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
    remote_code = f"import base64; exec(base64.b64decode('{encoded_script}'))"
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        HOST,
        f"python3 -c {shlex.quote(remote_code)}",
    ]
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=REMOTE_TIMEOUT_SECONDS)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or f"ssh rc={p.returncode}")[:500])
    return json.loads(p.stdout)


def _build_heartbeat(sample: dict[str, Any], prev: dict[str, Any] | None = None) -> dict[str, Any]:
    now = time.time()
    total = _num(sample.get("library_assets")) or 0
    metadata_done = _num(sample.get("metadata_done")) or 0
    faces_done = _num(sample.get("faces_done")) or 0
    ocr_done = _num(sample.get("ocr_done")) or 0
    smart_done = _num(sample.get("smart_done")) or 0
    ml_errors = _num(sample.get("recent_ml_errors")) or 0
    metadata_timeouts = _num(sample.get("recent_metadata_timeouts")) or 0
    gpu_util = _num(sample.get("gpu_util_pct"))
    gpu_mem = _num(sample.get("gpu_mem_mb"))
    gpu_total = _num(sample.get("gpu_total_mb"))
    ml_vram = _num(sample.get("gallery_ml_vram_mb")) or 0

    # Providers are static per container lifecycle; container health (Docker's
    # own cached /ping check) is the cheap liveness proxy for "ML server is up
    # and CUDA-ready", replacing a per-cycle onnxruntime inference smoke-test.
    ml_health_status = str(sample.get("ml_health_status") or "")
    cuda_ready = ml_health_status == "healthy"
    containers = str(sample.get("containers") or "")
    containers_ok = "gallery_server" in containers and "gallery_machine_learning" in containers

    bull_depths = sample.get("bull_depths")
    if not isinstance(bull_depths, dict):
        bull_depths = {}
    # `total` (library_assets) undercounts by design (excludes hidden Live Photo
    # motion parts and a handful of assets that will never enrich), so
    # metadata_done can permanently fall short of it; a fully drained pipeline
    # (every Bull queue idle) is the other, reliable signal the scan is finished.
    all_queues_idle = bool(bull_depths) and all(
        (bull_depths.get(bull_name) or {}).get("active") == 0
        and (bull_depths.get(bull_name) or {}).get("waiting") == 0
        for bull_name in _BULL_QUEUE_NAMES.values()
    )

    state = "running"
    if not containers_ok or sample.get("db_rc") != 0 or sample.get("ml_health_rc") != 0:
        state = "failed"
    elif ml_errors == 0 and ((total and metadata_done >= total) or all_queues_idle):
        state = "done"

    name = str(sample.get("library_name") or "latest library")
    message_bits = [
        f"{name}: metadata {metadata_done}/{total}" if total else f"{name}: waiting for assets",
        f"smart {smart_done}",
        f"faces {faces_done}",
        f"ocr {ocr_done}",
        "CUDA ready" if cuda_ready else "CUDA not ready",
    ]
    if ml_errors:
        message_bits.append(f"ML errors 30m={ml_errors}")
    if metadata_timeouts:
        message_bits.append(f"EXIF timeouts 30m={metadata_timeouts}")

    metrics: dict[str, Any] = {
        "library_assets": total,
        "metadata_done": metadata_done,
        "smart_done": smart_done,
        "faces_done": faces_done,
        "ocr_done": ocr_done,
        "ml_errors_30m": ml_errors,
        "exif_timeouts_30m": metadata_timeouts,
        "ml_vram_mb": ml_vram,
    }
    if gpu_util is not None:
        metrics["gpu_util_pct"] = gpu_util
    if gpu_mem is not None:
        metrics["gpu_mem_mb"] = gpu_mem
    if gpu_total is not None:
        metrics["gpu_total_mb"] = gpu_total

    gpus: list[dict[str, Any]] = []
    for g in sample.get("gpus") or []:
        if not isinstance(g, dict):
            continue
        uuid = str(g.get("uuid") or "")
        role = next((r for prefix, r in _GPU_ROLES.items() if uuid.startswith(prefix)), str(g.get("name") or "gpu"))
        gpus.append({
            "role": role,
            "name": g.get("name"),
            "util": _num(g.get("util")),
            "enc": _num(g.get("enc")),
            "dec": _num(g.get("dec")),
            "mem_used": _num(g.get("mem_used")),
            "mem_total": _num(g.get("mem_total")),
            "temp": _num(g.get("temp")),
        })

    thumbnails_done = _num(sample.get("thumbnails_done")) or 0
    video_done = _num(sample.get("video_done")) or 0
    video_total = _num(sample.get("video_total")) or 0

    queues = [
        _queue_entry("metadata", metadata_done, total, now, bull_depths),
        _queue_entry("faces", faces_done, total, now, bull_depths),
        _queue_entry("ocr", ocr_done, total, now, bull_depths),
        _queue_entry("smart", smart_done, total, now, bull_depths),
        _queue_entry("thumbnails", thumbnails_done, total, now, bull_depths),
        _queue_entry("video", video_done, video_total, now, bull_depths),
    ]

    # Top-level done/total/unit are the aggregate across all queues, so the
    # Nexus progress bar reflects the whole ML pipeline rather than just the
    # metadata queue. Fall back to the metadata-only figures if queues is
    # somehow empty so the bar never goes blank.
    if queues:
        agg_done = sum(q["done"] for q in queues)
        agg_total = sum(q["total"] for q in queues)
        agg_unit = "tasks"
    else:
        agg_done = metadata_done
        agg_total = total
        agg_unit = "assets"

    # Identity: stable across ticks unless there's real evidence this is a
    # different attempt (see _is_new_attempt) — NOT re-derived from wall-clock
    # "now"/"today" every tick like the old run_id/started did (that's what
    # made a 9-day-finished scan look like it restarted daily). ended_at is
    # the flip side: stamped once on the tick that transitions into a terminal
    # state, then carried forward unchanged for as long as the attempt stays
    # terminal; `ts` alone remains the raw "I sampled at this instant" field.
    new_attempt = _is_new_attempt(prev, state, agg_done, queues)
    if new_attempt:
        run_id = f"{JOB_ID}-{int(now * 1000)}"
        started = _iso(now)
        ended_at = None
    else:
        run_id = str(prev.get("run_id") or f"{JOB_ID}-{int(now * 1000)}")
        started = prev.get("started") or _iso(now)
        ended_at = prev.get("ended_at")
    if state in ("done", "failed"):
        if not ended_at:
            ended_at = _iso(now)
    else:
        ended_at = None

    return {
        "job": JOB_ID,
        "host": HOST,
        "kind": "job",
        "run_id": run_id,
        "started": started,
        "ts": now,
        "ended_at": ended_at,
        "state": state,
        "done": agg_done,
        "total": agg_total,
        "unit": agg_unit,
        "metrics": metrics,
        "queues": queues,
        "gpus": gpus,
        "message": " · ".join(message_bits),
    }


def is_settled_terminal(prev: dict[str, Any] | None) -> bool:
    """True only for a terminal record that came from an actual completed
    sample (has `queues`, `ended_at` stamped) — NOT from the exception
    fallback below, which writes state="failed" on a bare SSH/connectivity
    error with no sample behind it. A transient connectivity blip must keep
    polling at the normal cadence; only a confirmed done/failed scan result
    is safe to throttle down to discovery-poll cadence. Shared with
    health_watch._eval_heartbeat_stale so a settled-terminal Gallery record is
    also exempt from age-based staleness alarms, not just discovery-poll
    throttling."""
    return bool(
        prev
        and str(prev.get("state")) in ("done", "failed")
        and prev.get("ended_at")
        and isinstance(prev.get("queues"), list)
    )


async def run_gallery_heartbeat() -> None:
    global _last_poll_ts
    path = settings.heartbeats_dir / f"{JOB_ID}.json"
    prev = await asyncio.to_thread(_load_prev_heartbeat, path)
    now = time.time()
    if (is_settled_terminal(prev) and _last_poll_ts is not None
            and (now - _last_poll_ts) < TERMINAL_POLL_INTERVAL_SECONDS):
        # Cheap local no-op: the last confirmed sample is still terminal and
        # we're within the discovery-poll cooldown — skip the SSH round-trip
        # entirely and leave the heartbeat file untouched (no rewrite) rather
        # than re-stamping `ts` on a tick that learned nothing new. A genuine
        # active scan resumes 60s polling on its own the next time we DO poll
        # and observe a non-terminal state.
        return
    _last_poll_ts = now
    try:
        sample = await asyncio.to_thread(_run_remote)
        heartbeat = _build_heartbeat(sample, prev)
    except Exception as e:
        log.warning("gallery heartbeat failed: %s", e)
        fail_ts = time.time()
        heartbeat = {
            "job": JOB_ID,
            "host": HOST,
            "ts": fail_ts,
            "state": "failed",
            "message": f"Gallery heartbeat failed: {type(e).__name__}: {str(e)[:160]}",
            "metrics": {},
        }
        # A bare SSH/connectivity failure is not evidence of a new attempt —
        # carry the prior identity forward so it doesn't masquerade as a scan
        # restart the next time a real sample succeeds (see _is_new_attempt).
        if prev and prev.get("run_id"):
            heartbeat["run_id"] = prev["run_id"]
        if prev and prev.get("started"):
            heartbeat["started"] = prev["started"]
        heartbeat["ended_at"] = (prev.get("ended_at") if prev else None) or _iso(fail_ts)
    await asyncio.to_thread(_atomic_write, path, heartbeat)
    log.info("gallery heartbeat wrote %s", path)
