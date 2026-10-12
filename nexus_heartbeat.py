"""
nexus_heartbeat — the whole integration for a job to appear on the Nexus.

A long-running job on ANY fleet box calls `beat(...)` in its loop and `done()`/`fail()`
at the end. That atomically writes `~/Vaults/loupe-vault/heartbeats/<job>.json` (the
synced vault); the Nexus's poller reads it locally and auto-renders a job card. Zero
dashboard code per job — this file is the entire contract on the job's side.

NO dependency on the dashboard. Copy this one file next to your job, or import it:

    from nexus_heartbeat import beat, done, fail
    for i, item in enumerate(work):
        ...
        beat("myjob", done=i, total=len(work), rate=r, unit="items/min")
    done("myjob")

Path: defaults to ~/Vaults/loupe-vault/heartbeats/ ; override with $NEXUS_HEARTBEATS_DIR.
Write is atomic (temp + os.replace) so the poller never reads a torn file.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _dir() -> Path:
    env = (os.environ.get("NEXUS_HEARTBEATS_DIR")
           or os.environ.get("PANEL_HEARTBEATS_DIR")
           or os.environ.get("FLEET_HEARTBEATS_DIR"))
    return Path(env) if env else Path.home() / "Vaults" / "loupe-vault" / "heartbeats"


# Process-local attempt registry: job -> {"run_id", "started"}. The FIRST beat()
# of a job in this process stamps a fresh run_id (uuid) + start time; every beat
# carries them, so the Nexus can group a process's beats into ONE attempt. done()
# / fail() clear the entry, so if the same process (or a relaunch under the same
# job id) beats again afterward, it opens a NEW attempt automatically. A job that
# dies and is relaunched under the same id yields two attempts with zero effort.
_RUNS: dict[str, dict] = {}


def beat(job, *, done=None, total=None, rate=None, unit=None, eta=None,
         state="running", message=None, host=None, ended_at=None, **metrics):
    """Atomically write this job's heartbeat. Only `job` matters; all else is
    optional. `metrics` is any extra name=number pairs (shown + sparklined)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run = _RUNS.get(job)
    if run is None:
        run = {"run_id": uuid.uuid4().hex, "started": ts}
        _RUNS[job] = run
    payload = {
        "job": job,
        "kind": "job",
        "ts": ts,
        "host": host or socket.gethostname(),
        "state": state,
        "run_id": run["run_id"],     # stable per attempt; groups this process's beats
        "started": run["started"],   # this attempt's first-beat time (attempt start)
    }
    for k, v in (("done", done), ("total", total), ("rate", rate),
                 ("unit", unit), ("eta", eta), ("message", message),
                 ("ended_at", ended_at)):
        if v is not None:
            payload[k] = v
    if metrics:
        payload["metrics"] = {k: v for k, v in metrics.items() if v is not None}
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{job}.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)   # atomic swap — readers see old or new, never partial
    # a terminal beat closes this attempt: drop the process-local run so the next
    # beat() for the same job opens a fresh attempt (the stop→restart collapse case).
    if state in ("done", "failed"):
        _RUNS.pop(job, None)
    return path


def done(job, **k):
    """Mark the job complete (state=done). Pass final done/total/metrics if handy."""
    k.setdefault("state", "done")
    return beat(job, **k)


def fail(job, **k):
    """Mark the job failed (state=failed). Pass a `message=` with the reason."""
    k.setdefault("state", "failed")
    return beat(job, **k)


if __name__ == "__main__":  # tiny self-test: writes a demo beat and prints the path
    p = beat("selftest", done=3, total=10, rate=42.0, unit="items/min",
             message="hello from nexus_heartbeat", cpu=38, mem_gb=2.1)
    print("wrote", p)
