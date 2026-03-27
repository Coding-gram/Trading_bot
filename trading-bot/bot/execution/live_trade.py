"""
Live Trading Engine for Binance (ccxt/ccxt.pro)
Handles real order creation, cancellation, reconciliation, and advanced order state management.
"""
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from bot.execution.schema_migrations import apply_live_trades_migrations
from bot.risk.risk_manager import TrailingStopLoss
from bot.config import (
    DB_PATH,
    MIN_ORDER_NOTIONAL_USDT,
    MAX_ORDER_NOTIONAL_USDT,
    MAX_ENTRY_SPREAD_PCT,
    MAX_SLIPPAGE_PCT,
    LIVE_RECONCILE_MAX_UNSYNC,
    LIVE_RECONCILE_HARD_FAIL,
)
from bot import telemetry
from bot.strategy.performance_tracker import PerformanceTracker

logger = logging.getLogger(__name__)

ORDER_CREATE_MAX_RETRIES = 3
ORDER_RETRY_BACKOFF_SECONDS = 1.0
CIRCUIT_BREAKER_MAX_CONSECUTIVE_FAILURES = 3

class LiveTradeEngine:
    def __init__(self, exchange) -> None:
        self.exchange = exchange
        self.positions = {}
        self._trailing_stops: dict[str, TrailingStopLoss] = {}
        self._perf_tracker = PerformanceTracker()
        self.last_startup_reconcile_report = {}
        self.last_rejection_reason: str | None = None
        self.last_realized_slippage_pct: float = 0.0
        self._consecutive_order_failures: int = 0
        self._init_db()
        self._load_open_positions()
        self.last_startup_reconcile_report = self._startup_reconcile()

    @staticmethod
    def _db_connect(row_factory=None):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        if row_factory is not None:
            conn.row_factory = row_factory
        return conn

    def _track_realized_slippage(self, symbol: str, side: str, reference_price: float, order: dict) -> None:
        """Track post-fill slippage for live market/limit entries."""
        try:
            ref = float(reference_price or 0.0)
            fill = self._to_float(order.get("average"), 0.0)
            if ref <= 0 or fill <= 0:
                return

            side_txt = str(side or "").lower()
            if side_txt in {"buy", "long"}:
                slip_pct = (fill - ref) / ref
            else:
                slip_pct = (ref - fill) / ref

            slip_pct = float(slip_pct)
            self.last_realized_slippage_pct = slip_pct
            telemetry.record_event("last_realized_slippage_pct", round(slip_pct, 6))

            if abs(slip_pct) > MAX_SLIPPAGE_PCT:
                logger.warning(
                    "[LIVE] High realized slippage on %s (%s): %.4f%% (max %.4f%%) | ref=%.8f fill=%.8f",
                    symbol,
                    side_txt.upper(),
                    slip_pct * 100,
                    MAX_SLIPPAGE_PCT * 100,
                    ref,
                    fill,
                )
                telemetry.increment("live_high_slippage_events")
        except Exception as slippage_err:
            logger.warning("[LIVE] Could not compute realized slippage for %s: %s", symbol, slippage_err)

    def _init_db(self):
        with self._db_connect() as conn:
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
                sync_required INTEGER DEFAULT 0,
                open_time TEXT,
                close_time TEXT,
                pnl REAL,
                fee REAL
            )''')
            apply_live_trades_migrations(conn)

    def _load_open_positions(self):
        with self._db_connect(sqlite3.Row) as conn:
            rows = conn.execute("SELECT * FROM live_trades WHERE status = 'open'").fetchall()
        for row in rows:
            r = dict(row)
            symbol = r["symbol"]
            self.positions[symbol] = r
            # Initialize trailing stop for restored positions
            entry = self._to_float(r.get("entry_price"), 0.0)
            sl = self._to_float(r.get("stop_loss"), 0.0)
            direction = str(r.get("direction") or "buy").lower()
            atr = self._to_float(r.get("atr"), 0.0)
            if entry > 0 and sl > 0:
                tsl = TrailingStopLoss(entry, sl, "long" if direction in {"buy", "long"} else "short", atr=atr)
                tsl.current_sl = sl  # Restore persisted SL level
                self._trailing_stops[symbol] = tsl

    def _db_open_trade_snapshot(self) -> dict:
        with self._db_connect(sqlite3.Row) as conn:
            rows = conn.execute(
                """
                SELECT id, symbol, order_id, entry_filled, sync_required
                FROM live_trades
                WHERE status='open'
                """
            ).fetchall()

        symbols = [str(r["symbol"] or "") for r in rows if str(r["symbol"] or "")]
        duplicate_symbols = max(len(symbols) - len(set(symbols)), 0)

        return {
            "db_open_rows": len(rows),
            "db_sync_required_open": sum(int(self._to_float(r["sync_required"], 0)) for r in rows),
            "db_open_without_order_id_pending": sum(
                1
                for r in rows
                if not str(r["order_id"] or "").strip() and int(self._to_float(r["entry_filled"], 0)) != 1
            ),
            "db_duplicate_open_symbols": duplicate_symbols,
        }

    def _get_open_trade_by_symbol(self, symbol: str) -> dict | None:
        with self._db_connect(sqlite3.Row) as conn:
            row = conn.execute(
                """
                SELECT *
                FROM live_trades
                WHERE symbol=? AND status='open'
                ORDER BY COALESCE(open_time, '') DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def _find_existing_open_trade_id(self, symbol: str, order_id: str | None) -> str | None:
        with self._db_connect(sqlite3.Row) as conn:
            row = None
            order_id_txt = str(order_id or "").strip()
            if order_id_txt:
                row = conn.execute(
                    """
                    SELECT id
                    FROM live_trades
                    WHERE order_id=? AND status='open'
                    ORDER BY COALESCE(open_time, '') DESC
                    LIMIT 1
                    """,
                    (order_id_txt,),
                ).fetchone()

            if row is None:
                row = conn.execute(
                    """
                    SELECT id
                    FROM live_trades
                    WHERE symbol=? AND status='open'
                    ORDER BY COALESCE(open_time, '') DESC
                    LIMIT 1
                    """,
                    (symbol,),
                ).fetchone()

        if not row:
            return None
        return str(row["id"] or "").strip() or None

    def _startup_reconcile(self) -> dict:
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            before = self._db_open_trade_snapshot()
            self.reconcile_orders()
            adopted_orphans = self._adopt_orphan_open_orders()
            after = self._db_open_trade_snapshot()

            local_sync_required_open = sum(
                1 for pos in self.positions.values() if int(self._to_float(pos.get("sync_required"), 0)) == 1
            )
            local_open_without_order_id_pending = sum(
                1
                for pos in self.positions.values()
                if not str(pos.get("order_id") or "").strip()
                and int(self._to_float(pos.get("entry_filled"), 0)) != 1
            )

            unresolved = max(
                after.get("db_sync_required_open", 0),
                local_sync_required_open,
                after.get("db_open_without_order_id_pending", 0),
                local_open_without_order_id_pending,
                after.get("db_duplicate_open_symbols", 0),
            )

            report = {
                "started_at": started_at,
                "local_open_positions": len(self.positions),
                "adopted_orphans": adopted_orphans,
                "before": before,
                "after": after,
                "local_sync_required_open": local_sync_required_open,
                "local_open_without_order_id_pending": local_open_without_order_id_pending,
                "unresolved_critical_count": unresolved,
                "max_allowed_unsynced": int(max(LIVE_RECONCILE_MAX_UNSYNC, 0)),
                "hard_fail_enabled": bool(LIVE_RECONCILE_HARD_FAIL),
                "ok": unresolved <= int(max(LIVE_RECONCILE_MAX_UNSYNC, 0)),
            }
            telemetry.record_event("live_startup_reconcile_report", report)

            if report["ok"]:
                logger.info("[LIVE] Startup reconcile report: %s", report)
            else:
                logger.error("[LIVE] Startup reconcile unresolved count=%d exceeds max=%d", unresolved, int(max(LIVE_RECONCILE_MAX_UNSYNC, 0)))
                if LIVE_RECONCILE_HARD_FAIL:
                    raise RuntimeError("startup reconcile unresolved count exceeded threshold")

            return report
        except Exception as startup_err:
            logger.error("[LIVE] Startup reconcile failed: %s", startup_err)
            telemetry.increment("api_errors")
            failed_report = {
                "started_at": started_at,
                "ok": False,
                "error": str(startup_err),
                "hard_fail_enabled": bool(LIVE_RECONCILE_HARD_FAIL),
            }
            telemetry.record_event("live_startup_reconcile_report", failed_report)
            if LIVE_RECONCILE_HARD_FAIL:
                raise
            return failed_report

    def _adopt_orphan_open_orders(self) -> int:
        try:
            open_orders = self._fetch_open_orders_all()
        except Exception as fetch_err:
            logger.warning("[LIVE] Could not fetch orphan open orders: %s", fetch_err)
            return 0

        known_order_ids = {
            str(pos.get("order_id") or "").strip()
            for pos in self.positions.values()
            if str(pos.get("order_id") or "").strip()
        }

        adopted_count = 0
        for order in open_orders:
            order_id = str(order.get("id") or "").strip()
            symbol = str(order.get("symbol") or "").strip()
            side = str(order.get("side") or "buy").lower()
            if not order_id or not symbol:
                continue
            if order_id in known_order_ids:
                continue
            if symbol in self.positions and str(self.positions[symbol].get("order_id") or "").strip() == order_id:
                continue

            amount = self._to_float(order.get("amount"), 0.0)
            filled = self._to_float(order.get("filled"), 0.0)
            avg = self._to_float(order.get("average"), 0.0)
            price = self._to_float(order.get("price"), 0.0)
            entry_price = avg if avg > 0 else price
            qty = filled if filled > 0 else amount
            direction = "buy" if side in {"buy", "long"} else "sell"

            recovered_order = {
                "id": order_id,
                "amount": qty,
                "filled": filled,
                "average": entry_price,
                "status": str(order.get("status") or "open").lower(),
            }

            self._track_exchange_order_in_memory(
                recovered_order,
                symbol,
                direction,
                qty,
                entry_price,
                risk_params={"sl": 0.0, "tp1": 0.0, "tp2": 0.0},
                sync_required=1,
            )

            persisted = self._save_order(
                recovered_order,
                symbol,
                direction,
                qty,
                entry_price,
                risk_params={"sl": 0.0, "tp1": 0.0, "tp2": 0.0},
            )
            if persisted:
                self._mark_sync_required(symbol, f"adopted orphan open order {order_id}")
            adopted_count += 1

        if adopted_count > 0:
            logger.warning("[LIVE] Adopted %d orphan open exchange order(s) into local state.", adopted_count)
        return adopted_count

    def _passes_pre_order_checks(self, symbol: str, side: str, qty: float, price: float | None) -> bool:
        try:
            qty = float(qty or 0)
            if qty <= 0:
                logger.warning("[LIVE] Rejecting %s: qty must be > 0", symbol)
                self.last_rejection_reason = "qty must be > 0"
                return False

            ticker = self.exchange.fetch_ticker(symbol)
            bid = float(ticker.get("bid") or 0)
            ask = float(ticker.get("ask") or 0)
            last = float(ticker.get("last") or 0)
            market_price = float(price or last or ask or bid or 0)
            if market_price <= 0:
                logger.warning("[LIVE] Rejecting %s: missing market price", symbol)
                self.last_rejection_reason = "missing market price"
                return False

            notional = abs(market_price * qty)
            if notional < MIN_ORDER_NOTIONAL_USDT:
                logger.warning("[LIVE] Rejecting %s: notional %.4f < min %.4f", symbol, notional, MIN_ORDER_NOTIONAL_USDT)
                self.last_rejection_reason = f"notional {notional:.4f} < min {MIN_ORDER_NOTIONAL_USDT:.4f}"
                return False
            if notional > MAX_ORDER_NOTIONAL_USDT:
                logger.warning("[LIVE] Rejecting %s: notional %.4f > max %.4f", symbol, notional, MAX_ORDER_NOTIONAL_USDT)
                self.last_rejection_reason = f"notional {notional:.4f} > max {MAX_ORDER_NOTIONAL_USDT:.4f}"
                return False

            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / bid if bid else 0
                if spread_pct > MAX_ENTRY_SPREAD_PCT:
                    logger.warning("[LIVE] Rejecting %s: spread %.4f%% > %.4f%%", symbol, spread_pct * 100, MAX_ENTRY_SPREAD_PCT * 100)
                    self.last_rejection_reason = f"spread {spread_pct * 100:.4f}% > max {MAX_ENTRY_SPREAD_PCT * 100:.4f}%"
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
                    self.last_rejection_reason = f"slippage proxy {slippage_proxy_pct * 100:.4f}% > max {MAX_SLIPPAGE_PCT * 100:.4f}%"
                    return False

            market = self.exchange.market(symbol)
            min_amount = ((market.get("limits") or {}).get("amount") or {}).get("min")
            if min_amount is not None and qty < float(min_amount):
                logger.warning("[LIVE] Rejecting %s: qty %.8f < min amount %.8f", symbol, qty, float(min_amount))
                self.last_rejection_reason = f"qty {qty:.8f} < min amount {float(min_amount):.8f}"
                return False

            valid_side = str(side).lower() in ("buy", "sell", "long", "short")
            if not valid_side:
                logger.warning("[LIVE] Rejecting %s: invalid side '%s'", symbol, side)
                self.last_rejection_reason = f"invalid side '{side}'"
                return False

            return True
        except Exception as e:
            logger.error("[LIVE] Pre-order check error on %s: %s", symbol, e)
            self.last_rejection_reason = f"pre-order check error: {e}"
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

    @staticmethod
    def _is_retriable_exchange_error(err: Exception) -> bool:
        """Return True if the exchange error is transient and worth retrying."""
        msg = str(err).lower()
        retriable_patterns = [
            "timeout", "timed out", "network", "connection",
            "service unavailable", "503", "502", "429",
            "rate limit", "request failed", "temporary",
        ]
        return any(p in msg for p in retriable_patterns)

    def _verify_order_fill(self, symbol: str, order_id: str, timeout_seconds: float = 10.0) -> dict | None:
        """Poll exchange to verify an order was filled. Returns the order dict or None."""
        import time as _time
        deadline = _time.time() + timeout_seconds
        while _time.time() < deadline:
            try:
                fetched = self.exchange.fetch_order(order_id, symbol)
                status = str(fetched.get("status") or "").lower()
                if status == "closed":
                    return fetched
                if status in ("canceled", "cancelled", "rejected", "expired"):
                    return fetched
                _time.sleep(0.5)
            except Exception as e:
                logger.warning("[LIVE] fill verification poll error for %s: %s", symbol, e)
                _time.sleep(1.0)
        return None

    def create_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float | None = None,
        type: str = "market",
        params: dict | None = None,
        risk_params: dict | None = None,
        signal: dict | None = None,
    ) -> dict | None:
        import time as _time
        try:
            self.last_rejection_reason = None

            # ── Circuit breaker: halt after repeated order failures ──
            if self._consecutive_order_failures >= CIRCUIT_BREAKER_MAX_CONSECUTIVE_FAILURES:
                reason = (
                    f"Order circuit breaker active: {self._consecutive_order_failures} "
                    f"consecutive failures (max {CIRCUIT_BREAKER_MAX_CONSECUTIVE_FAILURES})"
                )
                logger.error("[LIVE] %s", reason)
                self.last_rejection_reason = reason
                telemetry.increment("order_circuit_breaker_trips")
                return None

            if symbol in self.positions:
                logger.warning("[LIVE] Rejecting %s: local position already open", symbol)
                self.last_rejection_reason = "local position already open"
                telemetry.increment("order_rejections")
                return None

            existing_open = self._get_open_trade_by_symbol(symbol)
            if existing_open is not None:
                self.positions[symbol] = dict(existing_open)
                logger.warning("[LIVE] Rejecting %s: DB already has open trade id=%s", symbol, existing_open.get("id"))
                self.last_rejection_reason = f"DB already has open trade id={existing_open.get('id')}"
                telemetry.increment("order_rejections")
                telemetry.increment("live_state_desync_events")
                return None

            qty = self._normalize_order_qty(symbol, qty)
            if qty <= 0:
                logger.warning("[LIVE] Rejecting %s: normalized qty is 0", symbol)
                self.last_rejection_reason = "normalized qty is 0"
                telemetry.increment("order_rejections")
                return None

            if not self._passes_pre_order_checks(symbol, side, qty, price):
                if not self.last_rejection_reason:
                    self.last_rejection_reason = "failed pre-order checks"
                telemetry.increment("order_rejections")
                return None

            ticker = self.exchange.fetch_ticker(symbol)
            market_price = self._to_float(price, self._to_float(ticker.get("last"), 0.0))
            notional = abs(market_price * qty)
            if notional < MIN_ORDER_NOTIONAL_USDT:
                logger.warning("[LIVE] Rejecting %s: normalized notional %.4f < min %.4f", symbol, notional, MIN_ORDER_NOTIONAL_USDT)
                self.last_rejection_reason = f"normalized notional {notional:.4f} < min {MIN_ORDER_NOTIONAL_USDT:.4f}"
                telemetry.increment("order_rejections")
                return None
            if notional > MAX_ORDER_NOTIONAL_USDT:
                logger.warning("[LIVE] Rejecting %s: normalized notional %.4f > max %.4f", symbol, notional, MAX_ORDER_NOTIONAL_USDT)
                self.last_rejection_reason = f"normalized notional {notional:.4f} > max {MAX_ORDER_NOTIONAL_USDT:.4f}"
                telemetry.increment("order_rejections")
                return None

            # ── Retry loop for transient exchange errors ──
            order = None
            last_err = None
            for attempt in range(1, ORDER_CREATE_MAX_RETRIES + 1):
                try:
                    order = self.exchange.create_order(symbol, type, side, qty, price, params or {})
                    break
                except Exception as create_err:
                    last_err = create_err
                    if attempt < ORDER_CREATE_MAX_RETRIES and self._is_retriable_exchange_error(create_err):
                        logger.warning(
                            "[LIVE] Order attempt %d/%d failed for %s (retriable): %s",
                            attempt, ORDER_CREATE_MAX_RETRIES, symbol, create_err,
                        )
                        telemetry.increment("order_retries")
                        _time.sleep(ORDER_RETRY_BACKOFF_SECONDS * attempt)
                    else:
                        raise

            if order is None:
                raise last_err or RuntimeError("Order creation returned None after retries")

            logger.info(f"Order placed: {order}")

            # ── Verify fill for market orders ──
            order_id = str(order.get("id") or "").strip()
            order_status = str(order.get("status") or "").lower()
            if type == "market" and order_id and order_status not in ("closed",):
                verified = self._verify_order_fill(symbol, order_id, timeout_seconds=10.0)
                if verified:
                    order = verified
                    logger.info("[LIVE] Fill verified for %s order %s: status=%s", symbol, order_id, verified.get("status"))
                    telemetry.increment("order_fills_verified")
                else:
                    logger.warning("[LIVE] Fill verification timed out for %s order %s", symbol, order_id)
                    telemetry.increment("order_fill_verification_timeouts")

            self._track_realized_slippage(symbol, side, market_price, order)
            saved_trade_id = self._save_order(order, symbol, side, qty, price, risk_params=risk_params)
            if not saved_trade_id:
                logger.error(
                    "[LIVE] Order placed on exchange but local persistence failed for %s. Marking sync_required in memory.",
                    symbol,
                )
                self._track_exchange_order_in_memory(order, symbol, side, qty, price, risk_params=risk_params)
                telemetry.increment("live_state_desync_events")
            elif signal:
                try:
                    self._perf_tracker.record_entry(str(saved_trade_id), signal)
                except Exception:
                    pass
            telemetry.increment("orders_opened")
            self._consecutive_order_failures = 0  # Reset on success
            return order
        except Exception as e:
            logger.error(f"Order error: {e}")
            self.last_rejection_reason = f"order error: {e}"
            telemetry.increment("api_errors")
            self._consecutive_order_failures += 1
            if self._consecutive_order_failures >= CIRCUIT_BREAKER_MAX_CONSECUTIVE_FAILURES:
                telemetry.increment("order_circuit_breaker_trips")
                logger.error(
                    "[LIVE] Order circuit breaker TRIPPED: %d consecutive failures",
                    self._consecutive_order_failures,
                )
            return None

    def reset_order_circuit_breaker(self) -> None:
        """Manually reset the order circuit breaker after investigation."""
        prev = self._consecutive_order_failures
        self._consecutive_order_failures = 0
        logger.info("[LIVE] Order circuit breaker reset (was %d failures)", prev)

    def cancel_order(self, symbol: str, order_id: str) -> dict | None:
        try:
            result = self.exchange.cancel_order(order_id, symbol)
            logger.info(f"Order cancelled: {order_id}")
            return result
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            telemetry.increment("api_errors")
            return None

    def _save_order(self, order, symbol, side, qty, price, risk_params=None) -> str | None:
        risk_params = risk_params or {}
        raw_order_id = order.get("id")
        order_id = str(raw_order_id).strip() if raw_order_id is not None else ""
        order_id = order_id or None
        existing_trade_id = self._find_existing_open_trade_id(symbol, order_id)
        trade_id = existing_trade_id or order_id or f"local-{uuid.uuid4().hex[:12]}"
        order_amount = self._to_float(order.get("amount"), self._to_float(qty))
        order_filled = self._to_float(order.get("filled"), 0.0)
        avg_price = self._to_float(order.get("average"), 0.0)
        status = str(order.get("status") or "open").lower()
        entry_filled = int(status == "closed" and order_filled > 0)
        entry_price = avg_price if avg_price > 0 else self._to_float(price, 0.0)
        live_qty = order_filled if order_filled > 0 else order_amount

        try:
            with self._db_connect() as conn:
                conn.execute(
                    """
                    INSERT INTO live_trades
                    (id, symbol, direction, entry_price, qty, stop_loss, tp1, tp2, tp1_hit, entry_filled, status, order_id, sync_required, open_time, pnl, fee)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        symbol=excluded.symbol,
                        direction=excluded.direction,
                        entry_price=excluded.entry_price,
                        qty=excluded.qty,
                        stop_loss=excluded.stop_loss,
                        tp1=excluded.tp1,
                        tp2=excluded.tp2,
                        tp1_hit=excluded.tp1_hit,
                        entry_filled=excluded.entry_filled,
                        status=excluded.status,
                        order_id=excluded.order_id,
                        sync_required=excluded.sync_required,
                        pnl=excluded.pnl,
                        fee=excluded.fee
                    """,
                    (
                        trade_id,
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
                        order_id,
                        0,
                        datetime.now(timezone.utc).isoformat(),
                        0,
                        0,
                    ),
                )
        except Exception as db_err:
            logger.error("[LIVE] Save order failed for %s: %s", symbol, db_err)
            telemetry.increment("api_errors")
            return None

        self._track_exchange_order_in_memory(order, symbol, side, qty, price, risk_params=risk_params, sync_required=0)
        return trade_id

    def _track_exchange_order_in_memory(self, order, symbol, side, qty, price, risk_params=None, sync_required: int = 1):
        risk_params = risk_params or {}
        raw_order_id = order.get("id")
        order_id = str(raw_order_id).strip() if raw_order_id is not None else ""
        order_id = order_id or None
        trade_id = order_id or f"local-{uuid.uuid4().hex[:12]}"
        order_amount = self._to_float(order.get("amount"), self._to_float(qty))
        order_filled = self._to_float(order.get("filled"), 0.0)
        avg_price = self._to_float(order.get("average"), 0.0)
        status = str(order.get("status") or "open").lower()
        entry_filled = int(status == "closed" and order_filled > 0)
        entry_price = avg_price if avg_price > 0 else self._to_float(price, 0.0)
        live_qty = order_filled if order_filled > 0 else order_amount
        sl_val = self._to_float(risk_params.get("sl"), 0.0)

        self.positions[symbol] = {
            "id": trade_id,
            "symbol": symbol,
            "direction": side,
            "entry_price": entry_price,
            "qty": live_qty,
            "stop_loss": sl_val,
            "tp1": self._to_float(risk_params.get("tp1"), 0.0),
            "tp2": self._to_float(risk_params.get("tp2"), 0.0),
            "tp1_hit": 0,
            "entry_filled": entry_filled,
            "status": "open",
            "order_id": order_id,
            "sync_required": int(sync_required),
            "open_time": datetime.now(timezone.utc).isoformat(),
            "pnl": 0,
            "fee": 0,
        }

        # Initialize trailing stop for the new position
        if entry_price > 0 and sl_val > 0:
            direction = "long" if str(side).lower() in {"buy", "long"} else "short"
            self._trailing_stops[symbol] = TrailingStopLoss(entry_price, sl_val, direction)

    def _mark_sync_required(self, symbol: str, reason: str = "") -> None:
        position = self.positions.get(symbol)
        if position:
            position["sync_required"] = 1

        trade_id = str((position or {}).get("id") or "").strip()
        if trade_id:
            _ = self._update_live_trade_row(
                "UPDATE live_trades SET sync_required=1 WHERE id=? AND status='open'",
                (trade_id,),
            )
        else:
            _ = self._update_live_trade_row(
                "UPDATE live_trades SET sync_required=1 WHERE symbol=? AND status='open'",
                (symbol,),
            )
        if reason:
            logger.error("[LIVE] sync_required for %s: %s", symbol, reason)
        telemetry.increment("live_state_desync_events")

    def _clear_sync_required(self, symbol: str) -> None:
        position = self.positions.get(symbol)
        if position:
            position["sync_required"] = 0

        trade_id = str((position or {}).get("id") or "").strip()
        if trade_id:
            _ = self._update_live_trade_row(
                "UPDATE live_trades SET sync_required=0 WHERE id=? AND status='open'",
                (trade_id,),
            )
        else:
            _ = self._update_live_trade_row(
                "UPDATE live_trades SET sync_required=0 WHERE symbol=? AND status='open'",
                (symbol,),
            )

    @staticmethod
    def _to_float(value, default=0.0):
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def _mark_order_terminal(self, order_id: str, status: str):
        if not order_id:
            return
        _ = self._update_live_trade_row(
            "UPDATE live_trades SET status=?, close_time=?, sync_required=0 WHERE order_id=? AND status='open'",
            (status, datetime.now(timezone.utc).isoformat(), order_id),
        )

    def _update_live_trade_row(self, query: str, params: tuple) -> bool:
        try:
            with self._db_connect() as conn:
                conn.execute(query, params)
            return True
        except Exception as db_err:
            logger.error("[LIVE] DB update failed: %s", db_err)
            telemetry.increment("api_errors")
            return False

    def _fetch_open_orders_all(self):
        """Fetch all open orders in one call when supported to reduce API weight."""
        try:
            open_orders = self.exchange.fetch_open_orders() or []
            if isinstance(open_orders, list):
                return open_orders
        except TypeError:
            # Some ccxt wrappers require a symbol positional arg.
            pass
        except Exception as e:
            logger.warning("[LIVE] fetch_open_orders(all) failed, falling back per symbol: %s", e)

        # Fallback: per-symbol fetches for compatibility.
        collected = []
        seen = set()
        for symbol in list(self.positions.keys()):
            try:
                orders = self.exchange.fetch_open_orders(symbol) or []
            except Exception as sym_err:
                logger.warning("[LIVE] fetch_open_orders(%s) failed: %s", symbol, sym_err)
                continue
            for order in orders:
                order_id = str(order.get("id") or "")
                dedupe_key = (order_id, str(order.get("symbol") or symbol))
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                collected.append(order)
        return collected

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
            updated = self._update_live_trade_row(
                "UPDATE live_trades SET qty=?, entry_price=CASE WHEN ? > 0 THEN ? ELSE entry_price END, fee=COALESCE(fee, 0) + ?, sync_required=0 WHERE order_id=? AND status='open'",
                (filled, average, average, fee_cost, order_id),
            )
            if not updated:
                self._mark_sync_required(symbol, "partial fill update failed")

            pos = self.positions.get(symbol)
            if pos:
                pos["qty"] = filled
                if average > 0:
                    pos["entry_price"] = average
                if updated:
                    pos["sync_required"] = 0

        if status in {"closed", "canceled", "cancelled", "rejected", "expired"}:
            if status == "closed" and filled > 0:
                updated = self._update_live_trade_row(
                    "UPDATE live_trades SET entry_filled=1, qty=?, order_id=NULL, sync_required=0 WHERE order_id=? AND status='open'",
                    (filled, order_id),
                )
                if not updated:
                    self._mark_sync_required(symbol, "entry fill finalize failed")
                pos = self.positions.get(symbol)
                if pos:
                    pos["entry_filled"] = 1
                    pos["qty"] = filled
                    pos["order_id"] = None
                    if updated:
                        pos["sync_required"] = 0
            else:
                self._mark_order_terminal(order_id, "cancelled")
                if symbol in self.positions:
                    del self.positions[symbol]

        if amount > 0 and 0 < filled < amount and status in {"open", "partially_filled"}:
            logger.info("[LIVE] Partial fill %s: %.8f / %.8f", order_id, filled, amount)

    def reconcile_orders(self):
        """Sync local DB open orders with exchange order state."""
        try:
            all_open_orders = self._fetch_open_orders_all()
            open_orders_by_symbol = {}
            for order in all_open_orders:
                order_symbol = order.get("symbol")
                if not order_symbol:
                    continue
                open_orders_by_symbol.setdefault(order_symbol, []).append(order)

            for symbol, local in list(self.positions.items()):
                open_orders = open_orders_by_symbol.get(symbol, [])
                remote_ids = {str(o.get("id")) for o in open_orders if o.get("id") is not None}

                for order in open_orders:
                    self.handle_partial_fills(order)

                local_order_id = str(local.get("order_id") or "")
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
                    if fetched_status in {"open", "partially_filled"}:
                        self._mark_sync_required(
                            symbol,
                            f"order {local_order_id} missing from open list but fetch_order is non-terminal ({fetched_status})",
                        )
                        continue
                except Exception:
                    logger.warning("[LIVE] Could not fetch terminal state for %s %s", symbol, local_order_id)
                    self._mark_sync_required(symbol, f"unable to fetch terminal state for order {local_order_id}")
                    continue

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
        trade_id = str(position.get("id") or "").strip()

        if trade_id:
            updated = self._update_live_trade_row(
                "UPDATE live_trades SET qty=?, tp1_hit=1, stop_loss=entry_price, sync_required=0 WHERE id=? AND status='open'",
                (remaining_qty, trade_id),
            )
        else:
            updated = self._update_live_trade_row(
                "UPDATE live_trades SET qty=?, tp1_hit=1, stop_loss=entry_price, sync_required=0 WHERE symbol=? AND status='open'",
                (remaining_qty, symbol),
            )

        if not updated:
            self._mark_sync_required(symbol, "partial TP DB sync failed after exchange exit")
        else:
            self._clear_sync_required(symbol)

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
        trade_id = str(position.get("id") or "").strip()

        if trade_id:
            updated = self._update_live_trade_row(
                "UPDATE live_trades SET status='closed', close_time=?, pnl=?, order_id=NULL, sync_required=0 WHERE id=? AND status='open'",
                (datetime.now(timezone.utc).isoformat(), pnl, trade_id),
            )
        else:
            updated = self._update_live_trade_row(
                "UPDATE live_trades SET status='closed', close_time=?, pnl=?, order_id=NULL, sync_required=0 WHERE symbol=? AND status='open'",
                (datetime.now(timezone.utc).isoformat(), pnl, symbol),
            )

        if not updated:
            self._mark_sync_required(symbol, "close DB sync failed after exchange exit")

        try:
            self._perf_tracker.record_outcome(trade_id or symbol, pnl)
        except Exception:
            pass

        if symbol in self.positions:
            del self.positions[symbol]
        telemetry.increment("orders_closed")
        logger.info("[LIVE] Position closed (%s): %s @ %.6f | qty=%.6f | pnl=%.4f", reason, symbol, price, qty, pnl)

    def monitor_positions(self):
        """Apply live TP/SL lifecycle to filled live positions, including trailing stop."""
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

                # ── Trailing Stop Update ─────────────────────────────────
                trailing = self._trailing_stops.get(symbol)
                if trailing is not None:
                    prev_sl = trailing.current_sl
                    new_sl = trailing.update(price)
                    if new_sl != prev_sl:
                        pos["stop_loss"] = new_sl
                        sl = new_sl
                        trade_id = str(pos.get("id") or "").strip()
                        if trade_id:
                            self._update_live_trade_row(
                                "UPDATE live_trades SET stop_loss=? WHERE id=? AND status='open'",
                                (new_sl, trade_id),
                            )
                        else:
                            self._update_live_trade_row(
                                "UPDATE live_trades SET stop_loss=? WHERE symbol=? AND status='open'",
                                (new_sl, symbol),
                            )
                        logger.debug("[LIVE] Trailing SL for %s: %.6f → %.6f", symbol, prev_sl, new_sl)

                if not tp1_hit and tp1 > 0:
                    hit_tp1 = (is_long and price >= tp1) or ((not is_long) and price <= tp1)
                    half_qty = round(qty / 2.0, 8)
                    if hit_tp1 and half_qty > 0 and (half_qty * price) >= MIN_ORDER_NOTIONAL_USDT:
                        self._apply_partial_tp(symbol, price, pos, half_qty)
                        qty = self._to_float(pos.get("qty"), qty)
                        sl = self._to_float(pos.get("stop_loss"), sl)
                        # After TP1, move trailing SL to breakeven
                        if trailing is not None:
                            trailing.current_sl = entry

                hit_tp2 = tp2 > 0 and ((is_long and price >= tp2) or ((not is_long) and price <= tp2))
                hit_sl = sl > 0 and ((is_long and price <= sl) or ((not is_long) and price >= sl))

                if hit_tp2:
                    self._close_live_trade(symbol, price, pos, reason="tp2")
                    self._trailing_stops.pop(symbol, None)
                elif hit_sl:
                    self._close_live_trade(symbol, price, pos, reason="stop_loss")
                    self._trailing_stops.pop(symbol, None)
            except Exception as e:
                logger.error("[LIVE] monitor_positions error on %s: %s", symbol, e)
                telemetry.increment("api_errors")

    def close_position(self, symbol, exit_price, pnl, fee):
        position = self.positions.get(symbol) or {}
        trade_id = str(position.get("id") or "").strip()
        if trade_id:
            _ = self._update_live_trade_row(
                "UPDATE live_trades SET status='closed', close_time=?, pnl=?, fee=?, sync_required=0 WHERE id=? AND status='open'",
                (datetime.now(timezone.utc).isoformat(), pnl, fee, trade_id),
            )
        else:
            _ = self._update_live_trade_row(
                "UPDATE live_trades SET status='closed', close_time=?, pnl=?, fee=?, sync_required=0 WHERE symbol=? AND status='open'",
                (datetime.now(timezone.utc).isoformat(), pnl, fee, symbol),
            )
        if symbol in self.positions:
            del self.positions[symbol]
        try:
            self._perf_tracker.record_outcome(trade_id or symbol, pnl)
        except Exception:
            pass
        telemetry.increment("orders_closed")
