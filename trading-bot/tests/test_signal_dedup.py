"""
Unit tests for signal dedup logic — runs fully offline.
"""
import os
import sys
import time

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.signal_worker import should_emit_signal, _LAST_EMITTED_SIGNAL_BY_SYMBOL


@pytest.fixture(autouse=True)
def clear_dedup_cache():
    _LAST_EMITTED_SIGNAL_BY_SYMBOL.clear()
    yield
    _LAST_EMITTED_SIGNAL_BY_SYMBOL.clear()


class TestShouldEmitSignal:
    def test_first_signal_always_emitted(self):
        signal = {"symbol": "BTC/USDT", "direction": "long", "score": 75, "price": 50000.0}
        assert should_emit_signal(signal) is True

    def test_duplicate_within_cooldown_suppressed(self):
        signal = {"symbol": "BTC/USDT", "direction": "long", "score": 75, "price": 50000.0}
        should_emit_signal(signal)
        assert should_emit_signal(signal) is False

    def test_different_symbol_not_suppressed(self):
        sig1 = {"symbol": "BTC/USDT", "direction": "long", "score": 75, "price": 50000.0}
        sig2 = {"symbol": "ETH/USDT", "direction": "long", "score": 75, "price": 3000.0}
        should_emit_signal(sig1)
        assert should_emit_signal(sig2) is True

    def test_different_direction_emitted(self):
        sig1 = {"symbol": "BTC/USDT", "direction": "long", "score": 75, "price": 50000.0}
        sig2 = {"symbol": "BTC/USDT", "direction": "short", "score": 75, "price": 50000.0}
        should_emit_signal(sig1)
        assert should_emit_signal(sig2) is True

    def test_large_score_change_emitted(self):
        sig1 = {"symbol": "BTC/USDT", "direction": "long", "score": 75, "price": 50000.0}
        sig2 = {"symbol": "BTC/USDT", "direction": "long", "score": 85, "price": 50000.0}
        should_emit_signal(sig1)
        assert should_emit_signal(sig2) is True

    def test_large_price_move_emitted(self):
        sig1 = {"symbol": "BTC/USDT", "direction": "long", "score": 75, "price": 50000.0}
        sig2 = {"symbol": "BTC/USDT", "direction": "long", "score": 75, "price": 50500.0}  # 1% move
        should_emit_signal(sig1)
        assert should_emit_signal(sig2) is True

    def test_neutral_direction_always_passes(self):
        signal = {"symbol": "BTC/USDT", "direction": "neutral", "score": 50, "price": 50000.0}
        assert should_emit_signal(signal) is True
        assert should_emit_signal(signal) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
