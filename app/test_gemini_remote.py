from __future__ import annotations

import asyncio
import shlex
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from .gemini_remote import (
    TerminalSession,
    PROVIDERS,
    WorkerJob,
    WorkerManager,
    _provider_public,
    _remote_shell_command,
    _route,
    router,
    strategy_command,
    worker_command,
)


class ControlSurfaceTests(unittest.TestCase):
    CHROME = {
        "chrome_snap": None,
        "chrome_accent": {},
        "chrome_unread": 0,
        "chrome_stamp": "—",
    }

    def test_control_page_and_legacy_redirect(self) -> None:
        app = FastAPI()
        app.include_router(router)
        with (
            patch("app.gemini_remote.app_chrome_context", return_value=self.CHROME),
            TestClient(app) as client,
        ):
            response = client.get("/control")
            legacy = client.get("/gemini", follow_redirects=False)

        self.assertEqual(response.status_code, 200)
        self.assertIn("fleet CLI control", response.text)
        for label in ("Claude", "Codex", "Gemini", "Interactive", "Bounded run"):
            self.assertIn(label, response.text)
        self.assertEqual(legacy.status_code, 307)
        self.assertEqual(legacy.headers["location"], "/control?provider=gemini")

    def test_strategy_chat_shell_keeps_native_terminal_escape_hatch(self) -> None:
        app = FastAPI()
        app.include_router(router)
        with (
            patch("app.gemini_remote.app_chrome_context", return_value=self.CHROME),
            TestClient(app) as client,
        ):
            response = client.get("/control")

        self.assertEqual(response.status_code, 200)
        for marker in (
            'id="chatThread"', 'id="chatInput"', 'id="sendPrompt"',
            'id="strategyModel"', 'id="autoMode"', 'id="terminalView"',
            'data-open-terminal', 'enterkeyhint="enter"',
        ):
            self.assertIn(marker, response.text)
        self.assertIn('data-strategy-models=', response.text)

    def test_mobile_chat_contract_uses_native_composer_and_safe_keyboard_offset(self) -> None:
        root = Path(__file__).resolve().parent.parent
        script = (root / "static" / "gemini.js").read_text()
        style = (root / "static" / "gemini.css").read_text()
        self.assertIn("window.visualViewport", script)
        self.assertIn("chatComposer.requestSubmit()", script)
        self.assertIn("sendRaw(`${prompt}\\r`)", script)
        self.assertIn("--nexus-keyboard-offset", style)
        self.assertIn("font: 16px/1.45 var(--font-ui)", style)
        self.assertIn("min-height: 44px", style)
        self.assertIn("100dvh", style)

    def test_mobile_control_gives_the_session_the_viewport(self) -> None:
        root = Path(__file__).resolve().parent.parent
        script = (root / "static" / "gemini.js").read_text()
        style = (root / "static" / "gemini.css").read_text()
        self.assertIn(".nexus-sticky-shell { position: static; }", style)
        self.assertIn(".nexus-content-head { display: none; }", style)
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);", style)
        self.assertIn("height: calc(100dvh - 190px);", style)
        self.assertIn(".chat-thread { overflow-y: auto;", style)
        self.assertIn("strategyPanel.scrollIntoView", script)
        self.assertIn('.strategy-console[data-connected="true"] .intro-message', style)

    def test_picker_does_not_autostart_a_strategy_terminal(self) -> None:
        root = Path(__file__).resolve().parent.parent
        script = (root / "static" / "gemini.js").read_text()
        template = (root / "templates" / "gemini.html").read_text()
        self.assertIn('id="reconnectTerminal" type="button">Connect</button>', template)
        self.assertNotIn("fitAddon.fit();\n  connectTerminal(false);", script)
        self.assertIn("provider: selectedProvider", script)


class CommandContractTests(unittest.TestCase):
    def test_public_provider_contract_allowlists_only_live_session_overrides(self) -> None:
        claude = _provider_public(PROVIDERS["claude"])
        self.assertEqual(claude["strategy_model"], "opus")
        self.assertEqual(claude["strategy_models"], ["opus"])
        self.assertTrue(claude["supports_auto_mode"])
        self.assertEqual(
            _provider_public(PROVIDERS["gemini"])["strategy_models"],
            ["gemini-3.1-pro-high"],
        )

    def test_strategy_status_is_fixed_not_worker_routed(self) -> None:
        with patch("app.gemini_remote.quota_router.recommend") as recommend:
            route = _route(PROVIDERS["codex"], "strategy")
        self.assertTrue(route["ok"])
        self.assertEqual(route["model"], "gpt-5.6-sol")
        self.assertEqual(route["state"], "PINNED")
        recommend.assert_not_called()

    def test_gemini_commands_use_main_agent_and_fixed_model(self) -> None:
        local, cwd = strategy_command("alpha")
        self.assertEqual(cwd, "/home/david")
        self.assertNotIn("--agent", local)
        self.assertEqual(local[local.index("--project") + 1], "Homelab")
        self.assertIn("gemini-3.1-pro-high", local)
        self.assertNotIn("gemini-3.1-pro-low", local)
        self.assertIn("--effort", local)
        self.assertIn("high", local)

        remote, remote_cwd = strategy_command("charlie")
        self.assertIsNone(remote_cwd)
        self.assertEqual(remote[:2], ["ssh", "-tt"])
        self.assertEqual(remote[-2], "charlie")
        parsed = shlex.split(remote[-1])
        self.assertEqual(parsed[:3], ["cd", "/home/david", "&&"])
        self.assertNotIn("--agent", parsed)
        self.assertEqual(parsed[parsed.index("--project") + 1], "Homelab")

    def test_claude_interactive_uses_standard_profile_and_opus(self) -> None:
        local, cwd = strategy_command("alpha", "claude")
        self.assertEqual(cwd, "/home/david")
        self.assertNotIn("CLAUDE_CONFIG_DIR", local)
        self.assertIn("opus", local)
        self.assertIn("high", local)
        self.assertNotIn("--dangerously-skip-permissions", local)

    def test_codex_interactive_uses_standard_profile_and_sol(self) -> None:
        remote, cwd = strategy_command("delta", "codex")
        self.assertIsNone(cwd)
        parsed = shlex.split(remote[-1])
        self.assertIn("CODEX_HOME=/home/david/.codex", parsed)
        self.assertIn("gpt-5.6-sol", parsed)
        self.assertIn("--no-alt-screen", parsed)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", parsed)

    def test_worker_prompt_is_one_quoted_argv_element(self) -> None:
        prompt = "inspect only; touch /tmp/SHOULD_NOT_RUN && echo $(id)"
        remote, cwd = worker_command("delta", prompt, 900)
        self.assertIsNone(cwd)
        parsed = shlex.split(remote[-1])
        self.assertEqual(parsed[-1], prompt)
        self.assertEqual(parsed.count("&&"), 1)  # fixed ``cd HOME && exec``, no injected operator
        self.assertNotIn("--agent", parsed)
        self.assertEqual(parsed[parsed.index("--project") + 1], "Homelab")
        self.assertIn("gemini-3.1-pro-high", parsed)
        self.assertIn("--dangerously-skip-permissions", parsed)

    def test_claude_and_codex_bounded_runs_use_standard_profiles(self) -> None:
        prompt = "bounded; echo $(id) && touch /tmp/NO"
        claude, _ = worker_command("alpha", prompt, 60, "claude")
        self.assertEqual(claude[-1], prompt)
        self.assertIn("opus", claude)
        self.assertIn("--dangerously-skip-permissions", claude)

        codex, _ = worker_command("alpha", prompt, 60, "codex")
        self.assertEqual(codex[-1], prompt)
        self.assertIn("CODEX_HOME=/home/david/.codex", codex)
        self.assertIn("gpt-5.6-sol", codex)
        self.assertIn("danger-full-access", codex)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", codex)

    def test_worker_status_publishes_runtime_model(self) -> None:
        public = WorkerJob(
            job_id="agy-alpha-test",
            host="alpha",
            prompt="test",
            timeout_seconds=60,
        ).public()
        self.assertEqual(public["provider"], "gemini")
        self.assertEqual(public["provider_label"], "Gemini")
        self.assertEqual(public["model"], "gemini-3.1-pro-high")

    def test_remote_shell_command_quotes_each_argument(self) -> None:
        command = _remote_shell_command(["/bin/echo", "a; b", "$(id)"])
        parsed = shlex.split(command)
        self.assertEqual(
            parsed,
            ["cd", "/home/david", "&&", "exec", "/bin/echo", "a; b", "$(id)"],
        )

    def test_unknown_host_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            strategy_command("not-a-host")
        with self.assertRaises(ValueError):
            worker_command("not-a-host", "hello", 60)
        with self.assertRaises(ValueError):
            strategy_command("alpha", "not-a-provider")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            worker_command("alpha", "hello", 60, "not-a-provider")  # type: ignore[arg-type]


class TerminalSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pty_output_is_retained_for_reconnect(self) -> None:
        session = TerminalSession("alpha")
        fake = (["/bin/sh", "-c", "printf 'PTY_OK\\n'"], None)
        with patch("app.gemini_remote.strategy_command", return_value=fake):
            await session.start(80, 24)
            for _ in range(100):
                if session.exited_at is not None:
                    break
                await asyncio.sleep(0.01)
        queue = session.subscribe()
        replay = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertIn(b"PTY_OK", replay)
        self.assertFalse(session.running)
        self.assertEqual(session.exit_code, 0)
        session.unsubscribe(queue)


class WorkerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_launch_reserves_explicit_gemini_route(self) -> None:
        manager = WorkerManager()
        route = {
            "ok": True,
            "provider": "gemini",
            "model": "gemini-3.1-pro-high",
            "state": "GREEN",
            "reason": "explicit provider is available",
        }

        async def no_run(_job):
            return None

        with (
            patch("app.gemini_remote.quota_router.recommend", return_value=route) as recommend,
            patch.object(manager, "_run", side_effect=no_run),
        ):
            job = await manager.launch("alpha", "bounded test", 60, "tiny")
            await job.task

        self.assertEqual(job.task_size, "tiny")
        self.assertEqual(job.routing, route)
        recommend.assert_called_once()
        kwargs = recommend.call_args.kwargs
        self.assertEqual(kwargs["allowed_providers"], ("gemini",))
        self.assertEqual(kwargs["explicit_provider"], "gemini")
        self.assertTrue(kwargs["reserve"])

    async def test_worker_launch_can_explicitly_pin_codex(self) -> None:
        manager = WorkerManager()
        route = {
            "ok": True,
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "state": "GREEN",
            "reason": "explicit provider is available",
        }

        async def no_run(_job):
            return None

        with (
            patch("app.gemini_remote.quota_router.recommend", return_value=route) as recommend,
            patch.object(manager, "_run", side_effect=no_run),
        ):
            job = await manager.launch("charlie", "bounded test", 60, "small", "codex")
            await job.task

        self.assertEqual(job.provider, "codex")
        self.assertEqual(job.public()["model"], "gpt-5.6-sol")
        self.assertEqual(recommend.call_args.kwargs["allowed_providers"], ("codex",))
        self.assertEqual(recommend.call_args.kwargs["explicit_provider"], "codex")
