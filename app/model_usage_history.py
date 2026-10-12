"""Durable, secret-free history for Claude, Codex, and Gemini quota telemetry."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DB = (
    Path.home()
    / ".local"
    / "share"
    / "nexus"
    / "model-usage-history.sqlite3"
)
RANGE_SECONDS = {
    "24h": 86400,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
    "all": None,
}
BUCKET_SECONDS = {
    "24h": 300,
    "7d": 1800,
    "30d": 7200,
    "90d": 21600,
    "all": 86400,
}
PROVIDERS = ("claude", "codex", "gemini")
WINDOWS = ("five_hour", "weekly", "fable_weekly")


def _connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: Path = DEFAULT_DB) -> None:
    with _connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage_samples (
                id INTEGER PRIMARY KEY,
                captured_at INTEGER NOT NULL,
                provider TEXT NOT NULL CHECK(provider IN ('claude','codex','gemini')),
                ok INTEGER NOT NULL CHECK(ok IN (0,1)),
                source TEXT,
                source_generated_at INTEGER,
                fallback_from TEXT,
                error_class TEXT,
                plan_type TEXT,
                limit_id TEXT,
                UNIQUE(captured_at, provider)
            );
            CREATE INDEX IF NOT EXISTS usage_samples_provider_time
                ON usage_samples(provider, captured_at DESC);

            CREATE TABLE IF NOT EXISTS usage_windows (
                sample_id INTEGER NOT NULL REFERENCES usage_samples(id) ON DELETE CASCADE,
                window TEXT NOT NULL CHECK(window IN ('five_hour','weekly','fable_weekly')),
                used_percent REAL,
                remaining_percent REAL,
                resets_at INTEGER,
                duration_minutes INTEGER,
                PRIMARY KEY(sample_id, window),
                CHECK(used_percent IS NULL OR (used_percent >= 0 AND used_percent <= 100)),
                CHECK(remaining_percent IS NULL OR
                      (remaining_percent >= 0 AND remaining_percent <= 100))
            );
            CREATE INDEX IF NOT EXISTS usage_windows_reset
                ON usage_windows(window, resets_at);

            CREATE TABLE IF NOT EXISTS usage_events (
                id INTEGER PRIMARY KEY,
                captured_at INTEGER NOT NULL,
                provider TEXT NOT NULL CHECK(provider IN ('claude','codex','gemini')),
                window TEXT,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL CHECK(severity IN ('info','warn')),
                previous_used_percent REAL,
                used_percent REAL,
                previous_resets_at INTEGER,
                resets_at INTEGER,
                previous_source TEXT,
                source TEXT,
                UNIQUE(captured_at, provider, window, event_type)
            );
            CREATE INDEX IF NOT EXISTS usage_events_provider_time
                ON usage_events(provider, captured_at DESC);

            CREATE TABLE IF NOT EXISTS usage_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO usage_meta(key, value) VALUES('schema_version', '1');
            """
        )
    os.chmod(path, 0o600)


def _epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _percent(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def _normalize_windows(windows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(windows, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for name in WINDOWS:
        raw = windows.get(name)
        if not isinstance(raw, dict):
            continue
        used = _percent(raw.get("used_percent"))
        remaining = _percent(raw.get("remaining_percent"))
        if used is None and remaining is not None:
            used = round(100 - remaining, 4)
        if remaining is None and used is not None:
            remaining = round(100 - used, 4)
        if used is None:
            continue
        normalized[name] = {
            "used_percent": used,
            "remaining_percent": remaining,
            "resets_at": _epoch(raw.get("resets_at")),
            "duration_minutes": raw.get("duration_minutes"),
        }
    return normalized


def _freshest_codex(quota_dir: Path, now: int) -> dict[str, Any] | None:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for path in quota_dir.glob("*-codex.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        generated = _epoch(payload.get("generated_at"))
        limits = payload.get("rateLimits")
        if (
            payload.get("ok") is True
            and generated is not None
            and now - generated <= 900
            and isinstance(limits, dict)
        ):
            candidates.append((generated, payload))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _codex_record(quota_dir: Path, now: int) -> dict[str, Any]:
    payload = _freshest_codex(quota_dir, now)
    if payload is None:
        return {
            "ok": False,
            "source": "codex app-server",
            "error_class": "Unavailable",
            "windows": {},
        }
    limits = payload["rateLimits"]
    windows: dict[str, dict[str, Any]] = {}
    for raw in (limits.get("primary"), limits.get("secondary")):
        if not isinstance(raw, dict):
            continue
        duration = raw.get("windowDurationMins")
        name = "weekly" if duration == 10080 else "five_hour" if duration == 300 else None
        used = _percent(raw.get("usedPercent"))
        if name and used is not None:
            windows[name] = {
                "used_percent": used,
                "remaining_percent": round(100 - used, 4),
                "resets_at": _epoch(raw.get("resetsAt")),
                "duration_minutes": duration,
            }
    return {
        "ok": bool(windows),
        "source": "codex app-server",
        "source_generated_at": _epoch(payload.get("generated_at")),
        "plan_type": limits.get("planType"),
        "limit_id": limits.get("limitId"),
        "error_class": None if windows else "MissingWindow",
        "windows": windows,
    }


def provider_records(
    model_payload: dict[str, Any],
    quota_dir: Path,
    now: int,
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for provider, key in (("claude", "claude"), ("gemini", "gemini")):
        raw = model_payload.get(key)
        raw = raw if isinstance(raw, dict) else {}
        records[provider] = {
            "ok": raw.get("ok") is True,
            "source": raw.get("source"),
            "source_generated_at": _epoch(model_payload.get("generated_at")),
            "fallback_from": raw.get("fallback_from"),
            # Persist only bounded classes, never collector tails or response
            # text: those can contain authenticated endpoint details.
            "error_class": raw.get("direct_error")
            or ("Unavailable" if raw.get("ok") is not True else None),
            "plan_type": None,
            "limit_id": None,
            "windows": _normalize_windows(raw.get("windows")),
        }
    records["codex"] = _codex_record(quota_dir, now)
    return records


def _last_sample(conn: sqlite3.Connection, provider: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM usage_samples WHERE provider=? ORDER BY captured_at DESC LIMIT 1",
        (provider,),
    ).fetchone()


def _last_window(
    conn: sqlite3.Connection,
    provider: str,
    window: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.captured_at, s.source, w.*
        FROM usage_windows w JOIN usage_samples s ON s.id=w.sample_id
        WHERE s.provider=? AND w.window=?
        ORDER BY s.captured_at DESC LIMIT 1
        """,
        (provider, window),
    ).fetchone()


def _event(
    conn: sqlite3.Connection,
    *,
    captured_at: int,
    provider: str,
    event_type: str,
    severity: str,
    window: str | None = None,
    previous: sqlite3.Row | None = None,
    current: dict[str, Any] | None = None,
    previous_source: str | None = None,
    source: str | None = None,
) -> None:
    current = current or {}
    conn.execute(
        """
        INSERT OR IGNORE INTO usage_events(
            captured_at,provider,window,event_type,severity,
            previous_used_percent,used_percent,previous_resets_at,resets_at,
            previous_source,source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            captured_at,
            provider,
            window,
            event_type,
            severity,
            previous["used_percent"] if previous is not None else None,
            current.get("used_percent"),
            previous["resets_at"] if previous is not None else None,
            current.get("resets_at"),
            previous_source,
            source,
        ),
    )


def record_provider_snapshot(
    conn: sqlite3.Connection,
    captured_at: int,
    provider: str,
    record: dict[str, Any],
) -> bool:
    previous_sample = _last_sample(conn, provider)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO usage_samples(
            captured_at,provider,ok,source,source_generated_at,fallback_from,
            error_class,plan_type,limit_id
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            captured_at,
            provider,
            int(record.get("ok") is True),
            record.get("source"),
            record.get("source_generated_at"),
            record.get("fallback_from"),
            record.get("error_class"),
            record.get("plan_type"),
            record.get("limit_id"),
        ),
    )
    if cursor.rowcount == 0:
        return False
    sample_id = int(cursor.lastrowid)

    if previous_sample is not None:
        if bool(previous_sample["ok"]) != bool(record.get("ok")):
            _event(
                conn,
                captured_at=captured_at,
                provider=provider,
                event_type=(
                    "availability_restored" if record.get("ok") else "availability_lost"
                ),
                severity="info" if record.get("ok") else "warn",
                previous_source=previous_sample["source"],
                source=record.get("source"),
            )
        if (
            previous_sample["source"]
            and record.get("source")
            and previous_sample["source"] != record.get("source")
        ):
            _event(
                conn,
                captured_at=captured_at,
                provider=provider,
                event_type="source_changed",
                severity="info",
                previous_source=previous_sample["source"],
                source=record.get("source"),
            )
    if record.get("fallback_from"):
        _event(
            conn,
            captured_at=captured_at,
            provider=provider,
            event_type="fallback_used",
            severity="warn",
            previous_source=record.get("fallback_from"),
            source=record.get("source"),
        )

    for window, current in record.get("windows", {}).items():
        if window not in WINDOWS:
            continue
        previous = _last_window(conn, provider, window)
        conn.execute(
            """
            INSERT INTO usage_windows(
                sample_id,window,used_percent,remaining_percent,resets_at,duration_minutes
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                sample_id,
                window,
                current.get("used_percent"),
                current.get("remaining_percent"),
                current.get("resets_at"),
                current.get("duration_minutes"),
            ),
        )
        if previous is None:
            continue
        old_reset, new_reset = previous["resets_at"], current.get("resets_at")
        if old_reset and new_reset and abs(new_reset - old_reset) > 60:
            early = captured_at < old_reset - 300
            _event(
                conn,
                captured_at=captured_at,
                provider=provider,
                window=window,
                event_type="window_reanchored_early" if early else "window_rolled_over",
                severity="warn" if early else "info",
                previous=previous,
                current=current,
                previous_source=previous["source"],
                source=record.get("source"),
            )
        old_used, new_used = previous["used_percent"], current.get("used_percent")
        if (
            old_used is not None
            and new_used is not None
            and old_used - new_used >= 20
        ):
            _event(
                conn,
                captured_at=captured_at,
                provider=provider,
                window=window,
                event_type="usage_dropped",
                severity="info",
                previous=previous,
                current=current,
                previous_source=previous["source"],
                source=record.get("source"),
            )
    return True


def record_snapshot(
    model_payload: dict[str, Any],
    quota_dir: Path,
    db_path: Path = DEFAULT_DB,
    *,
    captured_at: int | None = None,
) -> int:
    captured_at = captured_at or int(time.time())
    init_db(db_path)
    inserted = 0
    with _connect(db_path) as conn:
        for provider, record in provider_records(
            model_payload, quota_dir, captured_at
        ).items():
            inserted += int(
                record_provider_snapshot(conn, captured_at, provider, record)
            )
    return inserted


def _routing_records(payload: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        provider = candidate.get("provider")
        if provider not in PROVIDERS:
            continue
        remaining = candidate.get("remaining") or {}
        resets = candidate.get("resets_at") or {}
        windows = {}
        for window in ("five_hour", "weekly"):
            rem = _percent(remaining.get(window))
            if rem is None:
                continue
            windows[window] = {
                "used_percent": round(100 - rem, 4),
                "remaining_percent": rem,
                "resets_at": _epoch(resets.get(window)),
                "duration_minutes": 300 if window == "five_hour" else 10080,
            }
        yield provider, {
            "ok": bool(windows),
            "source": candidate.get("source") or "routing backfill",
            "source_generated_at": _epoch(payload.get("generated_at")),
            "fallback_from": None,
            "error_class": None if windows else "Unavailable",
            "plan_type": None,
            "limit_id": None,
            "windows": windows,
        }


def backfill_routing(root: Path, db_path: Path = DEFAULT_DB) -> int:
    init_db(db_path)
    payloads: list[tuple[int, dict[str, Any]]] = []
    for path in root.glob("from-*/runs/*/routing.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        captured = _epoch(payload.get("generated_at"))
        if captured is not None:
            payloads.append((captured, payload))
    inserted = 0
    with _connect(db_path) as conn:
        for captured, payload in sorted(payloads, key=lambda item: item[0]):
            for provider, record in _routing_records(payload):
                inserted += int(
                    record_provider_snapshot(conn, captured, provider, record)
                )
    return inserted


def _series(
    conn: sqlite3.Connection,
    since: int | None,
    bucket: int,
    provider: str | None,
) -> list[dict[str, Any]]:
    clauses = ["w.used_percent IS NOT NULL"]
    params: list[Any] = []
    if since is not None:
        clauses.append("s.captured_at>=?")
        params.append(since)
    if provider:
        clauses.append("s.provider=?")
        params.append(provider)
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT s.captured_at,s.provider,s.source,w.window,w.used_percent,
                   w.remaining_percent,w.resets_at,
                   ROW_NUMBER() OVER (
                     PARTITION BY s.provider,w.window,CAST(s.captured_at / ? AS INTEGER)
                     ORDER BY s.captured_at DESC
                   ) AS rn
            FROM usage_samples s JOIN usage_windows w ON w.sample_id=s.id
            WHERE {' AND '.join(clauses)}
        )
        SELECT * FROM ranked WHERE rn=1
        ORDER BY captured_at,provider,window
        """,
        [bucket, *params],
    ).fetchall()
    return [dict(row) for row in rows]


def _latest(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH latest_samples AS (
            SELECT *,ROW_NUMBER() OVER(PARTITION BY provider ORDER BY captured_at DESC) rn
            FROM usage_samples
        )
        SELECT s.id,s.captured_at,s.provider,s.ok,s.source,s.fallback_from,
               s.error_class,s.plan_type,s.limit_id,w.window,w.used_percent,
               w.remaining_percent,w.resets_at
        FROM latest_samples s
        LEFT JOIN usage_windows w ON w.sample_id=s.id
        WHERE s.rn=1
        ORDER BY s.provider,w.window
        """
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = grouped.setdefault(
            row["provider"],
            {
                key: row[key]
                for key in (
                    "captured_at",
                    "provider",
                    "ok",
                    "source",
                    "fallback_from",
                    "error_class",
                    "plan_type",
                    "limit_id",
                )
            }
            | {"windows": {}},
        )
        if row["window"]:
            item["windows"][row["window"]] = {
                key: row[key]
                for key in ("used_percent", "remaining_percent", "resets_at")
            }
    return list(grouped.values())


def history_payload(
    range_name: str = "30d",
    provider: str | None = None,
    db_path: Path = DEFAULT_DB,
) -> dict[str, Any]:
    if range_name not in RANGE_SECONDS:
        raise ValueError("range must be 24h, 7d, 30d, 90d, or all")
    if provider == "all":
        provider = None
    if provider is not None and provider not in PROVIDERS:
        raise ValueError("provider must be claude, codex, gemini, or all")
    now = int(time.time())
    seconds = RANGE_SECONDS[range_name]
    since = now - seconds if seconds else None
    init_db(db_path)
    with _connect(db_path) as conn:
        clauses, params = [], []
        if since is not None:
            clauses.append("captured_at>=?")
            params.append(since)
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        summary = dict(
            conn.execute(
                f"""
                SELECT COUNT(*) samples,
                       COUNT(DISTINCT provider) providers,
                       MIN(captured_at) first_sample,
                       MAX(captured_at) last_sample,
                       ROUND(100.0*SUM(ok)/NULLIF(COUNT(*),0),1) healthy_percent,
                       SUM(CASE WHEN fallback_from IS NOT NULL THEN 1 ELSE 0 END) fallbacks
                FROM usage_samples {where}
                """,
                params,
            ).fetchone()
        )
        event_clauses, event_params = list(clauses), list(params)
        event_where = (
            f"WHERE {' AND '.join(event_clauses)}" if event_clauses else ""
        )
        events = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT * FROM usage_events {event_where}
                ORDER BY captured_at DESC LIMIT 200
                """,
                event_params,
            ).fetchall()
        ]
        event_counts = conn.execute(
            f"""
            SELECT COUNT(*) events,
                   SUM(CASE WHEN event_type='window_reanchored_early'
                            THEN 1 ELSE 0 END) early_reanchors
            FROM usage_events {event_where}
            """,
            event_params,
        ).fetchone()
        summary["events"] = event_counts["events"]
        summary["early_reanchors"] = event_counts["early_reanchors"] or 0
        return {
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "range": range_name,
            "provider": provider or "all",
            "summary": summary,
            "latest": _latest(conn),
            "series": _series(
                conn, since, BUCKET_SECONDS[range_name], provider
            ),
            "events": events,
            "database_bytes": db_path.stat().st_size,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backfill-routing", type=Path)
    args = parser.parse_args()
    if args.backfill_routing:
        print(
            json.dumps(
                {
                    "ok": True,
                    "inserted": backfill_routing(args.backfill_routing, args.db),
                    "db": str(args.db),
                }
            )
        )
    else:
        init_db(args.db)
        print(json.dumps({"ok": True, "db": str(args.db)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
