"""
Live Trading Engine for Binance (ccxt/ccxt.pro)
Handles real order creation, cancellation, reconciliation, and advanced order state management.
"""
import logging
import sqlite3
from datetime import datetime
from bot.config import (
    DB_PATH,
    MIN_ORDER_NOTIONAL_USDT,
    MAX_ORDER_NOTIONAL_USDT,
    MAX_ENTRY_SPREAD_PCT,
    MAX_SLIPPAGE_PCT,
)
from bot import telemetry

logger = logging.getLogger(__name__)

class LiveTradeEngine:
    def __init__(self, exchange):
        self.exchange = exchange
        self.positions = {}
        self._init_db()
        self._load_open_positions()

    def _init_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute('''CREATE TABLE IF NOT EXISTS live_trades (
            id TEXT PRIMARY KEY,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            qty REAL,
            stop_loss REAL,
            tp1 REAL,
            tp2 REAL,
            tp1_hit INTEGER DEFAULT 0,
            entry_filled INTEGER DEFAULT 0,
            status TEXT,
            order_id TEXT,
            open_time TEXT,
            close_time TEXT,
            pnl REAL,
            fee REAL
        )''')
        self._ensure_schema(conn)
        conn.commit()
        conn.close()

    def _ensure_schema(self, conn):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(live_trades)").fetchall()}
        alter_statements = []
        if "stop_loss" not in cols:
            alter_statements.append("ALTER TABLE live_trades ADD COLUMN stop_loss REAL")
        if "tp1" not in cols:
            alter_statements.append("ALTER TABLE live_trades ADD COLUMN tp1 REAL")
        if "tp2" not in cols:
            alter_statements.append("ALTER TABLE live_trades ADD COLUMN tp2 REAL")
        if "tp1_hit" not in cols:
            alter_statements.append("ALTER TABLE live_trades ADD COLUMN tp1_hit INTEGER DEFAULT 0")
        if "entry_filled" not in cols:
            alter_statements.append("ALTER TABLE live_trades ADD COLUMN entry_filled INTEGER DEFAULT 0")
        for sql in alter_statements:
            conn.execute(sql)

    def _load_open_positions(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM live_trades WHERE status = 'open'").fetchall()
        conn.close()
        for row in rows:
            self.positions[row["symbol"]] = dict(row)

    def _passes_pre_order_checks(self, symbol, side, qty, price):
        try:
            qty = float(qty or 0)
            if qty <= 0:
                logger.warning("[LIVE] Rejecting %s: qty must be > 0", symbol)
                return False

            ticker = self.exchange.fetch_ticker(symbol)
            bid = float(ticker.get("bid") or 0)
            ask = float(ticker.get("ask") or 0)
            last = float(ticker.get("last") or 0)
            market_price = float(price or last or ask or bid or 0)
            if market_price <= 0:
                logger.warning("[LIVE] Rejecting %s: missing market price", symbol)
                return False

            notional = abs(market_price * qty)
            if notional < MIN_ORDER_NOTIONAL_USDT:
                logger.warning("[LIVE] Rejecting %s: notional %.4f < min %.4f", symbol, notional, MIN_ORDER_NOTIONAL_USDT)
                return False
            if notional > MAX_ORDER_NOTIONAL_USDT:
                logger.warning("[LIVE] Rejecting %s: notional %.4f > max %.4f", symbol, notional, MAX_ORDER_NOTIONAL_USDT)
                return False

            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / bid if bid else 0
                if spread_pct > MAX_ENTRY_SPREAD_PCT:
                    logger.warning("[LIVE] Rejecting %s: spread %.4f%% > %.4f%%", symbol, spread_pct * 100, MAX_ENTRY_SPREAD_PCT * 100)
                    return False

            if price is not None and last > 0:
                slippage_proxy_pct = abs(float(price) - last) / last
                if slippage_proxy_pct > MAX_SLIPPAGE_PCT:
                    logger.warning(
                        "[LIVE] Rejecting %s: slippage proxy %.4f%% > %.4f%%",
                        symbol,
                        slippage_proxy_pct * 100,
                        MAX_SLIPPAGE_PCT * 100,
                    )
                    return False

            market = self.exchange.market(symbol)
            min_amount = ((market.get("limits") or {}).get("amount") or {}).get("min")
            if min_amount is not None and qty < float(min_amount):
                logger.warning("[LIVE] Rejecting %s: qty %.8f < min amount %.8f", symbol, qty, float(min_amount))
                return False

            valid_side = str(side).lower() in ("buy", "sell", "long", "short")
            if not valid_side:
                logger.warning("[LIVE] Rejecting %s: invalid side '%s'", symbol, side)
                return False

            return True
        except Exception as e:
            logger.error("[LIVE] Pre-order check error on %s: %s", symbol, e)
            return False

    def _normalize_order_qty(self, symbol: str, qty: float) -> float:
        qty = self._to_float(qty, 0.0)
        if qty <= 0:
            return 0.0

        normalized = qty
        try:
            if hasattr(self.exchange, "amount_to_precision"):
                normalized = float(self.exchange.amount_to_precision(symbol, qty))
        except Exception:
            normalized = qty

        try:
            market = self.exchange.market(symbol) or {}
            limits = market.get("limits") or {}
            min_amount = ((limits.get("amount") or {}).get("min"))
            max_amount = ((limits.get("amount") or {}).get("max"))
            if min_amount is not None:
                normalized = max(normalized, float(min_amount))
            if max_amount is not None:
                normalized = min(normalized, float(max_amount))
        except Exception:
            pass

        return round(float(normalized), 12)

    def create_order(self, symbol, side, qty, price=None, type="market", params=None, risk_params=None):
        try:
            qty = self._normalize_order_qty(symbol, qty)
            if qty <= 0:
                logger.warning("[LIVE] Rejecting %s: normalized qty is 0", symbol)
                telemetry.increment("order_rejections")
                return None

            if not self._passes_pre_order_checks(symbol, side, qty, price):
                telemetry.increment("order_rejections")
                return None

            ticker = self.exchange.fetch_ticker(symbol)
            market_price = self._to_float(price, self._to_float(ticker.get("last"), 0.0))
            notional = abs(market_price * qty)
            if notional < MIN_ORDER_NOTIONAL_USDT:
                logger.warning("[LIVE] Rejecting %s: normalized notional %.4f < min %.4f", symbol, notional, MIN_ORDER_NOTIONAL_USDT)
                telemetry.increment("order_rejections")
                return None
            if notional > MAX_ORDER_NOTIONAL_USDT:
                logger.warning("[LIVE] Rejecting %s: normalized notional %.4f > max %.4f", symbol, notional, MAX_ORDER_NOTIONAL_USDT)
                telemetry.increment("order_rejections")
                return None

            order = self.exchange.create_order(symbol, type, side, qty, price, params or {})
            logger.info(f"Order placed: {order}")
            # Save to DB
            self._save_order(order, symbol, side, qty, price, risk_params=risk_params)
            telemetry.increment("orders_opened")
            return order
        except Exception as e:
            logger.error(f"Order error: {e}")
            telemetry.increment("api_errors")
            return None

    def cancel_order(self, symbol, order_id):
        try:
            result = self.exchange.cancel_order(order_id, symbol)
            logger.info(f"Order cancelled: {order_id}")
            return result
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            telemetry.increment("api_errors")
            return None

    def _save_order(self, order, symbol, side, qty, price, risk_params=None):
        risk_params = risk_params or {}
        order_amount = self._to_float(order.get("amount"), self._to_float(qty))
        order_filled = self._to_float(order.get("filled"), 0.0)
        avg_price = self._to_float(order.get("average"), 0.0)
        status = str(order.get("status") or "open").lower()
        entry_filled = int(status == "closed" and order_filled > 0)
        entry_price = avg_price if avg_price > 0 else self._to_float(price, 0.0)
        live_qty = order_filled if order_filled > 0 else order_amount

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            INSERT OR REPLACE INTO live_trades
            (id, symbol, direction, entry_price, qty, stop_loss, tp1, tp2, tp1_hit, entry_filled, status, order_id, open_time, pnl, fee)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(order.get("id") or ""),
                symbol,
                side,
                entry_price,
                live_qty,
                self._to_float(risk_params.get("sl"), 0.0),
                self._to_float(risk_params.get("tp1"), 0.0),
                self._to_float(risk_params.get("tp2"), 0.0),
                0,
                entry_filled,
                "open",
                str(order.get("id") or ""),
                datetime.utcnow().isoformat(),
                0,
                0,
            ),
        )
        conn.commit()
        conn.close()

        self.positions[symbol] = {
            "id": str(order.get("id") or ""),
            "symbol": symbol,
            "direction": side,
            "entry_price": entry_price,
            "qty": live_qty,
            "stop_loss": self._to_float(risk_params.get("sl"), 0.0),
            "tp1": self._to_float(risk_params.get("tp1"), 0.0),
            "tp2": self._to_float(risk_params.get("tp2"), 0.0),
            "tp1_hit": 0,
            "entry_filled": entry_filled,
            "status": "open",
            "order_id": str(order.get("id") or ""),
            "open_time": datetime.utcnow().isoformat(),
            "pnl": 0,
            "fee": 0,
        }

    @staticmethod
    def _to_float(value, default=0.0):
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def _mark_order_terminal(self, order_id: str, status: str):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            "UPDATE live_trades SET status=?, close_time=? WHERE order_id=? AND status='open'",
            (status, datetime.utcnow().isoformat(), order_id),
        )
        conn.commit()
        conn.close()

    def handle_partial_fills(self, order):
        order_id = str(order.get("id") or "")
        symbol = order.get("symbol")
        if not order_id or not symbol:
            return

        amount = self._to_float(order.get("amount"), 0.0)
        filled = self._to_float(order.get("filled"), 0.0)
        average = self._to_float(order.get("average"), 0.0)
        status = str(order.get("status") or "open").lower()

        fee_data = order.get("fee") or {}
        fee_cost = self._to_float(fee_data.get("cost"), 0.0) if isinstance(fee_data, dict) else 0.0

        if filled > 0:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "UPDATE live_trades SET qty=?, entry_price=CASE WHEN ? > 0 THEN ? ELSE entry_price END, fee=COALESCE(fee, 0) + ? WHERE order_id=? AND status='open'",
                (filled, average, average, fee_cost, order_id),
            )
            conn.commit()
            conn.close()

            pos = self.positions.get(symbol)
            if pos:
                pos["qty"] = filled
                if average > 0:
                    pos["entry_price"] = average

        if status in {"closed", "canceled", "cancelled", "rejected", "expired"}:
            if status == "closed" and filled > 0:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    "UPDATE live_trades SET entry_filled=1, qty=?, order_id=NULL WHERE order_id=? AND status='open'",
                    (filled, order_id),
                )
                conn.commit()
                conn.close()
                pos = self.positions.get(symbol)
                if pos:
                    pos["entry_filled"] = 1
                    pos["qty"] = filled
                    pos["order_id"] = None
            else:
                self._mark_order_terminal(order_id, "cancelled")
                if symbol in self.positions:
                    del self.positions[symbol]

        if amount > 0 and 0 < filled < amount and status in {"open", "partially_filled"}:
            logger.info("[LIVE] Partial fill %s: %.8f / %.8f", order_id, filled, amount)

    def reconcile_orders(self):
        """Sync local DB open orders with exchange order state."""
        try:
            for symbol, local in list(self.positions.items()):
                open_orders = self.exchange.fetch_open_orders(symbol) or []
                remote_ids = {str(o.get("id")) for o in open_orders if o.get("id") is not None}

                for order in open_orders:
                    self.handle_partial_fills(order)

                local_order_id = str(local.get("order_id") or local.get("id") or "")
                if not local_order_id:
                    continue
                if local_order_id in remote_ids:
                    continue

                try:
                    fetched = self.exchange.fetch_order(local_order_id, symbol)
                    self.handle_partial_fills(fetched)
                    fetched_status = str(fetched.get("status") or "").lower()
                    if fetched_status in {"closed", "canceled", "cancelled", "rejected", "expired"}:
                        continue
                except Exception:
                    logger.warning("[LIVE] Could not fetch terminal state for %s %s", symbol, local_order_id)

                # If order disappeared from open orders and terminal state is unavailable,
                # conservatively mark it cancelled to avoid stale local exposure.
                self._mark_order_terminal(local_order_id, "cancelled")
                if symbol in self.positions:
                    del self.positions[symbol]
        except Exception as e:
            logger.error(f"Reconciliation error: {e}")
            telemetry.increment("api_errors")

    @staticmethod
    def _is_long(direction: str) -> bool:
        return str(direction).lower() in {"buy", "long"}

    def _exit_side_for(self, direction: str) -> str:
        return "sell" if self._is_long(direction) else "buy"

    def _calc_pnl(self, direction: str, entry: float, exit_price: float, qty: float) -> float:
        if self._is_long(direction):
            return (exit_price - entry) * qty
        return (entry - exit_price) * qty

    def _apply_partial_tp(self, symbol: str, price: float, position: dict, close_qty: float) -> None:
        exit_side = self._exit_side_for(position.get("direction", "buy"))
        self.exchange.create_order(symbol, "market", exit_side, close_qty, None, {})
        remaining_qty = max(self._to_float(position.get("qty"), 0.0) - close_qty, 0.0)

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            "UPDATE live_trades SET qty=?, tp1_hit=1, stop_loss=entry_price WHERE symbol=? AND status='open'",
            (remaining_qty, symbol),
        )
        conn.commit()
        conn.close()

        position["qty"] = remaining_qty
        position["tp1_hit"] = 1
        position["stop_loss"] = self._to_float(position.get("entry_price"), 0.0)
        logger.info("[LIVE] TP1 partial exit for %s @ %.6f (closed %.6f, remaining %.6f)", symbol, price, close_qty, remaining_qty)

    def _close_live_trade(self, symbol: str, price: float, position: dict, reason: str) -> None:
        qty = self._to_float(position.get("qty"), 0.0)
        if qty <= 0:
            return

        exit_side = self._exit_side_for(position.get("direction", "buy"))
        self.exchange.create_order(symbol, "market", exit_side, qty, None, {})

        entry = self._to_float(position.get("entry_price"), 0.0)
        pnl = self._calc_pnl(position.get("direction", "buy"), entry, price, qty)

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            "UPDATE live_trades SET status='closed', close_time=?, pnl=?, order_id=NULL WHERE symbol=? AND status='open'",
            (datetime.utcnow().isoformat(), pnl, symbol),
        )
        conn.commit()
        conn.close()

        if symbol in self.positions:
            del self.positions[symbol]
        telemetry.increment("orders_closed")
        logger.info("[LIVE] Position closed (%s): %s @ %.6f | qty=%.6f | pnl=%.4f", reason, symbol, price, qty, pnl)

    def monitor_positions(self):
        """Apply live TP/SL lifecycle to filled live positions."""
        for symbol, pos in list(self.positions.items()):
            try:
                if int(self._to_float(pos.get("entry_filled"), 0)) != 1:
                    continue

                qty = self._to_float(pos.get("qty"), 0.0)
                entry = self._to_float(pos.get("entry_price"), 0.0)
                sl = self._to_float(pos.get("stop_loss"), 0.0)
                tp1 = self._to_float(pos.get("tp1"), 0.0)
                tp2 = self._to_float(pos.get("tp2"), 0.0)
                tp1_hit = int(self._to_float(pos.get("tp1_hit"), 0)) == 1
                if qty <= 0 or entry <= 0:
                    continue

                ticker = self.exchange.fetch_ticker(symbol) or {}
                price = self._to_float(ticker.get("last"), 0.0)
                if price <= 0:
                    continue

                is_long = self._is_long(pos.get("direction", "buy"))

                if not tp1_hit and tp1 > 0:
                    hit_tp1 = (is_long and price >= tp1) or ((not is_long) and price <= tp1)
                    half_qty = round(qty / 2.0, 8)
                    if hit_tp1 and half_qty > 0 and (half_qty * price) >= MIN_ORDER_NOTIONAL_USDT:
                        self._apply_partial_tp(symbol, price, pos, half_qty)
                        qty = self._to_float(pos.get("qty"), qty)
                        sl = self._to_float(pos.get("stop_loss"), sl)

                hit_tp2 = tp2 > 0 and ((is_long and price >= tp2) or ((not is_long) and price <= tp2))
                hit_sl = sl > 0 and ((is_long and price <= sl) or ((not is_long) and price >= sl))

                if hit_tp2:
                    self._close_live_trade(symbol, price, pos, reason="tp2")
                elif hit_sl:
                    self._close_live_trade(symbol, price, pos, reason="stop_loss")
            except Exception as e:
                logger.error("[LIVE] monitor_positions error on %s: %s", symbol, e)
                telemetry.increment("api_errors")

    def close_position(self, symbol, exit_price, pnl, fee):
        # Mark position as closed in DB
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("UPDATE live_trades SET status='closed', close_time=?, pnl=?, fee=? WHERE symbol=? AND status='open'", (datetime.utcnow().isoformat(), pnl, fee, symbol))
        conn.commit()
        conn.close()
        if symbol in self.positions:
            del self.positions[symbol]
        telemetry.increment("orders_closed")
