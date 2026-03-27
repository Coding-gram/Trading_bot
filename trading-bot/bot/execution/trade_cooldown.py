import re
import sqlite3
from datetime import datetime, timezone

from bot.config import (
    DB_PATH,
    PRIMARY_TIMEFRAME,
    TRADE_COOLDOWN_CANDLES,
    TRADE_COOLDOWN_MINUTES,
)


_TIMEFRAME_RE = re.compile(r"^\s*(\d+)\s*([mhdwM])\s*$")


def _db_connect(row_factory=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


def _timeframe_to_minutes(timeframe: str) -> int:
    value = str(timeframe or "").strip()
    match = _TIMEFRAME_RE.match(value)
    if not match:
        return 0

    amount = int(match.group(1))
    unit = match.group(2)

    if amount <= 0:
        return 0
    if unit == "m":
        return amount
    if unit == "h":
        return amount * 60
    if unit == "d":
        return amount * 24 * 60
    if unit == "w":
        return amount * 7 * 24 * 60
    if unit == "M":
        return amount * 30 * 24 * 60
    return 0


def get_trade_cooldown_seconds() -> int:
    minutes = int(max(TRADE_COOLDOWN_MINUTES, 0))
    if minutes <= 0:
        tf_minutes = _timeframe_to_minutes(PRIMARY_TIMEFRAME)
        candles = int(max(TRADE_COOLDOWN_CANDLES, 0))
        minutes = tf_minutes * candles
    return int(max(minutes, 0) * 60)


def _latest_closed_trade_time(symbol: str) -> datetime | None:
    symbol_txt = str(symbol or "").strip()
    if not symbol_txt:
        return None

    try:
        with _db_connect(sqlite3.Row) as conn:
            has_paper = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades' LIMIT 1"
            ).fetchone() is not None
            has_live = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_trades' LIMIT 1"
            ).fetchone() is not None

            if has_paper and has_live:
                row = conn.execute(
                    """
                    SELECT close_time
                    FROM (
                        SELECT close_time FROM paper_trades
                        WHERE symbol=? AND close_time IS NOT NULL
                        UNION ALL
                        SELECT close_time FROM live_trades
                        WHERE symbol=? AND close_time IS NOT NULL
                    )
                    ORDER BY close_time DESC
                    LIMIT 1
                    """,
                    (symbol_txt, symbol_txt),
                ).fetchone()
            elif has_paper:
                row = conn.execute(
                    """
                    SELECT close_time
                    FROM paper_trades
                    WHERE symbol=? AND close_time IS NOT NULL
                    ORDER BY close_time DESC
                    LIMIT 1
                    """,
                    (symbol_txt,),
                ).fetchone()
            elif has_live:
                row = conn.execute(
                    """
                    SELECT close_time
                    FROM live_trades
                    WHERE symbol=? AND close_time IS NOT NULL
                    ORDER BY close_time DESC
                    LIMIT 1
                    """,
                    (symbol_txt,),
                ).fetchone()
            else:
                row = None
    except Exception:
        return None

    if not row:
        return None

    close_time = str(row["close_time"] or "").strip()
    if not close_time:
        return None

    try:
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def cooldown_remaining_seconds(symbol: str, now: datetime | None = None) -> int:
    cooldown_seconds = get_trade_cooldown_seconds()
    if cooldown_seconds <= 0:
        return 0

    last_closed = _latest_closed_trade_time(symbol)
    if last_closed is None:
        return 0

    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    elapsed = (current - last_closed).total_seconds()
    remaining = int(cooldown_seconds - elapsed)
    return max(remaining, 0)
