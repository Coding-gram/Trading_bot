"""
Integration test: signal → execution pipeline.
Tests the full path: synthetic data → score_signal → risk params → paper engine execution.
"""
import os
import sys
import tempfile

# Minimal env so config doesn't fail on import
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
# Use a temp DB so tests don't corrupt production data
_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = _test_db.name

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.analysis import indicators
from bot.strategy.scorer import score_signal
from bot.risk.risk_manager import (
    calculate_position_size,
    choose_stop_loss,
    calculate_take_profits,
    DailyLossGuard,
)
from bot.execution.paper_trade import PaperTradeEngine
from bot.config import MAX_ORDER_NOTIONAL_USDT


def _make_strong_uptrend_df(n=300, seed=42):
    """Create synthetic data with a strong uptrend that should generate a buy signal."""
    np.random.seed(seed)
    base = 100.0
    # Strong monotonic uptrend
    price = pd.Series(np.cumsum(np.abs(np.random.randn(n)) * 0.8) + base)
    # Add increasing volume to confirm trend
    volume = np.linspace(3000, 8000, n) + np.random.randint(0, 1000, n)

    df = pd.DataFrame({
        "open": price * 0.999,
        "high": price * 1.005,
        "low": price * 0.995,
        "close": price,
        "volume": volume.astype(float),
    })
    df.index = pd.date_range("2024-01-01", periods=n, freq="1h")
    return indicators.compute_all(df)


class TestSignalToPaperExecution:
    """End-to-end test verifying the full signal → risk → execution pipeline."""

    def test_score_generates_signal_and_paper_engine_opens_position(self):
        """Verify: data → score → risk params → paper trade → position opened."""
        # 1. Generate signal from scoring engine
        df_1h = _make_strong_uptrend_df(300, seed=42)
        df_4h = _make_strong_uptrend_df(100, seed=99)

        signal = score_signal(df_1h, df_4h, "TEST/USDT")

        # Signal should detect the trend
        assert signal["direction"] in ("long", "short"), f"Expected directional signal, got: {signal['direction']}"
        assert signal["score"] >= 0, f"Score should be non-negative, got: {signal['score']}"

        # 2. Calculate risk parameters (even if score doesn't hit threshold, test the pipeline)
        entry_price = float(df_1h["close"].iloc[-1])
        atr_val = float(df_1h["atr"].iloc[-1]) if "atr" in df_1h.columns else entry_price * 0.02

        stop_loss = choose_stop_loss(entry_price, atr_val, signal["direction"], signal.get("sr_levels"))
        take_profits = calculate_take_profits(entry_price, stop_loss, signal["direction"])

        # Validate risk params are sensible
        if signal["direction"] == "long":
            assert stop_loss < entry_price, "Long SL should be below entry"
            assert take_profits["tp1"] > entry_price, "Long TP1 should be above entry"
            assert take_profits["tp2"] > take_profits["tp1"], "TP2 should be above TP1"
        elif signal["direction"] == "short":
            assert stop_loss > entry_price, "Short SL should be above entry"
            assert take_profits["tp1"] < entry_price, "Short TP1 should be below entry"

        # 3. Position sizing
        capital = 1000.0
        sizing = calculate_position_size(capital, entry_price, stop_loss)
        assert sizing["qty"] > 0, "Should have non-zero quantity"
        assert sizing["usdt_value"] > 0, "Should have positive USD value"
        assert sizing["usdt_value"] <= capital * 0.25, "Should not exceed capital cap"

        # Keep notional safely below max guard because paper engine applies entry slippage.
        max_safe_qty = (float(MAX_ORDER_NOTIONAL_USDT) / entry_price) * 0.99 if entry_price > 0 else 0.0
        sizing["qty"] = min(float(sizing["qty"]), max_safe_qty)

        # 4. Execute on paper engine
        engine = PaperTradeEngine(starting_capital=capital)

        trade_result = engine.open_position(
            {
                "symbol": "TEST/USDT",
                "direction": signal["direction"],
                "score": signal["score"],
                "price": entry_price,
            },
            {
                "sl": stop_loss,
                "tp1": take_profits["tp1"],
                "tp2": take_profits["tp2"],
                "qty": sizing["qty"],
            },
        )

        # Verify position is open
        assert trade_result is not None, "Paper trade should succeed"
        assert "TEST/USDT" in engine.positions, "Position should be tracked"

        pos = engine.positions["TEST/USDT"]
        assert pos["direction"] == signal["direction"]
        assert pos["sl"] > 0
        assert pos["tp1"] > 0
        assert pos["tp2"] > 0

    def test_daily_loss_guard_blocks_after_loss_limit(self):
        """Verify DailyLossGuard integrates properly with execution pipeline."""
        guard = DailyLossGuard(starting_capital=1000.0)

        # Record enough losses to trigger the guard
        guard.record_trade(-30.0)
        guard.record_trade(-35.0)

        allowed, reason = guard.can_trade(935.0, 0.06, 0.15)
        assert not allowed, "Guard should block trading after exceeding daily loss limit"
        assert "Daily loss" in reason

    def test_paper_engine_stats_include_enhanced_fields(self):
        """Verify get_stats() returns the new enhanced fields."""
        engine = PaperTradeEngine(starting_capital=1000.0)
        stats = engine.get_stats()

        assert "best_trade" in stats
        assert "worst_trade" in stats
        assert "avg_rr" in stats
        assert "open_positions" in stats
        assert stats["open_positions"] == len(engine.positions)
        # New enhanced metrics
        assert "profit_factor" in stats
        assert "sharpe_ratio" in stats
        assert "max_consecutive_losses" in stats
        assert "avg_holding_hours" in stats


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
