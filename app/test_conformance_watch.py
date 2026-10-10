"""Focused tests for the independent, cache-only conformance transition
watcher (app/conformance_watch.py, CONFORMANCE-2 part D).

Isolation mirrors app/test_model_usage_watch.py: a temp-file-backed
notify_store DB (DB_PATH monkeypatch) and a temp conformance cache file passed
explicitly via scan_once(cache_path=...) -- no real events.db or state/
file is ever touched. Only the outward call (conformance_watch.notify) is
mocked; the real alerts-table watermark SQL runs for real, so a second
in-process call genuinely exercises "restart cannot replay" persistence.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import conformance_watch, notify_store


def _cache(path: Path, checks: list[dict], generated_at: str | None = None) -> None:
    payload = {
        "version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "overall": "ok",
        "counts": {"ok": 0, "warning": 0, "error": 0, "unknown": 0},
        "checks": checks,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _check(check_id: str, state: str, consecutive_non_ok_scans: int | None = None) -> dict:
    return {
        "id": check_id, "category": "service", "host": "alpha", "state": state,
        "expected": "active", "actual": state, "checked_at": "2026-07-30T10:00:00Z",
        "consecutive_non_ok_scans": (
            consecutive_non_ok_scans if consecutive_non_ok_scans is not None
            else (0 if state == "ok" else 1)
        ),
    }


class ConformanceWatchTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.cache_path = self.root / "conformance.json"

        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = self.root / "events.db"
        notify_store.init_db()

    def tearDown(self):
        notify_store.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    async def _tick(self, now=None):
        with patch.object(
            conformance_watch, "notify", new=AsyncMock(return_value={"suppressed": False}),
        ) as mock_notify:
            result = await conformance_watch.scan_once(cache_path=self.cache_path, now=now)
        return result, mock_notify


class SilentSeedTests(ConformanceWatchTestCase):
    async def test_first_tick_seeds_everything_with_zero_external_sends(self):
        _cache(self.cache_path, [_check("unit:alpha:a", "ok"), _check("unit:alpha:b", "error")])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"], {"seeded": 2, "fired_alarm": 0, "fired_recovery": 0, "retired": 0})
        self.assertFalse(result["cache"]["fired"])
        self.assertTrue(result["cache"]["seeded"])

    async def test_informational_peer_check_is_visible_but_never_alerted(self):
        check = _check("ssh:alpha:macbook", "error")
        check["impact"] = "informational"
        _cache(self.cache_path, [check])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"], {
            "seeded": 0, "fired_alarm": 0, "fired_recovery": 0, "retired": 0,
        })
        mock_notify.assert_not_awaited()
        self.assertEqual(
            notify_store.list_alerts_by_condition(conformance_watch.CHECK_CONDITION_KEY), []
        )
        mock_notify.assert_not_awaited()

    async def test_new_check_appearing_later_seeds_without_fabricating_transition(self):
        _cache(self.cache_path, [_check("unit:alpha:a", "ok")])
        await self._tick()
        # A second, brand-new check id appears already in an error state (the
        # manifest grew) -- it must seed silently, never fire on its very
        # first observation.
        _cache(self.cache_path, [_check("unit:alpha:a", "ok"), _check("unit:alpha:new", "error")])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"]["seeded"], 1)
        self.assertEqual(result["checks"]["fired_alarm"], 0)
        mock_notify.assert_not_awaited()


class PerCheckEdgeTests(ConformanceWatchTestCase):
    async def test_removed_check_is_retired_without_notification(self):
        _cache(self.cache_path, [_check("path:alpha:/retired", "ok")])
        await self._tick()
        _cache(self.cache_path, [])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"]["retired"], 1)
        mock_notify.assert_not_awaited()
        row = notify_store.get_alert_by_condition(
            conformance_watch.CHECK_CONDITION_KEY, "path:alpha:/retired"
        )
        self.assertEqual(row["last_seen"], "retired")
        self.assertIsNotNone(row["resolved_at"])

    async def test_reintroduced_retired_check_reseeds_without_recovery(self):
        _cache(self.cache_path, [_check("path:alpha:/retired", "ok")])
        await self._tick()
        _cache(self.cache_path, [])
        await self._tick()
        _cache(self.cache_path, [_check("path:alpha:/retired", "ok")])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"]["seeded"], 1)
        mock_notify.assert_not_awaited()
    async def test_one_scan_failure_is_deferred(self):
        _cache(self.cache_path, [_check("unit:alpha:a", "ok")])
        await self._tick()  # consume the seed

        _cache(self.cache_path, [_check("unit:alpha:a", "error")])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"]["fired_alarm"], 0)
        mock_notify.assert_not_awaited()

    async def test_second_consecutive_failure_fires_once(self):
        _cache(self.cache_path, [_check("unit:alpha:a", "ok")])
        await self._tick()  # consume the seed

        _cache(self.cache_path, [_check("unit:alpha:a", "error")])
        await self._tick()  # deferred first failure
        _cache(self.cache_path, [_check("unit:alpha:a", "error", 2)])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"]["fired_alarm"], 1)
        mock_notify.assert_awaited_once()
        payload = mock_notify.await_args.args[0]
        self.assertEqual(payload["condition"], "conformance_check_drift")
        self.assertEqual(payload["navigate"], "/operations?tab=conformance")
        self.assertIn("unit:alpha:a", payload["event_key"])
        self.assertIn("alarm", payload["event_key"])

    async def test_unchanged_non_ok_state_does_not_repeat(self):
        _cache(self.cache_path, [_check("unit:alpha:a", "ok")])
        await self._tick()
        _cache(self.cache_path, [_check("unit:alpha:a", "error", 2)])
        await self._tick()  # consumes the rising edge

        _cache(self.cache_path, [_check("unit:alpha:a", "error")])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"]["fired_alarm"], 0)
        self.assertEqual(result["checks"]["fired_recovery"], 0)
        mock_notify.assert_not_awaited()

    async def test_falling_edge_fires_recovery_once(self):
        _cache(self.cache_path, [_check("unit:alpha:a", "ok")])
        await self._tick()
        _cache(self.cache_path, [_check("unit:alpha:a", "error", 2)])
        await self._tick()

        _cache(self.cache_path, [_check("unit:alpha:a", "ok")])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"]["fired_recovery"], 1)
        mock_notify.assert_awaited_once()
        payload = mock_notify.await_args.args[0]
        self.assertEqual(payload["condition"], "conformance_check_recovery")
        self.assertIn("recovery", payload["event_key"])

    async def test_warning_to_error_both_non_ok_does_not_double_fire(self):
        _cache(self.cache_path, [_check("unit:alpha:a", "ok")])
        await self._tick()
        _cache(self.cache_path, [_check("unit:alpha:a", "warning", 2)])
        result1, mock_notify1 = await self._tick()
        self.assertEqual(result1["checks"]["fired_alarm"], 1)

        _cache(self.cache_path, [_check("unit:alpha:a", "error")])
        result2, mock_notify2 = await self._tick()
        self.assertEqual(result2["checks"]["fired_alarm"], 0)
        mock_notify2.assert_not_awaited()

    async def test_restart_cannot_replay_a_prior_edge(self):
        """A fresh call to scan_once (simulating a process restart, since the
        watermark lives in events.db, not memory) must not re-fire an edge
        that already fired before the 'restart'."""
        _cache(self.cache_path, [_check("unit:alpha:a", "ok")])
        await self._tick()
        _cache(self.cache_path, [_check("unit:alpha:a", "error", 2)])
        result, mock_notify = await self._tick()
        self.assertEqual(result["checks"]["fired_alarm"], 1)
        mock_notify.assert_awaited_once()

        # "Restart": call scan_once again against the SAME still-error cache.
        result_again, mock_notify_again = await self._tick()
        self.assertEqual(result_again["checks"]["fired_alarm"], 0)
        mock_notify_again.assert_not_awaited()


class CacheFreshnessTests(ConformanceWatchTestCase):
    async def test_stale_cache_fires_alarm_once(self):
        fresh_now = datetime.now(timezone.utc)
        _cache(self.cache_path, [], generated_at=fresh_now.isoformat().replace("+00:00", "Z"))
        await self._tick(now=fresh_now)  # seed as fresh

        stale_now = fresh_now + timedelta(seconds=conformance_watch.STALE_THRESHOLD_SECONDS + 60)
        result, mock_notify = await self._tick(now=stale_now)
        self.assertTrue(result["cache"]["fired"])
        self.assertEqual(result["cache"]["state"], "non_ok")
        mock_notify.assert_awaited_once()
        payload = mock_notify.await_args.args[0]
        self.assertEqual(payload["condition"], "conformance_cache_stale")

    async def test_cache_missing_is_treated_as_unavailable_and_recovers(self):
        fresh_now = datetime.now(timezone.utc)
        _cache(self.cache_path, [], generated_at=fresh_now.isoformat().replace("+00:00", "Z"))
        await self._tick(now=fresh_now)  # seed as fresh

        self.cache_path.unlink()
        later = fresh_now + timedelta(seconds=60)
        result, mock_notify = await self._tick(now=later)
        self.assertTrue(result["cache"]["fired"])
        mock_notify.assert_awaited_once()
        self.assertEqual(mock_notify.await_args.args[0]["condition"], "conformance_cache_stale")

        _cache(self.cache_path, [], generated_at=(later + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"))
        recovered, mock_notify2 = await self._tick(now=later + timedelta(seconds=2))
        self.assertTrue(recovered["cache"]["fired"])
        self.assertEqual(mock_notify2.await_args.args[0]["condition"], "conformance_cache_recovery")

    async def test_no_repeated_alarm_while_still_stale(self):
        fresh_now = datetime.now(timezone.utc)
        _cache(self.cache_path, [], generated_at=fresh_now.isoformat().replace("+00:00", "Z"))
        await self._tick(now=fresh_now)

        stale_now = fresh_now + timedelta(seconds=conformance_watch.STALE_THRESHOLD_SECONDS + 60)
        await self._tick(now=stale_now)
        result, mock_notify = await self._tick(now=stale_now + timedelta(seconds=300))
        self.assertFalse(result["cache"]["fired"])
        mock_notify.assert_not_awaited()


class MalformedCacheTests(ConformanceWatchTestCase):
    async def test_malformed_cache_degrades_to_unavailable_not_a_crash(self):
        self.cache_path.write_text("{not json", encoding="utf-8")
        result, mock_notify = await self._tick()
        self.assertTrue(result["cache"]["seeded"])
        self.assertEqual(result["checks"], {"seeded": 0, "fired_alarm": 0, "fired_recovery": 0, "retired": 0})
        mock_notify.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
