# Nexus

Nexus is the control surface for a small self-hosted fleet: a dashboard that
answers "is anything broken, and what is each machine doing right now" without
needing to SSH anywhere.

It runs scheduled health probes across the nodes — disk, thermals, network
paths, service liveness, backup freshness, certificate and tunnel health — and
writes each result into a single status contract the dashboard reads. Failures
surface as Web Push notifications rather than as something you have to go
looking for.

The seam that makes it extensible: a job drops into `app/jobs/`, registers in
`app/scheduler.py`, and writes into `status.json`. The dashboard reads only that
contract, so adding a probe never touches the UI. There is no build step and no client-side
framework — the front end is vanilla JS, with xterm.js vendored in for the
terminal view.


## Running this

Nexus is a FastAPI service plus a dependency-free vanilla-JS front end. There is
no build step.

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # then edit — see below
.venv/bin/python run.py     # serves on 127.0.0.1:8770
```

Everything deployment-specific lives in `.env`, which is gitignored. The
committed `.env.example` documents each key and ships documentation-range
placeholders, so a fresh checkout carries no real topology:

| Key | What it is |
|---|---|
| `NEXUS_BIND_ADDRESSES` | loopback plus this host's own tailnet address. Never `0.0.0.0` — see the note in `systemd/nexus.service`. |
| `NEXUS_STATE_DIR` | where `events.db`, job history and generated caches live |
| `NEXUS_VAULT_ROOT` / `NEXUS_RELAY_ROOT` | the Obsidian vault Nexus reads state from |
| `NEXUS_NAS_PROBE_TARGET` | storage host for the NAS path check |
| `NEXUS_VAPID_SUB` | Web Push contact address |

Paths that used to be hard-coded now resolve relative to the invoking user's
home, so the service runs as any user on any host.

**The fleet itself is declared in `app/config.py`, not in configuration.** That
is deliberate: the node list is stable and structured, so editing it is a code
change you review rather than an environment variable you fat-finger. Expect to
edit that file for a different fleet.

`systemd/` holds the unit files as worked examples — they assume this
repository at `~/nexus` and a user-level systemd session.

## Architecture (the seam)

```
APScheduler (in-process)
   └─ heartbeat job ── probes fleet ──► StatusSnapshot
                                          ├─► vault/ops/dashboard/status.json  (atomic swap)
                                          ├─► vault/ops/dashboard/history.jsonl (append)
                                          └─► off-box dead-man's-switch ping
FastAPI
   ├─ GET  /                 dashboard (Jinja, reads status.json)
   ├─ GET  /api/status       latest snapshot (dashboard auto-refresh polls this)
   ├─ GET  /api/history      recent rows for sparklines / uptime
   ├─ GET  /api/scheduler    APScheduler registry: registered jobs + next run
   ├─ POST /api/run/heartbeat  fire a sweep now ("Develop now")
   └─ POST /api/jobs/{job}/done, /undone  mark/unmute a heartbeat-derived job card
                             (distinct resource from /api/scheduler above — see app/routes.py)
```

The poller never renders HTML and the dashboard never probes the fleet. They
meet only at `status.json`, so either side evolves independently.

## Design standards

Dashboard modules and new interface features follow
[`PANEL-STANDARDIZATION.md`](PANEL-STANDARDIZATION.md). Treat it as the
implementation and review contract for naming, typography, module geometry,
responsive behavior, controls, state colors, and visual verification.

[`design-index.md`](design-index.md) maps the shared visual system to source
files, defines the navigation and icon taxonomy, and tracks page adoption.

## Probes

| kind   | how                                   | health logic                         |
|--------|---------------------------------------|--------------------------------------|
| ping   | TCP connect to :22                    | open = ok, else crit                 |
| disk   | `df -P` over ssh                      | ≥ crit% crit, ≥ warn% warn           |
| http   | GET the tunnel URL                    | <500 = ok (401 still proves it live) |
| backup | `stat` marker mtime over ssh          | age vs stale warn/crit hours         |
| relay  | mtime of from-worker1/from-worker5 return lanes (local) | present = ok            |
| smart  | `smartctl -H` over ssh (best-effort)  | PASSED = ok, unreadable = unknown    |

Every probe degrades to `unknown`/`crit` with a one-line detail — a failed
probe is data, never an exception that kills the sweep.

## Model-usage collector

`nexus-model-usage.timer` refreshes the Model Usage card every five minutes
with up to 30 seconds of jitter. The header **Scan** action requests the same
refresh alongside the fleet heartbeat, with a 60-second cache-age throttle.

- Claude uses the undocumented structured utilization endpoint used by Claude
  Code itself. It reads the OAuth token only from the marked isolated collector
  HOME, never writes credentials to the cache, and falls back to the
  authenticated Claude `/usage` panel if the endpoint, schema, or token state
  changes.
- Gemini uses the authenticated private
  `retrieveUserQuotaSummary` client RPC for exact rolling five-hour/week
  fractions and ISO reset timestamps. The request uses the installed client's
  `User-Agent: gemini` identity and the isolated collector HOME's OAuth
  token. Strict schema/auth/network failures fall back to Gemini's
  authenticated `/usage` panel; daily request-bucket endpoints are not used as
  subscription quota.
- Each provider fails independently. The output is mode `0600`, atomically
  replaced, and includes a source label so fallback behavior remains auditable.

Every successful timer invocation also appends normalized, secret-free samples
for Claude, Codex, and Gemini to the host-local WAL-mode SQLite database at
`~/.local/share/nexus/model-usage-history.sqlite3`. The database stores
percent used, reset anchors, bounded provenance, availability/fallback state,
and derived reset/source events—not credentials, raw API responses, terminal
output, or prompts. `/model-usage` provides the full history surface;
`/api/model-usage/history` returns downsampled ranges (`24h`, `7d`, `30d`,
`90d`, or `all`) with optional provider filtering.

Nexus's `model-usage-watch` scheduler reads only new rows from the quota event
ledger once per minute. Its durable watermark lives in `events.db`, so
pre-existing/backfilled history is silently baselined and each later event is
handled exactly once across restarts. Early reset reanchors, provider
availability changes, collector fallbacks, and source changes create PWA
posts. Expected window rollovers and correlated usage drops remain visible in
the notification feed without generating an iOS push. Every notification
navigates to `/model-usage`.

## Deploy on Alpha

```bash
# 1. land the code
mkdir -p ~/nexus && cd ~/nexus
# (copy app/ templates/ systemd/ requirements.txt .env.example here)

# 2. venv + deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. config
cp .env.example .env
# edit .env: set NEXUS_HEARTBEAT_PING_URL to your healthchecks.io URL

# 4. seed the marker delta's backup job should touch on success (optional but
#    makes the backup probe meaningful) — add to the rclone unit's ExecStartPost:
#    /bin/sh -c 'mkdir -p ~/.local/state/photo-archive && touch ~/.local/state/photo-archive/last-run'

# 5. install the user unit (matches Worker1/Worker5 user-unit + lingering pattern)
mkdir -p ~/.config/systemd/user
cp systemd/nexus.service ~/.config/systemd/user/
cp systemd/nexus-model-usage.{service,timer} ~/.config/systemd/user/
loginctl enable-linger "$USER"   # already on for the photo-archive unit's host; harmless to repeat
systemctl --user daemon-reload
systemctl --user enable --now nexus.service
systemctl --user enable --now nexus-model-usage.timer
systemctl --user status nexus.service
```

## Tunnel origin

Add one origin to the Cloudflare tunnel config on Alpha (same-box, so loopback):

```yaml
# ~/.cloudflared/config.yml — add ABOVE the catch-all 404 rule
  - hostname: light-table.example.com      # or a path on an existing hostname
    service: http://127.0.0.1:8770
```

Then `cloudflared tunnel route dns the tunnel light-table.example.com` and
restart the connector. Port 8770 keeps it clear of the MCP on 8765.

## Notes

- **Self-monitoring gap:** Alpha can't report its own death. The dead-man's
  switch (`NEXUS_HEARTBEAT_PING_URL`) covers it — silence there pages you.
- **Fonts** load from Google Fonts; self-host into `static/` if you want the
  dashboard fully offline-capable.
- **history.jsonl** grows ~unbounded (one line / run). A `standup` job or a
  logrotate rule can truncate it later; the dashboard only tails the last N.
- Writes to `status.json`/`history.jsonl` live under the vault, so Syncthing
  carries fleet state to every box for free.
```

## Development hygiene

Git history is Nexus's rollback mechanism. Do not create in-tree .bak*, backups/,
or .build-backups/ copies. Use a scoped commit or branch before edits. If an
emergency scratch copy is unavoidable, keep it outside the repository and
remove it after verification.

## Live activity retention

`~/.local/state/nexus/events.db` keeps seven days of hook activity for the dashboard tail and live
feed. A daily 08:35 UTC scheduler job deletes older rows from `events` only;
notification, alert, and run-watcher tables in the shared database are retained.
Routine pruning does not run `VACUUM`, so it cannot introduce a long blocking
rewrite while Nexus is live and SQLite can reuse the freed pages.

## Fleet conformance monitor

`tools/collect_conformance.py` reads the versioned declarative manifest at
`conformance/checks.json` every 15 minutes through
`nexus-conformance.timer`. It performs only bounded, noninteractive,
read-only checks: global contract hash parity, the directed SSH access floor,
required user services/timers, and required Git/conventions paths. It never
auto-repairs drift, evaluates manifest values in a shell, or records secrets.

The collector atomically replaces `~/.local/state/nexus/generated/conformance.json` and keeps a bounded
1,000-row summary history in the adjacent `conformance-history.jsonl`. HTTP handlers
read only the cache. `/conformance` exposes full evidence, `/api/conformance`
is the machine contract, and the dashboard shows a compact projection.

## Service-worker asset freshness

The service worker fetches same-origin static assets network-first and updates a
stable runtime cache after every successful 200 response. Asset deployments do
not require a cache-version edit; online clients receive the current file, while
previously fetched assets remain available as an offline fallback. HTML, API,
event, and WebSocket traffic is never cached.
