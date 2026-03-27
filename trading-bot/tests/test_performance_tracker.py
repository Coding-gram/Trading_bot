"""
Tests for PerformanceTracker — the performance feedback loop module.
"""
import os
import sys
import tempfile

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from bot.strategy.performance_tracker import PerformanceTracker
from bot.strategy import scorer


@pytest.fixture
def tracker(tmp_path):
    """Create a PerformanceTracker with an isolated temp DB."""
    db_path = str(tmp_path / "test_perf.db")
    return PerformanceTracker(db_path=db_path)


class TestPerformanceTracker:
    def test_record_entry_stores_data(self, tracker):
        signal = {
            "symbol": "BTC/USDT",
            "direction": "long",
            "score": 82,
            "details": {"trend": "+15 (bull)", "macd": "+10 (crossover)", "rsi": "+8 (buy zone)"},
        }
        tracker.record_entry("trade_001", signal)
        summary = tracker.get_summary()
        assert summary["total"] == 1
        assert summary["with_outcome"] == 0

    def test_record_outcome_updates_trade(self, tracker):
        signal = {
            "symbol": "ETH/USDT",
            "direction": "short",
            "score": 75,
            "details": {"trend": "+15 (bear)", "volume": "+5 (spike)"},
        }
        tracker.record_entry("trade_002", signal)
        tracker.record_outcome("trade_002", pnl=15.50)
        summary = tracker.get_summary()
        assert summary["with_outcome"] == 1
        assert summary["wins"] == 1

    def test_loss_outcome(self, tracker):
        signal = {
            "symbol": "SOL/USDT",
            "direction": "long",
            "score": 70,
            "details": {"trend": "+15 (bull)"},
        }
        tracker.record_entry("trade_003", signal)
        tracker.record_outcome("trade_003", pnl=-8.20)
        summary = tracker.get_summary()
        assert summary["losses"] == 1

    def test_indicator_report_with_enough_trades(self, tracker):
        # Create 6 trades where "trend" is always active
        for i in range(6):
            signal = {
                "symbol": f"COIN{i}/USDT",
                "direction": "long",
                "score": 80,
                "details": {"trend": "+15 (bull)", "macd": "+10 (crossover)"},
            }
            tracker.record_entry(f"trade_{i}", signal)
            pnl = 10.0 if i < 4 else -5.0  # 4 wins, 2 losses
            tracker.record_outcome(f"trade_{i}", pnl=pnl)

        report = tracker.get_indicator_report(min_trades=5)
        assert "trend" in report
        assert report["trend"]["total"] == 6
        assert report["trend"]["wins"] == 4
        assert report["trend"]["win_rate"] == pytest.approx(66.7, abs=0.1)

    def test_indicator_report_minimum_trades_filter(self, tracker):
        # Only 2 trades — below the min_trades=5 threshold
        for i in range(2):
            tracker.record_entry(f"trade_{i}", {
                "symbol": "X/USDT", "direction": "long", "score": 80,
                "details": {"trend": "+15", "rsi": "+8"},
            })
            tracker.record_outcome(f"trade_{i}", pnl=5.0)

        report = tracker.get_indicator_report(min_trades=5)
        assert len(report) == 0  # not enough trades

    def test_suggest_weight_adjustments(self, tracker):
        # 12 trades: trend always active, macd only in some
        for i in range(12):
            details = {"trend": "+15"}
            if i < 8:
                details["macd"] = "+10"
            tracker.record_entry(f"t_{i}", {
                "symbol": f"C{i}", "direction": "long", "score": 80,
                "details": details,
            })
            # Trend: 7/12 win. MACD: 6/8 win
            pnl = 10.0 if i < 7 else -5.0
            tracker.record_outcome(f"t_{i}", pnl=pnl)

        suggestions = tracker.suggest_weight_adjustments(min_trades=5)
        assert len(suggestions) > 0
        assert any(s["indicator"] == "trend" for s in suggestions)

    def test_empty_tracker_returns_empty(self, tracker):
        report = tracker.get_indicator_report()
        assert report == {}
        suggestions = tracker.suggest_weight_adjustments()
        assert suggestions == []
        summary = tracker.get_summary()
        assert summary["total"] == 0

    def test_apply_weight_feedback_adjusts_up_and_down(self, tracker, monkeypatch):
        baseline_weights = dict(scorer.WEIGHTS)
        monkeypatch.setattr(scorer, "WEIGHTS", dict(baseline_weights))

        old_macd = int(scorer.WEIGHTS["macd_signal"])
        old_rsi = int(scorer.WEIGHTS["rsi_zone"])

        # 10 total closed trades, baseline WR = 60%
        # macd appears in 4 wins (100% WR -> increase)
        # rsi appears in 4 losses (0% WR -> decrease)
        for i in range(10):
            details = {"trend": "+15"}
            if i < 4:
                details["macd"] = "+10"
            if i >= 6:
                details["rsi"] = "+8"

            tracker.record_entry(
                f"edge_{i}",
                {
                    "symbol": f"T{i}/USDT",
                    "direction": "long",
                    "score": 80,
                    "details": details,
                },
            )
            tracker.record_outcome(f"edge_{i}", pnl=(10.0 if i < 6 else -5.0))

        adjustments = tracker.apply_weight_feedback(
            min_indicator_trades=3,
            min_edge_pct=1.0,
            learning_rate=0.8,
            max_step_pct=0.5,
            min_weight=1,
            max_weight=200,
            persist=False,
        )

        assert any(a["weight_key"] == "macd_signal" for a in adjustments)
        assert any(a["weight_key"] == "rsi_zone" for a in adjustments)
        assert int(scorer.WEIGHTS["macd_signal"]) > old_macd
        assert int(scorer.WEIGHTS["rsi_zone"]) < old_rsi

    def test_auto_adjust_runs_on_trade_cadence(self, tracker, monkeypatch):
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_FEEDBACK_ENABLED", True)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MIN_CLOSED_TRADES", 2)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_RECALIBRATE_EVERY", 2)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_REQUIRE_TELEGRAM_APPROVAL", False)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MIN_INDICATOR_TRADES", 1)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MIN_EDGE_PCT", 0.0)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_LEARNING_RATE", 0.5)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MAX_STEP_PCT", 0.5)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MIN_WEIGHT", 1)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MAX_WEIGHT", 200)

        calls = {"count": 0}

        def _fake_apply(*args, **kwargs):
            calls["count"] += 1
            return []

        monkeypatch.setattr(PerformanceTracker, "apply_weight_feedback", _fake_apply)

        for i in range(4):
            tracker.record_entry(
                f"cad_{i}",
                {
                    "symbol": "CAD/USDT",
                    "direction": "long",
                    "score": 70,
                    "details": {"trend": "+15"},
                },
            )
            tracker.record_outcome(f"cad_{i}", pnl=(5.0 if i % 2 == 0 else -2.0))

        assert calls["count"] == 2

    def test_auto_adjust_waits_for_telegram_approval_then_applies(self, tracker, monkeypatch):
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_FEEDBACK_ENABLED", True)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MIN_CLOSED_TRADES", 2)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_RECALIBRATE_EVERY", 2)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MIN_INDICATOR_TRADES", 1)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_MIN_EDGE_PCT", 0.0)
        monkeypatch.setattr("bot.strategy.performance_tracker.AUTO_WEIGHT_REQUIRE_TELEGRAM_APPROVAL", True)

        state = {"decision": None, "preview_calls": 0, "apply_calls": 0, "sent_calls": 0}

        def _fake_apply(*args, **kwargs):
            persist = bool(kwargs.get("persist", True))
            if persist:
                state["apply_calls"] += 1
            else:
                state["preview_calls"] += 1
            return [
                {
                    "indicator": "trend",
                    "weight_key": "trend_alignment",
                    "old_weight": 20,
                    "new_weight": 21,
                    "edge": 4.0,
                    "win_rate": 60.0,
                    "baseline_win_rate": 56.0,
                    "trades": 8,
                }
            ]

        def _fake_send(*args, **kwargs):
            state["sent_calls"] += 1
            return True

        def _fake_poll(*args, **kwargs):
            return state["decision"]

        monkeypatch.setattr(PerformanceTracker, "apply_weight_feedback", _fake_apply)
        monkeypatch.setattr(PerformanceTracker, "_send_tg_approval_request", _fake_send)
        monkeypatch.setattr(PerformanceTracker, "_poll_tg_approval_decision", _fake_poll)

        # first two outcomes -> cadence reached, preview + approval request only
        for i in range(2):
            tracker.record_entry(
                f"ap_{i}",
                {
                    "symbol": "APT/USDT",
                    "direction": "long",
                    "score": 75,
                    "details": {"trend": "+15"},
                },
            )
            tracker.record_outcome(f"ap_{i}", pnl=10.0)

        assert state["sent_calls"] == 1
        assert state["preview_calls"] == 1
        assert state["apply_calls"] == 0

        # next outcome with explicit approval -> apply once
        state["decision"] = "approve"
        tracker.record_entry(
            "ap_2",
            {
                "symbol": "APT/USDT",
                "direction": "long",
                "score": 75,
                "details": {"trend": "+15"},
            },
        )
        tracker.record_outcome("ap_2", pnl=10.0)

        assert state["apply_calls"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
