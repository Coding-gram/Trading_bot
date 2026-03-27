"""
Risk Management – Stop Loss, Take Profit, Trailing SL, Position Sizing, Portfolio Heat
All designed to protect capital and lock in profits.
"""
import logging
import math
from bot.config import (
    ATR_MULTIPLIER_SL, RR_RATIO,
    PARTIAL_TP1_RATIO, PARTIAL_TP2_RATIO,
    MAX_RISK_PER_TRADE_PCT,
    MAX_CAPITAL_PER_TRADE_PCT, TOTAL_CAPITAL_USDT,
    MAX_TOTAL_PORTFOLIO_RISK_PCT,
)

logger = logging.getLogger(__name__)


def move_to_breakeven(entry: float, sl: float, tp1_hit: bool, rr: float, direction: str) -> float:
    """
    Move SL to entry price when TP1 is hit or RR >= 1:1.
    """
    if tp1_hit or rr >= 1.0:
        logger.info("Move-to-Breakeven: SL moved to entry price.")
        return entry
    return sl


def time_decay_stop_loss(entry: float, sl: float, direction: str, days_open: int, decay_start: int = 3, decay_end: int = 5) -> float:
    """
    Gradually tighten SL closer to price if trade stagnates (e.g., after 3 days).
    """
    if days_open < decay_start:
        return sl
    # Linearly tighten SL from original to halfway to entry over decay window
    decay_window = max(decay_end - decay_start, 1)
    progress = min(max(days_open - decay_start, 0) / decay_window, 1.0)
    if direction == "long":
        new_sl = sl + (entry - sl) * progress * 0.5
    else:
        new_sl = sl - (sl - entry) * progress * 0.5
    logger.info(f"Time-decay SL tightened to {new_sl:.4f} after {days_open} days open.")
    return round(new_sl, 8)


# ─── Stop Loss ────────────────────────────────────────────────────────────────

def atr_stop_loss(entry: float, atr: float, direction: str) -> float:
    """
    ATR-based stop loss. Adapts to current market volatility.
    Long:  SL = entry - (ATR × multiplier)
    Short: SL = entry + (ATR × multiplier)
    """
    offset = atr * ATR_MULTIPLIER_SL
    if direction == "long":
        return round(entry - offset, 8)
    return round(entry + offset, 8)


def support_stop_loss(nearest_support: float, entry: float, atr: float, k: float = 1.0) -> float:
    """
    Place SL just below nearest support (for longs), with buffer as min(0.5%, k * ATR/entry).
    k: scaling factor for ATR-based buffer (default 1.0)
    """
    atr_buffer = (atr / entry) * k if entry > 0 else 0.005
    buffer_pct = min(0.005, atr_buffer)
    return round(nearest_support * (1 - buffer_pct), 8)


def choose_stop_loss(entry: float, atr: float, direction: str, sr_levels: dict) -> float:
    """
    Choose the tighter of ATR-SL vs S/R SL (tighter = safer).
    For longs: higher SL = closer stop = less loss.
    For shorts: lower SL = closer = less loss.
    """
    atr_sl = atr_stop_loss(entry, atr, direction)
    if direction == "long":
        supports = [s for s in sr_levels.get("support", []) if s < entry]
        if supports:
            sr_sl = support_stop_loss(max(supports), entry, atr)
            return max(atr_sl, sr_sl)   # higher = tighter for long
        return atr_sl

    resistances = [r for r in sr_levels.get("resistance", []) if r > entry]
    if resistances:
        sr_sl = min(resistances) * 1.005
        return min(atr_sl, sr_sl)   # lower = tighter for short
    return atr_sl


# ─── Take Profit ──────────────────────────────────────────────────────────────

def calculate_take_profits(entry: float, stop_loss: float, direction: str, fib_levels: dict = None) -> dict:
    """
    Calculate TP1 (partial exit at 1R) and TP2 (full exit at 2R).
    Optionally aligns TPs with Fibonacci extension levels for precision.
    """
    risk = abs(entry - stop_loss)

    if direction == "long":
        tp1_rr = round(entry + risk * PARTIAL_TP1_RATIO, 8)
        tp2_rr = round(entry + risk * PARTIAL_TP2_RATIO, 8)
    else:
        tp1_rr = round(entry - risk * PARTIAL_TP1_RATIO, 8)
        tp2_rr = round(entry - risk * PARTIAL_TP2_RATIO, 8)

    # Snap to nearest Fibonacci extension if close enough
    tp1, tp2 = tp1_rr, tp2_rr
    if fib_levels:
        extensions = fib_levels.get("extensions", {})
        for level_name, level_price in extensions.items():
            if direction == "long" and level_price > entry:
                if abs(level_price - tp2_rr) / tp2_rr < 0.015:
                    tp2 = level_price
                    logger.info("TP2 snapped to Fibonacci extension %s: %s", level_name, tp2)
                    break
            elif direction == "short" and level_price < entry:
                if abs(level_price - tp2_rr) / tp2_rr < 0.015:
                    tp2 = level_price
                    logger.info("TP2 snapped to Fibonacci extension %s: %s", level_name, tp2)
                    break

    return {
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "rr_ratio": RR_RATIO,
    }


# ─── Trailing Stop Loss (ATR-based) ──────────────────────────────────────────

class TrailingStopLoss:
    """
    ATR-based trailing stop that adapts to current market volatility.
    Falls back to a default offset if ATR is not available.
    Call update() on each new candle.
    """

    DEFAULT_TRAIL_PCT = 0.015   # fallback if ATR unavailable

    def __init__(self, entry: float, initial_sl: float, direction: str, atr: float = 0.0):
        self.direction = direction
        self.initial_sl = initial_sl
        self.current_sl = initial_sl
        self.best_price = entry
        self.atr = max(atr, 0.0)

    def _trail_offset(self, reference_price: float) -> float:
        """Compute the trailing offset: ATR * 1.5, or fallback to fixed %."""
        if self.atr > 0:
            return self.atr * 1.5
        return reference_price * self.DEFAULT_TRAIL_PCT

    def update(self, current_price: float) -> float:
        """Update trailing SL. Returns new SL."""
        offset = self._trail_offset(current_price)
        if self.direction == "long":
            if current_price > self.best_price:
                self.best_price = current_price
                new_sl = self.best_price - offset
                if new_sl > self.current_sl:
                    self.current_sl = new_sl
                    logger.debug("Trailing SL moved to %.4f (ATR-based)", self.current_sl)
        else:
            if current_price < self.best_price:
                self.best_price = current_price
                new_sl = self.best_price + offset
                if new_sl < self.current_sl:
                    self.current_sl = new_sl
                    logger.debug("Trailing SL moved to %.4f (ATR-based)", self.current_sl)
        return self.current_sl

    def is_triggered(self, current_price: float) -> bool:
        """Check if price has hit the trailing SL."""
        if self.direction == "long":
            return current_price <= self.current_sl
        return current_price >= self.current_sl


# ─── Position Sizing ──────────────────────────────────────────────────────────

def calculate_position_size(
    capital: float,
    entry: float,
    stop_loss: float,
    available_slots: int = 1,
    consecutive_losses: int = 0
) -> dict:
    """
    Fixed-fractional position sizing: risk MAX_RISK_PER_TRADE_PCT per trade.
    Also caps at MAX_CAPITAL_PER_TRADE_PCT to ensure diversification.

    Returns: {qty, usdt_risk, usdt_value, risk_pct}
    """
    risk_pct = MAX_RISK_PER_TRADE_PCT
    cap_scale = 1.0
    if consecutive_losses >= 3:
        risk_pct = risk_pct / 2.0
        cap_scale = 0.5
        logger.info("Throttling risk to %.2f%% due to %d consecutive losses", risk_pct * 100, consecutive_losses)

    risk_per_trade = capital * risk_pct     # e.g. 2% or 1% of capital
    sl_distance = abs(entry - stop_loss)

    # Guard: if entry == stop_loss (can happen due to rounding), avoid ZeroDivisionError.
    if sl_distance <= 0:
        logger.warning(
            "calculate_position_size: sl_distance=0 (entry=%s, sl=%s). Returning zero qty.",
            entry, stop_loss
        )
        return {"qty": 0.0, "usdt_value": 0.0, "usdt_risk": 0.0, "risk_pct": 0.0}

    # Units we can buy where our loss = risk_per_trade
    qty_by_risk = risk_per_trade / sl_distance

    # Cap total trade value at MAX_CAPITAL_PER_TRADE_PCT
    max_value = capital * MAX_CAPITAL_PER_TRADE_PCT * cap_scale
    qty_by_cap = max_value / entry

    # Use the smaller (safer) of both limits
    qty = min(qty_by_risk, qty_by_cap)

    usdt_value = round(qty * entry, 2)
    usdt_risk = round(qty * sl_distance, 2)

    logger.info(
        "Position: qty=%.6f | value=$%s | risk=$%s (%s%% capital)",
        qty, usdt_value, usdt_risk, f"{MAX_RISK_PER_TRADE_PCT*100:.1f}"
    )

    return {
        "qty": round(qty, 6),
        "usdt_value": usdt_value,
        "usdt_risk": usdt_risk,
        "risk_pct": round(usdt_risk / capital * 100, 2),
    }


# ─── Portfolio Heat Limit ─────────────────────────────────────────────────────

def portfolio_heat_ok(positions: dict, capital: float) -> tuple[bool, float]:
    """
    Check if total portfolio risk (sum of all open position risks) is within limit.
    Returns (allowed, current_heat_pct).
    """
    if capital <= 0:
        return False, 0.0

    total_risk = 0.0
    for sym, trade in positions.items():
        entry = float(trade.get("entry") or trade.get("entry_price") or 0)
        sl = float(trade.get("sl") or trade.get("stop_loss") or 0)
        qty = float(trade.get("qty") or 0)
        if entry > 0 and sl > 0 and qty > 0:
            total_risk += abs(entry - sl) * qty

    heat_pct = total_risk / capital
    allowed = heat_pct < MAX_TOTAL_PORTFOLIO_RISK_PCT
    if not allowed:
        logger.warning(
            "Portfolio heat too high: %.2f%% >= %.2f%% limit. New entries blocked.",
            heat_pct * 100, MAX_TOTAL_PORTFOLIO_RISK_PCT * 100,
        )
    return allowed, round(heat_pct * 100, 2)


# ─── Daily Loss Guard ─────────────────────────────────────────────────────────

class DailyLossGuard:
    """
    Tracks daily PnL and halts trading if daily loss limit or overall
    drawdown limit is exceeded.
    """

    def __init__(self, starting_capital: float):
        self.starting_capital = starting_capital
        self.peak_capital = starting_capital
        self.daily_start = starting_capital
        self.realized_pnl_day = 0.0
        self.consecutive_losses = 0

    def record_trade(self, pnl: float):
        """Record a completed trade's PnL."""
        self.realized_pnl_day += pnl
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def can_trade(self, current_capital: float, daily_loss_limit_pct: float, max_drawdown_pct: float) -> tuple:
        """Returns (allowed: bool, reason: str)."""
        # Daily loss check
        daily_loss_pct = -self.realized_pnl_day / self.daily_start
        if daily_loss_pct >= daily_loss_limit_pct:
            return False, f"Daily loss limit hit: {daily_loss_pct*100:.1f}%"

        # Drawdown check against peak capital
        if current_capital > self.peak_capital:
            self.peak_capital = current_capital
        drawdown = (self.peak_capital - current_capital) / self.peak_capital
        if drawdown >= max_drawdown_pct:
            return False, f"Max drawdown hit: {drawdown*100:.1f}%"

        return True, "OK"

    def reset_daily(self, current_capital: float):
        """Call at start of each trading day."""
        self.daily_start = current_capital
        self.realized_pnl_day = 0.0
