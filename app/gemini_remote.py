"""Authenticated provider-neutral CLI control surface for the fleet.

The browser never receives a general shell. It can attach only to fixed Claude,
Codex, or Gemini commands on the three registered hosts. Interactive and
bounded launches use the same provider home and fixed model. User text is always passed
as one argv element after shell quoting on an SSH hop, never interpolated as
executable shell syntax.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import logging
import os
import pty
import shlex
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .shell_context import SHELL_ASSET_VERSION, app_chrome_context
from .runtime_paths import CONTROL_STATE_DIR
from .fleet_nodes import NODES

TOWER_ROOT = Path(os.getenv("TOWER_ROOT", os.path.expanduser("~/tower")))
if str(TOWER_ROOT) not in sys.path:
    sys.path.insert(0, str(TOWER_ROOT))
from quota_router import QuotaRouter  # noqa: E402

log = logging.getLogger("nexus.control")

router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)
templates.env.globals["shell_asset_v"] = SHELL_ASSET_VERSION

APP_ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = CONTROL_STATE_DIR
LOCAL_AGY = os.path.expanduser("~/.local/bin/agy")
# The home directory on the *remote* worker node. Defaults to this host's own
# home, which is correct when the fleet uses one account across nodes; override
# NEXUS_REMOTE_HOME when it does not.
REMOTE_HOME = os.environ.get("NEXUS_REMOTE_HOME", os.path.expanduser("~"))
MAX_INPUT_BYTES = 64 * 1024
MAX_BACKLOG_BYTES = 512 * 1024
MAX_WORKER_OUTPUT_BYTES = 2 * 1024 * 1024
MIN_WORKER_SECONDS = 30
MAX_WORKER_SECONDS = 7200
quota_router = QuotaRouter()

ProviderKey = Literal["claude", "codex", "gemini"]


@dataclass(frozen=True)
class ProviderSpec:
    key: ProviderKey
    label: str
    router_key: str
    strategy_model: str
    worker_model: str
    strategy_detail: str
    worker_detail: str
    strategy_models: tuple[str, ...] = ()
    supports_auto_mode: bool = False


PROVIDERS: dict[str, ProviderSpec] = {
    "claude": ProviderSpec(
        "claude", "Claude", "claude", "opus", "opus",
        "Opus 5 · unified interactive fleet agent",
        "Opus 5 · unified bounded fleet run",
        ("opus",), True,
    ),
    "codex": ProviderSpec(
        "codex", "Codex", "codex", "gpt-5.6-sol", "gpt-5.6-sol",
        "gpt-5.6-sol · unified interactive fleet agent",
        "gpt-5.6-sol · unified bounded fleet run",
        ("gpt-5.6-sol",), False,
    ),
    "gemini": ProviderSpec(
        "gemini", "Gemini", "gemini", "gemini-3.1-pro-high",
        "gemini-3.1-pro-high", "Gemini 3.1 Pro High · unified interactive fleet agent",
        "Gemini 3.1 Pro High · unified bounded fleet run",
        ("gemini-3.1-pro-high",), False,
    ),
}


@dataclass(frozen=True)
class HostSpec:
    key: str
    label: str
    seat: str
    ssh_alias: str | None
    home: str = REMOTE_HOME
    agy: str = LOCAL_AGY
    claude: str = os.path.expanduser("~/.local/bin/claude")
    codex: str = os.path.expanduser("~/.local/bin/codex")


HOSTS: dict[str, HostSpec] = {
    "alpha": HostSpec(
        "alpha", NODES["alpha"].display_name,
        NODES["alpha"].worker_identity, NODES["alpha"].ssh_alias,
    ),
    "charlie": HostSpec(
        "charlie", NODES["charlie"].display_name,
        NODES["charlie"].worker_identity, NODES["charlie"].ssh_alias,
    ),
    "delta": HostSpec(
        "delta", NODES["delta"].display_name,
        NODES["delta"].worker_identity, NODES["delta"].ssh_alias,
        claude=os.path.expanduser("~/.npm-global/bin/claude"),
    ),
}


def _remote_shell_command(argv: list[str], *, home: str = REMOTE_HOME) -> str:
    """Return the fixed, safely quoted command executed by the remote login shell."""
    return f"cd {shlex.quote(home)} && exec {shlex.join(argv)}"


def _provider(provider: str) -> ProviderSpec:
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ValueError(f"unknown provider: {provider}")
    return spec


def strategy_command(
    host: str, provider: ProviderKey = "gemini"
) -> tuple[list[str], str | None]:
    spec = HOSTS.get(host)
    if spec is None:
        raise ValueError(f"unknown host: {host}")
    provider_spec = _provider(provider)
    if provider == "claude":
        cli_argv = [spec.claude, "--model", provider_spec.strategy_model, "--effort", "high"]
    elif provider == "codex":
        cli_argv = [
            "env", f"CODEX_HOME={spec.home}/.codex",
            spec.codex, "--model", provider_spec.strategy_model,
            "--cd", spec.home, "--no-alt-screen",
        ]
    else:
        cli_argv = [
            spec.agy, "--project", "Homelab", "--model",
            provider_spec.strategy_model, "--effort", "high",
        ]
    if spec.ssh_alias is None:
        return cli_argv, spec.home
    return (
        [
            "ssh",
            "-tt",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            spec.ssh_alias,
            _remote_shell_command(cli_argv, home=spec.home),
        ],
        None,
    )


def worker_command(
    host: str,
    prompt: str,
    timeout_seconds: int,
    provider: ProviderKey = "gemini",
) -> tuple[list[str], str | None]:
    spec = HOSTS.get(host)
    if spec is None:
        raise ValueError(f"unknown host: {host}")
    provider_spec = _provider(provider)
    if provider == "claude":
        cli_argv = [
            spec.claude, "--model", provider_spec.worker_model, "--effort", "high",
            "--print", "--output-format", "text",
            "--dangerously-skip-permissions", prompt,
        ]
    elif provider == "codex":
        cli_argv = [
            "env", f"CODEX_HOME={spec.home}/.codex", spec.codex,
            "exec", "--skip-git-repo-check", "--model",
            provider_spec.worker_model, "--sandbox", "danger-full-access",
            "--cd", spec.home, prompt,
        ]
    else:
        cli_argv = [
            spec.agy, "--project", "Homelab", "--model",
            provider_spec.worker_model, "--effort", "high", "--mode", "accept-edits",
            "--dangerously-skip-permissions", "--print-timeout",
            f"{timeout_seconds}s", "--print", prompt,
        ]
    if spec.ssh_alias is None:
        return cli_argv, spec.home
    return (
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            spec.ssh_alias,
            _remote_shell_command(cli_argv, home=spec.home),
        ],
        None,
    )


@dataclass
class TerminalSession:
    host: str
    provider: ProviderKey = "gemini"
    process: subprocess.Popen[bytes] | None = None
    master_fd: int | None = None
    started_at: float | None = None
    exited_at: float | None = None
    exit_code: int | None = None
    _reader_task: asyncio.Task[None] | None = None
    _subscribers: set[asyncio.Queue[bytes]] = field(default_factory=set)
    _backlog: deque[bytes] = field(default_factory=deque)
    _backlog_size: int = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def start(self, cols: int = 100, rows: int = 32) -> None:
        if self.running:
            self.resize(cols, rows)
            return
        argv, cwd = strategy_command(self.host, self.provider)
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
        if self.host == "alpha":
            # This PTY is local to Nexus rather than an sshd child, but it is
            # still a remote-control surface.  Force Gemini's documented
            # remote sign-in flow so it prints an authorization URL instead of
            # trying to open a browser on the headless Pi.
            env.setdefault("SSH_CONNECTION", "127.0.0.1 0 127.0.0.1 0")
        if self.process is not None:
            self._backlog.clear()
            self._backlog_size = 0
        try:
            self.process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        self.master_fd = master_fd
        self.started_at = time.time()
        self.exited_at = None
        self.exit_code = None
        self.resize(cols, rows)
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"control-{self.provider}-{self.host}"
        )
        log.info(
            "strategy terminal started: provider=%s host=%s pid=%s",
            self.provider, self.host, self.process.pid,
        )

    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        cols = max(20, min(int(cols), 320))
        rows = max(8, min(int(rows), 120))
        try:
            fcntl.ioctl(
                self.master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            return

    async def write(self, data: bytes) -> None:
        if not self.running or self.master_fd is None:
            raise RuntimeError("terminal is not running")
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError("terminal input is too large")
        await asyncio.to_thread(os.write, self.master_fd, data)

    def subscribe(self) -> asyncio.Queue[bytes]:
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        if self._backlog:
            queue.put_nowait(b"".join(self._backlog))
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[bytes]) -> None:
        self._subscribers.discard(queue)

    def _remember(self, chunk: bytes) -> None:
        self._backlog.append(chunk)
        self._backlog_size += len(chunk)
        while self._backlog_size > MAX_BACKLOG_BYTES and self._backlog:
            self._backlog_size -= len(self._backlog.popleft())

    def _broadcast(self, chunk: bytes) -> None:
        self._remember(chunk)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)

    async def _read_loop(self) -> None:
        assert self.master_fd is not None
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(os.read, self.master_fd, 16384)
                except OSError:
                    break
                if not chunk:
                    break
                self._broadcast(chunk)
        finally:
            if self.process is not None:
                self.exit_code = await asyncio.to_thread(self.process.wait)
            self.exited_at = time.time()
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
            label = _provider(self.provider).label
            exit_notice = f"\r\n[{label} terminal exited: {self.exit_code}]\r\n".encode()
            self._broadcast(exit_notice)
            log.info(
                "strategy terminal exited: provider=%s host=%s rc=%s",
                self.provider, self.host, self.exit_code,
            )

    async def stop(self) -> None:
        if not self.running or self.process is None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(asyncio.to_thread(self.process.wait), timeout=5)
        except asyncio.TimeoutError:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def public_status(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "provider": self.provider,
            "running": self.running,
            "started_at": self.started_at,
            "exited_at": self.exited_at,
            "exit_code": self.exit_code,
            "viewers": len(self._subscribers),
        }


class TerminalManager:
    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str], TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_start(
        self,
        host: str,
        cols: int,
        rows: int,
        provider: ProviderKey = "gemini",
    ) -> TerminalSession:
        if host not in HOSTS:
            raise ValueError(f"unknown host: {host}")
        _provider(provider)
        async with self._lock:
            key = (provider, host)
            session = self.sessions.get(key)
            if session is None:
                session = TerminalSession(host, provider)
                self.sessions[key] = session
            await session.start(cols, rows)
            return session

    async def stop(self, host: str, provider: ProviderKey = "gemini") -> None:
        session = self.sessions.get((provider, host))
        if session is not None:
            await session.stop()

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(session.stop() for session in self.sessions.values()),
            return_exceptions=True,
        )


@dataclass
class WorkerJob:
    job_id: str
    host: str
    prompt: str
    timeout_seconds: int
    provider: ProviderKey = "gemini"
    task_size: str = "small"
    routing: dict[str, Any] = field(default_factory=dict)
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    output: str = ""
    error: str = ""
    task: asyncio.Task[None] | None = None

    def public(self, include_prompt: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "host": self.host,
            "state": self.state,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "output": self.output[-MAX_WORKER_OUTPUT_BYTES:],
            "error": self.error[-MAX_WORKER_OUTPUT_BYTES:],
            "provider": self.provider,
            "provider_label": _provider(self.provider).label,
            "model": _provider(self.provider).worker_model,
            "task_size": self.task_size,
            "routing": self.routing,
        }
        if include_prompt:
            data["prompt"] = self.prompt
        return data


class WorkerManager:
    def __init__(self) -> None:
        self.jobs: dict[str, WorkerJob] = {}
        self._active_hosts: set[str] = set()
        self._lock = asyncio.Lock()

    async def launch(
        self,
        host: str,
        prompt: str,
        timeout_seconds: int,
        task_size: str = "small",
        provider: ProviderKey = "gemini",
    ) -> WorkerJob:
        if host not in HOSTS:
            raise ValueError(f"unknown host: {host}")
        provider_spec = _provider(provider)
        async with self._lock:
            if host in self._active_hosts:
                raise RuntimeError(f"{host} already has an active worker")
            job_id = f"ctl-{provider}-{host}-{uuid.uuid4().hex[:12]}"
            routing = quota_router.recommend(
                lane="worker",
                size=task_size,
                allowed_providers=(provider_spec.router_key,),
                explicit_provider=provider_spec.router_key,
                reserve=True,
                reservation_id=job_id,
                ttl_seconds=timeout_seconds + 900,
            )
            if not routing.get("ok"):
                raise RuntimeError(
                    f"{provider_spec.label} quota router refused launch: "
                    f"{routing.get('error', 'unavailable')}"
                )
            job = WorkerJob(
                job_id=job_id,
                host=host,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                provider=provider,
                task_size=task_size,
                routing=routing,
            )
            self.jobs[job.job_id] = job
            self._active_hosts.add(host)
            job.task = asyncio.create_task(self._run(job), name=job.job_id)
            return job

    async def _run(self, job: WorkerJob) -> None:
        STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        job_dir = STATE_ROOT / "runs" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        (job_dir / "request.txt").write_text(job.prompt + "\n", encoding="utf-8")
        os.chmod(job_dir / "request.txt", 0o600)
        argv, cwd = worker_command(
            job.host, job.prompt, job.timeout_seconds, job.provider
        )
        job.state = "running"
        job.started_at = time.time()
        self._write_metadata(job_dir, job)
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=job.timeout_seconds + 15
                )
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                job.state = "timed_out"
            else:
                job.state = "succeeded" if proc.returncode == 0 else "failed"
            job.exit_code = proc.returncode
            job.output = stdout.decode("utf-8", errors="replace")[-MAX_WORKER_OUTPUT_BYTES:]
            job.error = stderr.decode("utf-8", errors="replace")[-MAX_WORKER_OUTPUT_BYTES:]
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            job.state = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - durable error evidence for the UI
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            log.exception(
                "worker failed: provider=%s job=%s host=%s",
                job.provider, job.job_id, job.host,
            )
        finally:
            job.finished_at = time.time()
            (job_dir / "stdout.txt").write_text(job.output, encoding="utf-8")
            (job_dir / "stderr.txt").write_text(job.error, encoding="utf-8")
            self._write_metadata(job_dir, job)
            for path in job_dir.iterdir():
                os.chmod(path, 0o600)
            async with self._lock:
                self._active_hosts.discard(job.host)
            quota_router.release(job.job_id, "terminal")
            log.info(
                "worker finished: provider=%s job=%s host=%s state=%s rc=%s",
                job.provider, job.job_id,
                job.host,
                job.state,
                job.exit_code,
            )

    @staticmethod
    def _write_metadata(job_dir: Path, job: WorkerJob) -> None:
        """Atomically publish active/final state for the main worker cards."""
        path = job_dir / "run.json"
        tmp = job_dir / ".run.json.tmp"
        tmp.write_text(
            json.dumps(job.public(include_prompt=False), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def public_status(self) -> list[dict[str, Any]]:
        jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [job.public() for job in jobs[:30]]

    async def shutdown(self) -> None:
        tasks = [job.task for job in self.jobs.values() if job.task and not job.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


terminal_manager = TerminalManager()
worker_manager = WorkerManager()


class WorkerLaunch(BaseModel):
    host: str
    provider: ProviderKey = "gemini"
    prompt: str = Field(min_length=1, max_length=40_000)
    timeout_seconds: int = Field(default=1800, ge=MIN_WORKER_SECONDS, le=MAX_WORKER_SECONDS)
    task_size: Literal["tiny", "small", "medium", "large"] = "small"
    confirmed: bool = False


def _provider_public(spec: ProviderSpec) -> dict[str, Any]:
    return {
        "key": spec.key,
        "label": spec.label,
        "strategy_model": spec.strategy_model,
        "worker_model": spec.worker_model,
        "strategy_detail": spec.strategy_detail,
        "worker_detail": spec.worker_detail,
        "strategy_models": list(spec.strategy_models or (spec.strategy_model,)),
        "supports_auto_mode": spec.supports_auto_mode,
    }


@router.get("/control", response_class=HTMLResponse)
async def control_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "gemini.html",
        {
            **(await app_chrome_context()),
            "hosts": [
                {"key": spec.key, "label": spec.label, "seat": spec.seat}
                for spec in HOSTS.values()
            ],
            "providers": [_provider_public(spec) for spec in PROVIDERS.values()],
        },
    )


@router.get("/gemini", response_class=RedirectResponse)
async def legacy_gemini_page() -> RedirectResponse:
    return RedirectResponse("/control?provider=gemini", status_code=307)


def _route(provider: ProviderSpec, lane: str) -> dict[str, Any]:
    if lane == "strategy":
        return {
            "ok": True,
            "provider": provider.router_key,
            "model": provider.strategy_model,
            "state": "PINNED",
            "reason": "strategy profile is fixed; the worker quota router does not select strategy seats",
        }
    return quota_router.recommend(
        lane=lane,
        size="small",
        allowed_providers=(provider.router_key,),
        explicit_provider=provider.router_key,
    )


@router.get("/api/control/status")
@router.get("/api/gemini/status", deprecated=True)
async def control_status() -> dict[str, Any]:
    return {
        "providers": [_provider_public(spec) for spec in PROVIDERS.values()],
        "hosts": [
            {
                "key": spec.key,
                "label": spec.label,
                "seat": spec.seat,
                "strategies": {
                    provider: terminal_manager.sessions.get(
                        (provider, spec.key), TerminalSession(spec.key, provider)
                    ).public_status()
                    for provider in PROVIDERS
                },
            }
            for spec in HOSTS.values()
        ],
        "workers": worker_manager.public_status(),
        "routing": {
            provider: {
                "strategy": _route(spec, "strategy"),
                "worker": _route(spec, "worker"),
            }
            for provider, spec in PROVIDERS.items()
        },
    }


@router.post("/api/control/strategy/{provider}/{host}/stop")
async def stop_strategy(provider: str, host: str) -> dict[str, Any]:
    if host not in HOSTS:
        raise HTTPException(status_code=404, detail="Unknown fleet host")
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown CLI provider")
    await terminal_manager.stop(host, provider)  # type: ignore[arg-type]
    return {"ok": True, "host": host, "provider": provider}


@router.post("/api/gemini/strategy/{host}/stop", deprecated=True)
async def stop_legacy_gemini_strategy(host: str) -> dict[str, Any]:
    return await stop_strategy("gemini", host)


@router.post("/api/control/workers", status_code=202)
@router.post("/api/gemini/workers", status_code=202, deprecated=True)
async def launch_worker(payload: WorkerLaunch) -> dict[str, Any]:
    if payload.host not in HOSTS:
        raise HTTPException(status_code=404, detail="Unknown fleet host")
    if not payload.confirmed:
        raise HTTPException(
            status_code=400,
            detail="Confirm that this bounded worker prompt is ready to execute.",
        )
    prompt = payload.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Worker prompt is empty")
    try:
        job = await worker_manager.launch(
            payload.host,
            prompt,
            payload.timeout_seconds,
            payload.task_size,
            payload.provider,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.public()


@router.get("/api/control/workers/{job_id}")
@router.get("/api/gemini/workers/{job_id}", deprecated=True)
async def worker_status(job_id: str) -> dict[str, Any]:
    job = worker_manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown worker run")
    return job.public(include_prompt=True)


@router.websocket("/control/ws")
@router.websocket("/gemini/ws")
async def control_ws(ws: WebSocket) -> None:
    # Cloudflare Access authenticates public WebSocket upgrades before the
    # tunnel forwards them to this fixed-command control surface.
    host = ws.query_params.get("host", "")
    provider = ws.query_params.get("provider", "gemini")
    if host not in HOSTS:
        await ws.close(code=1008)
        return
    if provider not in PROVIDERS:
        await ws.close(code=1008)
        return
    try:
        cols = int(ws.query_params.get("cols", "100"))
        rows = int(ws.query_params.get("rows", "32"))
    except ValueError:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        session = await terminal_manager.get_or_start(
            host, cols, rows, provider  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001 - relay the fixed-command startup error
        log.exception(
            "failed to start strategy terminal: provider=%s host=%s",
            provider, host,
        )
        await ws.send_json({"type": "error", "message": str(exc)})
        await ws.close(code=1011)
        return
    queue = session.subscribe()

    async def sender() -> None:
        while True:
            chunk = await queue.get()
            await ws.send_json(
                {"type": "output", "data": base64.b64encode(chunk).decode("ascii")}
            )

    sender_task = asyncio.create_task(sender())
    await ws.send_json(
        {"type": "status", "running": session.running, "host": host, "provider": provider}
    )
    try:
        while True:
            message = await ws.receive_json()
            kind = message.get("type")
            if kind == "input":
                raw = base64.b64decode(str(message.get("data", "")), validate=True)
                await session.write(raw)
            elif kind == "resize":
                session.resize(int(message.get("cols", 100)), int(message.get("rows", 32)))
            elif kind == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, ValueError, TypeError):
        pass
    finally:
        sender_task.cancel()
        session.unsubscribe(queue)
        await asyncio.gather(sender_task, return_exceptions=True)
