import sqlite3
from datetime import datetime, timedelta, timezone

from bot.execution import trade_cooldown


def test_get_trade_cooldown_seconds_uses_explicit_minutes(monkeypatch):
    monkeypatch.setattr(trade_cooldown, "TRADE_COOLDOWN_MINUTES", 25)
    monkeypatch.setattr(trade_cooldown, "TRADE_COOLDOWN_CANDLES", 3)
    monkeypatch.setattr(trade_cooldown, "PRIMARY_TIMEFRAME", "1h")

    assert trade_cooldown.get_trade_cooldown_seconds() == 25 * 60


def test_get_trade_cooldown_seconds_uses_primary_timeframe_candles(monkeypatch):
    monkeypatch.setattr(trade_cooldown, "TRADE_COOLDOWN_MINUTES", 0)
    monkeypatch.setattr(trade_cooldown, "TRADE_COOLDOWN_CANDLES", 3)
    monkeypatch.setattr(trade_cooldown, "PRIMARY_TIMEFRAME", "1h")

    assert trade_cooldown.get_trade_cooldown_seconds() == 3 * 60 * 60


def test_cooldown_remaining_seconds_from_recent_close(tmp_path, monkeypatch):
    db_path = tmp_path / "cooldown_test.db"
    monkeypatch.setattr(trade_cooldown, "DB_PATH", str(db_path))
    monkeypatch.setattr(trade_cooldown, "TRADE_COOLDOWN_MINUTES", 30)
    monkeypatch.setattr(trade_cooldown, "TRADE_COOLDOWN_CANDLES", 0)
    monkeypatch.setattr(trade_cooldown, "PRIMARY_TIMEFRAME", "1h")

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE paper_trades (
                id TEXT PRIMARY KEY,
                symbol TEXT,
                close_time TEXT
            )
            """
        )
        closed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        conn.execute(
            "INSERT INTO paper_trades (id, symbol, close_time) VALUES (?, ?, ?)",
            ("t1", "BTC/USDT", closed_at.isoformat()),
        )

    remaining = trade_cooldown.cooldown_remaining_seconds("BTC/USDT")
    assert 19 * 60 <= remaining <= 20 * 60
