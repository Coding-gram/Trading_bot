"""
Main Bot Runner – orchestrates everything every SCAN_INTERVAL_SECONDS.
Fully automated in live mode: sends signal alerts and places orders when guards pass.
"""
import argparse
import contextlib
import logging
import logging.handlers
import time
import asyncio
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
import pandas as pd

# Fix for pandas-ta and old libraries trying to use deprecated applymap
if not hasattr(pd.DataFrame, 'applymap'):
    pd.DataFrame.applymap = pd.DataFrame.map

from bot.config import (
    WATCHLIST, PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME, DAILY_TIMEFRAME,
    SCAN_INTERVAL_SECONDS, TOTAL_CAPITAL_USDT,
    MAX_SIMULTANEOUS_TRADES, DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT,
    LIVE_ORDER_TYPE,
    WS_MAX_SYMBOLS,
    DB_PATH, DB_RETENTION_DAYS, SIGNAL_QUEUE_MAXSIZE,
)
from bot.analysis import indicators
from bot.data.fetcher import get_exchange, fetch_ticker, fetch_balance
from bot.data.ws_fetcher import WebSocketFetcher
from bot.strategy.scorer import score_signal
from bot.risk.risk_manager import DailyLossGuard
from bot.risk.correlation import compute_correlation_matrix, check_portfolio_correlation
from bot.execution.paper_trade import PaperTradeEngine
from bot.execution.live_trade import LiveTradeEngine
from bot.execution.db_maintenance import prune_old_trades
from bot.signal_worker import should_emit_signal, signal_worker_loop
from bot.signal_worker import (
    _telemetry_increment, _telemetry_record_event,
    _safe_send_panic_alert, _safe_send_trade_closed,
    _safe_send_daily_summary, _safe_send_risk_alert,
)
from bot.orchestrator import (
    handle_daily_reset as orchestrator_handle_daily_reset,
    process_open_positions as orchestrator_process_open_positions,
    scan_for_signals as orchestrator_scan_for_signals,
    is_spread_ok, is_volatility_normal,
)
from bot.startup import (
    run_startup_health_gate,
    run_live_arming_check,
    run_live_exchange_readiness_check,
)
from bot.telemetry import prune_old_events
from bot import telemetry


def _setup_logging():
    """Configure logging with rotation (5MB max, 5 backups)."""
    log_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Rotating file handler (5MB, 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        "bot.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)


_setup_logging()
logger = logging.getLogger("main")

ATR_SPIKE_MULT = 3.0
REST_SYMBOL_DELAY_SECONDS = 0.5
MAX_CANDLE_CACHE_ROWS = 1200

# Graceful shutdown event (cross-platform)
_shutdown_event = asyncio.Event()


async def run_bot_async(mode: str = "paper"):
    logger.info("🚀 Trading Bot Starting | Mode: %s (WebSocket)", mode.upper())
    telemetry.record_event("bot_session_started_at", datetime.now(timezone.utc).isoformat())
    engine = PaperTradeEngine(starting_capital=TOTAL_CAPITAL_USDT)
    loss_guard = DailyLossGuard(TOTAL_CAPITAL_USDT)
    today = date.today()
    symbols = WATCHLIST
    if WS_MAX_SYMBOLS > 0:
        ws_symbols = symbols[:WS_MAX_SYMBOLS]
        rest_symbols = symbols[WS_MAX_SYMBOLS:]
    else:
        ws_symbols = symbols
        rest_symbols = []

    if rest_symbols:
        logger.warning(
            "WS symbol cap active: %d on WS, %d on REST supplement.",
            len(ws_symbols), len(rest_symbols)
        )
        logger.info("WS symbols: %s", ", ".join(ws_symbols))
        logger.info("REST supplement symbols: %s", ", ".join(rest_symbols))

    timeframes = list(dict.fromkeys([PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME, DAILY_TIMEFRAME]))
    ws_fetcher = WebSocketFetcher(ws_symbols, timeframes, use_private_auth=(mode == "live"))
    error_count = 0
    CIRCUIT_BREAKER_THRESHOLD = 3
    exchange = get_exchange(paper_mode=(mode == "paper"))
    live_engine = LiveTradeEngine(exchange) if mode == "live" else None
    if mode == "paper":
        try:
            reconcile_report = await asyncio.to_thread(engine.reconcile_open_positions_with_market, exchange)
            logger.info(
                "[PAPER] Startup reconcile: checked=%d tp1_marked=%d closed_tp2=%d closed_sl=%d errors=%d",
                reconcile_report.get("checked", 0),
                reconcile_report.get("tp1_marked", 0),
                reconcile_report.get("closed_tp2", 0),
                reconcile_report.get("closed_sl", 0),
                reconcile_report.get("errors", 0),
            )
            _telemetry_record_event("paper_startup_reconcile", reconcile_report)
        except Exception as startup_reconcile_err:
            logger.error("[PAPER] Startup reconcile error: %s", startup_reconcile_err)
            _telemetry_increment("api_errors")
    candle_cache = {sym: {} for sym in ws_symbols}
    last_processed_primary_ts = {}
    rest_task = None
    signal_worker_task = None
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=SIGNAL_QUEUE_MAXSIZE)
    last_db_maintenance_day: date | None = None
    queue_overflow_alert_cooldown_seconds = 300
    last_queue_overflow_alert_at = 0.0

    async def _maybe_run_db_maintenance() -> None:
        nonlocal last_db_maintenance_day
        day = date.today()
        if last_db_maintenance_day == day:
            return
        last_db_maintenance_day = day
        try:
            result = await asyncio.to_thread(prune_old_trades, DB_PATH, DB_RETENTION_DAYS)
            logger.info(
                "DB maintenance complete: deleted=%d (paper=%d, live=%d), cutoff=%s",
                result.get("deleted_total", 0),
                result.get("paper_deleted", 0),
                result.get("live_deleted", 0),
                result.get("cutoff", "n/a"),
            )
            _telemetry_record_event("db_maintenance", result)
        except Exception as maintenance_err:
            logger.error("DB maintenance failed: %s", maintenance_err)
            _telemetry_increment("api_errors")
        # Prune old telemetry events (keep 7 days)
        try:
            telem_result = await asyncio.to_thread(prune_old_events, 7)
            if telem_result.get("deleted", 0) > 0:
                logger.info(
                    "Telemetry pruned: deleted=%d events older than %s",
                    telem_result["deleted"],
                    telem_result.get("cutoff", "n/a"),
                )
        except Exception as telem_err:
            logger.warning("Telemetry prune failed: %s", telem_err)

    def _handle_queue_overflow(symbol: str) -> None:
        nonlocal last_queue_overflow_alert_at
        now_ts = time.time()
        if now_ts - last_queue_overflow_alert_at >= queue_overflow_alert_cooldown_seconds:
            last_queue_overflow_alert_at = now_ts
            _safe_send_panic_alert(
                f"Signal queue overflow: dropping signals (latest symbol: {symbol}). Increase SIGNAL_QUEUE_MAXSIZE or reduce scan load."
            )
        _telemetry_record_event("signal_queue_depth", signal_queue.qsize())

    async def _run_rest_fallback_loop():
        logger.warning("Switching to REST polling fallback mode.")
        _telemetry_increment("ws_fallback_rest")
        exchange = get_exchange(paper_mode=(mode == "paper"))

        fallback_day = date.today()
        while not _shutdown_event.is_set():
            fallback_day = orchestrator_handle_daily_reset(engine, loss_guard, fallback_day)
            await _maybe_run_db_maintenance()

            allowed, reason = loss_guard.can_trade(
                engine.capital,
                DAILY_LOSS_LIMIT_PCT,
                MAX_DRAWDOWN_PCT,
            )
            if not allowed:
                logger.warning("Trading paused by risk guard: %s", reason)
                _safe_send_risk_alert(reason)
                _telemetry_increment("risk_halt_events")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                continue

            orchestrator_process_open_positions(exchange, engine, loss_guard, mode=mode, live_engine=live_engine)
            orchestrator_scan_for_signals(
                exchange,
                engine,
                loss_guard,
                mode,
                live_engine=live_engine,
                signal_queue=signal_queue,
                per_symbol_delay_seconds=REST_SYMBOL_DELAY_SECONDS,
                on_queue_overflow=_handle_queue_overflow,
            )
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def _run_rest_supplement_loop():
        if not rest_symbols:
            return
        while not _shutdown_event.is_set():
            try:
                orchestrator_process_open_positions(exchange, engine, loss_guard, mode=mode, live_engine=live_engine)
                orchestrator_scan_for_signals(
                    exchange,
                    engine,
                    loss_guard,
                    mode,
                    live_engine=live_engine,
                    symbols_to_scan=rest_symbols,
                    signal_queue=signal_queue,
                    per_symbol_delay_seconds=REST_SYMBOL_DELAY_SECONDS,
                    on_queue_overflow=_handle_queue_overflow,
                )
            except Exception as err:
                logger.error("REST supplement loop error: %s", err)
                _telemetry_increment("api_errors")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def on_candle_close(symbol, timeframe, ohlcv):
        nonlocal today
        if not ohlcv:
            return

        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        if df.empty:
            return
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        df.sort_index(inplace=True)
        df = df[~df.index.duplicated(keep="last")]

        if len(df) > MAX_CANDLE_CACHE_ROWS:
            df = df.iloc[-MAX_CANDLE_CACHE_ROWS:].copy()

        candle_cache.setdefault(symbol, {})[timeframe] = df
        today = orchestrator_handle_daily_reset(engine, loss_guard, today)
        await _maybe_run_db_maintenance()

        active_positions = live_engine.positions if (mode == "live" and live_engine is not None) else engine.positions

        if mode == "live" and live_engine is not None:
            await asyncio.to_thread(live_engine.reconcile_orders)

        if symbol in active_positions:
            latest_close = float(df["close"].iloc[-1])
            if mode == "paper":
                closed = await asyncio.to_thread(engine.update_positions, {symbol: latest_close})
                _telemetry_record_event("last_closed_count", len(closed))
                for c in closed:
                    _safe_send_trade_closed(
                        c["symbol"], c["status"], c["entry"],
                        c["exit_price"], c["pnl"], c["status"]
                    )
                    loss_guard.record_trade(c["pnl"])

        if timeframe != PRIMARY_TIMEFRAME:
            return

        _telemetry_increment("scans_total")
        _telemetry_increment("symbols_scanned")

        primary = candle_cache.get(symbol, {}).get(PRIMARY_TIMEFRAME)
        confirm = candle_cache.get(symbol, {}).get(CONFIRM_TIMEFRAME)
        if primary is None or confirm is None or primary.empty or confirm.empty:
            _telemetry_increment("symbols_skipped_missing_data")
            return

        primary_ts = primary.index[-1]
        if last_processed_primary_ts.get(symbol) == primary_ts:
            return
        last_processed_primary_ts[symbol] = primary_ts

        allowed, reason = loss_guard.can_trade(
            engine.capital,
            DAILY_LOSS_LIMIT_PCT,
            MAX_DRAWDOWN_PCT,
        )
        if not allowed:
            logger.warning("Trading paused by risk guard: %s", reason)
            _safe_send_risk_alert(reason)
            _telemetry_increment("risk_halt_events")
            return

        if symbol in active_positions:
            _telemetry_increment("symbols_skipped_already_open")
            return

        open_slots = MAX_SIMULTANEOUS_TRADES - len(active_positions)
        if open_slots <= 0:
            logger.info("Max positions open. Monitoring only.")
            _telemetry_increment("scan_skipped_max_positions")
            return

        ticker = fetch_ticker(exchange, symbol)
        if ticker and not is_spread_ok(ticker, symbol):
            _telemetry_increment("symbols_skipped_spread")
            return

        primary_with_indicators = indicators.compute_all(primary.copy())
        if not is_volatility_normal(primary_with_indicators, symbol):
            _telemetry_increment("symbols_skipped_volatility")
            return

        daily_df = candle_cache.get(symbol, {}).get(DAILY_TIMEFRAME)

        close_prices: dict[str, pd.Series] = {}
        for open_sym in active_positions.keys():
            open_primary = candle_cache.get(open_sym, {}).get(PRIMARY_TIMEFRAME)
            if open_primary is not None and not open_primary.empty:
                close_prices[open_sym] = open_primary["close"]
        close_prices[symbol] = primary["close"]
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
                return

        signal = score_signal(primary.copy(), confirm.copy(), symbol, df_daily=daily_df.copy() if daily_df is not None else None)
        if signal["send_alert"] and should_emit_signal(signal):
            _telemetry_increment("alerts_sent")
            try:
                signal_queue.put_nowait(signal)
                _telemetry_record_event("signal_queue_depth", signal_queue.qsize())
            except asyncio.QueueFull:
                logger.warning("Signal queue full; dropping signal for %s", symbol)
                _telemetry_increment("signals_dropped_queue_full")
                _handle_queue_overflow(symbol)

    try:
        signal_worker_task = asyncio.create_task(signal_worker_loop(signal_queue, engine, loss_guard, mode, live_engine=live_engine))
        await _maybe_run_db_maintenance()
        await ws_fetcher.connect()
        if rest_symbols:
            rest_task = asyncio.create_task(_run_rest_supplement_loop())
        while not _shutdown_event.is_set():
            try:
                await ws_fetcher.subscribe_klines(on_candle_close)
                error_count = 0
            except (ConnectionError, TimeoutError) as e:
                error_count += 1
                _telemetry_increment("api_errors")
                logger.error(f"WebSocket/API connection error: {e} (consecutive: {error_count})")
                if error_count >= CIRCUIT_BREAKER_THRESHOLD:
                    _safe_send_panic_alert(
                        f"{error_count} consecutive API/WebSocket errors. Falling back to REST polling."
                    )
                    await _run_rest_fallback_loop()
                    break
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"Unexpected error in WebSocket loop: {e}")
                raise
    except ConnectionError as e:
        logger.error("WebSocket startup failed: %s", e)
        await _run_rest_fallback_loop()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
        _safe_send_daily_summary(engine.get_stats())
    finally:
        logger.info("Shutting down gracefully...")
        if rest_task is not None:
            rest_task.cancel()
            try:
                await rest_task
            except asyncio.CancelledError:
                pass

        if signal_worker_task is not None:
            signal_worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await signal_worker_task

        await ws_fetcher.close()
        _safe_send_daily_summary(engine.get_stats())
        logger.info("Bot shutdown complete.")


def _request_shutdown():
    """Cross-platform shutdown handler."""
    logger.info("Shutdown signal received.")
    _shutdown_event.set()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--skip-health-gate", action="store_true", help="Skip health_gate.py pre-run checks")
    cl_args = parser.parse_args()
    if not run_live_arming_check(cl_args.mode):
        raise SystemExit(1)
    if not run_live_exchange_readiness_check(cl_args.mode):
        raise SystemExit(1)
    if not run_startup_health_gate(skip_health_gate=cl_args.skip_health_gate):
        raise SystemExit(1)

    # Register cross-platform shutdown (Windows-safe: no add_signal_handler)
    if sys.platform != "win32":
        import signal
        loop = asyncio.new_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _request_shutdown)
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_bot_async(mode=cl_args.mode))
    else:
        # On Windows, use threading to catch Ctrl+C
        import signal
        signal.signal(signal.SIGINT, lambda *_: _request_shutdown())
        asyncio.run(run_bot_async(mode=cl_args.mode))
