# Job-heartbeat framework — the pull-model job pulse

A **general** contract: any long-running job on any fleet box emits a standard
**heartbeat file**, and the Nexus (`:8770`) auto-discovers and renders it as a
**job card** — zero per-job dashboard code. Pull-model and decoupled: the job writes its
pulse; the poller reads it. "Read the pulse, never touch the patient." A malformed or
missing heartbeat degrades that one card and never breaks the sweep.

This generalizes the old one-off `gpu_job` panel (the video-cull scan on charlie). That
job still renders today via a **configured log-tail adapter** (below); the moment it
starts writing a heartbeat file, the file source takes over automatically.

## Two lanes: JOBS PANEL (detached) vs SEAT TILE (inline)

There are **two** heartbeat lanes, rendered in two different places on the Nexus:

| lane | who emits | file | rendered as |
|------|-----------|------|-------------|
| **Detached** | a separate launched process (`nexus_run` / `beat()`) | `heartbeats/<job>.json` | a **job card** in the jobs panel |
| **Inline** | a long tool-by-tool phase *within your own run* | `heartbeats/inline/<seat>.json` | a **progress bar on your seat tile** (Worker5/Worker1/Worker2) |

The jobs panel globs `heartbeats/*.json` **non-recursively**, so the `inline/` subdir is
invisible to it — an inline record shows **only** on the seat tile, **never** as a job card
(no double-show). Use the **detached** lane for a background job you launch and walk away
from; use the **inline** lane when your *own* run is grinding through a long phase (e.g. Worker1's
swap-remainder) that would otherwise look like a silently-busy seat.

### Inline emitter: `nexus_beat_inline`

Call it periodically from your inline loop (synced to `~/Vaults/loupe-vault/tools/bin/`,
`~/bin` shim per box, exactly like `nexus_run`):

```bash
nexus_beat_inline --done 3 --total 10 --label swap-remainder \
    --rate 2.5 --unit files/min --token FLEET-BUILD-20260704-foo
```

- **Seat auto-detected** from the hostname (charlie→Worker5, delta→Worker1, worker2→Worker2); override
  with `--seat`.
- `--token` binds the beat to your active run so a **stale** beat can't light a **new/idle**
  seat: the seatboard only shows the bar when the token matches the seat's live run (and the
  beat is fresh within `job_stale_seconds`). A **FREE/idle** seat never shows a bar.
- **ETA is progress-derived** when a fresh inline record is present — computed from real
  `done/total` over elapsed and shown **without** the `est.` tag. With no inline record the
  tile keeps its historical-median `est.` estimate. A terminal `--state done` (or deleting the
  file) clears the bar; a stale record ages it out on both server and client.
- Best-effort: a missing/flaky arg **never** raises — an inline beat is not load-bearing for
  the caller's loop. Atomic write (temp + `os.replace`), `ts` now-UTC, same as `beat()`.

## Standard launch path: `nexus_run` (any job, any box)

The fleet-standard way to start a long/detached job is the **`nexus_run`** wrapper — it
launches your command and emits the heartbeats *for* it, so a job auto-appears on the Nexus
without the launcher having to remember anything. Emission becomes a property of **how** you
launch, not a step each script hand-rolls:

```bash
setsid nohup nexus_run --job backup-nightly --total 5000 --unit files \
    --progress-cmd 'wc -l < /var/run/backup.progress' --interval 45 \
    -- ./do_backup.sh  >/tmp/backup.out 2>&1 &
```

`nexus_run` writes a `running` start beat, then every `--interval` seconds reads the
progress source (`--progress-cmd` prints the done count, or `--progress-file`'s contents are
the count) and beats with computed `rate`+`eta`; with no progress source it emits a **minimal
liveness beat** (elapsed only). A flaky progress read degrades to liveness, never crashes. On
exit it ALWAYS writes a terminal beat — **exit 0 → done**, **non-zero → failed**,
**signalled/killed → ended** (e.g. when a run is stopped). `--help` documents the flags;
`--job` is validated to a safe filename charset.

Single source: the authoritative copy lives in the synced `~/Vaults/loupe-vault/tools/bin/`
(so Syncthing carries `nexus_run` + `nexus_heartbeat.py` to every box), with a
`~/bin/nexus_run` shim on each host. It works identically on charlie/delta/worker2. Override
the heartbeat dir with `$NEXUS_HEARTBEATS_DIR`.

## Or embed the beat loop directly

Jobs that own their loop can import the one helper file and call `beat()`/`done()`/`fail()`
themselves — this is what `nexus_run` does under the hood, and remains fully supported:

```python
from nexus_heartbeat import beat, done, fail   # copy or import this one file

for i, item in enumerate(work):
    ...
    beat("myjob", done=i, total=len(work), rate=r, unit="items/min")
done("myjob")        # or fail("myjob", message="...") on error
```

That's the whole integration. The job appears on the Nexus on the next sweep; no
dashboard change needed. `nexus_heartbeat.py` has **no dependency on the dashboard** —
a job on another box just needs this file and a writable path into the synced vault.

> **video-cull:** `run_full.py` still renders via its legacy log-tail adapter (below) and
> MAY adopt `nexus_run`/`beat()` later; it is unchanged for now.

## The contract (heartbeat file)

A job writes an **atomic** JSON (temp + `os.replace`) to:

```
~/Vaults/loupe-vault/heartbeats/<job-id>.json
```

Syncthing carries it to worker2; the poller reads it locally each sweep. Only `job` + `ts`
are **required** — everything else is optional:

```json
{ "job":"backup-nightly", "ts":"2026-07-04T05:00:00Z",     // required: id + last-beat (ISO-UTC)
  "host":"delta", "state":"running",                      // opt: running|done|failed|stalled
  "done":1420, "total":5000, "rate":42.0, "unit":"files/min", "eta":"1h25m",  // opt progress
  "metrics":{"cpu":38,"mem_gb":2.1},                        // opt: name->number, shown + sparklined
  "message":"copied /photos/2024 …" }                       // opt: last line
```

| field     | type                         | meaning                                            |
|-----------|------------------------------|----------------------------------------------------|
| `job`     | string **(required)**        | job id; also the card title and the file stem      |
| `ts`      | ISO-8601 UTC or epoch seconds **(required)** | last beat — drives freshness          |
| `host`    | string                       | which box (defaults to the writer's hostname)      |
| `state`   | running \| done \| failed \| stalled | explicit state hint (poller also derives it) |
| `done`/`total` | int                     | drive the progress bar + %                         |
| `rate`    | number                       | throughput; sparklined if no `metrics`             |
| `unit`    | string                       | label for `rate` and the bar (e.g. `files/min`)    |
| `eta`     | string or minutes(number)    | shown as-is; else derived from `rate`+remaining    |
| `metrics` | object name→number           | extra tiles; the first drives the sparkline if no `rate` |
| `message` | string                       | last line, shown in a monospace strip              |

Write it **atomically** — temp file in the same dir, then `os.replace` (atomic on POSIX),
so the poller never reads a torn file. `nexus_heartbeat.beat()` does exactly this; if you
roll your own, mirror it. Override the target dir with `$NEXUS_HEARTBEATS_DIR`.

## State the poller derives (per job)

- **running** (cyan) — fresh beat, progress `< total`.
- **done** (cyan, dim) — `state:done` **or** `done >= total`.
- **failed** (amber/safelight) — `state:failed`.
- **stalled** (amber) — beat `ts` older than ~3 sweeps (`job_stale_seconds`, default 900s)
  while otherwise alive.
- **ended** (dim) — an *adapter* job whose process vanished before reaching total.
  A *file* job simply ages its card out when the file is deleted.
- **unknown** (dim) — an unreadable/malformed file; that one card warns, the rest proceed.

Each job keeps a small ring-buffer of a chosen numeric (`rate`, else the first `metrics`
value; the GPU adapter tracks util) for a sparkline, carried across sweeps.

## Two sources (poller side, `app/work.py::read_jobs`)

1. **File source (general, zero-config):** scans `heartbeats/*.json` (**non-recursive**, so
   the `inline/` subdir is excluded — that is the seat-tile lane, above), one job per file.
   This is the path every new detached job should use.
2. **Configured adapters:** for jobs that report otherwise. The **video-cull** adapter
   SSHes tensor (read-only) each sweep and tails `full_run.log`
   (`scanned N (db DONE/TOTAL) … win=RATEcpm`), plus `nvidia-smi` for gpu util/temp — so
   the in-flight run renders with no code change to `run_full.py`.

If a job-id has **both** a file and an adapter, the **file wins** — so when `run_full.py`
later calls `beat('video-cull', …)`, the panel auto-switches off the log adapter with no
dashboard change. Isolation is the rule throughout: a bad file or a failed adapter dims one
card; the job list, the other work readers, and the fleet sweep all still complete.

## Adapter tunables (`app/config.py`)

`heartbeats_subdir` (the file-source dir under the vault), `job_stale_seconds`,
`job_ring_len`; and the video-cull adapter's `gpu_job_ssh_host` / `gpu_job_pgrep` /
`gpu_job_log` / `gpu_job_stale_seconds`. The adapter also still honors a
`heartbeat.json` on the charlie box if one appears there.

## Glossary

This framework's architecture is **[[pull-model-heartbeat]]**: the job emits a pulse into
a synced drop-dir; the dashboard reads pulses and renders, never reaching into the job. The
same pull-model carries **two lanes** — a top-level `heartbeats/<job>.json` pulse renders a
**jobs-panel card** (detached work), while a `heartbeats/inline/<seat>.json` pulse renders a
**progress bar on the seat tile** (a long inline phase within a run).
