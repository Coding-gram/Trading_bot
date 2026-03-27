"""
Runtime safety verification script.
Validates execution guards and failure-path behavior.

Run from trading-bot directory:
  python verify_runtime_safety.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
DB_FILE = Path(__file__).resolve().parent / "_runtime_safety_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()
os.environ["DB_PATH"] = str(DB_FILE)

PASS = "[PASS]"
FAIL = "[FAIL]"
RESULTS = []


def check(label, condition, detail=""):
    tag = PASS if condition else FAIL
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    RESULTS.append((label, bool(condition)))


print("\n=== Runtime Safety: Paper Engine Guards ===")
from bot.execution.paper_trade import PaperTradeEngine

engine = PaperTradeEngine(starting_capital=1000.0)

stale_signal = {
    "symbol": "BTC/USDT",
    "direction": "long",
    "price": 50000.0,
    "score": 80,
    "generated_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
}
ok_risk = {"qty": 0.002, "sl": 49000.0, "tp1": 51000.0, "tp2": 52000.0}
stale_result = engine.open_position(stale_signal, ok_risk)
check("Reject stale signal", stale_result == {})

bad_layout_signal = {
    "symbol": "ETH/USDT",
    "direction": "long",
    "price": 3000.0,
    "score": 80,
    "generated_at": datetime.now(timezone.utc).isoformat(),
}
bad_layout_risk = {"qty": 0.02, "sl": 3100.0, "tp1": 3050.0, "tp2": 3200.0}
bad_layout_result = engine.open_position(bad_layout_signal, bad_layout_risk)
check("Reject invalid long SL/TP layout", bad_layout_result == {})

low_notional_signal = {
    "symbol": "SOL/USDT",
    "direction": "short",
    "price": 100.0,
    "score": 80,
    "generated_at": datetime.now(timezone.utc).isoformat(),
}
low_notional_risk = {"qty": 0.0001, "sl": 101.0, "tp1": 99.0, "tp2": 98.0}
low_notional_result = engine.open_position(low_notional_signal, low_notional_risk)
check("Reject too-small notional", low_notional_result == {})


print("\n=== Runtime Safety: Live Engine Pre-Order Checks ===")
from bot.execution.live_trade import LiveTradeEngine


class FakeExchange:
    def fetch_ticker(self, symbol):
        return {"bid": 100.0, "ask": 100.1, "last": 100.05}

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.01}}}

    def create_order(self, symbol, order_type, side, qty, price, params):
        return {"id": "oid-1", "symbol": symbol, "type": order_type, "side": side, "amount": qty, "price": price}


class BadSpreadExchange(FakeExchange):
    def fetch_ticker(self, symbol):
        return {"bid": 100.0, "ask": 110.0, "last": 105.0}


class ReconcileExchange(FakeExchange):
    def __init__(self):
        self._counter = 0
        self.open_orders = []
        self.order_states = {}
        self.last_price = 100.05

    def create_order(self, symbol, order_type, side, qty, price, params):
        self._counter += 1
        oid = f"oid-{self._counter}"
        return {
            "id": oid,
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": qty,
            "price": price,
            "filled": 0.0,
            "status": "open",
        }

    def fetch_open_orders(self, symbol):
        return [o for o in self.open_orders if o.get("symbol") == symbol]

    def fetch_order(self, order_id, symbol):
        state = self.order_states.get(order_id)
        if state:
            return state
        return {
            "id": order_id,
            "symbol": symbol,
            "amount": 0.0,
            "filled": 0.0,
            "average": None,
            "status": "open",
            "fee": {"cost": 0.0},
        }

    def fetch_ticker(self, symbol):
        return {"bid": self.last_price - 0.05, "ask": self.last_price + 0.05, "last": self.last_price}


class ConsistencyExchange(ReconcileExchange):
    pass


good_live = LiveTradeEngine(FakeExchange())
order = good_live.create_order("BTC/USDT", "buy", 0.1, price=100.05)
check("Allow valid live order", order is not None)

bad_live = LiveTradeEngine(BadSpreadExchange())
bad_order = bad_live.create_order("ETH/USDT", "buy", 0.1, price=100.05)
check("Reject wide-spread live order", bad_order is None)

startup_report = good_live.last_startup_reconcile_report or {}
check("Startup reconcile report is emitted", isinstance(startup_report, dict) and bool(startup_report))
check("Startup reconcile report has unresolved counter", "unresolved_critical_count" in startup_report)

recon_exchange = ReconcileExchange()
recon_live = LiveTradeEngine(recon_exchange)
created = recon_live.create_order(
    "XRP/USDT",
    "buy",
    0.2,
    price=100.05,
    risk_params={"sl": 98.0, "tp1": 101.0, "tp2": 102.5},
)
check("Create order for reconciliation flow", created is not None)

if created:
    oid = created["id"]
    recon_exchange.open_orders = [{
        "id": oid,
        "symbol": "XRP/USDT",
        "amount": 0.2,
        "filled": 0.1,
        "average": 100.0,
        "status": "open",
        "fee": {"cost": 0.001},
    }]
    recon_live.reconcile_orders()
    partial_qty = float((recon_live.positions.get("XRP/USDT") or {}).get("qty") or 0.0)
    check("Reconcile updates partial fill qty", abs(partial_qty - 0.1) < 1e-9)

    recon_exchange.open_orders = []
    recon_exchange.order_states[oid] = {
        "id": oid,
        "symbol": "XRP/USDT",
        "amount": 0.2,
        "filled": 0.2,
        "average": 100.0,
        "status": "closed",
        "fee": {"cost": 0.002},
    }
    recon_live.reconcile_orders()
    retained = recon_live.positions.get("XRP/USDT") or {}
    check("Reconcile retains filled live position", "XRP/USDT" in recon_live.positions)
    check("Reconcile marks entry as filled", int(float(retained.get("entry_filled") or 0)) == 1)

    recon_exchange.last_price = 103.0
    recon_live.monitor_positions()
    check("Live TP/SL monitor closes position at TP", "XRP/USDT" not in recon_live.positions)

# Duplicate-open guard: if DB already has an open row for symbol, create_order should reject.
recon_live._save_order(
    {
        "id": "oid-dup-1",
        "amount": 0.2,
        "filled": 0.0,
        "average": 0.0,
        "status": "open",
    },
    "SOL/USDT",
    "buy",
    0.2,
    100.0,
    risk_params={"sl": 95.0, "tp1": 101.0, "tp2": 102.0},
)
duplicate_order = recon_live.create_order("SOL/USDT", "buy", 0.2, price=100.0)
check("Reject duplicate open trade for symbol", duplicate_order is None)


print("\n=== Runtime Safety: Live Engine Fault Injection ===")
consistency_exchange = ConsistencyExchange()
consistency_live = LiveTradeEngine(consistency_exchange)

# 1) Exchange success + DB save failure should still return order and track position as sync_required.
original_save_order = consistency_live._save_order
consistency_live._save_order = lambda *args, **kwargs: False
created_fail_save = consistency_live.create_order(
    "ETH/USDT",
    "buy",
    0.2,
    price=100.0,
    risk_params={"sl": 95.0, "tp1": 102.0, "tp2": 105.0},
)
check("create_order returns exchange order when DB save fails", created_fail_save is not None)
check("Fallback in-memory position is tracked on DB save failure", "ETH/USDT" in consistency_live.positions)
sync_required = int(float((consistency_live.positions.get("ETH/USDT") or {}).get("sync_required") or 0))
check("DB save failure marks sync_required in memory", sync_required == 1)
consistency_live._save_order = original_save_order

# 2) Reconcile should not treat local trade id as remote order id when order_id is missing.
consistency_live.positions["XRP/USDT"] = {
    "id": "local-test-1234",
    "symbol": "XRP/USDT",
    "direction": "buy",
    "entry_price": 100.0,
    "qty": 0.2,
    "stop_loss": 95.0,
    "tp1": 102.0,
    "tp2": 105.0,
    "tp1_hit": 0,
    "entry_filled": 1,
    "status": "open",
    "order_id": None,
    "sync_required": 0,
}
consistency_exchange.open_orders = []
consistency_exchange.order_states = {}
consistency_live.reconcile_orders()
check("Reconcile keeps position when order_id is missing", "XRP/USDT" in consistency_live.positions)

# 3) Partial TP DB update failure should mark sync_required.
consistency_live.positions["BTC/USDT"] = {
    "id": "oid-partial",
    "symbol": "BTC/USDT",
    "direction": "buy",
    "entry_price": 100.0,
    "qty": 0.2,
    "stop_loss": 95.0,
    "tp1": 101.0,
    "tp2": 200.0,
    "tp1_hit": 0,
    "entry_filled": 1,
    "status": "open",
    "order_id": None,
    "sync_required": 0,
}

original_update_row = consistency_live._update_live_trade_row
consistency_live._update_live_trade_row = lambda *args, **kwargs: False
consistency_exchange.last_price = 101.5
consistency_live.monitor_positions()
partial_sync_required = int(float((consistency_live.positions.get("BTC/USDT") or {}).get("sync_required") or 0))
check("Partial TP DB failure marks sync_required", partial_sync_required == 1)
consistency_live._update_live_trade_row = original_update_row


print("\n" + "=" * 50)
passed = sum(1 for _, ok in RESULTS if ok)
failed = sum(1 for _, ok in RESULTS if not ok)
print(f"Results: {passed} passed, {failed} failed out of {len(RESULTS)} checks")
if failed:
    print("\nFailed checks:")
    for label, ok in RESULTS:
        if not ok:
            print(f"  {FAIL} {label}")
    sys.exit(1)
print("ALL CHECKS PASSED")
