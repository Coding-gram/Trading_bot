"""Tests for Monte Carlo robustness utilities."""

import os
import sys

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from robustness_check import monte_carlo_from_trades


def test_monte_carlo_all_wins_has_no_loss_or_ruin():
    trades = [5.0] * 20
    result = monte_carlo_from_trades(trades, starting_capital=1000.0, paths=500, seed=1)

    assert result["p_loss"] == 0.0
    assert result["p_ruin_10pct"] == 0.0
    assert result["p_drawdown_20pct"] == 0.0
    assert result["median_final_capital"] > 1000.0


def test_monte_carlo_empty_trades_is_safe_default():
    result = monte_carlo_from_trades([], starting_capital=1000.0, paths=300, seed=1)

    assert result["trades_per_path"] == 0
    assert result["median_final_capital"] == 1000.0
    assert result["p_loss"] == 0.0
