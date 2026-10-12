"""
The individual checks. Each probe is a pure-ish function: given a Node and
settings, it returns a ProbeResult and never raises — a failed probe is data
(UNKNOWN/CRIT with a detail), not an exception that kills the sweep.

All remote work goes over ssh with BatchMode so the headless poller never hits
an interactive trust prompt. Host keys are assumed already seeded on worker2
(ssh-keyscan), consistent with the fleet's keyless-mesh setup.
"""
from __future__ import annotations

import asyncio
import re
import socket
import time
import weakref
from pathlib import Path

import httpx

from .config import Node, Settings
from .config import NAS_PROBE_TARGET
from .models import Health, ProbeResult
from .seats import ALL_SEATS

SOURCE_HOST = socket.gethostname()

# A heartbeat fans every probe out concurrently.  Several Charlie checks use
# separate SSH commands, so an unconstrained sweep can create a burst of
# simultaneous handshakes and turn one short transport wobble into a red node.
# Keep a small per-host gate and retry transport failures once.  Negative
# command results (rc 1, etc.) remain immediate evidence and are never retried.
SSH_PER_HOST_CONCURRENCY = 2
TRANSPORT_ATTEMPTS = 2
TRANSPORT_RETRY_DELAY_SECONDS = 0.2
_SSH_GATES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _ssh_gate(host: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gates = _SSH_GATES.setdefault(loop, {})
    gate = gates.get(host)
    if gate is None:
        gate = asyncio.Semaphore(SSH_PER_HOST_CONCURRENCY)
        gates[host] = gate
    return gate


async def _local(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    """Run a command on THIS box (no ssh). Returns (rc, out, err); never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode().strip(), err.decode().strip()
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
        return 255, "", str(e)


async def _ssh_once(host: str, remote_cmd: str, timeout: int) -> tuple[int, str, str]:
    cmd = [
        "ssh", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "StrictHostKeyChecking=accept-new",
        host, remote_cmd,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout + 4)
        return proc.returncode, out.decode().strip(), err.decode().strip()
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
        return 255, "", str(e)


async def _ssh(host: str, remote_cmd: str, timeout: int) -> tuple[int, str, str]:
    """Run a bounded SSH command with per-host burst control and one retry.

    rc=255 is OpenSSH's transport/session failure class.  A real remote
    command failure is returned unchanged on its first attempt.
    """
    async with _ssh_gate(host):
        result = await _ssh_once(host, remote_cmd, timeout)
        if result[0] != 255:
            return result
        await asyncio.sleep(TRANSPORT_RETRY_DELAY_SECONDS)
        return await _ssh_once(host, remote_cmd, timeout)


async def probe_ping(node: Node, s: Settings) -> ProbeResult:
    """TCP reachability. Cheaper and more meaningful than ICMP for a service host."""
    start = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(TRANSPORT_ATTEMPTS):
        try:
            fut = asyncio.open_connection(node.address, node.tcp_port)
            reader, writer = await asyncio.wait_for(fut, timeout=s.tcp_timeout_seconds)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            ms = int((time.perf_counter() - start) * 1000)
            return ProbeResult(node=node.name, kind="ping", health=Health.OK,
                               value=f"{ms} ms", latency_ms=ms,
                               detail=f"tcp {node.address}:{node.tcp_port} open",
                               source_host=SOURCE_HOST, target=f"{node.address}:{node.tcp_port}",
                               method="tcp_connect",
                               timeout_ms=int(s.tcp_timeout_seconds * 1000))
        except Exception as e:
            last_error = e
            if attempt + 1 < TRANSPORT_ATTEMPTS:
                await asyncio.sleep(TRANSPORT_RETRY_DELAY_SECONDS)
    assert last_error is not None
    return ProbeResult(node=node.name, kind="ping", health=Health.CRIT,
                       value="unreachable", detail=f"{type(last_error).__name__}: {last_error}",
                       source_host=SOURCE_HOST, target=f"{node.address}:{node.tcp_port}",
                       method="tcp_connect",
                       timeout_ms=int(s.tcp_timeout_seconds * 1000),
                       error_class=type(last_error).__name__)


_PONG_RE = re.compile(r"pong from .+ via (\S+) in\s+(\d+(?:\.\d+)?)ms")


async def probe_tailscale_ping(node: Node, s: Settings) -> ProbeResult:
    """Tailnet reachability from worker2. This is the real ping-like path for
    machines worker2 only reaches over Tailscale.

    `tailscale ping -c 1` exits rc=1 for a perfectly valid DERP-relayed pong
    (it only exits 0 once a DIRECT path is established), so rc alone cannot
    drive health here — a successful DERP pong is not a failure, it is a
    working tailnet path through a relay. We parse the pong line itself and
    only fall back to a second bounded attempt when no pong line is present
    at all. A valid first-attempt pong is OK regardless of path (direct or
    DERP) — the card still visibly labels a DERP path and its latency, but a
    relayed pong on the first try is healthy, not degraded. A retry that
    recovers is downgraded to WARN (not OK) because it took two attempts to
    get a working path, even if that path is direct.
    """
    timeout = s.tcp_timeout_seconds + 2
    timeout_ms = int(timeout * 1000)
    cmd = ["tailscale", "ping", "-c", "1", node.address]

    async def _attempt() -> tuple[str, str] | None:
        """One bounded ping attempt. Returns (path, latency_str) on a valid
        pong line, None on a miss (no pong found in stdout/stderr)."""
        _rc, out, err = await _local(cmd, timeout)
        text = out or err
        m = _PONG_RE.search(text)
        if not m:
            return None
        return m.group(1), m.group(2)

    first = await _attempt()
    if first is not None:
        path, latency_str = first
        ms = int(float(latency_str))
        if path.upper().startswith("DERP"):
            return ProbeResult(node=node.name, kind="tailscale_ping", health=Health.OK,
                               value=f"reachable via {path} (relay) — {ms} ms",
                               latency_ms=ms, detail=f"pong via {path} in {ms}ms; no direct path established",
                               source_host=SOURCE_HOST, target=node.address,
                               method="tailscale_ping",
                               timeout_ms=timeout_ms)
        return ProbeResult(node=node.name, kind="tailscale_ping", health=Health.OK,
                           value=f"direct — {ms} ms",
                           latency_ms=ms, detail=f"pong via {path} (direct) in {ms}ms",
                           source_host=SOURCE_HOST, target=node.address,
                           method="tailscale_ping",
                           timeout_ms=timeout_ms)

    second = await _attempt()
    if second is not None:
        path, latency_str = second
        ms = int(float(latency_str))
        kind = "relay" if path.upper().startswith("DERP") else "direct"
        return ProbeResult(node=node.name, kind="tailscale_ping", health=Health.WARN,
                           value=f"reachable via {path} ({kind}) after retry — {ms} ms",
                           latency_ms=ms, detail=f"first attempt: no pong; retry: pong via {path} in {ms}ms",
                           source_host=SOURCE_HOST, target=node.address,
                           method="tailscale_ping",
                           timeout_ms=timeout_ms)

    return ProbeResult(node=node.name, kind="tailscale_ping", health=Health.CRIT,
                       value="unreachable", detail="no pong after 2 attempts",
                       source_host=SOURCE_HOST, target=node.address,
                       method="tailscale_ping",
                       timeout_ms=timeout_ms,
                       error_class="tailscale_ping_failed")


async def probe_ssh_banner(node: Node, s: Settings) -> ProbeResult:
    """TCP open plus SSH banner timing. This catches DSM/sshd stalls separately
    from raw network reachability."""
    start = time.perf_counter()
    last_error: Exception | None = None
    for attempt in range(TRANSPORT_ATTEMPTS):
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(node.address, node.tcp_port),
                timeout=s.tcp_timeout_seconds,
            )
            try:
                banner_b = await asyncio.wait_for(reader.readline(), timeout=s.ssh_timeout_seconds)
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            ms = int((time.perf_counter() - start) * 1000)
            banner = banner_b.decode(errors="replace").strip()
            if banner.startswith("SSH-"):
                health = Health.WARN if ms >= 2000 else Health.OK
                return ProbeResult(node=node.name, kind="ssh_banner", health=health,
                                   value=f"{ms} ms", latency_ms=ms,
                                   detail=banner[:120],
                                   source_host=SOURCE_HOST,
                                   target=f"{node.address}:{node.tcp_port}",
                                   method="ssh_banner",
                                   timeout_ms=int((s.tcp_timeout_seconds + s.ssh_timeout_seconds) * 1000))
            return ProbeResult(node=node.name, kind="ssh_banner", health=Health.WARN,
                               value=f"{ms} ms", latency_ms=ms,
                               detail=f"unexpected banner: {banner[:120]}",
                               source_host=SOURCE_HOST,
                               target=f"{node.address}:{node.tcp_port}",
                               method="ssh_banner",
                               timeout_ms=int((s.tcp_timeout_seconds + s.ssh_timeout_seconds) * 1000))
        except Exception as e:
            last_error = e
            if attempt + 1 < TRANSPORT_ATTEMPTS:
                await asyncio.sleep(TRANSPORT_RETRY_DELAY_SECONDS)
    assert last_error is not None
    return ProbeResult(node=node.name, kind="ssh_banner", health=Health.CRIT,
                       value="timeout", detail=f"{type(last_error).__name__}: {last_error}",
                       source_host=SOURCE_HOST,
                       target=f"{node.address}:{node.tcp_port}",
                       method="ssh_banner",
                       timeout_ms=int((s.tcp_timeout_seconds + s.ssh_timeout_seconds) * 1000),
                       error_class=type(last_error).__name__)


async def probe_remote_icmp(node: Node, s: Settings) -> ProbeResult:
    """Tiny delegated ICMP path check, used for storage-LAN truth that worker2
    cannot observe directly."""
    if not (node.path_probe_host and node.path_probe_target):
        return ProbeResult(node=node.name, kind="remote_icmp", health=Health.UNKNOWN,
                           value="?", detail="no path_probe_host/path_probe_target",
                           source_host=SOURCE_HOST, method="remote_icmp")
    rc, out, err = await _ssh(
        node.path_probe_host,
        f"ping -c 1 -W 2 {node.path_probe_target}",
        s.tcp_timeout_seconds + 2,
    )
    text = out or err
    m = re.search(r"time=(\d+(?:\.\d+)?)\s*ms", text)
    ms = int(float(m.group(1))) if m else None
    if rc == 0:
        health = Health.WARN if (ms is not None and ms >= 10) else Health.OK
        return ProbeResult(node=node.name, kind="remote_icmp", health=health,
                           value=(f"{ms} ms" if ms is not None else "ok"),
                           latency_ms=ms, detail=f"{node.path_probe_host} -> {node.path_probe_target}",
                           source_host=node.path_probe_host,
                           target=node.path_probe_target,
                           method="icmp",
                           timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000))
    if rc == 255 or err.startswith("ssh:") or err.startswith("SSH:"):
        return ProbeResult(node=node.name, kind="remote_icmp", health=Health.UNKNOWN,
                           value="?", detail=f"source host {node.path_probe_host} unreachable: {(err or out or 'ssh failed')[:160]}",
                           source_host=node.path_probe_host, target=node.path_probe_target,
                           method="icmp", timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000),
                           error_class="source_unreachable")
    return ProbeResult(node=node.name, kind="remote_icmp", health=Health.CRIT,
                       value="unreachable", detail=text[:200],
                       source_host=node.path_probe_host,
                       target=node.path_probe_target,
                       method="icmp",
                       timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000),
                       error_class="icmp_failed")


async def probe_disk(node: Node, s: Settings) -> ProbeResult:
    """df -P over ssh; warn/crit against thresholds."""
    if not node.ssh_host:
        return ProbeResult(node=node.name, kind="disk", health=Health.UNKNOWN,
                           detail="no ssh_host configured")
    rc, out, err = await _ssh(
        node.ssh_host, f"df -P {node.disk_path} | tail -1", s.ssh_timeout_seconds
    )
    if rc != 0 or not out:
        return ProbeResult(node=node.name, kind="disk", health=Health.UNKNOWN,
                           value="?", detail=(err or "df failed")[:200])
    try:
        pct = int(out.split()[4].rstrip("%"))
    except (IndexError, ValueError):
        return ProbeResult(node=node.name, kind="disk", health=Health.UNKNOWN,
                           detail=f"unparseable df: {out[:120]}")
    health = (Health.CRIT if pct >= s.disk_crit_pct
              else Health.WARN if pct >= s.disk_warn_pct
              else Health.OK)
    return ProbeResult(node=node.name, kind="disk", health=health,
                       value=f"{pct}%", detail=f"{node.disk_path} used")


async def probe_disk_local(node: Node, s: Settings) -> ProbeResult:
    """df on THIS box — worker2 hosts the dashboard, so it reads its own root fs
    locally rather than SSHing to its own tailnet name (meaningless self-probe)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "df", "-P", node.disk_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=s.tcp_timeout_seconds)
        rc, out, err = proc.returncode, out_b.decode().strip(), err_b.decode().strip()
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
        return ProbeResult(node=node.name, kind="disk", health=Health.UNKNOWN,
                           value="?", detail=str(e)[:200])
    line = out.splitlines()[-1] if out else ""
    if rc != 0 or not line:
        return ProbeResult(node=node.name, kind="disk", health=Health.UNKNOWN,
                           value="?", detail=(err or "df failed")[:200])
    try:
        pct = int(line.split()[4].rstrip("%"))
    except (IndexError, ValueError):
        return ProbeResult(node=node.name, kind="disk", health=Health.UNKNOWN,
                           detail=f"unparseable df: {line[:120]}")
    health = (Health.CRIT if pct >= s.disk_crit_pct
              else Health.WARN if pct >= s.disk_warn_pct
              else Health.OK)
    return ProbeResult(node=node.name, kind="disk", health=health,
                       value=f"{pct}%", detail=f"{node.disk_path} used")


async def probe_mem_local(node: Node, s: Settings) -> ProbeResult:
    """Memory pressure on THIS box, read from /proc/meminfo. Used% is measured
    against MemAvailable (the kernel's honest 'free-ish' figure), not MemFree,
    so cache/buffers don't read as pressure. Local-only — no ssh."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for row in fh:
                key, _, rest = row.partition(":")
                info[key.strip()] = int(rest.split()[0])  # kB
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
    except (OSError, KeyError, ValueError, IndexError) as e:
        return ProbeResult(node=node.name, kind="mem", health=Health.UNKNOWN,
                           value="?", detail=f"meminfo read failed: {e}"[:200])
    if total <= 0:
        return ProbeResult(node=node.name, kind="mem", health=Health.UNKNOWN,
                           value="?", detail="MemTotal=0")
    pct = int(round((total - avail) / total * 100))
    used_gb, total_gb = (total - avail) / 1048576, total / 1048576
    health = (Health.CRIT if pct >= s.mem_crit_pct
              else Health.WARN if pct >= s.mem_warn_pct
              else Health.OK)
    return ProbeResult(node=node.name, kind="mem", health=health,
                       value=f"{pct}%", detail=f"{used_gb:.1f}/{total_gb:.1f} GiB used")


async def probe_mem_remote(node: Node, s: Settings) -> ProbeResult:
    """Memory pressure on a REMOTE box, read over the SAME ssh transport the disk
    probe uses for this node (Tailscale SSH for charlie/delta; the ed25519-key
    ~/.ssh/config alias for echo). Used% = (MemTotal - MemAvailable) /
    MemTotal, mirroring probe_mem_local so remote cards render identically to worker2.

    echo is the the NAS NAS (DSM/busybox): /proc/meminfo exists but the
    environment is minimal and MemAvailable/parsing can differ. On ANY read or parse
    failure this degrades to UNKNOWN ('—'), never CRIT — a NAS quirk must not
    false-CRIT its card or drag the board dark."""
    if not node.ssh_host:
        return ProbeResult(node=node.name, kind="mem", health=Health.UNKNOWN,
                           value="—", detail="no ssh_host configured")
    rc, out, err = await _ssh(node.ssh_host, "cat /proc/meminfo", s.ssh_timeout_seconds)
    if rc != 0 or not out:
        return ProbeResult(node=node.name, kind="mem", health=Health.UNKNOWN,
                           value="—", detail=(err or "meminfo read failed")[:200])
    try:
        info: dict[str, int] = {}
        for row in out.splitlines():
            key, _, rest = row.partition(":")
            parts = rest.split()
            if parts:
                info[key.strip()] = int(parts[0])  # kB
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
    except (KeyError, ValueError, IndexError) as e:
        return ProbeResult(node=node.name, kind="mem", health=Health.UNKNOWN,
                           value="—", detail=f"meminfo unparseable: {e}"[:200])
    if total <= 0:
        return ProbeResult(node=node.name, kind="mem", health=Health.UNKNOWN,
                           value="—", detail="MemTotal=0")
    pct = int(round((total - avail) / total * 100))
    used_gb, total_gb = (total - avail) / 1048576, total / 1048576
    health = (Health.CRIT if pct >= s.mem_crit_pct
              else Health.WARN if pct >= s.mem_warn_pct
              else Health.OK)
    return ProbeResult(node=node.name, kind="mem", health=health,
                       value=f"{pct}%", detail=f"{used_gb:.1f}/{total_gb:.1f} GiB used")


async def probe_http(node: Node, s: Settings) -> ProbeResult:
    """HTTP health of an endpoint (the tunnel). Any <500 counts as up — an MCP
    endpoint answering 400/406 to a bare GET still proves the tunnel + origin live."""
    url = node.http_url or f"https://{node.address}"
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=s.http_timeout_seconds,
                                     follow_redirects=True) as client:
            r = await client.get(url)
        ms = int((time.perf_counter() - start) * 1000)
        up = r.status_code < 500
        return ProbeResult(node=node.name, kind="http",
                           health=Health.OK if up else Health.CRIT,
                           value=f"{r.status_code} · {ms} ms", latency_ms=ms,
                           detail=url)
    except Exception as e:
        return ProbeResult(node=node.name, kind="http", health=Health.CRIT,
                           value="down", detail=f"{type(e).__name__}: {e}")


# cloudflared runs as a SYSTEM unit on worker2 (token `tunnel run`); david is in
# systemd-journal, so its startup log is readable without sudo. The unit now PINS
# the metrics port via `--metrics 127.0.0.1:<port>` in ExecStart, so we read it
# straight from `systemctl show ... -p ExecStart` first. The journal-grep
# ("Starting metrics server on 127.0.0.1:<port>/metrics") is kept as a fallback
# for the auto-chosen-port case, where no `--metrics` arg is present.
CLOUDFLARED_UNIT = "cloudflared.service"

_EXECSTART_METRICS_RE = re.compile(r"--metrics\s+127\.0\.0\.1:(\d+)")


async def _discover_cf_metrics_port(timeout: int) -> int | None:
    """Metrics port for the cloudflared unit: pinned port from ExecStart if
    present, else the most-recent metrics-server port from the journal, or None."""
    rc, out, _err = await _local(
        ["systemctl", "show", CLOUDFLARED_UNIT, "-p", "ExecStart", "--value"],
        timeout,
    )
    if rc == 0 and out:
        m = _EXECSTART_METRICS_RE.search(out)
        if m:
            return int(m.group(1))

    rc, out, err = await _local(
        ["journalctl", "-u", CLOUDFLARED_UNIT, "--no-pager", "-o", "cat",
         "-g", "Starting metrics server on"],
        timeout,
    )
    if rc != 0 or not out:
        return None
    port = None
    for m in re.finditer(r"127\.0\.0\.1:(\d+)/metrics", out):
        port = int(m.group(1))          # keep the LAST (most recent boot) match
    return port


async def probe_tunnel_connector(node: Node, s: Settings) -> ProbeResult:
    """§A — edge/cloudflared connector health, ON-BOX (no Cloudflare Access noise).

    Probes the connector's LOCAL metrics `/ready` endpoint: HTTP 200 means the
    connector is registered with the edge, and the JSON `readyConnections` is the
    live edge-connection count. This line now means "is the pipe to Cloudflare's
    edge actually up" — nothing about Access, so the old 401=OK framing is gone.
    A dropped connector or a crashed cloudflared now legitimately shows non-OK.

    Falls back to the systemd unit's `is-active` if the metrics endpoint can't be
    reached (token-run tunnels have no cert.pem, so `cloudflared tunnel info` can't
    enumerate connections here — is-active is the reliable on-box signal without
    metrics). Fail-safes to UNKNOWN on anything unexpected, degrading only its own
    line, never blanking the card."""
    start = time.perf_counter()
    port = await _discover_cf_metrics_port(s.tcp_timeout_seconds)
    metrics_err = "metrics port not found in journal"
    if port:
        url = f"http://127.0.0.1:{port}/ready"
        # A LONE transient loopback blip (cloudflared mid config-reload) must not
        # read as a hard failure on one sweep. Re-probe ONCE after a short sleep
        # before any verdict; only if the SECOND probe also fails do we fall
        # through to the is-active fallback below.
        r = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
                    r = await client.get(url)
                break
            except Exception as e:
                metrics_err = f"metrics :{port} unreachable: {type(e).__name__}: {e}"
                r = None
                if attempt == 0:
                    await asyncio.sleep(0.5)
        if r is not None:
            ms = int((time.perf_counter() - start) * 1000)
            try:
                conns = int(r.json().get("readyConnections"))
            except Exception:
                conns = None
            if r.status_code == 200 and (conns is None or conns >= 1):
                cval = f"{conns} conn" if conns is not None else "up"
                return ProbeResult(node=node.name, kind="tunnel", health=Health.OK,
                                   value=f"up · {cval} · {ms} ms", latency_ms=ms,
                                   detail=f"cloudflared /ready :{port} "
                                          f"(readyConnections={conns})")
            # Reachable but not ready: connector has 0 live edge connections.
            return ProbeResult(node=node.name, kind="tunnel", health=Health.CRIT,
                               value=f"no edge · {r.status_code}", latency_ms=ms,
                               detail=f"cloudflared /ready :{port} readyConnections={conns}")
        # Both probes failed → metrics_err holds the last error; fall through.

    # Fallback: is the connector unit even running?
    rc, out, err = await _local(
        ["systemctl", "is-active", CLOUDFLARED_UNIT], s.tcp_timeout_seconds
    )
    state = (out or err).strip()
    if state == "active":
        # Running, but we couldn't confirm live edge connections this sweep.
        return ProbeResult(node=node.name, kind="tunnel", health=Health.WARN,
                           value="active · edge?", detail=f"unit active; {metrics_err}")
    if state:
        return ProbeResult(node=node.name, kind="tunnel", health=Health.CRIT,
                           value=state, detail=f"cloudflared {state}; {metrics_err}")
    return ProbeResult(node=node.name, kind="tunnel", health=Health.UNKNOWN,
                       value="?", detail=f"{metrics_err}; {err[:120]}")



async def _remote_icmp(source_host: str, target: str, node_name: str, kind: str,
                       s: Settings) -> ProbeResult:
    rc, out, err = await _ssh(
        source_host,
        f"ping -c 1 -W 2 {target}",
        s.tcp_timeout_seconds + 2,
    )
    text = out or err
    m = re.search(r"time=(\d+(?:\.\d+)?)\s*ms", text)
    ms = int(float(m.group(1))) if m else None
    if rc == 0:
        health = Health.WARN if (ms is not None and ms >= 10) else Health.OK
        return ProbeResult(node=node_name, kind=kind, health=health,
                           value=(f"{ms} ms" if ms is not None else "ok"),
                           latency_ms=ms, detail=f"{source_host} -> {target}",
                           source_host=source_host, target=target, method="icmp",
                           timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000))
    if rc == 255 or err.startswith("ssh:") or err.startswith("SSH:"):
        return ProbeResult(node=node_name, kind=kind, health=Health.UNKNOWN,
                           value="?", detail=f"source host {source_host} unreachable: {(err or out or 'ssh failed')[:160]}",
                           source_host=source_host, target=target, method="icmp",
                           timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000),
                           error_class="source_unreachable")
    return ProbeResult(node=node_name, kind=kind, health=Health.CRIT,
                       value="unreachable", detail=text[:200],
                       source_host=source_host, target=target, method="icmp",
                       timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000),
                       error_class="icmp_failed")


async def _remote_tcp(source_host: str, target: str, port: int, node_name: str,
                      kind: str, s: Settings) -> ProbeResult:
    cmd = f"python3 - <<'PY'\nimport socket, time\nstart=time.perf_counter()\ns=socket.create_connection(({target!r}, {port}), timeout=4)\ns.close()\nprint(int((time.perf_counter()-start)*1000))\nPY"
    rc, out, err = await _ssh(source_host, cmd, s.tcp_timeout_seconds + 2)
    if rc == 0 and out.strip().splitlines():
        try:
            ms = int(out.strip().splitlines()[-1])
        except ValueError:
            ms = None
        return ProbeResult(node=node_name, kind=kind, health=Health.OK,
                           value=(f"{ms} ms" if ms is not None else "open"),
                           latency_ms=ms, detail=f"{source_host} -> {target}:{port}",
                           source_host=source_host, target=f"{target}:{port}",
                           method="tcp_connect",
                           timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000))
    if rc == 255 or err.startswith("ssh:") or err.startswith("SSH:"):
        return ProbeResult(node=node_name, kind=kind, health=Health.UNKNOWN,
                           value="?", detail=f"source host {source_host} unreachable: {(err or out or 'ssh failed')[:160]}",
                           source_host=source_host, target=f"{target}:{port}",
                           method="tcp_connect",
                           timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000),
                           error_class="source_unreachable")
    return ProbeResult(node=node_name, kind=kind, health=Health.CRIT,
                       value="closed", detail=(err or out or "tcp failed")[:200],
                       source_host=source_host, target=f"{target}:{port}",
                       method="tcp_connect",
                       timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000),
                       error_class="tcp_failed")


async def probe_remote_icmp_delta(node: Node, s: Settings) -> ProbeResult:
    return await _remote_icmp("delta", NAS_PROBE_TARGET, node.name,
                              "remote_icmp_delta", s)


async def probe_nas_smb_charlie(node: Node, s: Settings) -> ProbeResult:
    return await _remote_tcp("charlie", NAS_PROBE_TARGET, 445, node.name,
                             "nas_smb_charlie", s)


async def probe_nas_smb_delta(node: Node, s: Settings) -> ProbeResult:
    return await _remote_tcp("delta", NAS_PROBE_TARGET, 445, node.name,
                             "nas_smb_delta", s)


async def probe_nas_mount(node: Node, s: Settings) -> ProbeResult:
    if node.name == "charlie":
        host, mount = "charlie", "/mnt/nas2/the compute host"
    elif node.name == "delta":
        host, mount = "delta", "/mnt/nas"
    else:
        return ProbeResult(node=node.name, kind="nas_mount", health=Health.UNKNOWN,
                           value="?", detail="no NAS mount expected",
                           source_host=SOURCE_HOST, method="findmnt")
    rc, out, err = await _ssh(
        host,
        f"timeout 4 findmnt -no FSTYPE {mount}",
        s.tcp_timeout_seconds + 2,
    )
    fstype = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if fstype == "cifs":
        ok, detail_fs = True, "cifs"
    elif fstype == "autofs":
        # systemd automount placeholder present, child idle-unmounted; healthy, mounts on access
        ok, detail_fs = True, "cifs (autofs, idle)"
    elif fstype:
        ok, detail_fs = False, f"unexpected fstype {fstype}"
    else:
        ok, detail_fs = False, (err[:120].strip() or "not mounted")
    return ProbeResult(node=node.name, kind="nas_mount",
                       health=Health.OK if ok else Health.WARN,
                       value=("mounted" if ok else "unknown"),
                       detail=f"{mount}: {detail_fs}",
                       source_host=host, target=mount, method="findmnt",
                       timeout_ms=int((s.tcp_timeout_seconds + 2) * 1000),
                       error_class=None if ok else "mount_unavailable")


async def probe_gpu(node: Node, s: Settings) -> ProbeResult:
    rc, out, err = await _ssh(
        "charlie",
        "nvidia-smi --query-gpu=temperature.gpu,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits | head -n 1",
        s.tcp_timeout_seconds + 2,
    )
    if rc != 0 or not out.strip():
        return ProbeResult(node=node.name, kind="gpu", health=Health.UNKNOWN,
                           value="?", detail=(err or "nvidia-smi unavailable")[:200],
                           source_host="charlie", method="nvidia_smi",
                           error_class="nvidia_smi_failed")
    parts = [p.strip() for p in out.splitlines()[0].split(",")]
    try:
        temp, used, total, util = [int(p) for p in parts[:4]]
    except ValueError:
        return ProbeResult(node=node.name, kind="gpu", health=Health.UNKNOWN,
                           value="?", detail=out[:120],
                           source_host="charlie", method="nvidia_smi",
                           error_class="parse_failed")
    health = Health.WARN if temp >= 80 or used / max(total, 1) >= 0.9 else Health.OK
    return ProbeResult(node=node.name, kind="gpu", health=health,
                       value=f"{temp}C · {used}/{total} MiB",
                       detail=f"{util}% util", source_host="charlie",
                       target="gpu0", method="nvidia_smi")


async def probe_ollama(node: Node, s: Settings) -> ProbeResult:
    rc, out, err = await _ssh(
        "charlie",
        "python3 - <<'PY'\nimport json, urllib.request\nwith urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=4) as r:\n data=json.load(r)\nprint(len(data.get('models', [])))\nPY",
        s.tcp_timeout_seconds + 2,
    )
    if rc == 0 and out.strip():
        return ProbeResult(node=node.name, kind="ollama", health=Health.OK,
                           value=f"{out.strip().splitlines()[-1]} models",
                           detail="127.0.0.1:11434/api/tags",
                           source_host="charlie", target="127.0.0.1:11434",
                           method="http_loopback")
    return ProbeResult(node=node.name, kind="ollama", health=Health.WARN,
                       value="down?", detail=(err or out or "no response")[:200],
                       source_host="charlie", target="127.0.0.1:11434",
                       method="http_loopback", error_class="ollama_check_failed")


async def probe_loupe_service(node: Node, s: Settings) -> ProbeResult:
    # Follows whichever node carries this probe. Was pinned to "delta" until
    # Loupe moved to tensor (2026-08-07); a hardcoded host silently reports the
    # wrong box the moment the app relocates.
    if not node.ssh_host:
        return ProbeResult(node=node.name, kind="loupe_service", health=Health.UNKNOWN,
                           detail="no ssh_host configured")
    rc, out, err = await _ssh(
        node.ssh_host,
        "systemctl --user is-active loupe.service 2>/dev/null || true",
        s.tcp_timeout_seconds + 2,
    )
    state = (out or err).strip().splitlines()[-1] if (out or err).strip() else "unknown"
    health = Health.OK if state == "active" else Health.CRIT
    return ProbeResult(node=node.name, kind="loupe_service", health=health,
                       value=state, detail="systemd --user loupe.service",
                       source_host=node.ssh_host, target="loupe.service",
                       method="systemd_is_active")


async def probe_db_freshness(node: Node, s: Settings) -> ProbeResult:
    cmd = "python3 - <<'PY'\nimport os, sqlite3, time\ncands=[os.path.expanduser('~/loupe/metadata.db'), os.path.expanduser('~/loupe-pipeline/metadata.db')]\nfor db in cands:\n    if os.path.exists(db) and os.path.getsize(db) > 0:\n        con=sqlite3.connect('file:'+db+'?mode=ro', uri=True)\n        has=con.execute(\"select count(*) from sqlite_master where type='table' and name='assets'\").fetchone()[0]\n        if has:\n            count=con.execute('select count(*) from assets').fetchone()[0]\n            age=(time.time()-os.path.getmtime(db))/3600\n            print(f'{os.path.basename(db)} {count} assets {age:.1f}h old')\n            raise SystemExit(0)\nprint('no assets db')\nraise SystemExit(1)\nPY"
    rc, out, err = await _ssh(node.ssh_host, cmd, s.tcp_timeout_seconds + 4)
    text = (out or err).strip()
    health = Health.OK if rc == 0 else Health.WARN
    return ProbeResult(node=node.name, kind="db_freshness", health=health,
                       value=(text[:40] if text else "?"), detail=text[:200],
                       source_host=node.ssh_host, target="metadata.db",
                       method="sqlite_schema")


async def probe_nexus_loopback(node: Node, s: Settings) -> ProbeResult:
    start = time.perf_counter()
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
                r = await client.get("http://127.0.0.1:8770/healthz")
            ms = int((time.perf_counter() - start) * 1000)
            return ProbeResult(node=node.name, kind="nexus_loopback",
                               health=Health.OK if r.status_code == 200 else Health.CRIT,
                               value=f"{r.status_code} · {ms} ms", latency_ms=ms,
                               detail="/healthz", source_host=SOURCE_HOST,
                               target="127.0.0.1:8770", method="http_loopback")
        except httpx.ConnectError as e:
            # A LONE transient loopback miss (e.g. uvicorn hasn't bound :8770
            # yet during ASGI lifespan startup) must not read as a hard failure
            # on one sweep. Re-probe ONCE after a short sleep before verdict;
            # only if the SECOND attempt also connect-fails do we crit.
            last_exc = e
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
        except Exception as e:
            last_exc = e
            break
    return ProbeResult(node=node.name, kind="nexus_loopback", health=Health.CRIT,
                       value="down", detail=f"{type(last_exc).__name__}: {last_exc}",
                       source_host=SOURCE_HOST, target="127.0.0.1:8770",
                       method="http_loopback", error_class=type(last_exc).__name__)


async def probe_sync_service(node: Node, s: Settings) -> ProbeResult:
    rc, out, err = await _local(
        ["systemctl", "--user", "is-active", "syncthing.service"],
        s.tcp_timeout_seconds,
    )
    state = (out or err).strip() or "unknown"
    return ProbeResult(node=node.name, kind="sync_service",
                       health=Health.OK if state == "active" else Health.WARN,
                       value=state, detail="systemd --user syncthing.service",
                       source_host=SOURCE_HOST, target="syncthing.service",
                       method="systemd_is_active")


async def probe_scheduler(node: Node, s: Settings) -> ProbeResult:
    start = time.perf_counter()
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
                r = await client.get("http://127.0.0.1:8770/healthz")
            ms = int((time.perf_counter() - start) * 1000)
            fresh = r.status_code == 200 and bool(r.json().get("scheduler_fresh"))
            return ProbeResult(node=node.name, kind="scheduler",
                               health=Health.OK if fresh else Health.WARN,
                               value=f"{r.status_code} · {ms} ms", latency_ms=ms,
                               detail=r.text[:160], source_host=SOURCE_HOST,
                               target="127.0.0.1:8770/healthz",
                               method="http_loopback")
        except httpx.ConnectError as e:
            # Mirrors probe_nexus_loopback: a lone loopback miss during ASGI
            # lifespan startup (port not bound yet) gets one retry before we
            # fall through to a verdict.
            last_exc = e
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
        except Exception as e:
            last_exc = e
            break
    return ProbeResult(node=node.name, kind="scheduler", health=Health.WARN,
                       value="unknown", detail=f"{type(last_exc).__name__}: {last_exc}",
                       source_host=SOURCE_HOST,
                       target="127.0.0.1:8770/healthz",
                       method="http_loopback", error_class=type(last_exc).__name__)


async def probe_process_rss(node: Node, s: Settings) -> ProbeResult:
    cmd = "ps -eo rss,args | awk '/tower|nexus/ && !/awk/ {sum+=$1} END {printf \"%.0f\", sum/1024}'"
    rc, out, err = await _local(["bash", "-lc", cmd], s.tcp_timeout_seconds)
    if rc == 0 and out.strip():
        mib = int(float(out.strip()))
        health = Health.WARN if mib >= 2500 else Health.OK
        return ProbeResult(node=node.name, kind="process_rss", health=health,
                           value=f"{mib} MiB", detail="tower + dashboard RSS",
                           source_host=SOURCE_HOST, method="process_table")
    return ProbeResult(node=node.name, kind="process_rss", health=Health.UNKNOWN,
                       value="?", detail=(err or "ps failed")[:120],
                       source_host=SOURCE_HOST, method="process_table",
                       error_class="ps_failed")


async def probe_backup_freshness(node: Node, s: Settings) -> ProbeResult:
    """How long since the backup marker was last touched. Stale = the job stopped
    running, which is exactly the failure a backup SPOF hides behind."""
    if not (node.ssh_host and node.marker_path):
        return ProbeResult(node=node.name, kind="backup", health=Health.UNKNOWN,
                           detail="no marker configured")
    rc, out, err = await _ssh(
        node.ssh_host, f"stat -c %Y {node.marker_path}", s.ssh_timeout_seconds
    )
    if rc != 0 or not out.isdigit():
        return ProbeResult(node=node.name, kind="backup", health=Health.UNKNOWN,
                           value="?", detail=(err or "marker missing")[:200])
    age_h = (time.time() - int(out)) / 3600
    health = (Health.CRIT if age_h >= s.backup_stale_crit_hours
              else Health.WARN if age_h >= s.backup_stale_warn_hours
              else Health.OK)
    return ProbeResult(node=node.name, kind="backup", health=health,
                       value=f"{age_h:.0f}h ago", detail="last successful run")


def _relay_lane_freshness(s: Settings) -> str:
    """Return-lane freshness across the seats ("worker1:Xh · worker3:Yh"), read locally on
    worker2. Genuine relay activity — how long since each seat last delivered. Falls
    back to a plain-English note when no artifacts exist yet. Local fs, no ssh.

    worker3/worker4 are included for display only — they run sporadically, so an
    absent or stale lane for them is normal, not an alarm. Neither this string
    nor the "seen" list drives Health beyond OK-if-any-lane-exists (see
    probe_relay_lanes below), so adding them cannot turn a stale/absent
    worker3 or worker4 lane into a WARN/CRIT: it can only ever add entries, never
    flip an existing worker1/worker3 read to something worse. worker2 is excluded
    (self-probe, by design)."""
    lanes = {
        seat: s.relay_root / f"from-{seat}" / "recon" / "latest_response.md"
        for seat in ALL_SEATS if seat != "worker2"
    }
    seen = []
    for seat, path in lanes.items():
        p = Path(path)
        if p.exists():
            age_h = (time.time() - p.stat().st_mtime) / 3600
            seen.append(f"{seat}:{age_h:.0f}h")
    return " · ".join(seen) if seen else "no return artifacts yet"


async def probe_relay_lanes(node: Node, s: Settings) -> ProbeResult:
    """Read the relay return lanes locally on worker2. Reports the most recent run
    state seen in from-worker1 / from-worker3. Local filesystem — no ssh."""
    seen = _relay_lane_freshness(s)
    if seen == "no return artifacts yet":
        return ProbeResult(node=node.name, kind="relay", health=Health.UNKNOWN,
                           value="idle", detail="no return artifacts found")
    return ProbeResult(node=node.name, kind="relay", health=Health.OK,
                       value=seen, detail="last return-lane writes")


# The Tower MCP listens on loopback :8765. Its JWT middleware exempts
# on-box callers (no cf-ray / cf-connecting-ip), so a plain loopback GET passes
# with no token; the MCP endpoint answers 406 to a bare GET — any response <500
# proves the Tower process is alive and serving.
TOWER_LOOPBACK_URL = "http://127.0.0.1:8765/mcp"


async def probe_tower_liveness(node: Node, s: Settings) -> ProbeResult:
    """§B — Tower liveness on loopback + return-lane freshness.

    Repoints the old public-URL RELAY line at the Tower ITSELF on 127.0.0.1:8765,
    so this line reflects the actual relay brain rather than Cloudflare Access at
    the edge. Any response <500 == the Tower is up (its MCP answers 406 to a bare
    GET). The existing return-lane freshness (worker1:Xh · worker3:Yh) rides along as the
    detail, so one line answers both "is the relay brain alive?" and "when did the
    seats last deliver?". Short timeout, clean up/down."""
    start = time.perf_counter()
    freshness = _relay_lane_freshness(s)
    try:
        async with httpx.AsyncClient(timeout=s.http_timeout_seconds) as client:
            r = await client.get(TOWER_LOOPBACK_URL)
        ms = int((time.perf_counter() - start) * 1000)
        up = r.status_code < 500
        return ProbeResult(node=node.name, kind="tower",
                           health=Health.OK if up else Health.CRIT,
                           value=(f"up · {ms} ms" if up else f"down · {r.status_code}"),
                           latency_ms=ms, detail=freshness)
    except Exception as e:
        return ProbeResult(node=node.name, kind="tower", health=Health.CRIT,
                           value="down",
                           detail=f"{freshness} · {type(e).__name__}: {e}"[:200])


async def probe_nas_smart(node: Node, s: Settings) -> ProbeResult:
    """Best-effort SMART read over ssh. DSM may not expose smartctl the usual way;
    UNKNOWN here means 'couldn't read', not 'disk failing'."""
    if not (node.ssh_host and node.smart_device):
        return ProbeResult(node=node.name, kind="smart", health=Health.UNKNOWN,
                           detail="no smart_device configured")
    rc, out, err = await _ssh(
        node.ssh_host,
        f"smartctl -H {node.smart_device} 2>/dev/null | grep -i 'overall-health'",
        s.ssh_timeout_seconds,
    )
    if rc != 0 or not out:
        return ProbeResult(node=node.name, kind="smart", health=Health.UNKNOWN,
                           value="?", detail="smartctl unavailable (DSM)")
    passed = "PASSED" in out.upper()
    return ProbeResult(node=node.name, kind="smart",
                       health=Health.OK if passed else Health.CRIT,
                       value="PASSED" if passed else "CHECK",
                       detail=out[:120])


# kind -> probe function
PROBE_DISPATCH = {
    "ping": probe_ping,
    "tailscale_ping": probe_tailscale_ping,
    "ssh_banner": probe_ssh_banner,
    "remote_icmp": probe_remote_icmp,
    "remote_icmp_delta": probe_remote_icmp_delta,
    "nas_smb_charlie": probe_nas_smb_charlie,
    "nas_smb_delta": probe_nas_smb_delta,
    "nas_mount": probe_nas_mount,
    "gpu": probe_gpu,
    "ollama": probe_ollama,
    "loupe_service": probe_loupe_service,
    "db_freshness": probe_db_freshness,
    "nexus_loopback": probe_nexus_loopback,
    "sync_service": probe_sync_service,
    "scheduler": probe_scheduler,
    "process_rss": probe_process_rss,
    "disk": probe_disk,
    "disk_local": probe_disk_local,
    "mem": probe_mem_local,
    "mem_remote": probe_mem_remote,
    "http": probe_http,
    "tunnel": probe_tunnel_connector,
    "backup": probe_backup_freshness,
    "relay": probe_relay_lanes,
    "tower": probe_tower_liveness,
    "smart": probe_nas_smart,
}
