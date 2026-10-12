"""
Focused stdlib tests for probe_tailscale_ping's DERP/direct/retry semantics
(FLEET-WORKER2-BUILD-20260711-temple-derp-semantics).

A successful `tailscale ping` via DERP exits rc=1 even though the pong is
valid — these tests pin the parser to the pong LINE, not the exit code, and
pin the one-retry-then-CRIT ladder. Pure stdlib unittest + unittest.mock,
no pytest dependency, so this runs the same in venv or bare python3.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from . import probes
from .config import Node
from .models import Health
from .probes import probe_nas_mount, probe_ping, probe_tailscale_ping


def _node() -> Node:
    return Node(name="echo", address="echo", kinds=["tailscale_ping"])


def _charlie_node() -> Node:
    return Node(name="charlie", address="charlie", kinds=["nas_mount"])


class _FakeSettings:
    tcp_timeout_seconds = 4
    ssh_timeout_seconds = 8


class ProbeTransportRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ssh_transport_failure_retries_once(self):
        recovered = (0, "ok", "")
        with (
            patch.object(
                probes, "_ssh_once", new=AsyncMock(side_effect=[(255, "", "timeout"), recovered])
            ) as mock_once,
            patch("app.probes.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        ):
            result = await probes._ssh("charlie", "true", 4)
        self.assertEqual(result, recovered)
        self.assertEqual(mock_once.call_count, 2)
        mock_sleep.assert_awaited_once()

    async def test_ssh_remote_command_failure_is_not_retried(self):
        with patch.object(
            probes, "_ssh_once", new=AsyncMock(return_value=(1, "", "not mounted"))
        ) as mock_once:
            result = await probes._ssh("charlie", "false", 4)
        self.assertEqual(result[0], 1)
        self.assertEqual(mock_once.call_count, 1)

    async def test_tcp_probe_recovers_on_second_connection_attempt(self):
        writer = unittest.mock.MagicMock()
        writer.wait_closed = AsyncMock()
        with (
            patch(
                "app.probes.asyncio.open_connection",
                new=AsyncMock(side_effect=[TimeoutError("first miss"), (object(), writer)]),
            ) as mock_connect,
            patch("app.probes.asyncio.sleep", new=AsyncMock()),
        ):
            result = await probe_ping(_charlie_node(), _FakeSettings())
        self.assertEqual(result.health, Health.OK)
        self.assertEqual(mock_connect.call_count, 2)

    async def test_tcp_probe_requires_two_failures_before_critical(self):
        with (
            patch(
                "app.probes.asyncio.open_connection",
                new=AsyncMock(side_effect=[TimeoutError("first"), TimeoutError("second")]),
            ) as mock_connect,
            patch("app.probes.asyncio.sleep", new=AsyncMock()),
        ):
            result = await probe_ping(_charlie_node(), _FakeSettings())
        self.assertEqual(result.health, Health.CRIT)
        self.assertEqual(mock_connect.call_count, 2)


class ProbeTailscalePingTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, side_effect):
        with patch("app.probes._local", new=AsyncMock(side_effect=side_effect)) as mock_local:
            result = await probe_tailscale_ping(_node(), _FakeSettings())
        return result, mock_local

    async def test_direct_rc0_ok_with_parsed_latency(self):
        pong = (
            "pong from echo (100.64.0.12) via 100.64.0.12:41641 in 24ms\n"
        )
        result, mock_local = await self._run([(0, pong, "")])
        self.assertEqual(result.health, Health.OK)
        self.assertEqual(result.latency_ms, 24)
        self.assertEqual(mock_local.call_count, 1)
        self.assertIn("direct", result.value.lower())

    async def test_derp_rc1_with_pong_is_ok_with_latency(self):
        text = (
            "pong from echo (100.64.0.12) via DERP(ord) in 113ms\n"
            "direct connection not established"
        )
        result, mock_local = await self._run([(1, text, "")])
        self.assertEqual(result.health, Health.OK)
        self.assertEqual(result.latency_ms, 113)
        self.assertEqual(mock_local.call_count, 1)
        self.assertIn("derp", result.value.lower())
        self.assertIn("DERP(ord)", result.detail)

    async def test_first_miss_then_direct_pong_is_warn_two_calls(self):
        miss = (1, "", "no reply from echo")
        pong = (0, "pong from echo (100.64.0.12) via 100.64.0.12:41641 in 30ms", "")
        result, mock_local = await self._run([miss, pong])
        self.assertEqual(result.health, Health.WARN)
        self.assertEqual(result.latency_ms, 30)
        self.assertEqual(mock_local.call_count, 2)
        self.assertIn("retry", result.detail.lower())

    async def test_first_miss_then_derp_pong_is_warn_two_calls(self):
        miss = (1, "", "no reply from echo")
        pong = (1, "pong from echo (100.64.0.12) via DERP(ord) in 145ms\ndirect connection not established", "")
        result, mock_local = await self._run([miss, pong])
        self.assertEqual(result.health, Health.WARN)
        self.assertEqual(result.latency_ms, 145)
        self.assertEqual(mock_local.call_count, 2)
        self.assertIn("retry", result.detail.lower())

    async def test_two_misses_is_crit_two_calls(self):
        miss = (1, "", "no reply from echo")
        result, mock_local = await self._run([miss, miss])
        self.assertEqual(result.health, Health.CRIT)
        self.assertEqual(result.value, "unreachable")
        self.assertEqual(mock_local.call_count, 2)
        self.assertIsNone(result.latency_ms)

    async def test_decimal_latency_parsing(self):
        pong = "pong from echo (100.64.0.12) via DERP(ord) in 113.7ms\ndirect connection not established"
        result, _ = await self._run([(1, pong, "")])
        self.assertEqual(result.latency_ms, 113)


class ProbeNasMountTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, side_effect):
        with patch("app.probes._ssh", new=AsyncMock(side_effect=side_effect)) as mock_ssh:
            result = await probe_nas_mount(_charlie_node(), _FakeSettings())
        return result, mock_ssh

    async def test_cifs_mounted_is_ok(self):
        result, _ = await self._run([(0, "cifs\n", "")])
        self.assertEqual(result.health, Health.OK)
        self.assertEqual(result.value, "mounted")
        self.assertTrue(result.detail.endswith("cifs"))

    async def test_autofs_idle_is_ok(self):
        result, _ = await self._run([(0, "autofs\n", "")])
        self.assertEqual(result.health, Health.OK)
        self.assertEqual(result.value, "mounted")
        self.assertTrue(result.detail.endswith("cifs (autofs, idle)"))

    async def test_not_mounted_is_warn(self):
        result, _ = await self._run([(1, "", "")])
        self.assertEqual(result.health, Health.WARN)
        self.assertEqual(result.value, "unknown")
        self.assertTrue(result.detail.endswith("not mounted"))


class ProbeDependentSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_host_ssh_failure_makes_dependent_probe_unknown(self):
        ssh_error = (255, "", "ssh: connect to host delta port 22: Connection timed out")
        with patch("app.probes._ssh", new=AsyncMock(return_value=ssh_error)):
            res_icmp = await probes.probe_remote_icmp_delta(_node(), _FakeSettings())
            res_smb = await probes.probe_nas_smb_delta(_node(), _FakeSettings())

        self.assertEqual(res_icmp.health, Health.UNKNOWN)
        self.assertEqual(res_icmp.error_class, "source_unreachable")
        self.assertIn("source host delta unreachable", res_icmp.detail)

        self.assertEqual(res_smb.health, Health.UNKNOWN)
        self.assertEqual(res_smb.error_class, "source_unreachable")
        self.assertIn("source host delta unreachable", res_smb.detail)

    async def test_reached_source_negative_remains_crit(self):
        ping_loss = (1, "1 packets transmitted, 0 received, 100% packet loss", "")
        with patch("app.probes._ssh", new=AsyncMock(return_value=ping_loss)):
            res_icmp = await probes.probe_remote_icmp_delta(_node(), _FakeSettings())

        self.assertEqual(res_icmp.health, Health.CRIT)
        self.assertEqual(res_icmp.error_class, "icmp_failed")
        self.assertEqual(res_icmp.value, "unreachable")

        tcp_closed = (1, "", "TimeoutError: [Errno 110] Connection timed out")
        with patch("app.probes._ssh", new=AsyncMock(return_value=tcp_closed)):
            res_smb = await probes.probe_nas_smb_delta(_node(), _FakeSettings())

        self.assertEqual(res_smb.health, Health.CRIT)
        self.assertEqual(res_smb.error_class, "tcp_failed")
        self.assertEqual(res_smb.value, "closed")


if __name__ == "__main__":
    unittest.main()
