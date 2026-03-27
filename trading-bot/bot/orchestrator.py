"""
Orchestrator – scan loops, position management, daily resets.
Extracted from main.py for cleaner module boundaries.
"""
import asyncio
import logging
import time
from datetime import date, datetime, timezone

import pandas as pd

from bot.config import (
    WATCHLIST, PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME, DAILY_TIMEFRAME,
    SCAN_INTERVAL_SECONDS, MAX_SIMULTANEOUS_TRADES,
    DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT,
    MAX_ENTRY_SPREAD_PCT,
)
from bot.analysis import indicators
from bot.data.fetcher import fetch_multi_timeframe, fetch_ticker
from bot.strategy.scorer import score_signal
from bot.risk.risk_manager import DailyLossGuard, portfolio_heat_ok
from bot.risk.correlation import compute_correlation_matrix, check_portfolio_correlation
from bot.signal_worker import (
    should_emit_signal, execute_signal,
    _telemetry_increment, _telemetry_record_event,
    _safe_send_trade_closed, _safe_send_daily_summary, _safe_send_risk_alert,
)
from bot import telemetry


logger = logging.getLogger(__name__)

MAX_SPREAD_PCT = MAX_ENTRY_SPREAD_PCT
ATR_SPIKE_MULT = 3.0
REST_SYMBOL_DELAY_SECONDS = 0.5


def is_spread_ok(ticker: dict, symbol: str) -> bool:
    """Return False if bid-ask spread is too wide (illiquid / manipulated)."""
    bid = ticker.get("bid") or 0
    ask = ticker.get("ask") or 0
    if bid <= 0 or ask <= 0:
        return True
    spread_pct = (ask - bid) / bid
    if spread_pct > MAX_SPREAD_PCT:
        logger.warning(
            "[%s] Spread too wide: %.3f%% > %.1f%% - skipping",
            symbol, spread_pct * 100, MAX_SPREAD_PCT * 100
        )
        return False
    return True


def is_volatility_normal(df: pd.DataFrame, symbol: str) -> bool:
    """Return False if ATR has spiked (likely a news event)."""
    if df is None or df.empty or "atr" not in df.columns:
        return True
    atr_col = df["atr"].dropna()
    if len(atr_col) < 20:
        return True
    avg_atr = atr_col.iloc[-20:-1].mean()
    current_atr = atr_col.iloc[-1]
    if avg_atr > 0 and current_atr > avg_atr * ATR_SPIKE_MULT:
        logger.warning(
            "[%s] Volatility spike detected: ATR %.4f > %.1fx avg %.4f - possible news event, skipping",
            symbol, current_atr, ATR_SPIKE_MULT, avg_atr
        )
        return False
    return True


def handle_daily_reset(engine, loss_guard: DailyLossGuard, today: date) -> date:
    """Check if a new day has started and reset daily stats."""
    current_date = date.today()
    if current_date != today:
        stats = engine.get_stats()
        _safe_send_daily_summary(stats)
        loss_guard.reset_daily(engine.capital)
        logger.info("📅 Daily reset done for %s.", current_date)
        return current_date
    return today


def process_open_positions(exchange, engine, loss_guard: DailyLossGuard, mode: str = "paper", live_engine=None):
    """Update stop-loss/take-profit for all currently open trades."""
    if mode == "live" and live_engine is not None:
        live_engine.reconcile_orders()
        live_engine.monitor_positions()
        return

    if not engine.positions:
        return

    live_prices = {}
    for sym in list(engine.positions.keys()):
        ticker = fetch_ticker(exchange, sym)
        if ticker:
            live_prices[sym] = ticker["price"]

    closed = engine.update_positions(live_prices)
    _telemetry_record_event("last_closed_count", len(closed))
    for c in closed:
        _safe_send_trade_closed(
            c["symbol"], c["status"], c["entry"],
            c["exit_price"], c["pnl"], c["status"]
        )
        loss_guard.record_trade(c["pnl"])


def scan_for_signals(
    exchange,
    engine,
    loss_guard: DailyLossGuard,
    mode: str,
    live_engine=None,
    symbols_to_scan: list[str] | None = None,
    signal_queue: asyncio.Queue | None = None,
    per_symbol_delay_seconds: float = 0.0,
    on_queue_overflow=None,
):
    """Scan the watchlist for new entries if slots are available."""
    _telemetry_increment("scans_total")
    active_positions = live_engine.positions if (mode == "live" and live_engine is not None) else engine.positions
    open_slots = MAX_SIMULTANEOUS_TRADES - len(active_positions)
    if open_slots == 0:
        logger.info("Max positions open. Monitoring only.")
        _telemetry_increment("scan_skipped_max_positions")
        return

    # Portfolio heat check
    heat_ok, heat_pct = portfolio_heat_ok(active_positions, engine.capital)
    if not heat_ok:
        _telemetry_increment("scan_skipped_portfolio_heat")
        return

    close_prices: dict[str, pd.Series] = {}
    for open_sym in active_positions.keys():
        open_dfs = fetch_multi_timeframe(exchange, open_sym, [PRIMARY_TIMEFRAME])
        open_df = open_dfs.get(PRIMARY_TIMEFRAME)
        if open_df is not None and not open_df.empty:
            close_prices[open_sym] = open_df["close"]

    symbols = symbols_to_scan if symbols_to_scan is not None else WATCHLIST
    for symbol in symbols:
        _telemetry_increment("symbols_scanned")
        if symbol in active_positions:
            _telemetry_increment("symbols_skipped_already_open")
            continue

        dfs = fetch_multi_timeframe(exchange, symbol, [PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME, DAILY_TIMEFRAME])
        df_1h = dfs.get(PRIMARY_TIMEFRAME)
        df_4h = dfs.get(CONFIRM_TIMEFRAME)
        df_daily = dfs.get(DAILY_TIMEFRAME)

        if df_1h is None or df_4h is None or df_1h.empty:
            _telemetry_increment("symbols_skipped_missing_data")
            continue

        close_prices[symbol] = df_1h["close"]
        if active_positions and len(close_prices) >= 2:
            corr_matrix = compute_correlation_matrix(close_prices, lookback=30)
            corr_ok, corr_reason = check_portfolio_correlation(
                symbol,
                list(active_positions.keys()),
                corr_matrix,
            )
            if not corr_ok:
                logger.warning("[%s] Correlation gate blocked entry: %s", symbol, corr_reason)
                _telemetry_increment("symbols_skipped_correlation")
                continue

        ticker = fetch_ticker(exchange, symbol)
        if ticker and not is_spread_ok(ticker, symbol):
            _telemetry_increment("symbols_skipped_spread")
            continue

        df_1h_with_indicators = indicators.compute_all(df_1h)
        if not is_volatility_normal(df_1h_with_indicators, symbol):
            _telemetry_increment("symbols_skipped_volatility")
            continue

        signal = score_signal(df_1h, df_4h, symbol, df_daily=df_daily)
        if signal["send_alert"] and should_emit_signal(signal):
            _telemetry_increment("alerts_sent")
            if signal_queue is not None:
                try:
                    signal_queue.put_nowait(signal)
                    _telemetry_record_event("signal_queue_depth", signal_queue.qsize())
                except asyncio.QueueFull:
                    logger.warning("Signal queue full; dropping signal for %s", symbol)
                    _telemetry_increment("signals_dropped_queue_full")
                    if on_queue_overflow is not None:
                        on_queue_overflow(symbol)
            else:
                execute_signal(engine, loss_guard, signal, mode, live_engine=live_engine)

        if per_symbol_delay_seconds > 0:
            time.sleep(per_symbol_delay_seconds)
