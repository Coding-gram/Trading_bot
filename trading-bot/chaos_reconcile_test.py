"""
Offline chaos test for live-state crash/restart reconciliation.

Run from trading-bot directory:
  python chaos_reconcile_test.py
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_FILE = ROOT / "_chaos_reconcile_test.db"
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ["DB_PATH"] = str(DB_FILE)
os.environ.setdefault("LIVE_RECONCILE_MAX_UNSYNC", "2")
os.environ.setdefault("LIVE_RECONCILE_HARD_FAIL", "false")

PASS = "[PASS]"
FAIL = "[FAIL]"
RESULTS = []


def check(label, condition, detail=""):
    ok = bool(condition)
    tag = PASS if ok else FAIL
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    RESULTS.append((label, ok))


class ChaosExchange:
    def __init__(self):
        self._counter = 0
        self.last_price = 100.0
        self.open_orders = []
        self.order_states = {}

    def fetch_ticker(self, symbol):
        return {"bid": self.last_price - 0.05, "ask": self.last_price + 0.05, "last": self.last_price}

    def market(self, symbol):
        return {"limits": {"amount": {"min": 0.01}}}

    def create_order(self, symbol, order_type, side, qty, price, params):
        self._counter += 1
        oid = f"oid-{self._counter}"
        order = {
            "id": oid,
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": qty,
            "filled": 0.0,
            "average": None,
            "price": price,
            "status": "open",
            "fee": {"cost": 0.0},
        }
        if order_type != "market" or side in {"buy", "sell"}:
            # entry/exit placement can appear as open until reconciled by test setup
            pass
        return order

    def fetch_open_orders(self, symbol=None):
        if symbol is None:
            return list(self.open_orders)
        return [o for o in self.open_orders if o.get("symbol") == symbol]

    def fetch_order(self, order_id, symbol):
        state = self.order_states.get(order_id)
        if state:
            return dict(state)
        return {
            "id": order_id,
            "symbol": symbol,
            "amount": 0.0,
            "filled": 0.0,
            "average": None,
            "status": "open",
            "fee": {"cost": 0.0},
        }


def _open_rows_for_symbol(symbol: str) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM live_trades WHERE status='open' AND symbol=?",
                (symbol,),
            ).fetchone()[0]
        )


def main() -> int:
    if DB_FILE.exists():
        DB_FILE.unlink()

    from bot.execution.live_trade import LiveTradeEngine

    print("\n=== Chaos Reconcile Test ===")
    ex = ChaosExchange()

    e1 = LiveTradeEngine(ex)
    created = e1.create_order(
        "BTC/USDT",
        "buy",
        0.2,
        price=100.0,
        risk_params={"sl": 95.0, "tp1": 101.0, "tp2": 102.0},
    )
    check("Create initial live order", created is not None)
    oid = (created or {}).get("id")

    ex.open_orders = [
        {
            "id": oid,
            "symbol": "BTC/USDT",
            "amount": 0.2,
            "filled": 0.0,
            "average": None,
            "price": 100.0,
            "status": "open",
            "side": "buy",
            "fee": {"cost": 0.0},
        }
    ]

    e2 = LiveTradeEngine(ex)
    check("Restart reloads open position", "BTC/USDT" in e2.positions)
    check("Startup reconcile report exists", bool(e2.last_startup_reconcile_report))

    ex.open_orders = [
        {
            "id": oid,
            "symbol": "BTC/USDT",
            "amount": 0.2,
            "filled": 0.1,
            "average": 100.0,
            "status": "open",
            "fee": {"cost": 0.001},
        }
    ]
    e2.reconcile_orders()
    qty_after_partial = float((e2.positions.get("BTC/USDT") or {}).get("qty") or 0.0)
    check("Reconcile updates partial fill qty", abs(qty_after_partial - 0.1) < 1e-9)

    ex.open_orders = []
    ex.order_states[str(oid)] = {
        "id": oid,
        "symbol": "BTC/USDT",
        "amount": 0.2,
        "filled": 0.2,
        "average": 100.0,
        "status": "closed",
        "fee": {"cost": 0.002},
    }
    e2.reconcile_orders()
    post_fill = e2.positions.get("BTC/USDT") or {}
    check("Reconcile marks entry filled", int(float(post_fill.get("entry_filled") or 0)) == 1)
    check("No duplicate open rows for symbol", _open_rows_for_symbol("BTC/USDT") <= 1)

    ex.last_price = 102.5
    e2.monitor_positions()
    check("TP/SL monitor closes filled position", "BTC/USDT" not in e2.positions)

    ex.open_orders = [
        {
            "id": "oid-orphan-1",
            "symbol": "ETH/USDT",
            "amount": 0.3,
            "filled": 0.0,
            "average": 2000.0,
            "price": 2000.0,
            "status": "open",
            "side": "buy",
            "fee": {"cost": 0.0},
        }
    ]
    e3 = LiveTradeEngine(ex)
    report = e3.last_startup_reconcile_report or {}
    check("Startup adopts orphan order", "ETH/USDT" in e3.positions)
    check("Startup report tracks adopted orphans", int(report.get("adopted_orphans") or 0) >= 1)
    check("Startup report unresolved count within threshold", int(report.get("unresolved_critical_count") or 0) <= int(report.get("max_allowed_unsynced") or 0))

    print("\n" + "=" * 50)
    passed = sum(1 for _, ok in RESULTS if ok)
    failed = sum(1 for _, ok in RESULTS if not ok)
    print(f"Results: {passed} passed, {failed} failed out of {len(RESULTS)} checks")

    if DB_FILE.exists():
        try:
            DB_FILE.unlink()
        except PermissionError:
            pass
    if failed:
        print("\nFailed checks:")
        for label, ok in RESULTS:
            if not ok:
                print(f"  {FAIL} {label}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
