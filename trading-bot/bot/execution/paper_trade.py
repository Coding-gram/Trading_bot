"""
Paper Trading Engine – simulates trade execution without real money.
Tracks virtual positions, PnL, and order history.
"""
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from bot.execution.schema_migrations import apply_paper_trades_migrations
from bot.config import (
    DB_PATH,
    MIN_ORDER_NOTIONAL_USDT,
    MAX_ORDER_NOTIONAL_USDT,
    MAX_SIGNAL_STALENESS_SECONDS,
    MAX_SIMULTANEOUS_TRADES,
    PAPER_SLIPPAGE_PCT,
    PAPER_FEE_PCT,
)
from bot.risk.risk_manager import TrailingStopLoss
from bot import telemetry
from bot.strategy.performance_tracker import PerformanceTracker

logger = logging.getLogger(__name__)


class PaperTradeEngine:
    """Simulates trade execution without real money."""

    def __init__(self, starting_capital: float = 1000.0):
        self.capital = starting_capital
        self.start_cap = starting_capital
        self.positions = {}   # {symbol: trade_dict}
        self.history = []
        self.last_rejection_reason: str | None = None
        self._perf_tracker = PerformanceTracker()
        self._init_db()
        self._restore_capital_from_db()
        self._load_open_positions()

    @staticmethod
    def _db_connect(row_factory=None):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        if row_factory is not None:
            conn.row_factory = row_factory
        return conn

    def _reject_open(self, symbol: str, reason: str) -> dict:
        self.last_rejection_reason = reason
        logger.warning("[PAPER] Rejecting %s: %s", symbol, reason)
        telemetry.increment("order_rejections")
        return {}

    def _restore_capital_from_db(self):
        """Adjust capital by adding realized PnL from previous sessions."""
        with self._db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status LIKE 'closed%'")
            realized_pnl = c.fetchone()[0] or 0.0
        self.capital = self.start_cap + realized_pnl
        if realized_pnl != 0:
            logger.info("Restored capital: $%.2f (realized PnL from DB: $%+.2f)", self.capital, realized_pnl)

    def _load_open_positions(self):
        """Reload open positions from DB into memory after a restart."""
        with self._db_connect(sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE status = 'open'"
            ).fetchall()

        for row in rows:
            r = dict(row)
            symbol = r["symbol"]
            entry = r["entry_price"] or 0.0
            sl = r["stop_loss"] or entry
            trade = {
                "id":         r["id"],
                "symbol":     symbol,
                "direction":  r["direction"],
                "entry":      entry,
                "qty":        r["qty"] or 0.0,
                "sl":         sl,
                "tp1":        r["tp1"] or 0.0,
                "tp2":        r["tp2"] or 0.0,
                "opened_at":  r["entry_time"] or datetime.now(timezone.utc).isoformat(),
                "status":     "open",
                "tp1_hit":    bool(int(r.get("tp1_hit") or 0)),
                "realized_pnl": float(r.get("realized_pnl") or 0.0),
                "trailing":   TrailingStopLoss(entry, sl, r["direction"]),
                "score":      self._normalize_score(r.get("score")),
                "half_qty":   round((r["qty"] or 0.0) / 2, 6),
            }
            self.positions[symbol] = trade
            logger.info("Restored open position: %s %s @ %.4f", r["direction"].upper(), symbol, entry)

        if self.positions:
            logger.info("Restored %d open position(s) from DB.", len(self.positions))

    def _init_db(self):
        with self._db_connect() as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id TEXT PRIMARY KEY,
                    symbol TEXT, direction TEXT,
                    entry_price REAL, qty REAL,
                    stop_loss REAL, tp1 REAL, tp2 REAL,
                    tp1_hit INTEGER DEFAULT 0,
                    realized_pnl REAL DEFAULT 0,
                    entry_time TEXT, exit_time TEXT,
                    exit_price REAL, pnl REAL, status TEXT,
                    score INTEGER, reason TEXT
                )
            """)
            apply_paper_trades_migrations(conn)

    def open_position(self, signal: dict, risk_params: dict) -> dict:
        """Open a simulated position based on signal + risk params."""
        symbol = signal["symbol"]
        self.last_rejection_reason = None
        direction = signal["direction"]
        quoted_entry = float(signal["price"])
        entry = self._apply_slippage(quoted_entry, direction, is_entry=True)

        qty = float(risk_params.get("qty", 0) or 0)
        if qty <= 0:
            return self._reject_open(symbol, "qty must be > 0")

        if direction not in ("long", "short"):
            return self._reject_open(symbol, f"invalid direction '{direction}'")

        sl = float(risk_params.get("sl", 0) or 0)
        tp1 = float(risk_params.get("tp1", 0) or 0)
        tp2 = float(risk_params.get("tp2", 0) or 0)

        if direction == "long" and not (sl < entry < tp1 <= tp2):
            return self._reject_open(symbol, "invalid long SL/TP layout")
        if direction == "short" and not (sl > entry > tp1 >= tp2):
            return self._reject_open(symbol, "invalid short SL/TP layout")

        notional = abs(entry * qty)
        if notional < MIN_ORDER_NOTIONAL_USDT:
            return self._reject_open(symbol, f"notional {notional:.4f} < min {MIN_ORDER_NOTIONAL_USDT:.4f}")
        if notional > MAX_ORDER_NOTIONAL_USDT:
            return self._reject_open(symbol, f"notional {notional:.4f} > max {MAX_ORDER_NOTIONAL_USDT:.4f}")

        generated_at = signal.get("generated_at")
        if generated_at:
            try:
                gen_dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
                if gen_dt.tzinfo is None:
                    gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                age_s = (datetime.now(timezone.utc) - gen_dt).total_seconds()
                if age_s > MAX_SIGNAL_STALENESS_SECONDS:
                    return self._reject_open(symbol, f"stale signal {age_s:.1f}s old")
            except Exception:
                return self._reject_open(symbol, "invalid generated_at on signal")

        if symbol in self.positions:
            return self._reject_open(symbol, "already have open position")

        if len(self.positions) >= MAX_SIMULTANEOUS_TRADES:
            return self._reject_open(symbol, "max simultaneous trades reached")

        trade = {
            "id": str(uuid.uuid4())[:8],
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "quoted_entry": quoted_entry,
            "qty": risk_params["qty"],
            "sl": risk_params["sl"],
            "tp1": risk_params["tp1"],
            "tp2": risk_params["tp2"],
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "tp1_hit": False,
            "realized_pnl": 0.0,
            "trailing": TrailingStopLoss(entry, risk_params["sl"], direction),
            "score": signal["score"],
            "half_qty": round(risk_params["qty"] / 2, 6),
        }

        self.positions[symbol] = trade
        self._save_trade(trade, pnl=0, exit_price=None, reason="opened")
        telemetry.increment("orders_opened")
        # Record indicator snapshot for performance feedback
        try:
            self._perf_tracker.record_entry(trade["id"], signal)
        except Exception:
            pass
        logger.info(
            "[PAPER] OPENED %s %s @ %s | SL=%s TP1=%s TP2=%s",
            direction.upper(), symbol, entry, risk_params['sl'], risk_params['tp1'], risk_params['tp2']
        )
        return trade

    def update_positions(self, prices: dict) -> list:
        """
        Call every tick. Checks SL/TP for each open position.
        prices = {symbol: current_price}
        Returns list of closed trade dicts.
        """
        closed = []
        for symbol, trade in list(self.positions.items()):
            price = prices.get(symbol)
            if price is None:
                continue

            direction = trade["direction"]
            prev_sl = float(trade.get("sl") or 0.0)
            prev_tp1_hit = bool(trade.get("tp1_hit", False))
            sl_price = trade["trailing"].update(price)
            trade["sl"] = sl_price
            if abs(sl_price - prev_sl) > 1e-12:
                self._persist_open_trade_state(trade)

            # ── Time-Based Exit (5 days) ────────────────────────────────────
            time_open = datetime.now(timezone.utc) - datetime.fromisoformat(trade["opened_at"])
            if time_open > timedelta(days=5):
                remaining_qty = trade["half_qty"] if trade["tp1_hit"] else trade["qty"]
                exit_price = self._apply_slippage(price, direction, is_entry=False)
                pnl_leg = self._net_pnl(trade["entry"], exit_price, remaining_qty, direction)
                total_pnl = float(trade.get("realized_pnl", 0.0)) + pnl_leg
                self.capital += pnl_leg
                trade["status"] = "closed_time"
                self._save_trade(trade, total_pnl, exit_price, reason="time_limit")
                telemetry.increment("orders_closed")
                try:
                    self._perf_tracker.record_outcome(trade["id"], total_pnl)
                except Exception:
                    pass
                closed.append({**trade, "exit_price": exit_price, "pnl": total_pnl})
                del self.positions[symbol]
                logger.info("[PAPER] TIME EXIT %s @ %s | Time: %s | Net PnL: $%s", symbol, exit_price, time_open, f"{total_pnl:.2f}")
                continue

            # ── TP1 partial exit ────────────────────────────────────────────
            if not trade["tp1_hit"]:
                hit_tp1 = (direction == "long" and price >= trade["tp1"]) or \
                          (direction == "short" and price <= trade["tp1"])
                if hit_tp1:
                    exit_price_tp1 = self._apply_slippage(price, direction, is_entry=False)
                    pnl_tp1 = self._net_pnl(trade["entry"], exit_price_tp1, trade["half_qty"], direction)
                    self.capital += pnl_tp1
                    trade["tp1_hit"] = True
                    trade["realized_pnl"] = float(trade.get("realized_pnl", 0.0)) + float(pnl_tp1)
                    logger.info("[PAPER] TP1 HIT %s @ %s | Net PnL: $%s", symbol, exit_price_tp1, f"{pnl_tp1:.2f}")
                    # Move SL to breakeven after TP1 hit
                    trade["trailing"].current_sl = trade["entry"]
                    trade["sl"] = trade["entry"]
                    if (not prev_tp1_hit) or abs(float(trade.get("sl") or 0.0) - prev_sl) > 1e-12:
                        self._persist_open_trade_state(trade)

            # ── TP2 full exit ───────────────────────────────────────────────
            hit_tp2 = (direction == "long" and price >= trade["tp2"]) or \
                      (direction == "short" and price <= trade["tp2"])
            if hit_tp2:
                remaining_qty = trade["half_qty"] if trade["tp1_hit"] else trade["qty"]
                exit_price = self._apply_slippage(price, direction, is_entry=False)
                pnl_leg = self._net_pnl(trade["entry"], exit_price, remaining_qty, direction)
                total_pnl = float(trade.get("realized_pnl", 0.0)) + pnl_leg
                self.capital += pnl_leg
                trade["status"] = "closed_tp2"
                self._save_trade(trade, total_pnl, exit_price, reason="tp2")
                telemetry.increment("orders_closed")
                try:
                    self._perf_tracker.record_outcome(trade["id"], total_pnl)
                except Exception:
                    pass
                closed.append({**trade, "exit_price": exit_price, "pnl": total_pnl})
                del self.positions[symbol]
                logger.info("[PAPER] TP2 HIT %s @ %s | Net PnL: $%s", symbol, exit_price, f"{total_pnl:.2f}")
                continue

            # ── Stop Loss hit ───────────────────────────────────────────────
            hit_sl = (direction == "long" and price <= sl_price) or \
                     (direction == "short" and price >= sl_price)
            if hit_sl:
                remaining_qty = trade["half_qty"] if trade["tp1_hit"] else trade["qty"]
                exit_price = self._apply_slippage(price, direction, is_entry=False)
                pnl_leg = self._net_pnl(trade["entry"], exit_price, remaining_qty, direction)
                total_pnl = float(trade.get("realized_pnl", 0.0)) + pnl_leg
                self.capital += pnl_leg
                trade["status"] = "closed_sl"
                self._save_trade(trade, total_pnl, exit_price, reason="stop_loss")
                telemetry.increment("orders_closed")
                try:
                    self._perf_tracker.record_outcome(trade["id"], total_pnl)
                except Exception:
                    pass
                closed.append({**trade, "exit_price": exit_price, "pnl": total_pnl})
                del self.positions[symbol]
                logger.info("[PAPER] SL HIT %s @ %s | Net PnL: $%s", symbol, exit_price, f"{total_pnl:.2f}")

        return closed

    def _calc_pnl(self, entry, exit_price, qty, direction):
        if direction == "long":
            return (exit_price - entry) * qty
        return (entry - exit_price) * qty

    @staticmethod
    def _normalize_score(value) -> int:
        if value is None:
            return 0
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            if len(raw) in (1, 2, 4, 8):
                return int.from_bytes(raw, byteorder="little", signed=False)
            try:
                return int(raw.decode("utf-8").strip())
            except Exception:
                return 0
        try:
            return int(value)
        except Exception:
            return 0

    def _apply_slippage(self, quoted_price: float, direction: str, is_entry: bool) -> float:
        price = float(quoted_price or 0.0)
        slip = max(float(PAPER_SLIPPAGE_PCT or 0.0), 0.0)
        is_long = direction == "long"

        if is_entry:
            adjusted = price * (1 + slip) if is_long else price * (1 - slip)
        else:
            adjusted = price * (1 - slip) if is_long else price * (1 + slip)
        return float(adjusted)

    def _trade_fee(self, entry_price: float, exit_price: float, qty: float) -> float:
        fee_pct = max(float(PAPER_FEE_PCT or 0.0), 0.0)
        if qty <= 0:
            return 0.0
        return (abs(entry_price) * qty + abs(exit_price) * qty) * fee_pct

    def _net_pnl(self, entry: float, exit_price: float, qty: float, direction: str) -> float:
        gross = self._calc_pnl(entry, exit_price, qty, direction)
        fees = self._trade_fee(entry, exit_price, qty)
        return gross - fees

    def get_stats(self) -> dict:
        with self._db_connect() as conn:
            c = conn.cursor()
            c.execute("SELECT pnl, status, entry_price, stop_loss, tp1, tp2, entry_time, exit_time FROM paper_trades WHERE status LIKE 'closed%'")
            rows = c.fetchall()

        if not rows:
            return {
                "total": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "capital": round(self.capital, 2),
                "return_pct": round((self.capital - self.start_cap) / self.start_cap * 100, 2) if self.start_cap else 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "avg_rr": 0.0,
                "open_positions": len(self.positions),
                "profit_factor": 0.0,
                "sharpe_ratio": 0.0,
                "max_consecutive_losses": 0,
                "avg_holding_hours": 0.0,
            }

        pnl_values = [r[0] for r in rows]
        wins = [r for r in rows if r[0] > 0]
        losses = [r for r in rows if r[0] <= 0]
        total_pnl = sum(pnl_values)
        win_rate = len(wins) / len(rows) if rows else 0
        best_trade = max(pnl_values) if pnl_values else 0.0
        worst_trade = min(pnl_values) if pnl_values else 0.0

        # Average reward-to-risk ratio for closed trades
        rr_values = []
        for r in rows:
            pnl, status, entry, sl, tp1, tp2 = r[:6]
            risk = abs(entry - sl) if entry and sl and abs(entry - sl) > 0 else None
            if risk and risk > 0 and pnl is not None:
                rr_values.append(pnl / risk if entry else 0.0)
        avg_rr = round(sum(rr_values) / len(rr_values), 2) if rr_values else 0.0

        # Profit factor: gross wins / abs(gross losses)
        gross_wins = sum(p for p in pnl_values if p > 0)
        gross_losses = abs(sum(p for p in pnl_values if p <= 0))
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0.0)

        # Sharpe ratio (simplified: mean PnL / std PnL)
        import statistics
        sharpe_ratio = 0.0
        if len(pnl_values) >= 2:
            std_pnl = statistics.stdev(pnl_values)
            if std_pnl > 0:
                sharpe_ratio = round(statistics.mean(pnl_values) / std_pnl, 2)

        # Max consecutive losses
        max_consecutive_losses = 0
        current_streak = 0
        for pnl in pnl_values:
            if pnl <= 0:
                current_streak += 1
                max_consecutive_losses = max(max_consecutive_losses, current_streak)
            else:
                current_streak = 0

        # Average holding duration in hours
        holding_hours = []
        for r in rows:
            entry_time_str = r[6]  # entry_time
            exit_time_str = r[7]   # exit_time
            if entry_time_str and exit_time_str:
                try:
                    entry_dt = datetime.fromisoformat(str(entry_time_str).replace("Z", "+00:00"))
                    exit_dt = datetime.fromisoformat(str(exit_time_str).replace("Z", "+00:00"))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    if exit_dt.tzinfo is None:
                        exit_dt = exit_dt.replace(tzinfo=timezone.utc)
                    hours = (exit_dt - entry_dt).total_seconds() / 3600.0
                    if hours >= 0:
                        holding_hours.append(hours)
                except Exception:
                    pass
        avg_holding_hours = round(sum(holding_hours) / len(holding_hours), 1) if holding_hours else 0.0

        return {
            "total": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "capital": round(self.capital, 2),
            "return_pct": round((self.capital - self.start_cap) / self.start_cap * 100, 2),
            "best_trade": round(best_trade, 2),
            "worst_trade": round(worst_trade, 2),
            "avg_rr": avg_rr,
            "open_positions": len(self.positions),
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe_ratio,
            "max_consecutive_losses": max_consecutive_losses,
            "avg_holding_hours": avg_holding_hours,
        }

    def _save_trade(self, trade, pnl, exit_price, reason):
        with self._db_connect() as conn:
            c = conn.cursor()
            score = self._normalize_score(trade.get("score", 0))
            c.execute("""
                INSERT OR REPLACE INTO paper_trades
                (id, symbol, direction, entry_price, qty, stop_loss, tp1, tp2, tp1_hit, realized_pnl,
                 entry_time, exit_time, exit_price, pnl, status, score, reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade["id"], trade["symbol"], trade["direction"],
                trade["entry"], trade["qty"], trade["sl"],
                trade["tp1"], trade["tp2"], int(bool(trade.get("tp1_hit", False))), float(trade.get("realized_pnl", 0.0)), trade["opened_at"],
                datetime.now(timezone.utc).isoformat() if exit_price else None,
                exit_price, pnl, trade["status"], score, reason
            ))

    def _persist_open_trade_state(self, trade) -> None:
        with self._db_connect() as conn:
            c = conn.cursor()
            c.execute(
                """
                UPDATE paper_trades
                SET stop_loss=?, tp1_hit=?, realized_pnl=?
                WHERE id=? AND status='open'
                """,
                (
                    float(trade.get("sl") or 0.0),
                    int(bool(trade.get("tp1_hit", False))),
                    float(trade.get("realized_pnl", 0.0)),
                    trade.get("id"),
                ),
            )

    def _parse_opened_at(self, opened_at: str) -> datetime:
        dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _candle_hits_level(direction: str, level: float, high: float, low: float) -> bool:
        if level <= 0:
            return False
        if direction == "long":
            return high >= level
        return low <= level

    @staticmethod
    def _candle_hits_stop(direction: str, stop_loss: float, high: float, low: float) -> bool:
        if stop_loss <= 0:
            return False
        if direction == "long":
            return low <= stop_loss
        return high >= stop_loss

    def _fetch_ohlcv_since(self, exchange, symbol: str, timeframe: str, since_ms: int, max_rows: int = 5000):
        rows = []
        cursor = int(since_ms)
        while len(rows) < max_rows:
            batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            last_ts = int(batch[-1][0])
            if len(batch) < 1000 or last_ts <= cursor:
                break
            cursor = last_ts + 1
        dedup = {}
        for c in rows:
            dedup[int(c[0])] = c
        return [dedup[k] for k in sorted(dedup.keys())][:max_rows]

    def reconcile_open_positions_with_market(self, exchange, timeframe: str = "5m") -> dict:
        """Reconcile open paper positions against market candles after downtime.

        Applies missed TP/SL outcomes so DB/dashboard state is accurate on restart.
        """
        report = {
            "checked": 0,
            "closed_tp2": 0,
            "closed_sl": 0,
            "tp1_marked": 0,
            "errors": 0,
        }

        for symbol, trade in list(self.positions.items()):
            report["checked"] += 1
            try:
                opened_at_dt = self._parse_opened_at(trade.get("opened_at"))
                since_ms = int(opened_at_dt.timestamp() * 1000)
                candles = self._fetch_ohlcv_since(exchange, symbol, timeframe, since_ms)
                if not candles:
                    continue

                direction = trade["direction"]
                tp1_hit = bool(trade.get("tp1_hit", False))
                sl = float(trade.get("sl") or 0.0)
                tp1 = float(trade.get("tp1") or 0.0)
                tp2 = float(trade.get("tp2") or 0.0)

                close_reason = None
                close_level = None

                for c in candles:
                    _, _o, high, low, close, _v = c

                    sl_price = trade["trailing"].update(float(close))
                    if abs(float(sl_price) - float(trade.get("sl") or 0.0)) > 1e-12:
                        trade["sl"] = float(sl_price)
                        self._persist_open_trade_state(trade)
                    sl = float(trade.get("sl") or 0.0)

                    if not tp1_hit and self._candle_hits_level(direction, tp1, float(high), float(low)):
                        tp1_hit = True
                        trade["tp1_hit"] = True
                        trade["trailing"].current_sl = trade["entry"]
                        trade["sl"] = float(trade["entry"])
                        sl = float(trade["sl"])
                        self._persist_open_trade_state(trade)
                        report["tp1_marked"] += 1

                    tp2_touched = self._candle_hits_level(direction, tp2, float(high), float(low))
                    sl_touched = self._candle_hits_stop(direction, sl, float(high), float(low))

                    if tp2_touched:
                        close_reason = "tp2"
                        close_level = tp2
                        break
                    if sl_touched:
                        close_reason = "stop_loss"
                        close_level = sl
                        break

                if close_reason:
                    remaining_qty = trade["half_qty"] if trade.get("tp1_hit") else trade["qty"]
                    exit_price = self._apply_slippage(float(close_level), direction, is_entry=False)
                    pnl = self._net_pnl(trade["entry"], exit_price, remaining_qty, direction)
                    self.capital += pnl
                    trade["status"] = "closed_tp2" if close_reason == "tp2" else "closed_sl"
                    self._save_trade(trade, pnl, exit_price, reason=close_reason)
                    if close_reason == "tp2":
                        report["closed_tp2"] += 1
                    else:
                        report["closed_sl"] += 1
                    if symbol in self.positions:
                        del self.positions[symbol]
            except Exception as reconcile_err:
                report["errors"] += 1
                logger.error("[PAPER] startup reconcile failed for %s: %s", symbol, reconcile_err)

        return report
