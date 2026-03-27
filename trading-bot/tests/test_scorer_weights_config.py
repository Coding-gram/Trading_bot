"""
Unit tests for scorer weight config compatibility and validation.
"""

import os
import sys

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.strategy import scorer


def test_normalize_supports_renamed_keys(monkeypatch):
    renamed_only = {
        "trend_alignment": 20,
        "macd_signal": 15,
        "rsi_signal": 10,
        "stochastic_signal": 10,
        "candlestick_bonus": 5,
        "pattern_bonus": 10,
        "volume_signal": 10,
        "obv_signal": 5,
        "vwap_signal": 5,
        "support_resistance": 15,
    }

    monkeypatch.setattr(scorer, "WEIGHTS", dict(renamed_only))
    scorer._normalize_weight_keys()
    scorer._validate_weight_config()

    assert scorer.WEIGHTS["rsi_zone"] == renamed_only["rsi_signal"]
    assert scorer.WEIGHTS["stochastic"] == renamed_only["stochastic_signal"]
    assert scorer.WEIGHTS["chart_pattern"] == renamed_only["pattern_bonus"]


def test_validate_raises_on_missing_required_keys(monkeypatch):
    monkeypatch.setattr(scorer, "WEIGHTS", {"rsi_zone": 10})

    with pytest.raises(ValueError, match="missing required scorer keys"):
        scorer._validate_weight_config()


def test_validate_raises_on_non_numeric_key(monkeypatch):
    bad_weights = dict(scorer.WEIGHTS)
    bad_weights["rsi_zone"] = "ten"
    monkeypatch.setattr(scorer, "WEIGHTS", bad_weights)

    with pytest.raises(ValueError, match="non-numeric scorer keys"):
        scorer._validate_weight_config()
