"""
In-memory runtime telemetry for operational observability.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import sqlite3
from threading import Lock
from bot.config import DB_PATH


_LOCK = Lock()
_COUNTERS = defaultdict(int)
_LAST_EVENT = {}


def _db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _init_db() -> None:
    try:
        with _db_connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry_counters (
                    metric TEXT PRIMARY KEY,
                    value INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric TEXT NOT NULL,
                    value_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
    except Exception:
        return


def _load_from_db() -> None:
    try:
        with _db_connect() as conn:
            rows = conn.execute("SELECT metric, value, updated_at FROM telemetry_counters").fetchall()
            for metric, value, updated_at in rows:
                _COUNTERS[metric] = int(value or 0)
                _LAST_EVENT[metric] = updated_at
            event_rows = conn.execute(
                """
                SELECT metric, value_json, created_at
                FROM telemetry_events
                WHERE id IN (
                    SELECT MAX(id)
                    FROM telemetry_events
                    GROUP BY metric
                )
                """
            ).fetchall()
            for metric, value_json, created_at in event_rows:
                parsed = None
                if value_json is not None:
                    try:
                        parsed = json.loads(value_json)
                    except Exception:
                        parsed = value_json
                _LAST_EVENT[metric] = {
                    "at": created_at,
                    "value": parsed,
                }
    except Exception:
        return


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def increment(metric: str, amount: int = 1):
    now = _now_iso()
    with _LOCK:
        _COUNTERS[metric] += amount
        _LAST_EVENT[metric] = now
        value = int(_COUNTERS[metric])

    try:
        with _db_connect() as conn:
            conn.execute(
                """
                INSERT INTO telemetry_counters(metric, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(metric) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (metric, value, now),
            )
    except Exception:
        return


def record_event(metric: str, value):
    now = _now_iso()
    with _LOCK:
        _LAST_EVENT[metric] = {
            "at": now,
            "value": value,
        }

    try:
        with _db_connect() as conn:
            conn.execute(
                "INSERT INTO telemetry_events(metric, value_json, created_at) VALUES (?, ?, ?)",
                (metric, json.dumps(value), now),
            )
    except Exception:
        return


def snapshot() -> dict:
    with _LOCK:
        counters = dict(_COUNTERS)
        last_event = dict(_LAST_EVENT)

    # Derived metrics with divide-by-zero protection
    scans = counters.get("scans_total", 0)
    symbols = counters.get("symbols_scanned", 0)
    alerts = counters.get("alerts_sent", 0)
    orders_opened = counters.get("orders_opened", 0)
    order_rejections = counters.get("order_rejections", 0)
    api_errors = counters.get("api_errors", 0)

    return {
        "counters": counters,
        "last_event": last_event,
        "derived": {
            "avg_symbols_per_scan": round(symbols / scans, 2) if scans else 0.0,
            "alert_rate_pct": round(alerts / symbols * 100, 2) if symbols else 0.0,
            "order_reject_rate_pct": round(order_rejections / (orders_opened + order_rejections) * 100, 2)
            if (orders_opened + order_rejections) else 0.0,
            "api_error_rate_pct": round(api_errors / scans * 100, 2) if scans else 0.0,
        },
    }


_init_db()
_load_from_db()
