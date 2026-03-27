"""
Unit tests for correlation risk module — runs fully offline.
"""
import os
import sys

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.risk.correlation import (
    compute_correlation_matrix,
    check_portfolio_correlation,
)


class TestComputeCorrelationMatrix:
    def test_empty_returns_empty(self):
        result = compute_correlation_matrix({})
        assert result.empty

    def test_single_symbol_returns_empty(self):
        result = compute_correlation_matrix({"BTC": pd.Series(range(100))})
        assert result.empty

    def test_correlated_assets(self):
        np.random.seed(42)
        base = np.cumsum(np.random.randn(100))
        prices = {
            "BTC": pd.Series(base * 1.0 + 100),
            "ETH": pd.Series(base * 0.5 + 50),  # highly correlated
        }
        result = compute_correlation_matrix(prices, lookback=30)
        assert not result.empty
        assert result.loc["BTC", "ETH"] > 0.9

    def test_uncorrelated_assets(self):
        np.random.seed(42)
        prices = {
            "BTC": pd.Series(np.cumsum(np.random.randn(100)) + 100),
            "RAND": pd.Series(np.cumsum(np.random.randn(100)) + 100),
        }
        result = compute_correlation_matrix(prices, lookback=30)
        assert not result.empty
        corr = abs(result.loc["BTC", "RAND"])
        assert corr < 0.9  # should not be highly correlated


class TestCheckPortfolioCorrelation:
    def test_empty_matrix_allows(self):
        allowed, reason = check_portfolio_correlation("SOL", ["BTC", "ETH"], pd.DataFrame())
        assert allowed

    def test_allows_below_threshold(self):
        matrix = pd.DataFrame(
            [[1.0, 0.3, 0.2], [0.3, 1.0, 0.1], [0.2, 0.1, 1.0]],
            index=["BTC", "ETH", "SOL"],
            columns=["BTC", "ETH", "SOL"],
        )
        allowed, reason = check_portfolio_correlation("SOL", ["BTC", "ETH"], matrix, threshold=0.75)
        assert allowed

    def test_blocks_when_too_correlated(self):
        matrix = pd.DataFrame(
            [[1.0, 0.9, 0.85], [0.9, 1.0, 0.88], [0.85, 0.88, 1.0]],
            index=["BTC", "ETH", "SOL"],
            columns=["BTC", "ETH", "SOL"],
        )
        allowed, reason = check_portfolio_correlation(
            "SOL", ["BTC", "ETH"], matrix, threshold=0.75, max_correlated=2
        )
        assert not allowed
        assert "Correlation limit" in reason

    def test_symbol_not_in_matrix_allows(self):
        matrix = pd.DataFrame(
            [[1.0, 0.9], [0.9, 1.0]],
            index=["BTC", "ETH"],
            columns=["BTC", "ETH"],
        )
        allowed, reason = check_portfolio_correlation("DOGE", ["BTC"], matrix)
        assert allowed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
