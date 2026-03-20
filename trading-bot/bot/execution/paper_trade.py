"""
Paper Trading Engine – simulates trade execution without real money.
Tracks virtual positions, PnL, and order history.
"""
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from bot.config import (
    DB_PATH,
    MIN_ORDER_NOTIONAL_USDT,
    MAX_ORDER_NOTIONAL_USDT,
    MAX_SIGNAL_STALENESS_SECONDS,
    MAX_SIMULTANEOUS_TRADES,
)
from bot.risk.risk_manager import TrailingStopLoss
from bot import telemetry

logger = logging.getLogger(__name__)


class PaperTradeEngine:
    """Simulates trade execution without real money."""

    def __init__(self, starting_capital: float = 1000.0):
        self.capital = starting_capital
        self.start_cap = starting_capital
        self.positions = {}   # {symbol: trade_dict}
        self.history = []
        self._init_db()
        self._restore_capital_from_db()
        self._load_open_positions()

    def _restore_capital_from_db(self):
        """Adjust capital by adding realized PnL from previous sessions."""
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(pnl), 0) FROM paper_trades WHERE status LIKE 'closed%'")
        realized_pnl = c.fetchone()[0] or 0.0
        conn.close()
        self.capital = self.start_cap + realized_pnl
        if realized_pnl != 0:
            logger.info("Restored capital: $%.2f (realized PnL from DB: $%+.2f)", self.capital, realized_pnl)

    def _load_open_positions(self):
        """Reload open positions from DB into memory after a restart."""
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open'"
        ).fetchall()
        conn.close()

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
                "opened_at":  r["entry_time"] or datetime.utcnow().isoformat(),
                "status":     "open",
                "tp1_hit":    False,
                "trailing":   TrailingStopLoss(entry, sl, r["direction"]),
                "score":      r.get("score") or 0,
                "half_qty":   round((r["qty"] or 0.0) / 2, 6),
            }
            self.positions[symbol] = trade
            logger.info("Restored open position: %s %s @ %.4f", r["direction"].upper(), symbol, entry)

        if self.positions:
            logger.info("Restored %d open position(s) from DB.", len(self.positions))

    def _init_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id TEXT PRIMARY KEY,
                symbol TEXT, direction TEXT,
                entry_price REAL, qty REAL,
                stop_loss REAL, tp1 REAL, tp2 REAL,
                entry_time TEXT, exit_time TEXT,
                exit_price REAL, pnl REAL, status TEXT,
                score INTEGER, reason TEXT
            )
        """)
        conn.commit()
        conn.close()

    def open_position(self, signal: dict, risk_params: dict) -> dict:
        """Open a simulated position based on signal + risk params."""
        symbol = signal["symbol"]
        direction = signal["direction"]
        entry = signal["price"]

        qty = float(risk_params.get("qty", 0) or 0)
        if qty <= 0:
            logger.warning("[PAPER] Rejecting %s: qty must be > 0", symbol)
            telemetry.increment("order_rejections")
            return {}

        if direction not in ("long", "short"):
            logger.warning("[PAPER] Rejecting %s: invalid direction '%s'", symbol, direction)
            telemetry.increment("order_rejections")
            return {}

        sl = float(risk_params.get("sl", 0) or 0)
        tp1 = float(risk_params.get("tp1", 0) or 0)
        tp2 = float(risk_params.get("tp2", 0) or 0)

        if direction == "long" and not (sl < entry < tp1 <= tp2):
            logger.warning("[PAPER] Rejecting %s: invalid long SL/TP layout", symbol)
            telemetry.increment("order_rejections")
            return {}
        if direction == "short" and not (sl > entry > tp1 >= tp2):
            logger.warning("[PAPER] Rejecting %s: invalid short SL/TP layout", symbol)
            telemetry.increment("order_rejections")
            return {}

        notional = abs(entry * qty)
        if notional < MIN_ORDER_NOTIONAL_USDT:
            logger.warning("[PAPER] Rejecting %s: notional %.4f < min %.4f", symbol, notional, MIN_ORDER_NOTIONAL_USDT)
            telemetry.increment("order_rejections")
            return {}
        if notional > MAX_ORDER_NOTIONAL_USDT:
            logger.warning("[PAPER] Rejecting %s: notional %.4f > max %.4f", symbol, notional, MAX_ORDER_NOTIONAL_USDT)
            telemetry.increment("order_rejections")
            return {}

        generated_at = signal.get("generated_at")
        if generated_at:
            try:
                gen_dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
                if gen_dt.tzinfo is None:
                    gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                age_s = (datetime.now(timezone.utc) - gen_dt).total_seconds()
                if age_s > MAX_SIGNAL_STALENESS_SECONDS:
                    logger.warning("[PAPER] Rejecting %s: stale signal %.1fs old", symbol, age_s)
                    telemetry.increment("order_rejections")
                    return {}
            except Exception:
                logger.warning("[PAPER] Rejecting %s: invalid generated_at on signal", symbol)
                telemetry.increment("order_rejections")
                return {}

        if symbol in self.positions:
            logger.info("Already have open position on %s, skipping.", symbol)
            telemetry.increment("order_rejections")
            return {}

        if len(self.positions) >= MAX_SIMULTANEOUS_TRADES:
            logger.info("Max simultaneous trades reached, skipping %s.", symbol)
            telemetry.increment("order_rejections")
            return {}

        trade = {
            "id": str(uuid.uuid4())[:8],
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "qty": risk_params["qty"],
            "sl": risk_params["sl"],
            "tp1": risk_params["tp1"],
            "tp2": risk_params["tp2"],
            "opened_at": datetime.utcnow().isoformat(),
            "status": "open",
            "tp1_hit": False,
            "trailing": TrailingStopLoss(entry, risk_params["sl"], direction),
            "score": signal["score"],
            "half_qty": round(risk_params["qty"] / 2, 6),
        }

        self.positions[symbol] = trade
        self._save_trade(trade, pnl=0, exit_price=None, reason="opened")
        telemetry.increment("orders_opened")
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
            sl_price = trade["trailing"].update(price)
            trade["sl"] = sl_price

            # ── Time-Based Exit (5 days) ────────────────────────────────────
            time_open = datetime.utcnow() - datetime.fromisoformat(trade["opened_at"])
            if time_open > timedelta(days=5):
                remaining_qty = trade["half_qty"] if trade["tp1_hit"] else trade["qty"]
                pnl = self._calc_pnl(trade["entry"], price, remaining_qty, direction)
                self.capital += pnl
                trade["status"] = "closed_time"
                self._save_trade(trade, pnl, price, reason="time_limit")
                telemetry.increment("orders_closed")
                closed.append({**trade, "exit_price": price, "pnl": pnl})
                del self.positions[symbol]
                logger.info("[PAPER] TIME EXIT %s @ %s | Time: %s | PnL: $%s", symbol, price, time_open, f"{pnl:.2f}")
                continue

            # ── TP1 partial exit ────────────────────────────────────────────
            if not trade["tp1_hit"]:
                hit_tp1 = (direction == "long" and price >= trade["tp1"]) or \
                          (direction == "short" and price <= trade["tp1"])
                if hit_tp1:
                    pnl_tp1 = self._calc_pnl(trade["entry"], price, trade["half_qty"], direction)
                    self.capital += pnl_tp1
                    trade["tp1_hit"] = True
                    logger.info("[PAPER] TP1 HIT %s @ %s | PnL: $%s", symbol, price, f"{pnl_tp1:.2f}")
                    # Move SL to breakeven after TP1 hit
                    trade["trailing"].current_sl = trade["entry"]
                    trade["sl"] = trade["entry"]

            # ── TP2 full exit ───────────────────────────────────────────────
            hit_tp2 = (direction == "long" and price >= trade["tp2"]) or \
                      (direction == "short" and price <= trade["tp2"])
            if hit_tp2:
                remaining_qty = trade["half_qty"] if trade["tp1_hit"] else trade["qty"]
                pnl = self._calc_pnl(trade["entry"], price, remaining_qty, direction)
                self.capital += pnl
                trade["status"] = "closed_tp2"
                self._save_trade(trade, pnl, price, reason="tp2")
                telemetry.increment("orders_closed")
                closed.append({**trade, "exit_price": price, "pnl": pnl})
                del self.positions[symbol]
                logger.info("[PAPER] TP2 HIT %s @ %s | PnL: $%s", symbol, price, f"{pnl:.2f}")
                continue

            # ── Stop Loss hit ───────────────────────────────────────────────
            hit_sl = (direction == "long" and price <= sl_price) or \
                     (direction == "short" and price >= sl_price)
            if hit_sl:
                remaining_qty = trade["half_qty"] if trade["tp1_hit"] else trade["qty"]
                pnl = self._calc_pnl(trade["entry"], price, remaining_qty, direction)
                self.capital += pnl
                trade["status"] = "closed_sl"
                self._save_trade(trade, pnl, price, reason="stop_loss")
                telemetry.increment("orders_closed")
                closed.append({**trade, "exit_price": price, "pnl": pnl})
                del self.positions[symbol]
                logger.info("[PAPER] SL HIT %s @ %s | PnL: $%s", symbol, price, f"{pnl:.2f}")

        return closed

    def _calc_pnl(self, entry, exit_price, qty, direction):
        if direction == "long":
            return (exit_price - entry) * qty
        return (entry - exit_price) * qty

    def get_stats(self) -> dict:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        c = conn.cursor()
        c.execute("SELECT pnl, status FROM paper_trades WHERE status LIKE 'closed%'")
        rows = c.fetchall()
        conn.close()

        if not rows:
            return {
                "total": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "capital": round(self.capital, 2),
                "return_pct": round((self.capital - self.start_cap) / self.start_cap * 100, 2) if self.start_cap else 0.0
            }

        wins = [r for r in rows if r[0] > 0]
        losses = [r for r in rows if r[0] <= 0]
        total_pnl = sum(r[0] for r in rows)
        win_rate = len(wins) / len(rows) if rows else 0

        return {
            "total": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "capital": round(self.capital, 2),
            "return_pct": round((self.capital - self.start_cap) / self.start_cap * 100, 2),
        }

    def _save_trade(self, trade, pnl, exit_price, reason):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA journal_mode=WAL;")
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO paper_trades
            (id, symbol, direction, entry_price, qty, stop_loss, tp1, tp2,
             entry_time, exit_time, exit_price, pnl, status, score, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade["id"], trade["symbol"], trade["direction"],
            trade["entry"], trade["qty"], trade["sl"],
            trade["tp1"], trade["tp2"], trade["opened_at"],
            datetime.utcnow().isoformat() if exit_price else None,
            exit_price, pnl, trade["status"], trade.get("score", 0), reason
        ))
        conn.commit()
        conn.close()
