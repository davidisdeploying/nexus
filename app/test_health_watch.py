"""
Focused stdlib test for the settled-terminal Gallery heartbeat exemption in
app/health_watch.py::_eval_heartbeat_stale
(FLEET-WORKER2-BUILD-20260722-panel-gallery-terminal-stale-alert).

Per the recon, a settled-terminal gallery-library-scan.json record (state
done/failed, ended_at stamped, queues present) is EXPECTED to stop updating
every tick once the producer (app/jobs/gallery.py::run_gallery_heartbeat)
throttles itself down to a 15-minute discovery poll — age alone can't tell
that apart from a dead producer. `_eval_heartbeat_stale` now reuses
jobs.gallery.is_settled_terminal to exempt that exact shape from the
300s age threshold; every other shape (active, missing, malformed, or a
failed exception-fallback record with no queues list) still falls through to
the unchanged age check, on every host.

Isolation: a temp-file-backed notify_store DB (DB_PATH monkeypatch, mirrors
test_milestone_watch.py) and a temp heartbeats_dir (settings monkeypatch) so
no real events.db or vault file is touched. Only the single outward call —
app.health_watch.notify — is mocked; the real evaluate()/notify_store
watermark SQL runs for real.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from . import health_watch, notify_store


def _write(dir_path: Path, name: str, payload: dict) -> None:
    (dir_path / name).write_text(json.dumps(payload))


class HeartbeatStaleGalleryTerminalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._hb_dir = Path(self._tmpdir.name) / "heartbeats"
        self._hb_dir.mkdir()

        self._orig_db_path = notify_store.DB_PATH
        notify_store.DB_PATH = Path(self._tmpdir.name) / "events.db"
        notify_store.init_db()

        self._settings_patcher = patch.object(
            health_watch, "settings", new=type("S", (), {"heartbeats_dir": self._hb_dir})()
        )
        self._settings_patcher.start()

    def tearDown(self):
        self._settings_patcher.stop()
        notify_store.DB_PATH = self._orig_db_path
        self._tmpdir.cleanup()

    async def _tick(self):
        with patch.object(health_watch, "notify", new=AsyncMock(return_value={"suppressed": False})) as mock_notify:
            results = await health_watch._eval_heartbeat_stale(None)
        return results, mock_notify

    def _gallery_result(self, results):
        return next(r for r in results if r["host"] == "gallery-library-scan")

    async def _seed_all_healthy(self):
        """evaluate()'s WATERMARK-FROM-NOW seed means the FIRST-EVER tick for
        a condition/host never fires regardless of its state -- it just
        baselines. Tests asserting a genuine alarm fire need a prior healthy
        tick to consume that seed first, exactly like a real fleet that was
        already running before the condition went bad."""
        _write(self._hb_dir, "host-charlie.json", {"updated_at": self._now_iso()})
        _write(self._hb_dir, "host-delta.json", {"updated_at": self._now_iso()})
        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "running", "queues": [{"name": "metadata", "done": 1, "total": 2}],
            "ts": time.time(),
        })
        await self._tick()

    @staticmethod
    def _now_iso():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    async def test_settled_terminal_done_very_old_ts_never_opens_stale(self):
        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "done", "ended_at": "2026-07-13T03:58:38Z",
            "queues": [{"name": "metadata", "done": 1, "total": 1}],
            "ts": time.time() - 30 * 24 * 3600,
        })
        results, mock_notify = await self._tick()
        r = self._gallery_result(results)
        self.assertEqual(r["state"], "clear")
        self.assertFalse(r["fired"])
        self.assertEqual(mock_notify.call_count, 0)

        # a later tick (still no age movement) must not remind either
        results2, mock_notify2 = await self._tick()
        r2 = self._gallery_result(results2)
        self.assertFalse(r2["fired"])
        self.assertEqual(mock_notify2.call_count, 0)

    async def test_settled_terminal_failed_full_shape_never_opens_stale(self):
        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "failed", "ended_at": "2026-07-13T03:58:38Z",
            "queues": [],
            "ts": time.time() - 30 * 24 * 3600,
        })
        results, mock_notify = await self._tick()
        r = self._gallery_result(results)
        self.assertEqual(r["state"], "clear")
        self.assertFalse(r["fired"])
        self.assertEqual(mock_notify.call_count, 0)

    async def test_active_record_still_opens_stale_after_threshold(self):
        await self._seed_all_healthy()
        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "running", "queues": [{"name": "metadata", "done": 1, "total": 2}],
            "ts": time.time() - (health_watch.GALLERY_HEARTBEAT_STALE_SECONDS + 60),
        })
        results, mock_notify = await self._tick()
        r = self._gallery_result(results)
        self.assertEqual(r["state"], "alarm")
        self.assertTrue(r["fired"])
        self.assertEqual(mock_notify.call_count, 1)

    async def test_failed_exception_fallback_without_queues_still_opens_stale(self):
        await self._seed_all_healthy()
        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "failed", "ended_at": "2026-07-13T03:58:38Z",
            # no `queues` key at all -- the exception-fallback shape
            "ts": time.time() - (health_watch.GALLERY_HEARTBEAT_STALE_SECONDS + 60),
        })
        results, mock_notify = await self._tick()
        r = self._gallery_result(results)
        self.assertEqual(r["state"], "alarm")
        self.assertTrue(r["fired"])
        self.assertEqual(mock_notify.call_count, 1)

    async def test_missing_heartbeat_file_behavior_unchanged(self):
        await self._seed_all_healthy()
        # file never written for gallery-library-scan.json on this tick
        (self._hb_dir / "gallery-library-scan.json").unlink()
        results, mock_notify = await self._tick()
        r = self._gallery_result(results)
        self.assertEqual(r["state"], "alarm")
        self.assertTrue(r["fired"])
        self.assertEqual(mock_notify.call_count, 1)

    async def test_malformed_heartbeat_file_behavior_unchanged(self):
        await self._seed_all_healthy()
        (self._hb_dir / "gallery-library-scan.json").write_text("{not json")
        results, mock_notify = await self._tick()
        r = self._gallery_result(results)
        self.assertEqual(r["state"], "alarm")
        self.assertTrue(r["fired"])
        self.assertEqual(mock_notify.call_count, 1)

    async def test_genuine_active_stale_to_fresh_recovery_still_fires(self):
        await self._seed_all_healthy()
        old_ts = time.time() - (health_watch.GALLERY_HEARTBEAT_STALE_SECONDS + 60)
        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "running", "queues": [{"name": "metadata", "done": 1, "total": 2}], "ts": old_ts,
        })
        results1, mock_notify1 = await self._tick()
        self.assertTrue(self._gallery_result(results1)["fired"])
        self.assertEqual(mock_notify1.call_count, 1)

        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "running", "queues": [{"name": "metadata", "done": 2, "total": 2}], "ts": time.time(),
        })
        results2, mock_notify2 = await self._tick()
        r2 = self._gallery_result(results2)
        self.assertEqual(r2["state"], "clear")
        self.assertTrue(r2["fired"])
        self.assertEqual(mock_notify2.call_count, 1)

    async def test_other_hosts_preserve_existing_behavior(self):
        await self._seed_all_healthy()
        old_ts_iso = "2020-01-01T00:00:00+00:00"
        _write(self._hb_dir, "host-charlie.json", {"updated_at": old_ts_iso})
        _write(self._hb_dir, "host-delta.json", {"updated_at": old_ts_iso})
        # give host-charlie a settled-terminal-*looking* shape to prove the
        # exemption is scoped to host == "gallery-library-scan" only
        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "running", "queues": [{"name": "metadata", "done": 1, "total": 2}],
            "ts": time.time(),
        })
        results, mock_notify = await self._tick()
        by_host = {r["host"]: r for r in results}
        self.assertEqual(by_host["charlie"]["state"], "alarm")
        self.assertEqual(by_host["delta"]["state"], "alarm")
        self.assertTrue(by_host["charlie"]["fired"])
        self.assertTrue(by_host["delta"]["fired"])
        self.assertEqual(mock_notify.call_count, 2)

    async def test_preopen_false_alert_normalized_silently(self):
        """Documents the exact silent-normalization mechanism used against the
        live DB: seed an open alarm episode (mirrors the real pre-existing
        false alert), normalize it with the SAME two existing notify_store
        calls the live rollout uses (mark_alert_resolved + update_alert_seen)
        -- no notify() call -- then prove the first post-fix tick against a
        settled-terminal record stays silent with the watermark already
        clear."""
        seeded = notify_store.seed_alert("heartbeat_stale", "gallery-library-scan", "clear")
        # force it into the open "alarm" shape the live row was found in
        notify_store.mark_alert_notified(seeded["id"], "alarm")
        row = notify_store.get_alert_by_condition("heartbeat_stale", "gallery-library-scan")
        self.assertEqual(row["last_seen"], "alarm")
        self.assertIsNone(row["resolved_at"])

        with patch.object(health_watch, "notify", new=AsyncMock(return_value={"suppressed": False})) as mock_notify:
            notify_store.mark_alert_resolved(row["id"])
            notify_store.update_alert_seen(row["id"], "clear")
        self.assertEqual(mock_notify.call_count, 0)

        normalized = notify_store.get_alert_by_condition("heartbeat_stale", "gallery-library-scan")
        self.assertEqual(normalized["last_seen"], "clear")
        self.assertIsNotNone(normalized["resolved_at"])

        _write(self._hb_dir, "gallery-library-scan.json", {
            "state": "done", "ended_at": "2026-07-13T03:58:38Z",
            "queues": [{"name": "metadata", "done": 1, "total": 1}],
            "ts": time.time() - 30 * 24 * 3600,
        })
        results, mock_notify2 = await self._tick()
        r = self._gallery_result(results)
        self.assertEqual(r["state"], "clear")
        self.assertFalse(r["fired"])
        self.assertEqual(mock_notify2.call_count, 0)


if __name__ == "__main__":
    unittest.main()
