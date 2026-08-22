"""
Focused stdlib tests for events.db retention
(FLEET-WORKER2-BUILD-20260721-panel-bounded-retention).

Pins: notification_log/run_watch_seen prune on their authoritative timestamp
column with an exact cutoff (rows strictly before the cutoff go, rows at/after
survive); the pre-existing events-table prune is unchanged; the combined
sweep (events.prune_retention_sweep, called by the daily events-retention
scheduler job instead of a second job) runs all three deletes in one
transaction and is idempotent on a second call.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from . import events, notify_store


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class PruneNotificationLogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "events.db"
        # notify_store.init_db() hardcodes DB_PATH; build the schema directly
        # against our temp file instead of monkeypatching module state.
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE notification_log (
                id INTEGER PRIMARY KEY, event_key TEXT, channel TEXT, prio INTEGER,
                title TEXT, body TEXT, navigate TEXT, created_at TEXT NOT NULL,
                read_at TEXT, sent_pwa INTEGER DEFAULT 0, sent_ntfy INTEGER DEFAULT 0
            )"""
        )
        conn.commit()
        conn.close()
        self.now = datetime(2026, 7, 21, tzinfo=timezone.utc)

    def _insert(self, days_old: int) -> None:
        conn = sqlite3.connect(self.db_path)
        ts = _iso(self.now - timedelta(days=days_old))
        conn.execute(
            "INSERT INTO notification_log (event_key, channel, prio, title, created_at) "
            "VALUES ('k', 'c', 1, 't', ?)",
            (ts,),
        )
        conn.commit()
        conn.close()

    def test_exact_cutoff_boundary(self):
        self._insert(31)   # older than cutoff -> pruned
        self._insert(30)   # exactly at cutoff -> retained (cutoff is exclusive)
        self._insert(1)    # within window -> retained

        result = notify_store.prune_notification_log(30, db_path=self.db_path, now=self.now)

        self.assertEqual(result["before"], 3)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["after"], 2)

        conn = sqlite3.connect(self.db_path)
        remaining = [r[0] for r in conn.execute("SELECT created_at FROM notification_log")]
        conn.close()
        self.assertNotIn(_iso(self.now - timedelta(days=31)), remaining)

    def test_no_rows_older_than_cutoff_is_a_noop(self):
        self._insert(1)
        result = notify_store.prune_notification_log(30, db_path=self.db_path, now=self.now)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["after"], 1)

    def test_idempotent_second_prune_deletes_nothing(self):
        self._insert(45)
        self._insert(1)
        first = notify_store.prune_notification_log(30, db_path=self.db_path, now=self.now)
        second = notify_store.prune_notification_log(30, db_path=self.db_path, now=self.now)
        self.assertEqual(first["deleted"], 1)
        self.assertEqual(second["deleted"], 0)
        self.assertEqual(second["before"], second["after"])

    def test_rejects_out_of_range_retention(self):
        with self.assertRaises(ValueError):
            notify_store.prune_notification_log(0, db_path=self.db_path, now=self.now)


class PruneRunWatchSeenTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "events.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE run_watch_seen (
                token TEXT PRIMARY KEY, seat TEXT, outcome TEXT, seen_at TEXT NOT NULL
            )"""
        )
        conn.commit()
        conn.close()
        self.now = datetime(2026, 7, 21, tzinfo=timezone.utc)

    def _insert(self, token: str, days_old: int, outcome: str = "success") -> None:
        conn = sqlite3.connect(self.db_path)
        ts = _iso(self.now - timedelta(days=days_old))
        conn.execute(
            "INSERT INTO run_watch_seen (token, seat, outcome, seen_at) VALUES (?,?,?,?)",
            (token, "worker2", outcome, ts),
        )
        conn.commit()
        conn.close()

    def test_prunes_on_seen_at_not_baseline_exemption(self):
        # A 'baseline' row gets no special treatment — seen_at is still the
        # authoritative cutoff column per the build prompt's instruction to
        # use the authoritative last-seen/timestamp column, not invent one.
        self._insert("old-baseline", 40, outcome="baseline")
        self._insert("recent", 5)

        result = notify_store.prune_run_watch_seen(30, db_path=self.db_path, now=self.now)

        self.assertEqual(result["deleted"], 1)
        conn = sqlite3.connect(self.db_path)
        remaining = {r[0] for r in conn.execute("SELECT token FROM run_watch_seen")}
        conn.close()
        self.assertEqual(remaining, {"recent"})

    def test_no_records_newer_than_cutoff_deleted(self):
        self._insert("a", 29)
        self._insert("b", 15)
        self._insert("c", 1)
        result = notify_store.prune_run_watch_seen(30, db_path=self.db_path, now=self.now)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["after"], 3)


class SharedConnectionTests(unittest.TestCase):
    """Both prune functions accept a caller-owned `conn` so the scheduler can
    run events + notification_log + run_watch_seen in one transaction."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "events.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE notification_log (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE run_watch_seen (token TEXT PRIMARY KEY, seen_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        self.now = datetime(2026, 7, 21, tzinfo=timezone.utc)

    def test_caller_owned_connection_is_not_committed_by_the_prune_call(self):
        conn = sqlite3.connect(self.db_path)
        old = _iso(self.now - timedelta(days=45))
        conn.execute("INSERT INTO notification_log (created_at) VALUES (?)", (old,))
        conn.commit()

        notify_store.prune_notification_log(30, db_path=self.db_path, now=self.now, conn=conn)
        # A second, independent connection should NOT see the delete yet —
        # proves the shared-conn path left the commit to the caller.
        other = sqlite3.connect(self.db_path)
        other.execute("PRAGMA busy_timeout=5000")
        count_before_commit = other.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
        other.close()

        conn.commit()
        conn.close()

        final = sqlite3.connect(self.db_path)
        count_after_commit = final.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
        final.close()

        self.assertEqual(count_before_commit, 1)
        self.assertEqual(count_after_commit, 0)


class PruneRetentionSweepTests(unittest.TestCase):
    """events.prune_retention_sweep — the combined body the daily
    events-retention scheduler job now calls, in place of prune_events alone."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "events.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, seat TEXT, host TEXT,
                session_id TEXT, run_token TEXT, event_type TEXT, tool_name TEXT,
                summary TEXT, payload TEXT
            )"""
        )
        conn.execute(
            "CREATE TABLE notification_log (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE run_watch_seen (token TEXT PRIMARY KEY, seen_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()
        self.now = datetime(2026, 7, 21, tzinfo=timezone.utc)

    def _seed(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO events (ts) VALUES (?)", (_iso(self.now - timedelta(days=8)),)
        )
        conn.execute(
            "INSERT INTO events (ts) VALUES (?)", (_iso(self.now - timedelta(days=1)),)
        )
        conn.execute(
            "INSERT INTO notification_log (created_at) VALUES (?)",
            (_iso(self.now - timedelta(days=31)),),
        )
        conn.execute(
            "INSERT INTO notification_log (created_at) VALUES (?)",
            (_iso(self.now - timedelta(days=1)),),
        )
        conn.execute(
            "INSERT INTO run_watch_seen (token, seen_at) VALUES ('t1', ?)",
            (_iso(self.now - timedelta(days=31)),),
        )
        conn.execute(
            "INSERT INTO run_watch_seen (token, seen_at) VALUES ('t2', ?)",
            (_iso(self.now - timedelta(days=1)),),
        )
        conn.commit()
        conn.close()

    def test_sweep_prunes_all_three_tables_with_correct_cutoffs(self):
        self._seed()

        result = events.prune_retention_sweep(
            db_path=self.db_path,
            now=self.now,
            events_retention_days=7,
            notification_log_retention_days=30,
            run_watch_seen_retention_days=30,
        )

        self.assertEqual(result["events"]["deleted"], 1)
        self.assertEqual(result["events"]["after"], 1)
        self.assertEqual(result["notification_log"]["deleted"], 1)
        self.assertEqual(result["notification_log"]["after"], 1)
        self.assertEqual(result["run_watch_seen"]["deleted"], 1)
        self.assertEqual(result["run_watch_seen"]["after"], 1)

    def test_sweep_is_idempotent(self):
        self._seed()
        events.prune_retention_sweep(
            db_path=self.db_path, now=self.now,
            events_retention_days=7, notification_log_retention_days=30,
            run_watch_seen_retention_days=30,
        )
        second = events.prune_retention_sweep(
            db_path=self.db_path, now=self.now,
            events_retention_days=7, notification_log_retention_days=30,
            run_watch_seen_retention_days=30,
        )
        self.assertEqual(second["events"]["deleted"], 0)
        self.assertEqual(second["notification_log"]["deleted"], 0)
        self.assertEqual(second["run_watch_seen"]["deleted"], 0)

    def test_sweep_commits_once_all_or_nothing(self):
        """A failure pruning one table must not leave an earlier table's
        DELETE committed — the whole sweep is one transaction."""
        self._seed()
        real_connect = sqlite3.connect

        class _BoomConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if "run_watch_seen" in sql and sql.strip().startswith("DELETE"):
                    raise sqlite3.OperationalError("simulated failure")
                return super().execute(sql, *args, **kwargs)

        def boom(db_path, *a, **kw):
            return real_connect(db_path, *a, factory=_BoomConnection, **kw)

        with mock.patch("app.events.sqlite3.connect", side_effect=boom):
            with self.assertRaises(sqlite3.OperationalError):
                events.prune_retention_sweep(
                    db_path=self.db_path, now=self.now,
                    events_retention_days=7, notification_log_retention_days=30,
                    run_watch_seen_retention_days=30,
                )

        # events and notification_log deletes ran on the same (never-committed)
        # connection as the run_watch_seen failure -> nothing was persisted.
        conn = real_connect(self.db_path)
        events_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        notif_count = conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()[0]
        conn.close()
        self.assertEqual(events_count, 2)
        self.assertEqual(notif_count, 2)


if __name__ == "__main__":
    unittest.main()
