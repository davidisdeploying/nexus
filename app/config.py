"""
Configuration for the Tower dashboard.

Tunables and secrets come from the environment (.env); the fleet itself is
declared here in code because it's stable and structured. Editing the fleet is
a code change you review, not an env-var you fat-finger.
"""
from __future__ import annotations

import os

from enum import Enum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .fleet_nodes import NODES
from .runtime_paths import GENERATED_STATE_DIR

# The storage host's address is deployment-specific, not part of the product.
# Set NEXUS_NAS_PROBE_TARGET in .env; the default is an RFC 5737 documentation
# address so a published checkout carries no real topology.
NAS_PROBE_TARGET = os.environ.get("NEXUS_NAS_PROBE_TARGET", "192.0.2.10")
PLANNED_OFFLINE_NODES = frozenset(
    item.strip()
    for item in os.environ.get("NEXUS_PLANNED_OFFLINE_NODES", "").split(",")
    if item.strip()
)


class ProbeKind(str, Enum):
    """What a node check actually does. One node can carry several."""
    PING = "ping"                    # legacy TCP reachability
    TAILSCALE_PING = "tailscale_ping" # tailscale ping from worker2
    SSH_BANNER = "ssh_banner"        # TCP open + SSH banner latency
    REMOTE_ICMP = "remote_icmp"      # ICMP from a delegated source host
    REMOTE_ICMP_DELTA = "remote_icmp_delta"
    NAS_SMB_CHARLIE = "nas_smb_charlie"
    NAS_SMB_DELTA = "nas_smb_delta"
    NAS_MOUNT = "nas_mount"
    GPU = "gpu"
    OLLAMA = "ollama"
    LOUPE_SERVICE = "loupe_service"
    DB_FRESHNESS = "db_freshness"
    NEXUS_LOOPBACK = "nexus_loopback"
    SYNC_SERVICE = "sync_service"
    SCHEDULER = "scheduler"
    PROCESS_RSS = "process_rss"
    DISK = "disk"                    # df over ssh
    DISK_LOCAL = "disk_local"        # df on THIS box (worker2 hosts the dashboard)
    MEM = "mem"                      # memory pressure on THIS box (local read)
    MEM_REMOTE = "mem_remote"        # memory pressure on a remote box (df-style ssh read)
    HTTP = "http"                    # HTTP(S) health of an endpoint
    TUNNEL_CONNECTOR = "tunnel"      # cloudflared connector /ready on-box (§A)
    BACKUP_FRESHNESS = "backup"      # mtime of a marker over ssh
    RELAY_LANES = "relay"            # local vault run-state read
    TOWER_LIVENESS = "tower"         # Tower loopback liveness + lane freshness (§B)
    NAS_SMART = "smart"              # smartctl over ssh (best-effort)


class Node(BaseSettings):
    """One thing the heartbeat watches. Not all fields apply to all kinds."""
    name: str
    address: str                     # tailnet name or IP the probe dials
    kinds: list[ProbeKind]
    ssh_host: str | None = None      # ssh alias for disk/backup/smart probes
    tcp_port: int = 22               # port the PING probe connects to
    http_url: str | None = None      # for HTTP probes
    marker_path: str | None = None   # for BACKUP_FRESHNESS: file to stat, on ssh_host
    disk_path: str = "/"             # for DISK: mount to measure
    smart_device: str | None = None  # for NAS_SMART: e.g. /dev/sda
    # Optional delegated path probe: worker2 cannot see every network
    # segment directly, so a node can ask another fleet host to run
    # a tiny ICMP check and report that path separately.
    path_probe_host: str | None = None
    path_probe_target: str | None = None
    # Optional per-kind display-label override. Lets one node carry two probes of
    # the same generic kind (e.g. an HTTP tunnel check + a RELAY lane read) but
    # render them as distinct, named sub-rows ("tunnel · edge", "relay ·
    # archive") without forking the probe logic. Keyed by ProbeResult.kind.
    labels: dict[str, str] = Field(default_factory=dict)
    # Optional card-heading override, purely cosmetic. `name` stays the internal
    # key (probe correlation, health_watch node matching, seat/event matching,
    # anchor ids) — this only swaps the text the Nexus renders on the
    # node's top-row card.
    display_name: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="FLEET_", extra="ignore"
    )

    # --- paths ---------------------------------------------------------------
    vault_root: Path = Field(default=Path.home() / "Vaults" / "loupe-vault")
    # Root for the relay's from-{seat}/{runs,transcripts,recon} lanes. Split from
    # vault_root 2026-07-10 when the Tower audit moved active relay state to the
    # Library — heartbeats intentionally stayed on vault_root/loupe-vault
    # (see heartbeats_dir below), so this must NOT be folded back into vault_root.
    relay_root: Path = Field(default=Path.home() / "Vaults" / "homelab-vault")
    # Runtime state (status.json + history.jsonl) is LOCAL and NON-synced, mirroring
    # jobs_history.db — it is rebuilt on every sweep and must NOT ride Syncthing.
    # It used to live under vault_root/ops/dashboard/, but that path is inside the
    # synced vault: a stale dashboard on another seat would write its own status.json
    # (bogus host-* cards) and Syncthing replicated it onto worker2, clobbering worker2's
    # clean snapshot (see status.sync-conflict-*.json). Local path = single writer,
    # no cross-seat clobber. (LEGACY-BUILD-20260705-snapshot-local.)
    state_dir: Path = Field(default=GENERATED_STATE_DIR)
    # Durable, host-local cloud quota telemetry. This is operational state, not
    # vault content: one Alpha writer, WAL mode, no credentials/raw responses.
    model_usage_history_db: Path = Field(
        default=Path.home()
        / ".local"
        / "share"
        / "nexus"
        / "model-usage-history.sqlite3"
    )

    # --- scheduling ----------------------------------------------------------
    heartbeat_interval_seconds: int = 300     # how often the fleet is swept
    events_retention_days: int = 7           # rolling live-activity window
    # Notification bookkeeping (events.db notification_log/run_watch_seen) is
    # low-churn compared to the events feed, so it gets a longer window; both
    # prune inside the same daily events-retention job (FLEET-WORKER2-BUILD-
    # 20260721-nexus-bounded-retention), not a separate scheduler job.
    notification_log_retention_days: int = 30
    run_watch_seen_retention_days: int = 30

    # --- thresholds ----------------------------------------------------------
    disk_warn_pct: int = 80
    disk_crit_pct: int = 90                    # charlie hit 93% once; catch it earlier
    # Worker2 memory pressure is a live watch item (the box hosts the MCP + this
    # dashboard). Used% = (MemTotal - MemAvailable) / MemTotal.
    mem_warn_pct: int = 85
    mem_crit_pct: int = 95
    backup_stale_warn_hours: int = 36          # nightly job + slack
    backup_stale_crit_hours: int = 72

    # --- probe hygiene -------------------------------------------------------
    ssh_timeout_seconds: int = 8
    http_timeout_seconds: int = 8
    tcp_timeout_seconds: int = 4

    # --- job-heartbeat framework (pull-model) --------------------------------
    # General file source: any job on any box atomically writes heartbeats/<job>.json
    # into the synced vault; the poller reads them LOCALLY each sweep and auto-renders
    # a card. Zero per-job dashboard code. See HEARTBEAT.md + nexus_heartbeat.py.
    heartbeats_subdir: str = "heartbeats"      # under vault_root; the file source
    # A live job whose last beat (ts) is older than this → stalled/WARN. ~3 sweeps.
    job_stale_seconds: int = 900
    job_ring_len: int = 60                      # per-job sparkline ring depth

    # --- gpu-job adapter (video-cull scan on charlie) -------------------------
    # A CONFIGURED adapter (not the file source): the poller SSHes charlie each sweep
    # and tails the run log so the in-flight video-cull run keeps rendering with no
    # code change. Read-only; the running job stays untouched. When run_full.py later
    # calls beat('video-cull', …), the file source takes over (a file wins over an
    # adapter for the same job-id) and this adapter goes quiet automatically.
    gpu_job_ssh_host: str = "charlie"
    gpu_job_name: str = "video-cull"                 # display label
    # pgrep -f pattern for the process. The [f] bracket is deliberate: the regex
    # matches "run_full.py" but the literal pattern string does NOT, so pgrep can
    # never match the ssh/pgrep command carrying this very pattern (self-match).
    gpu_job_pgrep: str = r"python3 run_[f]ull\.py"
    gpu_job_log: str = os.environ.get("NEXUS_GPU_JOB_LOG", os.path.expanduser("~/loupe-video/full_run.log"))
    # Legacy on-box heartbeat.json the adapter still honors if it appears on charlie.
    # (The general path is now the synced heartbeats/ file source — see HEARTBEAT.md;
    # once run_full.py calls beat('video-cull', …) the file source wins over this
    # adapter entirely.)
    gpu_job_heartbeat: str = os.environ.get("NEXUS_GPU_JOB_HEARTBEAT", os.path.expanduser("~/loupe-video/heartbeat.json"))
    # Progress older than this (from the log/heartbeat timestamp) with the process
    # still alive → stalled/WARN. ~3 sweep intervals; a progress line lands ~90s.
    gpu_job_stale_seconds: int = 900
    gpu_job_ring_len: int = 60                        # sparkline ring-buffer depth

    # --- off-box dead-man's switch ------------------------------------------
    # The poller pings this URL at the END of every successful run. If worker2 or
    # the poller dies, the pings stop and the external service alerts you.
    # Leave empty to disable. healthchecks.io-style base URL expected.
    heartbeat_ping_url: str = ""

    # --- server --------------------------------------------------------------
    host: str = "127.0.0.1"          # tunnel origin dials loopback
    port: int = 8770                 # MCP is 8765; keep them apart

    # --- notifications (Phase 0 foundations; no sender wired yet) ------------
    # secrets/ sits beside the app dir, chmod 700, never synced to the vault.
    # VAPID keys are read once at import (see vapid_public_key_b64url below) and
    # NEVER rotated casually — rotation orphans every device subscription
    # (panel-notifications-design.md §C.4).
    secrets_dir: Path = Field(default=Path.home() / "nexus" / "secrets")
    vapid_private_key_file: str = "vapid_private.pem"
    vapid_public_key_file: str = "vapid_public.pem"
    notify_bearer_token_file: str = "notify_bearer_token.txt"
    # Phase 5 (nexus-alarm reliable transport, design §E): public ntfy.sh, topic
    # as password (D-3) — no self-hosting. The topic file is the ONLY secret;
    # ntfy_base_url is not sensitive.
    ntfy_topic_file: str = "ntfy_topic.txt"
    ntfy_base_url: str = "https://ntfy.sh"
    # Absolute origin for ntfy's Click deep-link — unlike the PWA's relative
    # `navigate` (resolved in-page), the native ntfy app opens Click as a bare
    # URL and needs a fully-qualified one. nexus.example.com is the
    # CONFIRMED live Nexus hostname (design-note.md "ACCESS CUTOVER COMPLETE",
    # 2026-07-10).
    public_origin: str = "https://nexus.example.com"

    @property
    def status_file(self) -> Path:
        return self.state_dir / "status.json"

    @property
    def history_file(self) -> Path:
        return self.state_dir / "history.jsonl"

    @property
    def heartbeats_dir(self) -> Path:
        return self.vault_root / self.heartbeats_subdir

    @property
    def vapid_private_key_path(self) -> Path:
        return self.secrets_dir / self.vapid_private_key_file

    @property
    def vapid_public_key_path(self) -> Path:
        return self.secrets_dir / self.vapid_public_key_file

    @property
    def notify_bearer_token_path(self) -> Path:
        return self.secrets_dir / self.notify_bearer_token_file

    @property
    def notify_bearer_token(self) -> str | None:
        """The /api/notify bearer token, read fresh off disk (chmod 600, not
        synced). None if the secret hasn't been generated yet -> the route
        fails closed (every request 401s) rather than accepting anything."""
        try:
            return self.notify_bearer_token_path.read_text().strip() or None
        except OSError:
            return None

    @property
    def ntfy_topic_path(self) -> Path:
        return self.secrets_dir / self.ntfy_topic_file

    @property
    def ntfy_topic(self) -> str | None:
        """The ntfy.sh topic (chmod 600, not synced) — IS the password for this
        channel (public ntfy.sh, D-3), so it is read fresh off disk and never
        cached/logged. None if not yet provisioned -> send_ntfy no-ops rather
        than posting to a guessable/empty topic."""
        try:
            return self.ntfy_topic_path.read_text().strip() or None
        except OSError:
            return None

    @property
    def vapid_public_key_b64url(self) -> str | None:
        """The applicationServerKey the browser's pushManager.subscribe() needs
        (uncompressed EC point, base64url, no padding). Derived from the PEM on
        each read rather than cached, so pytest/reload never sees a stale key."""
        try:
            from cryptography.hazmat.primitives import serialization
            from py_vapid import Vapid
            import base64
            v = Vapid.from_file(str(self.vapid_private_key_path))
            raw = v.public_key.public_bytes(
                encoding=serialization.Encoding.X962,
                format=serialization.PublicFormat.UncompressedPoint,
            )
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        except Exception:
            return None


# The fleet. Declared once, imported everywhere. Planned-offline nodes are
# omitted entirely so maintenance does not render as a health incident.
_FLEET: list[Node] = [
    Node(
        name=NODES["charlie"].health_key,
        address=NODES["charlie"].tailscale_target,
        ssh_host=NODES["charlie"].ssh_alias,
        path_probe_host=NODES["charlie"].ssh_alias,
        path_probe_target=NAS_PROBE_TARGET,
        kinds=[
            ProbeKind.TAILSCALE_PING,
            ProbeKind.SSH_BANNER,
            ProbeKind.DISK,
            ProbeKind.MEM_REMOTE,
            ProbeKind.GPU,
            ProbeKind.OLLAMA,
            ProbeKind.NAS_MOUNT,
            ProbeKind.REMOTE_ICMP,
            ProbeKind.NAS_SMB_CHARLIE,
            ProbeKind.DB_FRESHNESS,
        ],
        disk_path="/",
        labels={
            "tailscale_ping": "ping · tailnet",
            "ssh_banner": "ssh · banner",
            "gpu": "gpu · nvidia",
            "ollama": "model · ollama",
            "nas_mount": "nas · mount",
            "remote_icmp": "nas · lan",
            "nas_smb_charlie": "nas · smb",
            "db_freshness": "db · metadata",
        },
    ),
    Node(
        name=NODES["delta"].health_key,
        address=NODES["delta"].tailscale_target,
        ssh_host=NODES["delta"].ssh_alias,
        path_probe_host=NODES["delta"].ssh_alias,
        path_probe_target=NAS_PROBE_TARGET,
        kinds=[
            ProbeKind.TAILSCALE_PING,
            ProbeKind.SSH_BANNER,
            ProbeKind.DISK,
            ProbeKind.MEM_REMOTE,
            ProbeKind.LOUPE_SERVICE,
            ProbeKind.BACKUP_FRESHNESS,
            ProbeKind.NAS_MOUNT,
            ProbeKind.REMOTE_ICMP,
            ProbeKind.NAS_SMB_DELTA,
        ],
        disk_path="/",
        # Compact Loupe serving and photo-archive timers moved to Delta for
        # Charlie's Omarchy outage; the rclone trigger preserves this marker.
        marker_path=os.path.expanduser("~/.local/state/photo-archive/last-run"),
        labels={
            "tailscale_ping": "ping · tailnet",
            "ssh_banner": "ssh · banner",
            "loupe_service": "loupe · service",
            "nas_mount": "nas · mount",
            "remote_icmp": "nas · lan",
            "nas_smb_delta": "nas · smb",
        },
    ),
    Node(
        name="echo",
        address="echo",
        ssh_host="echo",
        path_probe_host="delta",
        path_probe_target=NAS_PROBE_TARGET,
        # echo is storage, not a seat. Split its health into paths:
        # worker2 sees the NAS through the tailnet, while charlie/delta see the
        # storage LAN. The old PING row was a TCP connect to :22 and could look
        # like a giant "ping" during SSH/banner stalls even when the NAS wire was
        # clean. These probes make that distinction explicit.
        kinds=[
            ProbeKind.TAILSCALE_PING,
            ProbeKind.SSH_BANNER,
            ProbeKind.REMOTE_ICMP_DELTA,
            ProbeKind.NAS_SMB_DELTA,
            ProbeKind.MEM_REMOTE,
        ],
        labels={
            "tailscale_ping": "ping · tailnet",
            "ssh_banner": "ssh · banner",
            "remote_icmp_delta": "lan · delta",
            "nas_smb_delta": "smb · delta",
        },
    ),
    Node(
        # worker2 — the box that hosts this dashboard, the Tower MCP, the
        # edge tunnel, and the archive relay. A PHYSICAL machine, so it earns a
        # top-row node card. Merged 2026-07-04 from the former standalone
        # `edge-tunnel` (a Cloudflare tunnel) + `archive` (the relay) service
        # cards — those are services ON worker2, not machines. Its hardware health is
        # its OWN root fs + memory, read LOCALLY (worker2 never probes its own tailnet
        # name — a box pinging itself is meaningless). The tunnel + relay ride along
        # as named sub-rows via `labels`.
        #
        # 2026-07-04 split (LEGACY-BUILD-...-worker2-tunnel-relay-split): both lines
        # used to hit the PUBLIC tower.example.com/mcp and read 401→OK —
        # but that 401 is Cloudflare Access at the EDGE, so the card stayed green
        # even if cloudflared dropped or the Tower crashed. Now each line probes the
        # real thing ON-BOX: TUNNEL_CONNECTOR reads the cloudflared connector's local
        # /ready metrics (edge-connection health, no Access noise); TOWER_LIVENESS
        # hits the Tower directly on loopback :8765 (JWT-exempt for on-box
        # callers) and appends the return-lane freshness. Each can now WARN/CRIT on
        # its own — worker2 may legitimately read non-OK if the connector or Tower dies.
        name=NODES["alpha"].health_key,
        display_name=NODES["alpha"].key,
        address="local",
        kinds=[
            ProbeKind.DISK_LOCAL,
            ProbeKind.MEM,
            ProbeKind.PROCESS_RSS,
            ProbeKind.NEXUS_LOOPBACK,
            ProbeKind.SCHEDULER,
            ProbeKind.SYNC_SERVICE,
            ProbeKind.TUNNEL_CONNECTOR,
            ProbeKind.TOWER_LIVENESS,
        ],
        disk_path="/",
        labels={
            "process_rss": "rss · control",
            "nexus_loopback": "nexus · loopback",
            "scheduler": "scheduler",
            "sync_service": "vault · sync",
            "tunnel": "tunnel · edge",
            "tower": "relay · Tower",
        },
    ),
]


def active_fleet(nodes: list[Node], planned_offline: frozenset[str]) -> list[Node]:
    """Return only nodes intended to participate in live health polling."""
    return [node for node in nodes if node.name not in planned_offline]


FLEET: list[Node] = active_fleet(_FLEET, PLANNED_OFFLINE_NODES)

# Render-only cap: the top live "jobs" panel shows at most this many job cards
# (first N of the current sort). History recording is unaffected; this bounds
# ONLY the jobs-panel render loop. Tune here.
JOBS_PANEL_MAX = 10

# Render-only cap for the dashboard's Worker Activity summary. The complete
# relay history remains available in Activity; this keeps the standardized
# 284px dashboard module free of nested scrolling.
WORKER_ACTIVITY_PANEL_MAX = 5

# --- Jobs UI: non-job classification -----------------------------------
# Some heartbeats/*.json records are watchdogs/guards monitoring something
# else, not a task with its own start + terminal outcome — they ride the same
# generic schema as real jobs but aren't one. Filtered ONLY when building Jobs
# UI contexts (routes.py); the heartbeat reader/snapshot keeps reading them
# untouched (thermal_watch.py still reads thermal-guard-charlie.json normally).
# Classification is by explicit kind/type only (PANEL-2 final compat
# retirement, FLEET-WORKER2-BUILD-20260723-slate2-final-compat-retirement) —
# the pre-kind-field legacy-id fallback was retired once every live producer
# either sets kind/type or was archived out of heartbeats/.
JOB_NONJOB_KINDS = {"watch", "guard"}   # honored if a producer sets kind/type
JOB_KIND_VALUES = {"job", "watch", "guard"}   # full recognized enum

settings = Settings()
