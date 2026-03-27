"""
Paper Trade Engine — unit tests (Suggestion #3).
Tests: open position validation, TP/SL lifecycle, time exit, slippage/fee
simulation, stats, reconciliation, and edge cases.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

# Minimal env so config doesn't fail on import
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.execution.paper_trade import PaperTradeEngine
from bot.config import MIN_ORDER_NOTIONAL_USDT, MAX_ORDER_NOTIONAL_USDT


def _make_signal(symbol="TEST/USDT", direction="long", price=100.0, score=80):
    return {
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "score": score,
    }


def _make_risk(sl=95.0, tp1=105.0, tp2=110.0, qty=1.0):
    return {"sl": sl, "tp1": tp1, "tp2": tp2, "qty": qty}


class TestOpenPosition:
    def test_opens_valid_long(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        result = engine.open_position(_make_signal(), _make_risk())
        assert result, "Should open position"
        assert "TEST/USDT" in engine.positions
        assert engine.positions["TEST/USDT"]["direction"] == "long"

    def test_opens_valid_short(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        signal = _make_signal(direction="short", price=100.0)
        risk = _make_risk(sl=105.0, tp1=95.0, tp2=90.0)
        result = engine.open_position(signal, risk)
        assert result
        assert engine.positions["TEST/USDT"]["direction"] == "short"

    def test_rejects_zero_qty(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        result = engine.open_position(_make_signal(), _make_risk(qty=0.0))
        assert not result
        assert engine.last_rejection_reason

    def test_rejects_duplicate_symbol(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        engine.open_position(_make_signal(), _make_risk())
        result = engine.open_position(_make_signal(), _make_risk())
        assert not result
        assert "already have open position" in engine.last_rejection_reason

    def test_rejects_invalid_direction(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        signal = _make_signal(direction="sideways")
        result = engine.open_position(signal, _make_risk())
        assert not result

    def test_rejects_below_min_notional(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        tiny_qty = (MIN_ORDER_NOTIONAL_USDT / 100.0) * 0.001
        result = engine.open_position(_make_signal(), _make_risk(qty=tiny_qty))
        assert not result

    def test_rejects_above_max_notional(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        huge_qty = (MAX_ORDER_NOTIONAL_USDT / 100.0) * 2.0
        result = engine.open_position(_make_signal(), _make_risk(qty=huge_qty))
        assert not result

    def test_rejects_invalid_long_layout(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        # SL above entry is invalid for long
        result = engine.open_position(
            _make_signal(price=100.0),
            _make_risk(sl=105.0, tp1=110.0, tp2=115.0),
        )
        assert not result

    def test_rejects_invalid_short_layout(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        # SL below entry is invalid for short
        result = engine.open_position(
            _make_signal(direction="short", price=100.0),
            _make_risk(sl=95.0, tp1=90.0, tp2=85.0),
        )
        assert not result


class TestUpdatePositions:
    def test_tp1_partial_close(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        engine.open_position(_make_signal(price=100.0), _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0))
        closed = engine.update_positions({"TEST/USDT": 110.0})
        assert len(closed) == 0  # TP1 is partial, not a full close
        assert engine.positions["TEST/USDT"]["tp1_hit"] == True

    def test_tp2_full_close(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        engine.open_position(_make_signal(price=100.0), _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0))
        # Hit TP1 first
        engine.update_positions({"TEST/USDT": 110.0})
        # Now hit TP2
        closed = engine.update_positions({"TEST/USDT": 120.0})
        assert len(closed) == 1
        assert closed[0]["pnl"] > 0
        assert "TEST/USDT" not in engine.positions

    def test_stop_loss_closes_position(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        engine.open_position(_make_signal(price=100.0), _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0))
        closed = engine.update_positions({"TEST/USDT": 94.0})
        assert len(closed) == 1
        assert closed[0]["pnl"] < 0
        assert "TEST/USDT" not in engine.positions

    def test_short_tp_hit(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        signal = _make_signal(direction="short", price=100.0)
        risk = _make_risk(sl=105.0, tp1=95.0, tp2=90.0, qty=1.0)
        engine.open_position(signal, risk)
        # Hit TP1
        engine.update_positions({"TEST/USDT": 95.0})
        assert engine.positions["TEST/USDT"]["tp1_hit"] == True
        # Hit TP2
        closed = engine.update_positions({"TEST/USDT": 90.0})
        assert len(closed) == 1
        assert closed[0]["pnl"] > 0

    def test_short_stop_loss(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        signal = _make_signal(direction="short", price=100.0)
        risk = _make_risk(sl=105.0, tp1=95.0, tp2=90.0, qty=1.0)
        engine.open_position(signal, risk)
        closed = engine.update_positions({"TEST/USDT": 106.0})
        assert len(closed) == 1
        assert closed[0]["pnl"] < 0

    def test_trailing_stop_ratchets(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        engine.open_position(_make_signal(price=100.0), _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0))
        initial_sl = engine.positions["TEST/USDT"]["sl"]
        # Price moves up significantly
        engine.update_positions({"TEST/USDT": 115.0})
        new_sl = engine.positions["TEST/USDT"]["sl"]
        assert new_sl >= initial_sl, "Trailing SL should ratchet up"

    def test_no_close_on_missing_price(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        engine.open_position(_make_signal(price=100.0), _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0))
        closed = engine.update_positions({})  # No price for TEST/USDT
        assert len(closed) == 0
        assert "TEST/USDT" in engine.positions


class TestTimeBasedExit:
    def test_position_closed_after_5_days(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        engine.open_position(_make_signal(price=100.0), _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0))
        # Backdate the opened_at to 6 days ago
        engine.positions["TEST/USDT"]["opened_at"] = (
            datetime.now(timezone.utc) - timedelta(days=6)
        ).isoformat()
        closed = engine.update_positions({"TEST/USDT": 102.0})
        assert len(closed) == 1
        assert closed[0]["status"] == "closed_time"


class TestSlippageAndFees:
    def test_entry_slippage_applied(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        result = engine.open_position(
            _make_signal(price=100.0),
            _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0),
        )
        assert result
        pos = engine.positions["TEST/USDT"]
        # Long entry should have positive slippage (worse fill)
        assert pos["entry"] >= 100.0

    def test_net_pnl_includes_fees(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        gross = engine._calc_pnl(100.0, 110.0, 1.0, "long")
        net = engine._net_pnl(100.0, 110.0, 1.0, "long")
        assert net < gross, "Net PnL should be less than gross due to fees"


class TestCapitalTracking:
    def test_capital_changes_on_close(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        engine.open_position(_make_signal(price=100.0), _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0))
        initial_cap = engine.capital
        engine.update_positions({"TEST/USDT": 94.0})  # SL hit
        assert engine.capital < initial_cap


class TestStats:
    def test_empty_stats(self):
        engine = PaperTradeEngine(starting_capital=1000.0)
        stats = engine.get_stats()
        assert stats["total"] == 0
        assert stats["capital"] == 1000.0
        assert "profit_factor" in stats
        assert "sharpe_ratio" in stats
        assert "max_consecutive_losses" in stats
        assert "avg_holding_hours" in stats

    def test_stats_after_trades(self):
        engine = PaperTradeEngine(starting_capital=10000.0)
        # Open and close via TP2
        engine.open_position(_make_signal(price=100.0), _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0))
        engine.update_positions({"TEST/USDT": 110.0})  # TP1
        engine.update_positions({"TEST/USDT": 120.0})  # TP2
        stats = engine.get_stats()
        assert stats["total"] >= 1
        assert stats["wins"] >= 1


class TestDBPersistence:
    def test_position_survives_restart(self):
        engine1 = PaperTradeEngine(starting_capital=10000.0)
        engine1.open_position(
            _make_signal(symbol="RESTART/USDT", price=100.0),
            _make_risk(sl=95.0, tp1=110.0, tp2=120.0, qty=1.0),
        )
        assert "RESTART/USDT" in engine1.positions

        # Create a new engine instance (simulates restart)
        engine2 = PaperTradeEngine(starting_capital=10000.0)
        assert "RESTART/USDT" in engine2.positions, "Position should be restored from DB"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
