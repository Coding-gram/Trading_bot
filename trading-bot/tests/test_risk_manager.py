"""
Unit tests for risk_manager.py — runs fully offline.
"""
import os
import sys

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.risk.risk_manager import (
    calculate_position_size,
    TrailingStopLoss,
    DailyLossGuard,
    portfolio_heat_ok,
    atr_stop_loss,
    choose_stop_loss,
    calculate_take_profits,
)


class TestPositionSizing:
    def test_zero_sl_distance(self):
        result = calculate_position_size(1000.0, 50000.0, 50000.0)
        assert result["qty"] == 0.0
        assert result["usdt_risk"] == 0.0

    def test_normal_sizing(self):
        result = calculate_position_size(1000.0, 100.0, 95.0)
        assert result["qty"] > 0
        assert result["usdt_value"] > 0
        assert result["risk_pct"] > 0

    def test_consecutive_losses_reduce_risk(self):
        normal = calculate_position_size(1000.0, 100.0, 95.0)
        throttled = calculate_position_size(1000.0, 100.0, 95.0, consecutive_losses=3)
        assert throttled["qty"] < normal["qty"]

    def test_capital_cap(self):
        result = calculate_position_size(1000.0, 100.0, 99.99)
        max_value = 1000.0 * 0.20  # MAX_CAPITAL_PER_TRADE_PCT
        assert result["usdt_value"] <= max_value + 1  # small rounding tolerance


class TestTrailingStopLoss:
    def test_long_trailing_moves_up(self):
        tsl = TrailingStopLoss(entry=100.0, initial_sl=95.0, direction="long", atr=3.0)
        assert tsl.current_sl == 95.0

        tsl.update(105.0)
        assert tsl.current_sl > 95.0

    def test_long_trailing_never_moves_down(self):
        tsl = TrailingStopLoss(entry=100.0, initial_sl=95.0, direction="long", atr=3.0)
        tsl.update(110.0)
        high_sl = tsl.current_sl
        tsl.update(105.0)
        assert tsl.current_sl == high_sl  # never went down

    def test_short_trailing_moves_down(self):
        tsl = TrailingStopLoss(entry=100.0, initial_sl=105.0, direction="short", atr=3.0)
        tsl.update(95.0)
        assert tsl.current_sl < 105.0

    def test_short_trailing_never_moves_up(self):
        tsl = TrailingStopLoss(entry=100.0, initial_sl=105.0, direction="short", atr=3.0)
        tsl.update(90.0)
        low_sl = tsl.current_sl
        tsl.update(95.0)
        assert tsl.current_sl == low_sl

    def test_atr_based_offset(self):
        tsl = TrailingStopLoss(entry=100.0, initial_sl=95.0, direction="long", atr=2.0)
        tsl.update(110.0)
        expected_sl = 110.0 - 2.0 * 1.5
        assert abs(tsl.current_sl - expected_sl) < 0.01

    def test_fallback_when_no_atr(self):
        tsl = TrailingStopLoss(entry=100.0, initial_sl=95.0, direction="long", atr=0.0)
        tsl.update(110.0)
        # Uses DEFAULT_TRAIL_PCT = 0.015 → 110 * 0.015 = 1.65
        expected_sl = 110.0 - 110.0 * 0.015
        assert abs(tsl.current_sl - expected_sl) < 0.01

    def test_trigger_detection(self):
        tsl = TrailingStopLoss(entry=100.0, initial_sl=95.0, direction="long", atr=2.0)
        assert not tsl.is_triggered(100.0)
        assert tsl.is_triggered(94.0)


class TestDailyLossGuard:
    def test_initial_state_allows_trading(self):
        guard = DailyLossGuard(starting_capital=1000.0)
        allowed, reason = guard.can_trade(1000.0, 0.06, 0.15)
        assert allowed

    def test_daily_loss_halts_trading(self):
        guard = DailyLossGuard(starting_capital=1000.0)
        guard.record_trade(-60.0)  # 6% loss
        allowed, reason = guard.can_trade(940.0, 0.06, 0.15)
        assert not allowed
        assert "Daily loss" in reason

    def test_drawdown_halts_trading(self):
        guard = DailyLossGuard(starting_capital=1000.0)
        guard.peak_capital = 1200.0
        allowed, reason = guard.can_trade(1000.0, 0.06, 0.15)
        assert not allowed
        assert "drawdown" in reason

    def test_daily_reset(self):
        guard = DailyLossGuard(starting_capital=1000.0)
        guard.record_trade(-50.0)
        guard.reset_daily(950.0)
        assert guard.realized_pnl_day == 0.0
        assert guard.daily_start == 950.0

    def test_consecutive_losses_tracked(self):
        guard = DailyLossGuard(starting_capital=1000.0)
        guard.record_trade(-10.0)
        guard.record_trade(-10.0)
        guard.record_trade(-10.0)
        assert guard.consecutive_losses == 3
        guard.record_trade(5.0)
        assert guard.consecutive_losses == 0


class TestPortfolioHeat:
    def test_empty_positions_allowed(self):
        allowed, heat = portfolio_heat_ok({}, 1000.0)
        assert allowed
        assert heat == 0.0

    def test_high_heat_blocked(self):
        positions = {
            "BTC/USDT": {"entry_price": 100, "stop_loss": 90, "qty": 10},  # risk = 100
            "ETH/USDT": {"entry_price": 50, "stop_loss": 45, "qty": 10},   # risk = 50
        }
        # Total risk = 150 on capital of 1000 = 15% > 6%
        allowed, heat = portfolio_heat_ok(positions, 1000.0)
        assert not allowed

    def test_low_heat_allowed(self):
        positions = {
            "BTC/USDT": {"entry_price": 100, "stop_loss": 99, "qty": 1},  # risk = 1
        }
        # Total risk = 1 on capital of 1000 = 0.1%
        allowed, heat = portfolio_heat_ok(positions, 1000.0)
        assert allowed

    def test_zero_capital(self):
        allowed, heat = portfolio_heat_ok({}, 0.0)
        assert not allowed


class TestATRStopLoss:
    def test_long_sl_below_entry(self):
        sl = atr_stop_loss(100.0, 5.0, "long")
        assert sl < 100.0

    def test_short_sl_above_entry(self):
        sl = atr_stop_loss(100.0, 5.0, "short")
        assert sl > 100.0


class TestTakeProfits:
    def test_long_tps_above_entry(self):
        tp = calculate_take_profits(100.0, 95.0, "long")
        assert tp["tp1"] > 100.0
        assert tp["tp2"] > tp["tp1"]

    def test_short_tps_below_entry(self):
        tp = calculate_take_profits(100.0, 105.0, "short")
        assert tp["tp1"] < 100.0
        assert tp["tp2"] < tp["tp1"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
