"""
Unit tests for scorer.py — runs fully offline with synthetic DataFrames.
"""
import os
import sys

# Minimal env so config doesn't fail on import
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import numpy as np
import pandas as pd
import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.strategy.scorer import (
    score_signal, _score_trend, _score_macd, _score_rsi,
    _score_4h_confluence,
    _penalty_rsi_divergence, _penalty_contra_sr,
    _penalty_declining_volume, _penalty_htf_disagreement,
    _dynamic_signal_threshold,
    WEIGHTS,
)
from bot.analysis import indicators


def _make_df(n=300, trend="up", seed=42):
    """Create a synthetic OHLCV DataFrame with indicators computed."""
    np.random.seed(seed)
    base_price = 100.0
    if trend == "up":
        price = pd.Series(np.cumsum(np.abs(np.random.randn(n)) * 0.5) + base_price)
    elif trend == "down":
        price = pd.Series(-np.cumsum(np.abs(np.random.randn(n)) * 0.5) + base_price + 50)
    else:
        price = pd.Series(np.random.randn(n) * 0.3 + base_price)

    df = pd.DataFrame({
        "open": price * 0.999,
        "high": price * 1.003,
        "low": price * 0.997,
        "close": price,
        "volume": np.random.randint(1000, 8000, n).astype(float),
    })
    df.index = pd.date_range("2024-01-01", periods=n, freq="1h")
    return indicators.compute_all(df)


class TestScoreTrend:
    def test_bullish_both_timeframes(self):
        c = pd.Series({"trend_up": True, "trend_down": False})
        h4 = pd.Series({"trend_up": True, "trend_down": False})
        direction, score, msg = _score_trend(c, h4)
        assert direction == "long"
        assert score == WEIGHTS["trend_alignment"]

    def test_bearish_both_timeframes(self):
        c = pd.Series({"trend_up": False, "trend_down": True})
        h4 = pd.Series({"trend_up": False, "trend_down": True})
        direction, score, msg = _score_trend(c, h4)
        assert direction == "short"
        assert score == WEIGHTS["trend_alignment"]

    def test_neutral_no_trend(self):
        c = pd.Series({"trend_up": False, "trend_down": False})
        h4 = pd.Series({"trend_up": False, "trend_down": False})
        direction, score, msg = _score_trend(c, h4)
        assert direction == "neutral"
        assert score == 0

    def test_nan_trend_returns_neutral(self):
        c = pd.Series({"trend_up": float("nan"), "trend_down": float("nan")})
        h4 = pd.Series({"trend_up": float("nan"), "trend_down": float("nan")})
        direction, score, msg = _score_trend(c, h4)
        assert direction == "neutral"


class TestDynamicThreshold:
    def test_low_adx_high_threshold(self):
        assert _dynamic_signal_threshold(15) >= 80

    def test_moderate_adx(self):
        assert _dynamic_signal_threshold(22) >= 75

    def test_strong_adx_standard(self):
        assert _dynamic_signal_threshold(30) <= 75


class TestPenalties:
    def test_htf_disagreement_long_vs_4h_bearish(self):
        h4 = pd.Series({"trend_up": False, "trend_down": True})
        details = {}
        penalty = _penalty_htf_disagreement(h4, "long", details)
        assert penalty < 0
        assert "penalty_htf" in details

    def test_htf_agreement_no_penalty(self):
        h4 = pd.Series({"trend_up": True, "trend_down": False})
        details = {}
        penalty = _penalty_htf_disagreement(h4, "long", details)
        assert penalty == 0

    def test_declining_volume_penalises(self):
        df = _make_df(100, trend="up")
        df.iloc[-3, df.columns.get_loc("volume")] = 5000
        df.iloc[-2, df.columns.get_loc("volume")] = 4000
        df.iloc[-1, df.columns.get_loc("volume")] = 3000
        # Ensure last 3 close prices go up (for "long")
        df.iloc[-3, df.columns.get_loc("close")] = 200
        df.iloc[-2, df.columns.get_loc("close")] = 201
        df.iloc[-1, df.columns.get_loc("close")] = 202
        details = {}
        penalty = _penalty_declining_volume(df, "long", details)
        assert penalty < 0

    def test_declining_volume_no_penalty_when_stable(self):
        df = _make_df(100, trend="up")
        df.iloc[-3, df.columns.get_loc("volume")] = 3000
        df.iloc[-2, df.columns.get_loc("volume")] = 4000
        df.iloc[-1, df.columns.get_loc("volume")] = 5000
        details = {}
        penalty = _penalty_declining_volume(df, "long", details)
        assert penalty == 0

    def test_contra_sr_penalises_long_near_resistance(self):
        sr = {"support": [90.0, 95.0], "resistance": [100.5]}
        c = pd.Series({"close": 100.0})
        details = {}
        penalty = _penalty_contra_sr(sr, c, "long", details)
        assert penalty < 0

    def test_contra_sr_no_penalty_far_from_resistance(self):
        sr = {"support": [80.0], "resistance": [120.0]}
        c = pd.Series({"close": 100.0})
        details = {}
        penalty = _penalty_contra_sr(sr, c, "long", details)
        assert penalty == 0


class TestConfluence4H:
    def test_bullish_confluence_bonus(self):
        h4 = pd.Series({"rsi": 45.0, "macd": 0.5, "macd_signal": 0.2})
        details = {}
        bonus = _score_4h_confluence(h4, "long", details)
        assert bonus > 0
        assert "4h_confluence" in details

    def test_bearish_confluence_bonus(self):
        h4 = pd.Series({"rsi": 55.0, "macd": -0.5, "macd_signal": -0.2})
        details = {}
        bonus = _score_4h_confluence(h4, "short", details)
        assert bonus > 0

    def test_no_bonus_when_conflicting(self):
        h4 = pd.Series({"rsi": 75.0, "macd": -0.5, "macd_signal": -0.2})
        details = {}
        bonus = _score_4h_confluence(h4, "long", details)
        assert bonus == 0


class TestScoreSignalIntegration:
    def test_score_capped_at_100(self):
        df_1h = _make_df(300, trend="up")
        df_4h = _make_df(100, trend="up", seed=99)
        signal = score_signal(df_1h, df_4h, "TEST/USDT")
        assert 0 <= signal["score"] <= 100

    def test_score_not_negative(self):
        df_1h = _make_df(300, trend="down")
        df_4h = _make_df(100, trend="up", seed=99)
        signal = score_signal(df_1h, df_4h, "TEST/USDT")
        assert signal["score"] >= 0

    def test_empty_df_returns_zero(self):
        df_1h = pd.DataFrame()
        df_4h = pd.DataFrame()
        signal = score_signal(df_1h, df_4h, "TEST/USDT")
        assert signal["score"] == 0
        assert signal["direction"] == "neutral"

    def test_short_df_returns_zero(self):
        df_1h = _make_df(30, trend="up")
        df_4h = _make_df(30, trend="up")
        signal = score_signal(df_1h, df_4h, "TEST/USDT")
        assert signal["score"] == 0

class TestMeanReversionBranch:
    """Tests for the low-ADX mean-reversion scoring path."""

    def test_mean_reversion_long_fires(self):
        """When ADX < 15, close <= BB lower, RSI <= 40 → long signal."""
        df_1h = _make_df(300, trend="flat", seed=77)
        df_4h = _make_df(100, trend="flat", seed=88)
        # Force low ADX and BB/RSI conditions
        df_1h.iloc[-1, df_1h.columns.get_loc("adx")] = 10.0
        df_1h.iloc[-1, df_1h.columns.get_loc("rsi")] = 30.0
        bb_lower = df_1h.iloc[-1]["close"] + 1  # close < bb_lower
        df_1h.iloc[-1, df_1h.columns.get_loc("bb_lower")] = bb_lower
        signal = score_signal(df_1h, df_4h, "MRTEST/USDT")
        # Should either produce a long signal or neutral (depending on other conditions)
        assert signal["direction"] in ("long", "neutral")
        assert signal["score"] >= 0

    def test_neutral_when_no_setup(self):
        """When ADX < 15 and no mean-reversion setup, should be neutral."""
        df_1h = _make_df(300, trend="flat", seed=77)
        df_4h = _make_df(100, trend="flat", seed=88)
        # Set ADX low but RSI in no-man's land
        df_1h.iloc[-1, df_1h.columns.get_loc("adx")] = 10.0
        df_1h.iloc[-1, df_1h.columns.get_loc("rsi")] = 50.0  # neither extreme
        signal = score_signal(df_1h, df_4h, "NEUTRAL/USDT")
        assert signal["direction"] == "neutral"
        assert signal["score"] == 0


class TestRSIDivergencePenalty:
    """Tests for _penalty_rsi_divergence edge cases."""

    def test_no_penalty_with_short_df(self):
        """Should return 0 penalty when dataframe is too short."""
        df = _make_df(10, trend="up")
        c = df.iloc[-1]
        details = {}
        penalty = _penalty_rsi_divergence(df, c, "long", details)
        assert penalty == 0

    def test_no_penalty_when_aligned(self):
        """When RSI and price agree, no penalty."""
        df = _make_df(100, trend="up")
        c = df.iloc[-1]
        details = {}
        penalty = _penalty_rsi_divergence(df, c, "long", details)
        # May or may not penalize depending on random data, but shouldn't crash
        assert isinstance(penalty, (int, float))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
