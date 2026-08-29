"""
PANEL-4 — explicit, versioned Watchdogs registry (static data only).

Hand-authored from three read-only per-token recons plus two PANEL-2 build
tokens that repaired findings those recons surfaced:
  - from-worker2/runs/FLEET-WORKER2-RECON-20260723-slate4-alpha-inventory (10 rows)
  - from-worker1/runs/FLEET-WORKER1-RECON-20260723-slate4-tower-collision (9 rows)
  - from-worker3/runs/FLEET-WORKER3-RECON-20260723-slate4-charlie-inventory (6 rows)
  - from-worker3/runs/FLEET-WORKER3-BUILD-20260723-slate2-charlie-kind-pathfix
    (repaired charlie-fleet-host-probe's dead ExecStart path)
  - from-worker1/runs/FLEET-WORKER1-BUILD-20260723-slate2-delta-recovery-canary
    (confirmed the delta host-probe symlink/crontab repair landed; superseded
     2026-08-03 -- see delta-systemd-host-probe, now a supervised unit)

No runtime systemctl/journalctl/ssh/subprocess call lives here or is ever
made from this module — every field is a point-in-time evidence snapshot
recorded at `evidence_as_of`, not a live probe. Reuses the existing
job/watch/guard `kind` vocabulary (app/config.py JOB_KIND_VALUES) rather than
inventing a second one.
"""
from __future__ import annotations

WATCHDOG_KIND_VALUES = {"watch", "guard"}
WATCHDOG_STATUS_VALUES = {"active", "dormant", "stale_evidence", "orphaned", "retired"}
# Updated 2026-08-25: compact Loupe, Preftool, and the photo-archive trigger
# moved from Charlie to Delta for Charlie's Omarchy outage.
_EXPECTED_HOST_COUNTS = {"alpha": 12, "charlie": 9, "delta": 9, "echo": 1}

_REQUIRED_FIELDS = (
    "id", "kind", "owner", "host", "label", "source", "protected_target",
    "cadence_timeout", "last_check_evidence", "last_action_evidence",
    "status", "status_detail", "source_of_truth", "evidence_as_of",
)

ALPHA_ROWS: list[dict] = [
    {
        "id": "alpha-systemd-nexus", "kind": "guard", "owner": "david", "host": "alpha",
        "label": "nexus.service supervision",
        "source": "~/.config/systemd/user/nexus.service (Restart=always, RestartSec=5)",
        "protected_target": "nexus.service -- Light Table dashboard + APScheduler process",
        "cadence_timeout": "continuous; 5s restart backoff",
        "last_check_evidence": "systemctl --user show -p ActiveEnterTimestamp,MainPID,SubState -> active/running",
        "last_action_evidence": "clean Stopping->Stopped->Started sequence at 2026-07-23T00:41:54 CDT (manual/deploy restart, not a crash-catch); NRestarts=0",
        "status": "dormant", "status_detail": "Registered and running; Restart= policy has never fired a crash-restart.",
        "source_of_truth": "systemctl --user cat nexus.service",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-systemd-tower", "kind": "guard", "owner": "david", "host": "alpha",
        "label": "Tower (primary)",
        "source": "~/.config/systemd/user/tower.service (Restart=always, RestartSec=3)",
        "protected_target": "tower.service -- canonical vault-search MCP server",
        "cadence_timeout": "continuous; 3s restart backoff",
        "last_check_evidence": "active since 2026-07-22T13:15:09-05:00, MainPID=33346",
        "last_action_evidence": "NRestarts=0 (no crash-restart observed)",
        "status": "dormant", "status_detail": "Canonical Tower host per CLAUDE.md; actively redeployed 2026-07-22; guard never yet needed to fire.",
        "source_of_truth": "systemctl --user cat tower.service",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-systemd-compendium-serve", "kind": "guard", "owner": "david", "host": "alpha",
        "label": "compendium-serve.service supervision",
        "source": "~/.config/systemd/user/compendium-serve.service (Restart=on-failure, RestartSec=5)",
        "protected_target": "compendium-serve.service -- loopback-only static server (127.0.0.1:8878)",
        "cadence_timeout": "continuous; 5s restart backoff",
        "last_check_evidence": "active since 2026-07-22T07:50:58-05:00",
        "last_action_evidence": "NRestarts=0",
        "status": "dormant", "status_detail": "Registered and running; guard never yet needed to fire.",
        "source_of_truth": "systemctl --user cat compendium-serve.service",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-hw-watchdog", "kind": "guard", "owner": "kernel/systemd (vendor)", "host": "alpha",
        "label": "BCM2835 hardware watchdog",
        "source": "/dev/watchdog0; systemd RuntimeWatchdogUSec=1min",
        "protected_target": "whole-host / PID1 liveness -> forced hardware reboot if systemd hangs",
        "cadence_timeout": "60s hardware timeout",
        "last_check_evidence": "wdctl: ~7s of 60s remaining at check time (actively being petted)",
        "last_action_evidence": "journalctl -b0: watchdog registered at boot 2026-07-22T07:50:55 UTC; no forced-reboot event this boot",
        "status": "dormant", "status_detail": "Active since boot; no trigger observed this boot (host has not hung).",
        "source_of_truth": "wdctl; systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-aps-run-watcher", "kind": "watch", "owner": "david (nexus app)", "host": "alpha",
        "label": "Relay run-outcome watcher",
        "source": "app/scheduler.py (job id=run-watcher); app/run_watcher.py",
        "protected_target": "relay run visibility across from-*/runs/* -- success/failure/blocked/collision/turn_end_death detection",
        "cadence_timeout": "120s tick; 21600s (6h) turn_end_death synthesis window",
        "last_check_evidence": "journalctl: 'Relay run-outcome watcher ... executed successfully' recurring",
        "last_action_evidence": "3x 'notify: routed event_key=run:<token>:success' lines observed live-firing in this recon window",
        "status": "active", "status_detail": "Confirmed live-firing via journal in the source recon.",
        "source_of_truth": "journalctl --user -u nexus.service | grep run_watcher",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-aps-thermal-watch", "kind": "watch", "owner": "david (nexus app)", "host": "alpha",
        "label": "Charlie thermal-guard watch",
        "source": "app/scheduler.py (job id=thermal-watch); app/thermal_watch.py",
        "protected_target": "charlie's fleet-thermal-guard heartbeat -- halt/recovery/approaching-emergency edges -> notify()",
        "cadence_timeout": "60s tick",
        "last_check_evidence": "source registration confirmed; process logged 'registered 9 job(s)' at last restart",
        "last_action_evidence": "no fired-edge log this window (steady-state 'ok' cooling_state -- nothing to fire)",
        "status": "stale_evidence", "status_detail": "Registered and running; only source-level + coarse runtime proof this window, no fired-edge log.",
        "source_of_truth": "cat app/thermal_watch.py; app/scheduler.py job registration",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-aps-health-watch", "kind": "watch", "owner": "david (nexus app)", "host": "alpha",
        "label": "Generic fleet health-condition watch",
        "source": "app/scheduler.py (job id=health-watch); app/health_watch.py",
        "protected_target": "disk_warn/disk_critical/backup_stale/service_down/heartbeat_stale across charlie/delta/echo",
        "cadence_timeout": "60s tick; 12h reminder re-fire",
        "last_check_evidence": "source registration confirmed; process logged 'registered 9 job(s)' at last restart",
        "last_action_evidence": "not independently observed firing in this recon's journal window",
        "status": "stale_evidence", "status_detail": "Registered and running; no matching journal lines sampled this window.",
        "source_of_truth": "cat app/health_watch.py; app/scheduler.py job registration",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-aps-nexus-selftest", "kind": "watch", "owner": "david (nexus app)", "host": "alpha",
        "label": "Notification-transport self-test canary",
        "source": "app/scheduler.py (job id=nexus-selftest); app/self_test.py",
        "protected_target": "push+ntfy notification transports themselves -- catches a silent iOS PWA-subscription drop",
        "cadence_timeout": "weekly, Sunday 09:00 America/Chicago",
        "last_check_evidence": "source registration confirmed",
        "last_action_evidence": "not observed firing this run's journal window (weekly cadence, last scheduled fire not in-window)",
        "status": "stale_evidence", "status_detail": "Registered and running; weekly cadence, no fire observed in this window.",
        "source_of_truth": "cat app/self_test.py; app/scheduler.py job registration",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-aps-deadman-ping", "kind": "guard", "owner": "david (nexus app)", "host": "alpha",
        "label": "External dead-man's switch (Nexus process liveness)",
        "source": "app/scheduler.py (job id=deadman-ping); app/deadman.py",
        "protected_target": "the Nexus process's own liveness -- catches Nexus crash / worker2 outage / tunnel death via an off-box healthchecks.io-style grace-window alert",
        "cadence_timeout": "300s tick",
        "last_check_evidence": "secrets/deadman_ping_url.txt exists, 57 bytes, mtime 2026-07-10 (provisioned)",
        "last_action_evidence": "ping success/failure not independently verified from alpha (would require the external service's own dashboard)",
        "status": "stale_evidence", "status_detail": "Provisioned and registered; external-side confirmation is out of local scope.",
        "source_of_truth": "cat app/deadman.py; ls -la secrets/deadman_ping_url.txt",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-embedded-fleet-deadman", "kind": "guard", "owner": "david (nexus app)", "host": "alpha",
        "label": "Embedded external dead-man's switch (fleet health)",
        "source": "app/jobs/heartbeat.py (_push_dead_mans_switch, called from run_heartbeat)",
        "protected_target": "FLEET health visibility itself -- external dead-man's switch keyed to overall!=crit, distinct target from the Nexus-process deadman-ping",
        "cadence_timeout": "rides the heartbeat Job's own 300s interval",
        "last_check_evidence": "source confirmed in app/jobs/heartbeat.py",
        "last_action_evidence": "journal shows 'heartbeat: overall=warn/crit' lines every ~5min -- confirms the sweep (and this embedded ping call) executes on schedule",
        "status": "active", "status_detail": "Embedded and executing every heartbeat tick.",
        "source_of_truth": "cat app/jobs/heartbeat.py; grep heartbeat_ping_url app/config.py",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "alpha-aps-conformance-watch", "kind": "watch", "owner": "david (nexus app)", "host": "alpha",
        "label": "Fleet conformance transition watcher",
        "source": "app/scheduler.py (job id=conformance-watch); app/conformance_watch.py",
        "protected_target": "per-check ok<->non-ok transitions, explicit check retirement, and cache stale/unavailable<->fresh edges across the revisioned conformance manifest -- notify()-routed, no SSH/systemd/file probes of its own",
        "cadence_timeout": "300s (5min) tick; exact-once per edge, no reminder re-fire",
        "last_check_evidence": "source registration confirmed at build time (FLEET-WORKER2-BUILD-20260730-conformance2-signal-durability); process logs 'registered N job(s)' at next restart",
        "last_action_evidence": "not yet observed firing in a live journal window -- newly registered this build, first tick silently seeds every check/cache watermark with no notification",
        "status": "stale_evidence", "status_detail": "Registered by this build; no fired-edge log yet since no real transition has occurred since registration.",
        "source_of_truth": "cat app/conformance_watch.py; app/scheduler.py job registration",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
]

CHARLIE_ROWS: list[dict] = [
    {
        "id": "charlie-thermal-governor", "kind": "guard", "owner": "david", "host": "charlie",
        "label": "Thermal governor (fan-curve control)",
        "source": "/etc/systemd/system/charlie-thermal-governor.service",
        "protected_target": "CPU/GPU thermal headroom via active fan-curve (sysfs-pwm) control, docker-job-aware",
        "cadence_timeout": "10s poll; 30s heartbeat state log; Restart=always/5s",
        "last_check_evidence": "journalctl: state=idle log line at 2026-07-23T05:18:06Z (<1min old at recon)",
        "last_action_evidence": "idle<->warming transitions logged continuously through 2026-07-23T04:35Z tied to job=True (gallery ML active), active PWM writes each warming cycle",
        "status": "active", "status_detail": "Continuous fan-curve control, live state transitions observed.",
        "source_of_truth": "systemctl status charlie-thermal-governor.service; journalctl -u charlie-thermal-governor.service",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "charlie-thermal-guard", "kind": "guard", "owner": "david", "host": "charlie",
        "label": "fleet-thermal-guard (emergency stop/restart)",
        "source": "/etc/systemd/system/fleet-thermal-guard.service -> /usr/local/sbin/fleet-thermal-guard",
        "protected_target": "gallery_server + gallery_machine_learning containers -- stopped/restarted on CPU cpu-temp emergency (>=110C for 15s -> stop; <80C for 120s -> restart)",
        "cadence_timeout": "5s poll; vault publish on state/action edge + 120s keepalive; EMERGENCY_C=110, RESTART_C=80, HOLDOFF_MAX=3/hour",
        "last_check_evidence": "/run/fleet/thermal-guard-charlie.json 4s old and vault copy 119s old at check (5s poll, 120s keepalive); cooling_state=ok, reindex of thresholds unchanged, holdoff=false",
        "last_action_evidence": "thermal_guard_action=started:gallery_machine_learning,gallery_server @2026-07-22T05:25:06Z, corroborated by docker inspect StartedAt exact match -- a real emergency-stop/restart cycle fired and recovered",
        "status": "active", "status_detail": "Real fired-and-recovered emergency cycle observed. PANEL-2 (token FLEET-WORKER3-BUILD-20260723-slate2-charlie-kind-pathfix) added an explicit top-level kind:\"guard\" field to its heartbeat, aligning it with this registry's taxonomy.",
        "source_of_truth": "cat /run/fleet/thermal-guard-charlie.json (local truth, 5s); cat ~/Vaults/loupe-vault/heartbeats/thermal-guard-charlie.json (mesh transport, edge + 120s); systemctl status fleet-thermal-guard.service",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "charlie-smartmontools", "kind": "watch", "owner": "system (vendor package)", "host": "charlie",
        "label": "smartd SMART health monitor",
        "source": "/usr/lib/systemd/system/smartmontools.service; /etc/smartd.conf",
        "protected_target": "3x NVMe drives -- SMART health/error-count regression, mail-alert on threshold breach",
        "cadence_timeout": "~30min default smartd check interval",
        "last_check_evidence": "journalctl: NVMe error-count deltas logged per device at last check",
        "last_action_evidence": "no alert fired -- all devices remain PASSED, non-critical spare-remaining",
        "status": "dormant", "status_detail": "Active since 2026-07-10T04:53:44Z; no threshold breach observed.",
        "source_of_truth": "systemctl status smartmontools.service; journalctl -u smartmontools.service",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "charlie-gallery-faces-resume-watcher", "kind": "watch", "owner": "david", "host": "charlie",
        "label": "Gallery faces-resume watcher (one-shot)",
        "source": "~/.config/systemd/user/gallery-faces-resume-watcher.{service,timer}",
        "protected_target": "auto-resumes paused gallery ML queues once GPU backlog drains and the ML GPU is idle -- single-fire, self-disabling by design",
        "cadence_timeout": "was OnUnitActiveSec=5min; self-disables on first successful fire",
        "last_check_evidence": "watcher.log last line 2026-07-10T04:44:15Z: 'appended vault session note'",
        "last_action_evidence": "fired 2026-07-10T04:44:16Z: resumed all 5 queues, ntfy sent, FIRED_MARKER written, timer self-disabled -- correct designed behavior",
        "status": "retired", "status_detail": "Retired by design after its single intended fire on 2026-07-10 -- expected, not a failure. Zero ongoing coverage now, by design.",
        "source_of_truth": "systemctl --user status gallery-faces-resume-watcher.timer",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "charlie-fleet-host-probe", "kind": "watch", "owner": "david", "host": "charlie",
        "label": "Host telemetry probe",
        "source": "/etc/systemd/system/fleet-host-probe.service -> ExecStart=/usr/bin/python3 /home/david/Vaults/homelab-vault/tools/bin/fleet_host_probe.py --loop --interval 120",
        "protected_target": "writes host-charlie.json heartbeat every 120s -- underlies other watchdogs' visibility into charlie liveness",
        "cadence_timeout": "120s loop interval; Restart=always/RestartSec=3",
        "last_check_evidence": "systemctl is-active=active, ExecStart --interval 120, exactly one live process (PID 2940857); crontab -l now returns zero host_probe lines",
        "last_action_evidence": "repaired by PANEL-2 (token FLEET-WORKER3-BUILD-20260723-slate2-charlie-kind-pathfix): ExecStart repointed from the deleted library path to the canonical homelab-vault path, restarted (PID 861251->4092143), duplicate @reboot crontab entry reported fixed. Re-verified 2026-08-03: the @reboot line was still present and would have started a second writer alongside the unit at next boot; removed (backup ~/crontab.bak-20260803-hostprobe). Loop interval raised 25s -> 120s against the 600s consumer staleness budget",
        "status": "active", "status_detail": "Dead ExecStart path (pointed at a retired, deleted vault tree) found by recon and repaired same-day by PANEL-2 -- this row reflects the REPAIRED state, not the recon's original orphan finding.",
        "source_of_truth": "systemctl cat fleet-host-probe.service; crontab -l",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "charlie-gallery-docker-guard", "kind": "guard", "owner": "docker engine (infra-level)", "host": "charlie",
        "label": "Docker restart-policy + healthcheck (gallery stack)",
        "source": "docker inspect gallery_server / gallery_machine_learning / gallery_redis / gallery_postgres",
        "protected_target": "all 4 gallery containers -- auto-restarted on crash by dockerd; server+ML also carry an active healthcheck",
        "cadence_timeout": "RestartPolicy=always (unlimited); healthcheck at image-default interval",
        "last_check_evidence": "docker inspect .State.Health.Status=healthy, FailingStreak=0 for server+ML",
        "last_action_evidence": "server+ML StartedAt=2026-07-22T05:25:06-07Z -- matches fleet-thermal-guard's emergency restart exactly (same event)",
        "status": "active", "status_detail": "All 4 containers up, server+ML reporting healthy.",
        "source_of_truth": "docker ps -a; docker inspect <name>",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "charlie-temple-link-observer", "kind": "watch", "owner": "david", "host": "charlie",
        "label": "Temple external link observer",
        "source": "~/.config/systemd/user/temple-link-observer.service -> ~/.local/bin/temple-link-observer.py",
        "protected_target": "Temple's storage-subnet ICMP + SMB reachability, Charlie-to-Delta peer-fabric health, ARP state, route, and independent Tailscale reachability",
        "cadence_timeout": "5s poll; 300s evidence heartbeat; Restart=always/5s",
        "last_check_evidence": "service active/running at 2026-08-03T00:47Z; observer_start persisted state=ok with ICMP, SMB, peer-fabric, and Tailscale all true",
        "last_action_evidence": "SIGKILL lifecycle canary changed MainPID 2626232->2627130 and systemd restored the observer automatically; a second observer_start record persisted",
        "status": "active", "status_detail": "Continuously records the network-side half of a Temple outage without reading NAS bytes.",
        "source_of_truth": "systemctl --user status temple-link-observer.service; ~/.local/state/temple-link-observer/{latest.json,events.jsonl}",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "charlie-temple-link-watch-ensure", "kind": "guard", "owner": "david", "host": "charlie",
        "label": "Temple local collector re-seed guard",
        "source": "~/.config/systemd/user/temple-link-watch-ensure.{service,timer} -> Temple ~/.local/bin/temple-link-watch-ensure.sh",
        "protected_target": "restarts Temple's local carrier/driver collector within about 60s after Temple becomes SSH-reachable",
        "cadence_timeout": "60s timer with <=5s randomized delay; Charlie user linger enabled",
        "last_check_evidence": "timer enabled/active with successful SSH ensure result at 2026-08-03T00:47Z",
        "last_action_evidence": "real collector lifecycle canary stopped PID 20970; the guard rejected the stale reused PID by command line and re-seeded healthy PID 21036",
        "status": "active", "status_detail": "Charlie-owned persistence avoids relying on DSM boot-task state that synoschedtask does not expose.",
        "source_of_truth": "systemctl --user status temple-link-watch-ensure.timer temple-link-watch-ensure.service; ssh echo cat ~/.local/state/temple-link-watch/watch.pid",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "charlie-eno1-hang-watchdog", "kind": "guard", "owner": "david", "host": "charlie",
        "label": "eno1 e1000e transmit-hang auto-reset",
        "source": "/etc/systemd/system/eno1-hang-watchdog.{service,timer} -> /usr/local/sbin/eno1-hang-watchdog (system scope, root); canonical copy homelab-vault/tools/bin/eno1-hang-watchdog",
        "protected_target": "the 192.0.2.0/24 storage subnet and Temple's NAT/Tailscale gateway, both carried by eno1; resets the NIC when it enters a continuous e1000e transmit-unit hang",
        "cadence_timeout": "60s timer (OnBootSec=3min, AccuracySec=5s); acts only on >=5 hang messages in 3min with all storage peers unreachable; hard cap 3 resets/hour then aborts for a human",
        "last_check_evidence": "timer active/enabled cycling at 60s with service Result=success at 2026-08-07T16:33Z; healthy-path manual run exited 0 leaving carrier_changes unchanged at 6 and writing no reset record",
        "last_action_evidence": "false-positive guard canary with unreachable peers and no hang signature correctly refused to reset, logged 'only 0 hang msgs/3min (need 5) - NOT resetting, this is a different fault', carrier_changes unchanged",
        "status": "active", "status_detail": "THE ONLY MUTATING TEMPLE-PATH MECHANISM. The link-forensics collectors stay evidence-only by design; this one performs the proven ip link down/up recovery, which preserves the static address, dnsmasq's interface-bound DHCP socket, and the NAT rules. Added after the hang recurred 2026-08-07 having run ~3h undetected, twice.",
        "source_of_truth": "systemctl status eno1-hang-watchdog.timer; journalctl -t eno1-hang-watchdog; /var/lib/eno1-hang-watchdog/resets.log",
        "evidence_as_of": "2026-08-07T16:33Z",
    },
]

DELTA_ROWS: list[dict] = [
    {
        "id": "delta-wifi-guard", "kind": "guard", "owner": "david", "host": "delta",
        "label": "fleet-wifi-guard (Broadcom wl hang recovery)",
        "source": "/etc/systemd/system/fleet-wifi-guard.service -> /usr/local/sbin/fleet-wifi-guard (runs as root; modprobe requires it)",
        "protected_target": "wlp2s0 -- delta's ONLY internet path. The 192.0.2.0/24 storage subnet is link-scope with no default route, so a wl hang is a total internet blackout with no fallback (Loupe, Prospect, Worker1 remote control, nightly GitHub push).",
        "cadence_timeout": "30s poll; acts after 5 consecutive LAN-gateway failures (~2.5 min); 180s boot grace, 120s post-recovery grace; HOLDOFF_MAX=3/hour then sticky give-up",
        "last_check_evidence": "installed 2026-08-03T12:56Z; state=ok, gateway=192.168.1.254 reachable, operstate=up carrier=1, consecutive_failures=0; /run heartbeat 22s old against a 30s poll and the vault copy throttling correctly (62s -> 212s untouched between 300s keepalives)",
        "last_action_evidence": "none -- no recovery has fired since install",
        "status": "dormant", "status_detail": "Registered and running; guard never yet needed to fire. Probes the LAN gateway OUT OF wlp2s0 rather than an internet address, because a WAN outage and a wl hang are indistinguishable from Tailscale's view: 2026-08-01 was a 5h37m WAN failure that self-healed with the interface healthy, while 2026-08-03 was a 2h10m wl hang needing a manual modprobe reload. Probing the internet would tear down a working interface during every ISP outage.",
        "source_of_truth": "systemctl status fleet-wifi-guard.service; cat /run/fleet/wifi-guard-delta.json; homelab-vault/heartbeats/wifi-guard-delta.json",
        "evidence_as_of": "2026-08-03T13:04Z",
    },
    {
        "id": "delta-kernel-nmi-watchdog", "kind": "guard", "owner": "kernel (vendor)", "host": "delta",
        "label": "NMI hard/soft-lockup watchdog",
        "source": "kernel.nmi_watchdog=1 / kernel.watchdog_thresh=10s",
        "protected_target": "system-wide hard/soft-lockup detection",
        "cadence_timeout": "10s threshold",
        "last_check_evidence": "sysctl confirms nmi_watchdog=1 active",
        "last_action_evidence": "no lockup detected",
        "status": "dormant", "status_detail": "Active/healthy; no lockup ever triggered.",
        "source_of_truth": "sysctl kernel.nmi_watchdog kernel.watchdog_thresh",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "delta-systemd-loupe", "kind": "guard", "owner": "david", "host": "delta",
        "label": "loupe.service supervision",
        "source": "~/.config/systemd/user/loupe.service (Restart=always/3s)",
        "protected_target": "loupe.service (:8000)",
        "cadence_timeout": "continuous; 3s restart backoff",
        "last_check_evidence": "active 2026-08-25T00:42Z (delta); listening 0.0.0.0:8000; HTTP 200",
        "last_action_evidence": "NRestarts=0",
        "status": "dormant", "status_detail": "Registered and running; guard never yet needed to fire.",
        "source_of_truth": "systemctl --user cat loupe.service",
        "evidence_as_of": "2026-08-25T00:42Z",
    },
    {
        "id": "alpha-systemd-prospect", "kind": "guard", "owner": "david", "host": "alpha",
        "label": "prospect.service supervision",
        "source": "~/.config/systemd/user/prospect.service (Restart=always/3s)",
        "protected_target": "prospect.service (:8787)",
        "cadence_timeout": "continuous; 3s restart backoff",
        "last_check_evidence": "active since 2026-08-06T15:36:33Z (alpha); listening *:8787",
        "last_action_evidence": "NRestarts=0",
        "status": "dormant", "status_detail": "Registered and running; guard never yet needed to fire.",
        "source_of_truth": "systemctl --user cat prospect.service",
        "evidence_as_of": "2026-08-07T14:40Z",
    },
    {
        "id": "delta-systemd-preftool", "kind": "guard", "owner": "david", "host": "delta",
        "label": "preftool.service supervision",
        "source": "~/.config/systemd/user/preftool.service (Restart=on-failure/100ms)",
        "protected_target": "preftool.service",
        "cadence_timeout": "continuous",
        "last_check_evidence": "active 2026-08-25T00:42Z (delta); listening 0.0.0.0:8770; HTTP 200",
        "last_action_evidence": "NRestarts=0",
        "status": "dormant", "status_detail": "Registered and running; guard never yet needed to fire.",
        "source_of_truth": "systemctl --user cat preftool.service",
        "evidence_as_of": "2026-08-25T00:42Z",
    },
    {
        "id": "delta-systemd-vault-webdav", "kind": "guard", "owner": "david", "host": "delta",
        "label": "vault-webdav.service (retired 2026-08-07 -- disabled, no ingress)",
        "source": "~/.config/systemd/user/vault-webdav.service (Restart=on-failure/5s; unit retained, disabled)",
        "protected_target": "vault-webdav.service (formerly :8775)",
        "cadence_timeout": "n/a -- unit disabled, no longer scheduled to run",
        "last_check_evidence": "inactive; UnitFileState=disabled; zero listeners on :8775",
        "last_action_evidence": "retired 2026-08-07: David confirmed the Obsidian WebDAV setup was never "
                                "completed and he uses Syncthing directly",
        "status": "retired",
        "status_detail": "Deliberately retired, not a failure. Unit file retained for reversibility.",
        "source_of_truth": "systemctl --user status vault-webdav.service; ss -ltnp | grep 8775 (delta)",
        "evidence_as_of": "2026-08-07T14:40Z",
    },
    {
        "id": "delta-systemd-photo-archive-backup", "kind": "guard", "owner": "david", "host": "delta",
        "label": "photo-archive-backup.service supervision",
        "source": "~/.config/systemd/user/photo-archive-backup.service (Restart=no; timer-owned)",
        "protected_target": "photo-archive-backup.service",
        "cadence_timeout": "timer-fired; no service restart loop",
        "last_check_evidence": "timer enabled and active on delta; service idle between timer fires (expected)",
        "last_action_evidence": "NRestarts=0",
        "status": "dormant", "status_detail": "Registered; idle between scheduled timer fires, guard never yet needed to fire.",
        "source_of_truth": "systemctl --user cat photo-archive-backup.service",
        "evidence_as_of": "2026-08-25T00:37Z",
    },
    {
        "id": "delta-systemd-syncthing", "kind": "guard", "owner": "david", "host": "delta",
        "label": "syncthing.service supervision",
        "source": "~/.config/systemd/user/syncthing.service (Restart=on-failure/5s)",
        "protected_target": "syncthing.service (:22000/:8384)",
        "cadence_timeout": "continuous; 5s restart backoff",
        "last_check_evidence": "active, current uptime confirmed",
        "last_action_evidence": "fired once 2026-07-08 (NOPERMISSION), self-healed via Restart=",
        "status": "active", "status_detail": "Real fired-and-recovered crash-restart on 2026-07-08.",
        "source_of_truth": "systemctl --user cat syncthing.service",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "delta-systemd-tower", "kind": "guard", "owner": "david", "host": "delta",
        "label": "Tower (retired secondary -- disabled, no ingress)",
        "source": "~/.config/systemd/user/tower.service (Restart=on-failure/3s; unit retained, disabled)",
        "protected_target": "tower.service -- vault-search MCP, formerly 0.0.0.0:8765, CF-Access-JWT gated",
        "cadence_timeout": "n/a -- unit disabled, no longer scheduled to run",
        "last_check_evidence": "reversibly retired 2026-08-02: systemctl --user disable --now tower.service -> inactive/dead, port 8765 no longer listening",
        "last_action_evidence": "retired by token FLEET-AUTO-BUILD-20260802-panel-live-watchdog-evidence: service disabled/inactive on delta, unit file retained (not deleted), preimage/state backed up to /home/david/.local/state/tower-retirement-20260802T225000Z on delta for rollback",
        "status": "retired", "status_detail": "Deliberately retired secondary (never had a working ingress path since before this retirement) -- expected, not a failure. Distinct from alpha-systemd-tower, which remains the active primary; Lookout cold standby remains inactive by design.",
        "source_of_truth": "systemctl --user status tower.service; ss -ltnp | grep 8765 (delta)",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
    {
        "id": "delta-systemd-host-probe", "kind": "watch", "owner": "david", "host": "delta",
        "label": "Host telemetry probe (systemd)",
        "source": "/etc/systemd/system/fleet-host-probe.service -> ExecStart=/usr/bin/python3 /home/david/Vaults/homelab-vault/tools/bin/fleet_host_probe.py --loop --interval 120",
        "protected_target": "delta host telemetry -> loupe-vault/heartbeats/host-delta.json",
        "cadence_timeout": "120s loop interval; Restart=always/RestartSec=3",
        "last_check_evidence": "systemctl is-active=active, is-enabled=enabled, exactly one live process (PID 138986); host-delta.json mtime delta 11:29:42 -> 11:31:44 confirms the 120s cadence; crontab -l returns zero host_probe lines",
        "last_action_evidence": "replaced 2026-08-03: PID 2822258 was NOT cron-supervised -- it was a manual process started 2026-07-23T05:52 inside login session-c49410.scope (State=closing), reparented to init, with no unit at all (systemctl is-enabled returned not-found). It would not have survived a reboot while health_watch.py alarms on host-delta.json at 600s. Installed an enabled /etc/systemd/system/fleet-host-probe.service (Restart=always) mirroring charlie's, killed the orphan, and removed the @reboot crontab line that would otherwise have started a second writer on the same file at next boot (backup ~/crontab.bak-20260803-hostprobe)",
        "status": "active", "status_detail": "Now a supervised systemd unit. The prior cron @reboot mechanism is retired: it coexisted with an unsupervised manual process, and after the unit was installed it would have produced two concurrent writers on host-delta.json at next boot.",
        "source_of_truth": "crontab -l; readlink ~/bin/nexus_run; cat ~/Vaults/loupe-vault/heartbeats/host-delta.json",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
]

TEMPLE_ROWS: list[dict] = [
    {
        "id": "temple-local-link-forensics", "kind": "watch", "owner": "david", "host": "echo",
        "label": "Local eth0 carrier and r8169 forensic collector",
        "source": "~/.local/bin/temple-link-watch.py; ~/.local/state/temple-link-watch/{latest.json,events.jsonl}",
        "protected_target": "loss of eth0 carrier, link renegotiation, carrier-change count, r8169/private and sysfs error counters, kernel evidence, boot identity, and uptime while Temple is unreachable externally",
        "cadence_timeout": "event-driven ip monitor link; 300s evidence heartbeat; Charlie re-seed <=65s after SSH reachability",
        "last_check_evidence": "collector PID 21036 running at 2026-08-03T00:47Z with carrier=1, speed=1000, duplex=full, carrier_changes=322",
        "last_action_evidence": "TERM lifecycle canary persisted watch_stop and the re-seed guard produced a new watch_start under PID 21036 without disturbing eth0",
        "status": "active", "status_detail": "Evidence remains local through a single-NIC outage and is available for collection when Temple returns.",
        "source_of_truth": "ssh echo cat ~/.local/state/temple-link-watch/latest.json; events.jsonl; watch.pid",
        "evidence_as_of": "2026-08-03T12:35Z",
    },
]

REGISTRY: list[dict] = [*ALPHA_ROWS, *CHARLIE_ROWS, *DELTA_ROWS, *TEMPLE_ROWS]


def _validate(rows: list[dict]) -> None:
    seen_ids: set[str] = set()
    host_counts: dict[str, int] = {}
    for row in rows:
        missing = [f for f in _REQUIRED_FIELDS if not row.get(f)]
        if missing:
            raise ValueError(f"watchdogs_registry row {row.get('id')!r} missing fields: {missing}")
        rid = row["id"]
        if rid in seen_ids:
            raise ValueError(f"watchdogs_registry duplicate id: {rid!r}")
        seen_ids.add(rid)
        if row["kind"] not in WATCHDOG_KIND_VALUES:
            raise ValueError(f"watchdogs_registry row {rid!r} has invalid kind: {row['kind']!r}")
        if row["status"] not in WATCHDOG_STATUS_VALUES:
            raise ValueError(f"watchdogs_registry row {rid!r} has invalid status: {row['status']!r}")
        host_counts[row["host"]] = host_counts.get(row["host"], 0) + 1
    if host_counts != _EXPECTED_HOST_COUNTS:
        raise ValueError(
            f"watchdogs_registry host counts {host_counts} != expected {_EXPECTED_HOST_COUNTS}"
        )
    if len(rows) != sum(_EXPECTED_HOST_COUNTS.values()):
        raise ValueError(
            f"watchdogs_registry total rows {len(rows)} != "
            f"{sum(_EXPECTED_HOST_COUNTS.values())}"
        )


_validate(REGISTRY)


def get_registry(host: str | None = None) -> list[dict]:
    """Static registry rows, optionally filtered to an exact host. Pure data
    access -- no subprocess/systemctl/journalctl/ssh call is ever made here."""
    if host is None:
        return list(REGISTRY)
    return [row for row in REGISTRY if row["host"] == host]


def summary() -> dict:
    """Per-host counts + flagged (non-active/dormant) counts, for the
    collapsed accordion header row."""
    hosts: dict[str, dict] = {}
    for row in REGISTRY:
        h = hosts.setdefault(row["host"], {"host": row["host"], "count": 0, "flagged": 0})
        h["count"] += 1
        if row["status"] in ("stale_evidence", "orphaned"):
            h["flagged"] += 1
    return {
        "hosts": [hosts[h] for h in ("alpha", "charlie", "delta", "echo") if h in hosts],
        "total": len(REGISTRY),
    }
