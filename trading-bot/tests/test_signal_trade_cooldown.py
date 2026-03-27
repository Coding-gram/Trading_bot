from bot.signal_worker import execute_signal


class DummyEngine:
    def __init__(self):
        self.capital = 1000.0
        self.open_calls = 0

    def open_position(self, signal, risk_params):
        self.open_calls += 1
        return {}


class DummyLossGuard:
    consecutive_losses = 0


def test_execute_signal_blocks_when_symbol_cooldown_active(monkeypatch):
    engine = DummyEngine()
    guard = DummyLossGuard()
    signal = {
        "symbol": "BTC/USDT",
        "direction": "long",
        "score": 80,
        "price": 50000.0,
        "atr": 1000.0,
        "sr_levels": {},
        "fib_levels": {},
    }

    rejected = {}

    monkeypatch.setattr("bot.signal_worker.cooldown_remaining_seconds", lambda _symbol: 125)

    def _capture_rejected(payload, mode, reason):
        rejected["symbol"] = payload.get("symbol")
        rejected["mode"] = mode
        rejected["reason"] = reason

    monkeypatch.setattr("bot.signal_worker._safe_send_execution_rejected", _capture_rejected)

    execute_signal(engine, guard, signal, mode="paper", live_engine=None)

    assert engine.open_calls == 0
    assert rejected["symbol"] == "BTC/USDT"
    assert rejected["mode"] == "paper"
    assert "cooldown active" in rejected["reason"]
