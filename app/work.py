"""
Jobs and relay-run-log panels folded into status.json alongside the fleet.

The live-fleet probes answer "is the machinery up?"; these cover the relay run
log and jobs. Vault-note browsing belongs to the separate Compendium product,
not Nexus.

Hard rule: every reader is isolated. A parse or read failure yields a graceful
empty/"n/a" value and NEVER aborts the sweep or breaks the page. `gather_work`
runs each reader under its own guard, so one unreadable file dims one panel and
nothing else. Nothing here is load-bearing for fleet health.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import job_history
from .config import settings
from .seats import RUN_SOURCE_IDS, RUN_SOURCE_TO_CARD

log = logging.getLogger("nexus.work")

# ~/Vaults — the corpus root. loupe-vault is one child; wiki/, homelab-vault/,
# developed-vault/ are its siblings.
LOUPE = settings.vault_root
# The relay's from-{seat}/{runs,transcripts,recon} lanes live under a SEPARATE
# root from LOUPE (split 2026-07-10) — heartbeats stay on LOUPE/vault_root.
RELAY = settings.relay_root

# GPU-scan status files: from-worker3/runs/<run-token>/progress.json — Syncthing-carried
# so worker2 reads them LOCALLY. The dir is globbed once per sweep and each file matched
# to a job card by its run_token slug (see _read_progress_json). Reboot-durable; NOT
# the throwaway :8677 status server.
PROGRESS_RUNS_DIR = RELAY / "from-worker3" / "runs"


def _read(path: Path, limit_bytes: int = 400_000) -> str:
    """Read a text file defensively. Empty string on any problem."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit_bytes)
    except Exception:
        return ""



def _parse_epoch(value: Any) -> float | None:
    """Epoch seconds from an int/float timestamp or an ISO8601 string (as
    written by the various heartbeat producers' `ts`/`started`/`ended_at`
    fields). None for anything else."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return None
    return None


def _rel_age(mtime: float) -> str:
    """Human relative age from an epoch mtime."""
    secs = max(0, time.time() - mtime)
    if secs < 90:
        return "just now"
    mins = secs / 60
    if mins < 90:
        return f"{mins:.0f}m ago"
    hrs = mins / 60
    if hrs < 36:
        return f"{hrs:.0f}h ago"
    return f"{hrs / 24:.0f}d ago"


# --------------------------------------------------------------------------- #
# 7. Relay run log  — from-{worker1,worker5,worker2}/runs/*
# --------------------------------------------------------------------------- #
def read_relay_runs() -> dict[str, Any]:
    """Last few relay runs across the seats. State from the `done` sentinel
    (its content is the exit code) with status.json as fallback; age from the
    run dir mtime."""
    rows: list[dict[str, Any]] = []
    for seat in RUN_SOURCE_IDS:
        base = RELAY / f"from-{seat}" / "runs"
        try:
            dirs = [d for d in base.iterdir() if d.is_dir()]
        except Exception:
            continue
        for d in dirs:
            try:
                mtime = d.stat().st_mtime
            except Exception:
                continue
            done_f = d / "done"
            state = "running"
            if done_f.exists():
                code = _read(done_f, 32).strip()
                state = "done" if code in ("", "0") else "died"
            else:
                # no sentinel: trust status.json if it looks stale
                sj = _read(d / "status.json", 2000)
                sm = re.search(r'"status"\s*:\s*"([^"]+)"', sj)
                st = sm.group(1) if sm else ""
                if st and st != "running":
                    state = st
                elif (time.time() - mtime) > 6 * 3600:
                    state = "died"  # "running" for >6h with no sentinel = orphaned
            rows.append({
                "token": d.name,
                "seat": RUN_SOURCE_TO_CARD.get(seat, seat),
                "state": state,
                "age": _rel_age(mtime),
                "_mtime": mtime,
            })
    if not rows:
        return {}
    rows.sort(key=lambda r: r["_mtime"], reverse=True)
    for r in rows:
        r.pop("_mtime", None)
    return {"runs": rows[:10]}


# --------------------------------------------------------------------------- #
# 8. GPU-job heartbeat  — the video-cull scan (run_full.py) on charlie
# --------------------------------------------------------------------------- #
# PULL model: one batched, read-only ssh to charlie per sweep gathers process
# liveness, GPU state, and progress. The running job is NEVER touched. Layered by
# preference: heartbeat.json (future) -> log tail -> nvidia-smi only. Fully
# isolated — any ssh/read/parse failure yields state="unknown" and dims one
# panel; it can never abort the sweep (gather_work guards it too).

# The single remote script. Emits @@MARKER@@-delimited sections so one round-trip
# collects everything. The [f] bracket in the pgrep pattern (from settings) keeps
# pgrep from matching the ssh command that carries the pattern.
_GPU_REMOTE = r"""
LOG='{log}'
HB='{hb}'
PID=$(pgrep -f '{pgrep}' | head -1)
echo "@@PID@@ ${{PID}}"
[ -n "$PID" ] && echo "@@ETIMES@@ $(ps -o etimes= -p $PID 2>/dev/null | tr -d ' ')"
echo "@@GPU@@ $(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)"
echo "@@APPS@@"
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null
echo "@@HB@@"
[ -f "$HB" ] && {{ echo "mtime=$(stat -c %Y "$HB" 2>/dev/null)"; cat "$HB" 2>/dev/null; }}
echo "@@LOGSTAT@@ $([ -f "$LOG" ] && stat -c %Y "$LOG" 2>/dev/null)"
echo "@@LOGTAIL@@"
[ -f "$LOG" ] && grep -aE 'scanned [0-9]+ \(db' "$LOG" 2>/dev/null | tail -n 3
"""

# One progress line:
# [2026-07-04T04:06:54] scanned 3229 (db 28309/35133) ok=... win=20.19cpm med_vlm=3194ms
_PROG_TS = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]")
_PROG_DB = re.compile(r"\(db\s+(\d+)\s*/\s*(\d+)\)")
_PROG_SCAN = re.compile(r"scanned\s+(\d+)")
_PROG_RATE = re.compile(r"win=([\d.]+)cpm")


def _ssh_gpu_probe() -> str | None:
    """One batched, non-interactive, read-only ssh. Returns stdout or None on
    any failure (bad host, timeout, ssh missing). Never raises."""
    host = settings.gpu_job_ssh_host
    if not host:
        return None
    script = _GPU_REMOTE.format(
        log=settings.gpu_job_log,
        hb=settings.gpu_job_heartbeat,
        pgrep=settings.gpu_job_pgrep,
    )
    to = settings.ssh_timeout_seconds
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
             "-o", f"ConnectTimeout={to}",
             "-o", "StrictHostKeyChecking=accept-new",
             host, "bash -s"],
            input=script, capture_output=True, text=True, timeout=to + 6,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0 and not proc.stdout.strip():
        return None
    return proc.stdout


def _parse_gpu_sections(out: str) -> dict[str, Any]:
    """Split the @@MARKER@@ blob into a flat dict of raw section text."""
    sections: dict[str, list[str]] = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"@@([A-Z]+)@@\s?(.*)$", line)
        if m:
            cur = m.group(1)
            sections[cur] = []
            if m.group(2):
                sections[cur].append(m.group(2))
        elif cur is not None:
            sections[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _fmt_uptime(secs: float) -> str:
    secs = int(max(0, secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _fmt_eta(minutes: float) -> str:
    if minutes <= 0 or minutes != minutes:  # nan-safe
        return ""
    return _fmt_uptime(minutes * 60)


# --- generic job helpers ----------------------------------------------------
_STATE_RANK = {"running": 0, "stalled": 1, "failed": 2,
               "unknown": 3, "done": 4, "ended": 5}


def _num(x):
    """Coerce to a number or None; never raises."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return x
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _spark(series: list[float], fixed_max: float | None = None) -> str:
    """Polyline points over a 100x24 viewBox for a numeric series. Auto-scales
    to the series' own min..max unless fixed_max is given (e.g. 100 for a %)."""
    if len(series) < 2:
        return ""
    lo = 0.0 if fixed_max is not None else min(series)
    hi = fixed_max if fixed_max is not None else max(series)
    span = (hi - lo) or 1.0
    step = 100.0 / (len(series) - 1)
    pts = []
    for i, v in enumerate(series):
        f = min(1.0, max(0.0, (v - lo) / span))
        pts.append(f"{i * step:.1f},{23.0 - f * 22.0:.1f}")
    return " ".join(pts)


# Per-job sparkline rings from the previous snapshot, loaded ONCE per sweep (in
# read_jobs) so each job's _finish_job reads it without re-parsing the snapshot.
_PREV_RINGS: dict[str, list[float]] = {}


def _load_prev_rings() -> dict[str, list[float]]:
    """Read the previous snapshot's work.jobs and pull each job's ring forward.
    Isolated: any failure yields empty rings (sparklines just start fresh)."""
    rings: dict[str, list[float]] = {}
    try:
        from .store import read_snapshot          # local import: avoid import cycle
        prev = read_snapshot()
        if prev and isinstance(prev.work, dict):
            for j in prev.work.get("jobs") or []:
                if isinstance(j, dict) and j.get("job") and isinstance(j.get("ring"), list):
                    rings[str(j["job"])] = [x for x in j["ring"] if isinstance(x, (int, float))]
    except Exception:
        return {}
    return rings


def _derive_state(state_hint, done, total, beat_age, stale, alive=None,
                  cooling_state=None, thermal_guard_holdoff=None,
                  ended_at=None) -> str:
    """Unify the state machine across the file source and the adapters.
      fresh + progress<total → running ; state:done or done>=total → done ;
      state:failed → failed ; explicit stopped/cancelled + ended_at → ended ;
      alive but ts stale > ~3 sweeps → stalled ;
      adapter process gone & not done → ended ; no-progress monitor heartbeat
      (identified by a cooling_state field, e.g. thermal-guard) → done when
      healthy, failed on guard holdoff, else falls through unchanged."""
    hint = (state_hint or "").strip().lower()
    if hint == "failed":
        return "failed"
    if hint == "done" or (done is not None and total and done >= total):
        return "done"
    if ended_at and hint in {"stopped", "cancelled", "canceled", "ended"}:
        return "ended"
    if alive is False:                      # adapter: process vanished, not done
        return "ended"
    if beat_age is not None and beat_age > stale:
        return "stalled"
    if hint == "stalled":
        return "stalled"
    if not total and cooling_state is not None:
        if thermal_guard_holdoff:
            return "failed"
        if cooling_state == "ok":
            return "done"
    if hint in ("running", "", None):
        return "running"
    return hint or "running"


def _finish_job(job: dict[str, Any], now: float, spark_series: list[float] | None,
                spark_key: str | None, spark_label: str | None,
                spark_fixed_max: float | None) -> dict[str, Any]:
    """Attach the ring buffer + sparkline to a job dict, keyed by job id, using
    the previous snapshot's ring. spark_series is this sweep's sample(s)."""
    ring = list(_PREV_RINGS.get(job.get("job", ""), []))
    if spark_series:
        ring.extend(float(v) for v in spark_series)
    ring = ring[-settings.job_ring_len:]
    job["ring"] = ring
    if len(ring) >= 2:
        job["spark"] = _spark(ring, fixed_max=spark_fixed_max)
        if spark_key:
            job["spark_key"] = spark_key      # attach spark to this metric tile
        if spark_label:
            job["spark_label"] = spark_label  # else render as a standalone spark
    return job


# --------------------------------------------------------------------------- #
# 8a. File source — heartbeats/<job>.json (general, zero-config)
# --------------------------------------------------------------------------- #
def _job_from_file(path: Path, now: float) -> dict[str, Any]:
    """Parse one heartbeat file into a generic job dict. A malformed/unreadable
    file yields a lone state='unknown' card — it never breaks the others."""
    raw = _read(path, 64_000)
    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = now
    job_id = path.stem
    try:
        hb = json.loads(raw) if raw.strip() else None
    except (json.JSONDecodeError, ValueError):
        hb = None
    if not isinstance(hb, dict):
        return {"job": job_id, "state": "unknown", "source": "file",
                "detail": "unreadable heartbeat file"}

    jid = str(hb.get("job") or job_id)
    job: dict[str, Any] = {"job": jid, "source": "file"}
    if hb.get("host"):
        job["host"] = str(hb["host"])[:40]
    # attempt identity (helper-stamped): run_id groups a process's beats into one
    # attempt; started marks that attempt's first beat. The recorder uses both.
    if hb.get("run_id"):
        job["run_id"] = str(hb["run_id"])[:64]
    if hb.get("started"):
        job["started"] = hb["started"]
    if hb.get("ended_at"):
        job["ended_at"] = hb["ended_at"]
    if hb.get("kind"):
        job["kind"] = str(hb["kind"])[:16]

    # freshness: ts wins, file mtime is the backstop. This is the RUNNING-job
    # freshness signal and also feeds _derive_state's staleness check below —
    # a terminal job's DISPLAYED age is overridden from ended_at further down,
    # once state is known, but state derivation itself always uses this one.
    beat_age = _parse_epoch(hb.get("ts"))
    if beat_age is not None:
        beat_age = max(0.0, now - beat_age)
    if beat_age is None:
        beat_age = max(0.0, now - mtime)
    job["beat_age_s"] = int(beat_age)
    job["beat_age"] = _rel_age(now - beat_age)

    done = _num(hb.get("done"))
    total = _num(hb.get("total"))
    if done is not None and total:
        job["done"], job["total"] = int(done), int(total)
        job["pct"] = round(100.0 * done / total, 1) if total else None
    rate = _num(hb.get("rate"))
    if rate is not None:
        job["rate"] = round(rate, 1)
        job["unit"] = str(hb.get("unit") or "rate")[:16]
    # eta: explicit string/number, else derive from rate
    if hb.get("eta") not in (None, ""):
        eta = hb["eta"]
        job["eta"] = _fmt_eta(float(eta)) if isinstance(eta, (int, float)) else str(eta)[:16]
    elif rate and total and done is not None:
        try:
            job["eta"] = _fmt_eta((total - done) / rate)
        except (ValueError, ZeroDivisionError):
            pass
    metrics = hb.get("metrics")
    if isinstance(metrics, dict):
        # Keep numbers as numbers (value + sparkline) and short strings as
        # strings (e.g. last_path → truncated mono on the card). Drop obvious
        # envelope/noise keys so they never render as a metric tile.
        noise = {"run_token", "started_at", "updated_at", "status", "pid"}
        clean: dict[str, Any] = {}
        for k, v in metrics.items():
            ks = str(k)[:24]
            if ks in noise:
                continue
            n = _num(v)
            if n is not None:
                clean[ks] = n
            elif isinstance(v, str) and v.strip():
                clean[ks] = v.strip()[:200]
        if clean:
            job["metrics"] = clean
    queues = hb.get("queues")
    if isinstance(queues, list):
        clean_queues = [q for q in queues if isinstance(q, dict)]
        if clean_queues:
            job["queues"] = clean_queues
    gpus = hb.get("gpus")
    if isinstance(gpus, list):
        clean_gpus = [g for g in gpus if isinstance(g, dict)]
        if clean_gpus:
            job["gpus"] = clean_gpus
    if hb.get("message"):
        job["message"] = str(hb["message"])[:200]

    job["state"] = _derive_state(hb.get("state"), done, total,
                                 beat_age, settings.job_stale_seconds,
                                 cooling_state=hb.get("cooling_state"),
                                 thermal_guard_holdoff=hb.get("thermal_guard_holdoff"),
                                 ended_at=hb.get("ended_at"))
    if job["state"] in ("done", "failed", "ended") and hb.get("ended_at"):
        # Terminal jobs display their age from when they actually finished,
        # not from the latest poll's `ts` — a job left in a steady terminal
        # state must age normally on the card instead of reading "just now"
        # forever. Running-job freshness (beat_age computed above) is unaffected.
        ended_epoch = _parse_epoch(hb["ended_at"])
        if ended_epoch is not None:
            display_age = max(0.0, now - ended_epoch)
            job["beat_age_s"] = int(display_age)
            job["beat_age"] = _rel_age(now - display_age)
    if job["state"] == "done" and "pct" in job:
        # A queue-aggregate progress bar can intentionally sit a hair under
        # 100% (see app/jobs/gallery.py's non-additive `total`) even once the
        # producer has decided the job is done; the card-level bar must still
        # read 100% for a done job. Detailed per-queue metrics keep their real
        # values — this only touches the top-level display figure.
        job["pct"] = 100.0
    if job["state"] == "stalled" and "detail" not in job:
        job["detail"] = f"no beat for {_rel_age(now - beat_age)}"

    # sparkline: rate if present, else the first metric value
    series = None
    key = label = None
    if rate is not None:
        series, label = [rate], "rate"
    elif job.get("metrics"):
        # first NUMERIC metric — string metrics (last_path) can't be sparklined
        num_k = next((k for k, v in job["metrics"].items()
                      if isinstance(v, (int, float))), None)
        if num_k is not None:
            series, key = [job["metrics"][num_k]], num_k
    return _finish_job(job, now, series, key, label, None)


def _read_file_jobs(now: float) -> dict[str, dict[str, Any]]:
    """Scan heartbeats/*.json → {job_id: job}. Each file guarded independently.
    host-<hostname>.json are a per-box CPU/thermal metrics SIDECAR (folded onto a
    job's card by host via _read_host_metrics), NOT jobs — skip them so they never
    render as their own bogus card."""
    out: dict[str, dict[str, Any]] = {}
    try:
        files = sorted(settings.heartbeats_dir.glob("*.json"))
    except Exception:
        return out
    for fp in files:
        # CROSS-CUTTING GUARD — do NOT remove (regressing it spawns bogus job cards).
        # host-<hostname>.json is the per-box CPU/thermal metrics SIDECAR, consumed
        # ONLY by _read_host_metrics() to fold the HOST·CPU block onto REAL cards. It
        # must never become a job card of its own. Skip it here at the ITERATION level
        # (by stem == job-id, BEFORE _job_from_file parses it) so a future edit to
        # _job_from_file can't silently clobber this filter again.
        if fp.stem.startswith("host-"):
            continue
        try:
            j = _job_from_file(fp, now)
            if j and j.get("job"):
                out[j["job"]] = j
        except Exception as e:      # isolation: one bad file warns, others proceed
            log.warning("heartbeat file %s failed: %s", fp.name, e)
            out[fp.stem] = {"job": fp.stem, "state": "unknown", "source": "file",
                            "detail": "parse error"}
    return out


# --------------------------------------------------------------------------- #
# 8b. Configured adapter — video-cull scan (run_full.py) on charlie
# --------------------------------------------------------------------------- #
# PULL model: one batched, read-only ssh to charlie per sweep gathers process
# liveness, GPU state, and progress by tailing the run log. The running job is
# NEVER touched. Kept so the in-flight run renders with no code change; when
# run_full.py later calls beat('video-cull', …) the file source wins and this
# adapter is simply not merged. Fully isolated — any ssh/parse failure yields a
# state='unknown' card and never aborts the sweep.

# Process-lifetime cache of the last time the adapter actually SSH'd
# successfully (not persisted — a restart just costs one extra poll, never a
# correctness problem). None means "poll now". Mirrors jobs/gallery.py's
# `_last_poll_ts`/`TERMINAL_POLL_INTERVAL_SECONDS` pattern, scoped to this one
# adapter call: only advanced on a successful probe, so a failure never masks
# a real recovery behind the throttle window (see _video_cull_should_poll).
_video_cull_last_poll_ts: float | None = None


def _video_cull_should_poll(file_job: dict[str, Any] | None, now: float) -> bool:
    """True unless the file source already has a trustworthy settled-terminal
    video-cull record (done/failed + ended_at — same criteria as
    jobs/gallery.py's is_settled_terminal, adapted to this adapter's plain
    job schema which has no `queues`) AND we polled within the last
    gpu_job_stale_seconds. An active/unsettled/missing file source keeps the
    current every-sweep behavior unchanged."""
    settled = bool(
        file_job
        and str(file_job.get("state")) in ("done", "failed")
        and file_job.get("ended_at")
    )
    if not settled:
        return True
    if _video_cull_last_poll_ts is None:
        return True
    return (now - _video_cull_last_poll_ts) >= settings.gpu_job_stale_seconds


def _adapter_video_cull(now: float, by_id: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The video-cull adapter, emitting the SAME generic job schema as the file
    source (job/host/state/source/done/total/rate/unit/eta/metrics/message).
    `by_id` is this sweep's file-source map — used only to look up this job's
    own file-source record (if any) to decide whether the SSH round-trip is
    needed (see _video_cull_should_poll); the file source already wins the
    merge in read_jobs() either way."""
    global _video_cull_last_poll_ts
    jid = settings.gpu_job_name
    host = settings.gpu_job_ssh_host
    file_job = (by_id or {}).get(jid)

    if not host:
        # Charlie is intentionally absent during the Omarchy maintenance
        # window. Do not synthesize a failed job or make an SSH attempt.
        return {}

    if not _video_cull_should_poll(file_job, now):
        # File source already covers this job id with a settled-terminal
        # record and we're within the throttle window — skip the ssh
        # round-trip. This result is discarded anyway (a file always wins
        # over an adapter for the same job id in read_jobs()'s merge).
        return {"job": jid, "host": host, "state": "unknown", "source": "none",
                "detail": "throttled: file source settled-terminal"}

    out = _ssh_gpu_probe()
    if out is not None:
        _video_cull_last_poll_ts = now

    if out is None:
        return {"job": jid, "host": host, "state": "unknown", "source": "none",
                "detail": "charlie unreachable / ssh failed"}

    sec = _parse_gpu_sections(out)
    pid = sec.get("PID", "").strip()
    alive = bool(pid)
    job: dict[str, Any] = {"job": jid, "host": host, "source": "none",
                           "alive": alive, "pid": pid or None}

    etimes = sec.get("ETIMES", "").strip()
    if etimes.isdigit():
        job["uptime"] = _fmt_uptime(int(etimes))
        job["uptime_s"] = int(etimes)   # raw — the recorder infers attempt start/reset

    # GPU state → metrics (gpu_util / gpu_temp), matching the old card's row.
    util = None
    metrics: dict[str, Any] = {}
    gpu_raw = sec.get("GPU", "").strip()
    if gpu_raw:
        parts = [p.strip() for p in gpu_raw.split(",")]
        try:
            util = int(float(parts[0]))
            metrics["gpu_util"] = util
            metrics["gpu_temp"] = int(float(parts[3]))
        except (IndexError, ValueError):
            pass
    apps = [ln for ln in sec.get("APPS", "").splitlines() if ln.strip()]
    gpu_active = (util is not None and util >= 5) or len(apps) > 0

    done = total = rate = None
    beat_age = None
    source = "nvidia"

    # heartbeat.json on the box (legacy adapter path) → still honored.
    hb_txt = sec.get("HB", "").strip()
    hb = None
    if hb_txt:
        body = hb_txt
        mt = re.match(r"mtime=(\d+)\s*(.*)$", hb_txt, re.DOTALL)
        if mt:
            body = mt.group(2).strip()
        try:
            hb = json.loads(body) if body else None
        except (json.JSONDecodeError, ValueError):
            hb = None
    if isinstance(hb, dict):
        source = "heartbeat"
        done, total, rate = hb.get("done"), hb.get("total"), hb.get("rate")
        if hb.get("phase"):
            job["phase"] = str(hb["phase"])[:40]
        ts = hb.get("ts")
        try:
            if isinstance(ts, (int, float)):
                beat_age = max(0.0, now - float(ts))
            elif isinstance(ts, str):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                beat_age = max(0.0, now - dt.timestamp())
        except (ValueError, TypeError):
            beat_age = None

    if hb is None:
        tail = sec.get("LOGTAIL", "").strip()
        prog_lines = [ln for ln in tail.splitlines()
                      if _PROG_DB.search(ln) and _PROG_SCAN.search(ln)]
        if prog_lines:
            source = "log"
            last = prog_lines[-1]
            job["message"] = last.strip()[:200]
            dbm = _PROG_DB.search(last)
            if dbm:
                done, total = int(dbm.group(1)), int(dbm.group(2))
            rm = _PROG_RATE.search(last)
            if rm:
                rate = float(rm.group(1))
            tsm = _PROG_TS.search(last)
            if tsm:
                try:
                    dt = datetime.fromisoformat(tsm.group(1)).replace(tzinfo=timezone.utc)
                    beat_age = max(0.0, now - dt.timestamp())
                except ValueError:
                    beat_age = None
        if beat_age is None:
            logstat = sec.get("LOGSTAT", "").strip()
            if logstat.isdigit():
                beat_age = max(0.0, now - int(logstat))
                if source == "nvidia":
                    source = "log"

    job["source"] = source
    if metrics:
        job["metrics"] = metrics
    if done is not None and total:
        job["done"], job["total"] = int(done), int(total)
        job["pct"] = round(100.0 * int(done) / int(total), 1) if total else None
    if rate is not None:
        job["rate"] = round(float(rate), 1)
        job["unit"] = "cpm"
        if total is not None and done is not None:
            job["eta"] = _fmt_eta((int(total) - int(done)) / float(rate)) if rate else ""
    if beat_age is not None:
        job["beat_age_s"] = int(beat_age)
        job["beat_age"] = _rel_age(now - beat_age)

    # state: reuse the shared machine, but keep the adapter's richer details.
    job["state"] = _derive_state(None, done, total, beat_age,
                                 settings.gpu_job_stale_seconds, alive=alive)
    if job["state"] == "stalled" and beat_age is not None:
        job["detail"] = f"process alive but no progress for {_rel_age(now - beat_age)}"
    elif job["state"] == "done":
        job["detail"] = "scan complete — process exited"
    elif job["state"] == "ended":
        job["detail"] = "no run_full.py process — run ended"

    # sparkline tracks GPU util (a %) to keep the video-cull card visually stable.
    series = [util] if util is not None else None
    return _finish_job(job, now, series, "gpu_util", None, 100.0)


# The configured adapters. A callable list; a job whose id already came from a
# file is skipped (files win). Adding a job here is the only per-job code —
# and only for jobs that can't yet write a heartbeat file themselves. Each
# adapter receives this sweep's whole file-source `by_id` map so it can look
# up its own job id's file record (e.g. to throttle a redundant probe — see
# _adapter_video_cull) without read_jobs() needing to know per-adapter details.
_ADAPTERS: list[Callable[[float, dict[str, dict[str, Any]]], dict[str, Any]]] = [_adapter_video_cull]


# --------------------------------------------------------------------------- #
# 8c. Attempt history — record each sweep, attach the logical-job aggregate
# --------------------------------------------------------------------------- #
# One logical job = one card, many attempts. Each sweep we record the current
# state of every job into the history store (opening/closing attempts as jobs
# stop/fail/restart) and then attach the derived aggregate (Σ active-time across
# attempts, wall-span, outcomes-summary) to the card. Two detection paths:
#   • helper — the heartbeat carries a run_id; a process's beats share one
#     attempt, done()/fail() close it, a relaunch under the same id opens a new one.
#   • inferred (adapters w/o the helper, e.g. video-cull log-tail) — open on
#     liveness absent→present or a uptime/pid reset; close on present→absent.
# Fully isolated: any store failure degrades one card's aggregate to live-only
# and never aborts read_jobs or the sweep. Prefer helper boundaries over inferred.
def _started_epoch(started, fallback: float) -> float:
    """A helper heartbeat's `started` (ISO or epoch) → epoch; fallback if absent."""
    if isinstance(started, (int, float)) and not isinstance(started, bool):
        return float(started)
    if isinstance(started, str) and started:
        try:
            dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
    return fallback


def _record_helper_attempt(jid, run_id, job, state, done, total, host, now) -> None:
    open_a = job_history.get_open_attempt(jid)
    if open_a and open_a.get("run_id") != run_id:
        # a prior process of this job died without a terminal beat — close it out
        end = job_history._epoch(open_a.get("updated_utc")) or now
        base = open_a.get("started_epoch") or end
        job_history.close_attempt(open_a["run_id"], end, "ended",
                                  duration_s=int(max(0, end - base)))
        open_a = None
    if open_a is None:
        base = _started_epoch(job.get("started"), now)
        job_history.open_attempt(run_id, jid, host, base, "live", done, total)
    else:
        base = open_a.get("started_epoch") or _started_epoch(job.get("started"), now)
    dur = int(max(0, now - base))
    if state in ("done", "failed", "ended"):
        job_history.close_attempt(run_id, now, state, done, total, dur)
    else:
        job_history.update_attempt(run_id, now, done, total, dur)


def _close_inferred(open_a, now, forced_state=None) -> None:
    pd, pt = open_a.get("progress_done"), open_a.get("progress_total")
    if forced_state in ("done", "failed", "ended"):
        outcome = forced_state
    elif pd is not None and pt and pd >= pt:
        outcome = "done"
    else:
        outcome = "ended"
    # end at last-seen (the process was already gone this sweep), not `now`
    end = job_history._epoch(open_a.get("updated_utc")) or now
    base = open_a.get("started_epoch") or end
    job_history.close_attempt(open_a["run_id"], end, outcome, pd, pt,
                              int(max(0, end - base)))


def _record_inferred_attempt(jid, job, state, done, total, host, now) -> None:
    open_a = job_history.get_open_attempt(jid)
    alive = job.get("alive")               # True/False when the probe reached the box
    pid = job.get("pid")
    etimes = job.get("uptime_s")
    terminal = state in ("done", "failed", "ended")
    if alive is True and not terminal:
        inferred = (now - etimes) if etimes else (
            open_a.get("started_epoch") if open_a else now)
        is_new = (
            open_a is None
            or (pid and open_a.get("pid") and str(pid) != str(open_a["pid"]))
            or (etimes and open_a.get("started_epoch")
                and inferred > open_a["started_epoch"] + job_history.RESET_TOLERANCE_S)
        )
        if is_new:
            if open_a:
                _close_inferred(open_a, now)
            run_id = f"{jid}:{pid or 'na'}:{int(inferred)}"
            job_history.open_attempt(run_id, jid, host, inferred, "live",
                                     done, total, pid=pid)
            open_a = job_history.get_open_attempt(jid)
        base = open_a.get("started_epoch") if open_a else inferred
        job_history.update_attempt(open_a["run_id"], now, done, total,
                                   int(max(0, now - base)), pid=pid)
    elif alive is False:
        if open_a:                          # process affirmatively gone → close
            _close_inferred(open_a, now)
    elif terminal and open_a:
        _close_inferred(open_a, now, forced_state=state)
    # else alive is None (probe unreachable/unknown): leave the open attempt as-is


def _record_one(job: dict[str, Any], now: float) -> None:
    jid = job.get("job")
    if not jid:
        return
    # Manual "mark done": if this job was muted, force its card to `done` and do
    # NOT record/re-open an attempt this sweep. Set state BEFORE render so both
    # renderers see it; aggregate() folds via setdefault so it won't clobber it,
    # and the open attempt is already closed so outcomes_summary won't re-append
    # a trailing "running". is_muted auto-clears on a genuine relaunch (new pid /
    # uptime reset), so a real restart brings the card back to live on its own.
    pid = job.get("pid")
    et = job.get("uptime_s")
    started_epoch = (now - et) if et else None
    if job_history.is_muted(jid, pid=pid, started_epoch=started_epoch):
        job["state"] = "done"
        return
    state = job.get("state") or "running"
    if state == "unknown":
        return                              # a malformed/unreachable card: don't record
    done, total, host = job.get("done"), job.get("total"), job.get("host")
    run_id = job.get("run_id")
    if run_id:
        _record_helper_attempt(jid, run_id, job, state, done, total, host, now)
    else:
        _record_inferred_attempt(jid, job, state, done, total, host, now)


# --------------------------------------------------------------------------- #
# 8d. GPU-scan progress.json enrichment — rich datapoints from the run's own
# status file (from-worker3/runs/<token>/progress.json), Syncthing-carried local.
# Purely additive: matched to a card by run_token slug, folded with setdefault so
# it never clobbers a live adapter field. Fully isolated — any read/parse error
# skips the file and the card renders exactly as before.
# --------------------------------------------------------------------------- #
# Central-time offset, mirroring job_history.dual_stamp (CDT = UTC-5, summer).
_PROG_CENTRAL_OFFSET_H = 5
_PROG_CENTRAL_LABEL = "CDT"
# run_token → slug: text after the -YYYYMMDD- date segment.
_PROG_TOKEN_RE = re.compile(r"-\d{8}-(.+)$")
# card job-id → slug: strip a leading seat prefix. cc dropped (retired seat,
# deploy role moved to worker1; no cc- heartbeats ever appear). worker3 added
# (charlie GPU-adjacent worker, same rationale as worker1/worker2 below); worker4
# excluded — Mac, no GPU, structurally never emits GPU-scan progress lines.
_PROG_SEAT_RE = re.compile(r"^(?:worker3|worker1|worker2)-")


def _prog_slug_from_token(tok: str) -> str:
    m = _PROG_TOKEN_RE.search(tok or "")
    return (m.group(1) if m else (tok or "")).strip().lower()


def _prog_slug_from_jobid(jid: str) -> str:
    return _PROG_SEAT_RE.sub("", (jid or "").strip().lower())


def _naive_utc_to_pair(iso_naive: str) -> dict[str, str] | None:
    """A naive-UTC ISO stamp (e.g. progress.json eta_finish '2026-07-05T01:38:45')
    → {'utc': ..., 'central': ...}, UTC first per the fleet convention. None on any
    parse failure. Fixed CDT offset, mirroring job_history.dual_stamp."""
    if not iso_naive:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_naive).replace("Z", ""))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    c = dt - timedelta(hours=_PROG_CENTRAL_OFFSET_H)
    return {"utc": f"{dt:%b %d %H:%M} UTC",
            "central": f"{c:%b %d %H:%M} {_PROG_CENTRAL_LABEL}"}


def _scan_progress_files(now: float) -> list[dict[str, Any]]:
    """Read every from-worker3/runs/*/progress.json once per sweep. Each file is read
    fresh and guarded independently — a mid-write partial or unreadable file is
    skipped, never raised. Returns a list of {slug, mtime, data} for matching."""
    out: list[dict[str, Any]] = []
    try:
        paths = sorted(PROGRESS_RUNS_DIR.glob("*/progress.json"))
    except Exception:
        return out
    for p in paths:
        try:
            mtime = p.stat().st_mtime
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue                     # partial write / missing / bad JSON → skip
        if not isinstance(data, dict):
            continue
        tok = data.get("run_token")
        if not tok:
            continue
        out.append({"slug": _prog_slug_from_token(str(tok)),
                    "mtime": mtime, "data": data})
    return out


# Rich keys lifted verbatim from progress.json onto the card (all precomputed
# upstream — trusted as-is). Deliberately excludes done/total/rate/eta/state so the
# live adapter's own fields are never touched even before setdefault.
_PROG_KEYS = (
    "resumed_from", "scanned_this_run", "remaining",
    "clips_per_min", "clips_per_min_cumulative",
    "eta_hours", "eta_hours_cumulative", "elapsed_hours", "eta_finish",
    "thermal", "tripwire",
    "passa_errors", "passb_errors", "parse_errors", "dead_clips", "last_path",
    "io_fallbacks", "decoder_fallbacks",   # top-level fallback (schema keeps these in tripwire)
)


def _read_progress_json(job_id: str, now: float,
                        parsed: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Match a run's progress.json to this card by run_token slug and return a
    namespaced dict of its rich fields plus locally-computed freshness/eta-zone.
    Returns None if no file confidently matches exactly this card (never raises).

    Match rule: card id '<seat>-<slug>' ↔ token 'FLEET-<SEAT>-BUILD-<date>-<slug>';
    both are normalized to <slug> (token: text after -YYYYMMDD-; card: leading seat
    prefix stripped) and compared. Newest-mtime breaks a multi-file tie for one card.
    Card ids are unique, so a file never bleeds onto a second card."""
    files = _scan_progress_files(now) if parsed is None else parsed
    want = _prog_slug_from_jobid(job_id)
    if not want:
        return None
    hits = [f for f in files if f["slug"] == want]
    if not hits:
        return None
    best = max(hits, key=lambda f: f["mtime"])   # newest re-run of this logical job
    data = best["data"]
    pj: dict[str, Any] = {k: data[k] for k in _PROG_KEYS if k in data}
    # locally-computed freshness (updated_at is naive-UTC iso in the file)
    updated_epoch = None
    try:
        u = str(data.get("updated_at") or "")
        if u:
            udt = datetime.fromisoformat(u.replace("Z", ""))
            if udt.tzinfo is not None:
                udt = udt.astimezone(timezone.utc).replace(tzinfo=None)
            updated_epoch = udt.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        updated_epoch = None
    if updated_epoch is not None:
        age = max(0.0, now - updated_epoch)
        pj["progress_age_s"] = int(age)
        pj["progress_stale"] = age > 180
    zone = _naive_utc_to_pair(data.get("eta_finish"))
    if zone:
        pj["eta_zone"] = zone
    return pj or None


# Host CPU/thermal metrics — the CPU analog of the GPU thermal fold. The shared
# fleet_host_probe writes heartbeats/host-<hostname>.json (temp/load/util/mem/
# freq + fan/power where present); Syncthing carries them to worker2 so we read them
# LOCALLY each sweep. Namespaced host_* so they never collide with a job's own
# fields, folded via setdefault in _record_and_aggregate. Never raises.
_HOST_KEY_MAP = {
    # file key -> namespaced card key (only present-in-file keys are emitted)
    "temp_c": "host_temp_c",
    "temp_high_c": "host_temp_high_c",
    "temp_crit_c": "host_temp_crit_c",
    "load1": "host_load1",
    "cpu_util_pct": "host_cpu_util_pct",
    "mem_used_mb": "host_mem_used_mb",
    "mem_avail_mb": "host_mem_avail_mb",
    "freq_mhz": "host_freq_mhz",
    "freq_max_mhz": "host_freq_max_mhz",
    "undervolt_alarm": "host_undervolt_alarm",
    "fan_rpm": "host_fan_rpm",
    "fan1_rpm": "host_fan1_rpm",
    "fan2_rpm": "host_fan2_rpm",
    "fan4_rpm": "host_fan4_rpm",
    "fancontrol_active": "host_fancontrol_active",
    "pwm_full": "host_pwm_full",
    "nct_driver": "host_nct_driver",
    "cooling_state": "host_cooling_state",
    "thermal_throttle_count": "host_thermal_throttle_count",
    "thermal_guard_action": "host_thermal_guard_action",
    "thermal_guard_last_event": "host_thermal_guard_last_event",
    "emergency_threshold_c": "host_emergency_threshold_c",
    "power_w": "host_power_w",
    "nproc": "host_nproc",
}


def _read_host_metrics(host: str | None, now: float) -> dict[str, Any] | None:
    """Read heartbeats/host-<host>.json → namespaced host_* dict (+ freshness).
    Returns None if there is no host, no file, or it can't be parsed. Only keys
    actually present in the file are emitted, so the renderers can gate each tile
    on `is defined`. Never raises — a missing/garbage file simply yields None."""
    if not host:
        return None
    path = LOUPE / "heartbeats" / f"host-{host}.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    for src_k, dst_k in _HOST_KEY_MAP.items():
        if src_k in data and data[src_k] is not None:
            out[dst_k] = data[src_k]
    if not out:
        return None
    ts = data.get("ts")
    try:
        age = max(0.0, now - float(ts)) if ts is not None else None
    except (TypeError, ValueError):
        age = None
    if age is not None:
        out["host_age_s"] = int(age)
        out["host_stale"] = age > 60
    return out


def _record_and_aggregate(by_id: dict[str, dict[str, Any]], now: float) -> None:
    """Record this sweep's attempts, then fold each job's aggregate into its card.
    Guarded per-job: a store hiccup dims one aggregate, never the sweep."""
    try:
        job_history.init_db()
    except Exception as e:
        log.warning("job_history init failed; cards render live-only: %s", e)
        return
    # Read all progress.json status files once for this sweep (handful of local files).
    try:
        progress_files = _scan_progress_files(now)
    except Exception as e:                  # isolation: enrichment degrades, never aborts
        log.warning("progress.json scan failed; cards render un-enriched: %s", e)
        progress_files = []
    for jid, job in by_id.items():
        try:
            _record_one(job, now)
        except Exception as e:              # isolation: one bad record never breaks the rest
            log.warning("job_history record %s failed: %s", jid, e)
        try:
            agg = job_history.aggregate(jid, now)
            for k, v in (agg or {}).items():
                job.setdefault(k, v)        # never clobber the live card fields
        except Exception as e:
            log.warning("job_history aggregate %s failed: %s", jid, e)
        try:
            pj = _read_progress_json(jid, now, parsed=progress_files)
            if pj:
                for k, v in pj.items():
                    job.setdefault(k, v)    # additive; never clobber a live field
        except Exception as e:              # isolation: mirrors the aggregate fold
            log.warning("progress.json enrich %s failed: %s", jid, e)
        try:
            hm = _read_host_metrics(job.get("host"), now)
            if hm:
                for k, v in hm.items():
                    job.setdefault(k, v)    # additive; never clobber a live field
        except Exception as e:              # isolation: degrades one card, never the sweep
            log.warning("host metrics enrich %s failed: %s", jid, e)


# --------------------------------------------------------------------------- #
# 8. Jobs — unified pull-model heartbeat framework (file source + adapters)
# --------------------------------------------------------------------------- #
def read_jobs() -> list[dict[str, Any]]:
    """Build the unified `jobs` list from two sources, keyed by job-id:
      1. file source — heartbeats/<job>.json (general, zero-config)
      2. configured adapters — e.g. the video-cull log-tail (kept rendering)
    A file wins over an adapter for the same id. Everything isolated: a bad file
    or a failed adapter degrades one card; the list + the sweep still complete."""
    global _PREV_RINGS
    now = time.time()
    _PREV_RINGS = _load_prev_rings()          # once per sweep, for all jobs' sparklines
    by_id = _read_file_jobs(now)              # files first (they win)
    for adapter in _ADAPTERS:
        try:
            j = adapter(now, by_id)
        except Exception as e:                # isolation: one adapter never breaks the rest
            log.warning("job adapter failed: %s", e)
            continue
        if j and j.get("job") and j["job"] not in by_id:
            by_id[j["job"]] = j
    # Record attempts + fold in each logical job's aggregate (Σ active-time across
    # attempts, wall-span, outcomes). Self-isolating; runs even for one job.
    _record_and_aggregate(by_id, now)
    if not by_id:
        return []
    # NB: returns the LIST directly (not a wrapper dict). gather_work stores a
    # reader's return value under its key, so work["jobs"] is this list and the
    # template/JS iterate work.jobs. An empty list is falsy → the key is omitted.
    return sorted(by_id.values(),
                  key=lambda j: (_STATE_RANK.get(j.get("state"), 9), j.get("job", "")))



# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
_READERS: list[tuple[str, Callable[[], dict[str, Any]]]] = [
    ("relay_runs", read_relay_runs),
    ("jobs", read_jobs),
]


def gather_work() -> dict[str, Any]:
    """Run every reader under its own guard. A reader that raises or returns
    empty simply omits its key; the dashboard renders that panel as n/a."""
    work: dict[str, Any] = {}
    for key, fn in _READERS:
        try:
            val = fn()
            if val:
                work[key] = val
        except Exception as e:  # isolation: one bad reader never breaks the sweep
            log.warning("work reader %s failed: %s", key, e)
    return work
