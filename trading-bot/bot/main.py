"""
Main Bot Runner – orchestrates everything every SCAN_INTERVAL_SECONDS.
Fully automated in live mode: sends signal alerts and places orders when guards pass.
"""
import argparse
import contextlib
import logging
import time
import asyncio
import subprocess
import sys
from datetime import date
from pathlib import Path
import pandas as pd

# Fix for pandas-ta and old libraries trying to use deprecated applymap
if not hasattr(pd.DataFrame, 'applymap'):
    pd.DataFrame.applymap = pd.DataFrame.map

from bot.config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    WATCHLIST, PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME,
    SCAN_INTERVAL_SECONDS, TOTAL_CAPITAL_USDT,
    MAX_SIMULTANEOUS_TRADES, DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT,
    MAX_ENTRY_SPREAD_PCT, LIVE_ORDER_TYPE,
    LIVE_CONFIRMATION_PHRASE, LIVE_CONFIRMATION_REQUIRED_VALUE, WS_MAX_SYMBOLS,
    DB_PATH, DB_RETENTION_DAYS, SIGNAL_QUEUE_MAXSIZE,
)
from bot.analysis import indicators
from bot.data.fetcher import get_exchange, fetch_multi_timeframe, fetch_ticker, fetch_balance
from bot.data.ws_fetcher import WebSocketFetcher
from bot.strategy.scorer import score_signal
from bot.risk.risk_manager import (
    choose_stop_loss, calculate_take_profits, calculate_position_size, DailyLossGuard
)
from bot.execution.paper_trade import PaperTradeEngine
from bot.execution.live_trade import LiveTradeEngine
from bot.execution.db_maintenance import prune_old_trades
from bot.notifications.telegram import (
    send_signal_alert, send_trade_closed, send_daily_summary, send_risk_alert, send_panic_alert
)
from bot import telemetry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger("main")


MAX_SPREAD_PCT = MAX_ENTRY_SPREAD_PCT   # skip if bid-ask spread > threshold
ATR_SPIKE_MULT = 3.0     # skip if ATR is 3x the rolling average (news proxy)
REST_SYMBOL_DELAY_SECONDS = 0.5
MAX_CANDLE_CACHE_ROWS = 1200


def _telemetry_increment(metric: str, amount: int = 1) -> None:
    try:
        telemetry.increment(metric, amount)
    except Exception as telemetry_err:
        logger.warning("Telemetry increment failed for %s: %s", metric, telemetry_err)


def _telemetry_record_event(metric: str, value) -> None:
    try:
        telemetry.record_event(metric, value)
    except Exception as telemetry_err:
        logger.warning("Telemetry event failed for %s: %s", metric, telemetry_err)


def _safe_send_signal_alert(signal: dict, risk_params: dict) -> None:
    try:
        send_signal_alert(signal, risk_params)
    except Exception as alert_err:
        logger.error("Signal alert failed for %s: %s", signal.get("symbol"), alert_err)
        _telemetry_increment("api_errors")


def _safe_send_panic_alert(message: str) -> None:
    try:
        send_panic_alert(message)
    except Exception as alert_err:
        logger.error("Panic alert failed: %s", alert_err)
        _telemetry_increment("api_errors")


def _safe_send_trade_closed(symbol, status, entry, exit_price, pnl, reason) -> None:
    try:
        send_trade_closed(symbol, status, entry, exit_price, pnl, reason)
    except Exception as alert_err:
        logger.error("Trade-closed alert failed for %s: %s", symbol, alert_err)
        _telemetry_increment("api_errors")


def _safe_send_daily_summary(stats: dict) -> None:
    try:
        send_daily_summary(stats)
    except Exception as alert_err:
        logger.error("Daily summary alert failed: %s", alert_err)
        _telemetry_increment("api_errors")


def _safe_send_risk_alert(message: str) -> None:
    try:
        send_risk_alert(message)
    except Exception as alert_err:
        logger.error("Risk alert failed: %s", alert_err)
        _telemetry_increment("api_errors")


def is_spread_ok(ticker: dict, symbol: str) -> bool:
    """Return False if bid-ask spread is too wide (illiquid / manipulated)."""
    bid = ticker.get("bid") or 0
    ask = ticker.get("ask") or 0
    if bid <= 0 or ask <= 0:
        return True  # can't check, allow through
    spread_pct = (ask - bid) / bid
    if spread_pct > MAX_SPREAD_PCT:
        logger.warning(
            "[%s] Spread too wide: %.3f%% > %.1f%% - skipping",
            symbol, spread_pct * 100, MAX_SPREAD_PCT * 100
        )
        return False
    return True


def is_volatility_normal(df: pd.DataFrame, symbol: str) -> bool:
    """Return False if ATR has spiked (likely a news event) - protects from violent moves."""
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


def handle_daily_reset(engine: PaperTradeEngine, loss_guard: DailyLossGuard, today: date) -> date:
    """Check if a new day has started and reset daily stats."""
    current_date = date.today()
    if current_date != today:
        stats = engine.get_stats()
        _safe_send_daily_summary(stats)
        loss_guard.reset_daily(engine.capital)
        logger.info("📅 Daily reset done for %s.", current_date)
        return current_date
    return today


def process_open_positions(exchange, engine: PaperTradeEngine, loss_guard: DailyLossGuard, mode: str = "paper", live_engine: LiveTradeEngine | None = None):
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
    engine: PaperTradeEngine,
    loss_guard: DailyLossGuard,
    mode: str,
    live_engine: LiveTradeEngine | None = None,
    symbols_to_scan: list[str] | None = None,
    signal_queue: asyncio.Queue | None = None,
    per_symbol_delay_seconds: float = 0.0,
):
    """Scan the watchlist for new entries if slots are available."""
    _telemetry_increment("scans_total")
    active_positions = live_engine.positions if (mode == "live" and live_engine is not None) else engine.positions
    open_slots = MAX_SIMULTANEOUS_TRADES - len(active_positions)
    if open_slots == 0:
        logger.info("Max positions open. Monitoring only.")
        _telemetry_increment("scan_skipped_max_positions")
        return

    symbols = symbols_to_scan if symbols_to_scan is not None else WATCHLIST
    for symbol in symbols:
        _telemetry_increment("symbols_scanned")
        if symbol in active_positions:
            _telemetry_increment("symbols_skipped_already_open")
            continue

        dfs = fetch_multi_timeframe(exchange, symbol, [PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME])
        df_1h = dfs.get(PRIMARY_TIMEFRAME)
        df_4h = dfs.get(CONFIRM_TIMEFRAME)

        if df_1h is None or df_4h is None or df_1h.empty:
            _telemetry_increment("symbols_skipped_missing_data")
            continue

        ticker = fetch_ticker(exchange, symbol)
        if ticker and not is_spread_ok(ticker, symbol):
            _telemetry_increment("symbols_skipped_spread")
            continue

        # Compute ATR before volatility filter (ATR-based spike detection).
        df_1h_with_indicators = indicators.compute_all(df_1h)
        if not is_volatility_normal(df_1h_with_indicators, symbol):
            _telemetry_increment("symbols_skipped_volatility")
            continue

        signal = score_signal(df_1h, df_4h, symbol)
        if signal["send_alert"]:
            _telemetry_increment("alerts_sent")
            if signal_queue is not None:
                try:
                    signal_queue.put_nowait(signal)
                    _telemetry_record_event("signal_queue_depth", signal_queue.qsize())
                except asyncio.QueueFull:
                    logger.warning("Signal queue full; dropping signal for %s", symbol)
                    _telemetry_increment("signals_dropped_queue_full")
            else:
                _execute_signal(engine, loss_guard, signal, mode, live_engine=live_engine)

        if per_symbol_delay_seconds > 0:
            time.sleep(per_symbol_delay_seconds)


def _execute_signal(
    engine: PaperTradeEngine,
    loss_guard: DailyLossGuard,
    signal: dict,
    mode: str,
    live_engine: LiveTradeEngine | None = None,
):
    """Calculate risk parameters and execute the trade/alert."""
    entry = signal["price"]
    atr = signal["atr"]
    sr = signal.get("sr_levels", {})
    fib = signal.get("fib_levels", {})

    sl = choose_stop_loss(entry, atr, signal["direction"], sr)
    tps = calculate_take_profits(entry, sl, signal["direction"], fib)

    capital_for_sizing = engine.capital
    if mode == "live" and live_engine is not None:
        try:
            bal = fetch_balance(live_engine.exchange)
            live_free_usdt = float((bal or {}).get("USDT_free") or 0.0)
            if live_free_usdt > 0:
                capital_for_sizing = live_free_usdt
            else:
                logger.warning("[LIVE] Could not read positive free USDT from exchange; using fallback capital %.2f", capital_for_sizing)
        except Exception as balance_err:
            logger.warning("[LIVE] Balance fetch failed; using fallback capital %.2f (%s)", capital_for_sizing, balance_err)

    pos = calculate_position_size(capital_for_sizing, entry, sl, consecutive_losses=loss_guard.consecutive_losses)

    if mode == "live" and live_engine is not None:
        max_affordable_qty = capital_for_sizing / entry if entry > 0 else 0.0
        if max_affordable_qty > 0:
            pos["qty"] = round(min(float(pos.get("qty") or 0.0), max_affordable_qty), 8)
            pos["usdt_value"] = round(pos["qty"] * entry, 2)
            pos["usdt_risk"] = round(pos["qty"] * abs(entry - sl), 2)
            pos["risk_pct"] = round((pos["usdt_risk"] / capital_for_sizing) * 100, 2) if capital_for_sizing > 0 else 0.0

    risk_params = {
        "sl": sl,
        "tp1": tps["tp1"],
        "tp2": tps["tp2"],
        "qty": pos["qty"],
        "usdt_value": pos["usdt_value"],
        "risk_pct": pos["risk_pct"],
    }

    _safe_send_signal_alert(signal, risk_params)
    _telemetry_record_event("last_signal", {
        "symbol": signal.get("symbol"),
        "score": signal.get("score"),
        "direction": signal.get("direction"),
    })
    logger.info("📲 Alert sent for %s | Score: %s", signal['symbol'], signal['score'])

    if mode == "paper":
        engine.open_position(signal, risk_params)
        return

    if mode == "live" and live_engine is not None:
        side = "buy" if signal["direction"] == "long" else "sell"
        order_type = LIVE_ORDER_TYPE if LIVE_ORDER_TYPE in {"market", "limit"} else "market"
        order_price = entry if order_type == "limit" else None
        order = live_engine.create_order(
            symbol=signal["symbol"],
            side=side,
            qty=risk_params["qty"],
            price=order_price,
            type=order_type,
            risk_params=risk_params,
        )
        if order is None:
            _telemetry_increment("live_orders_failed")
            _safe_send_panic_alert(f"Live order rejected/failed for {signal['symbol']} ({side})")
            return

        _telemetry_increment("live_orders_created")
        _telemetry_record_event("last_live_order", {
            "symbol": signal["symbol"],
            "side": side,
            "qty": risk_params["qty"],
            "type": order_type,
        })
        logger.info("[LIVE] Order created for %s (%s)", signal["symbol"], side)

async def run_bot_async(mode: str = "paper"):
    logger.info("🚀 Trading Bot Starting | Mode: %s (WebSocket)", mode.upper())
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

    timeframes = [PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME]
    ws_fetcher = WebSocketFetcher(ws_symbols, timeframes, use_private_auth=(mode == "live"))
    error_count = 0
    CIRCUIT_BREAKER_THRESHOLD = 3
    exchange = get_exchange(paper_mode=(mode == "paper"))
    live_engine = LiveTradeEngine(exchange) if mode == "live" else None
    candle_cache = {sym: {} for sym in ws_symbols}
    last_processed_primary_ts = {}
    rest_task = None
    signal_worker_task = None
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=SIGNAL_QUEUE_MAXSIZE)
    last_db_maintenance_day: date | None = None

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

    async def _signal_worker() -> None:
        while True:
            signal = await signal_queue.get()
            try:
                await asyncio.to_thread(_execute_signal, engine, loss_guard, signal, mode, live_engine)
            except Exception as signal_err:
                logger.error("Signal execution failed for %s: %s", signal.get("symbol"), signal_err)
                _telemetry_increment("signal_exec_errors")
                _safe_send_panic_alert(f"Signal execution failed for {signal.get('symbol')}: {signal_err}")
            finally:
                signal_queue.task_done()
                _telemetry_record_event("signal_queue_depth", signal_queue.qsize())

    async def _run_rest_fallback_loop():
        logger.warning("Switching to REST polling fallback mode.")
        _telemetry_increment("ws_fallback_rest")
        exchange = get_exchange(paper_mode=(mode == "paper"))

        fallback_day = date.today()
        while True:
            fallback_day = handle_daily_reset(engine, loss_guard, fallback_day)
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

            process_open_positions(exchange, engine, loss_guard, mode=mode, live_engine=live_engine)
            scan_for_signals(
                exchange,
                engine,
                loss_guard,
                mode,
                live_engine=live_engine,
                signal_queue=signal_queue,
                per_symbol_delay_seconds=REST_SYMBOL_DELAY_SECONDS,
            )
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def _run_rest_supplement_loop():
        if not rest_symbols:
            return
        while True:
            try:
                process_open_positions(exchange, engine, loss_guard, mode=mode, live_engine=live_engine)
                scan_for_signals(
                    exchange,
                    engine,
                    loss_guard,
                    mode,
                    live_engine=live_engine,
                    symbols_to_scan=rest_symbols,
                    signal_queue=signal_queue,
                    per_symbol_delay_seconds=REST_SYMBOL_DELAY_SECONDS,
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
        today = handle_daily_reset(engine, loss_guard, today)
        await _maybe_run_db_maintenance()

        # Position management can happen on any candle close for that symbol.
        active_positions = live_engine.positions if (mode == "live" and live_engine is not None) else engine.positions

        if mode == "live" and live_engine is not None:
            live_engine.reconcile_orders()

        if symbol in active_positions:
            latest_close = float(df["close"].iloc[-1])
            if mode == "paper":
                closed = engine.update_positions({symbol: latest_close})
                _telemetry_record_event("last_closed_count", len(closed))
                for c in closed:
                    _safe_send_trade_closed(
                        c["symbol"], c["status"], c["entry"],
                        c["exit_price"], c["pnl"], c["status"]
                    )
                    loss_guard.record_trade(c["pnl"])

        # Only trigger entry checks when the primary timeframe closes.
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

        signal = score_signal(primary.copy(), confirm.copy(), symbol)
        if signal["send_alert"]:
            _telemetry_increment("alerts_sent")
            try:
                signal_queue.put_nowait(signal)
                _telemetry_record_event("signal_queue_depth", signal_queue.qsize())
            except asyncio.QueueFull:
                logger.warning("Signal queue full; dropping signal for %s", symbol)
                _telemetry_increment("signals_dropped_queue_full")

    try:
        signal_worker_task = asyncio.create_task(_signal_worker())
        await _maybe_run_db_maintenance()
        await ws_fetcher.connect()
        if rest_symbols:
            rest_task = asyncio.create_task(_run_rest_supplement_loop())
        while True:
            try:
                await ws_fetcher.subscribe_klines(on_candle_close)
                error_count = 0  # reset on success
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


def run_startup_health_gate(skip_health_gate: bool = False) -> bool:
    if skip_health_gate:
        logger.warning("Health gate skipped by --skip-health-gate flag.")
        return True

    project_root = Path(__file__).resolve().parents[1]
    health_gate_script = project_root / "health_gate.py"
    if not health_gate_script.exists():
        logger.error("health_gate.py not found at %s", health_gate_script)
        return False

    logger.info("Running startup health gate...")
    proc = subprocess.run([sys.executable, str(health_gate_script)], cwd=str(project_root))
    if proc.returncode == 0:
        logger.info("Startup health gate passed.")
        return True

    logger.error("Startup health gate failed (exit code %s). Bot start blocked.", proc.returncode)
    return False


def run_live_arming_check(mode: str) -> bool:
    if mode != "live":
        return True

    if LIVE_CONFIRMATION_PHRASE == LIVE_CONFIRMATION_REQUIRED_VALUE:
        logger.info("Live arming phrase verified. Live mode enabled.")
        return True

    logger.error(
        "Live mode blocked: set LIVE_CONFIRMATION_PHRASE to the exact required value '%s' in .env",
        LIVE_CONFIRMATION_REQUIRED_VALUE,
    )
    return False


def run_live_exchange_readiness_check(mode: str) -> bool:
    if mode != "live":
        return True

    logger.info("Running live exchange readiness check...")
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        logger.error(
            "Live mode blocked: BINANCE_API_KEY/BINANCE_API_SECRET missing. Set both in .env before starting live mode."
        )
        return False

    try:
        exchange = get_exchange(paper_mode=False)
        balance = fetch_balance(exchange)
        usdt_free = float((balance or {}).get("USDT_free") or 0.0)
        if usdt_free <= 0:
            logger.error("Live mode blocked: Binance USDT_free balance is 0 or unavailable.")
            return False

        missing_symbols = [s for s in WATCHLIST if s not in (exchange.markets or {})]
        if missing_symbols:
            logger.error("Live mode blocked: watchlist symbols missing on Binance: %s", ", ".join(missing_symbols))
            return False

        logger.info("Live readiness passed: USDT_free=%.4f and all watchlist symbols available.", usdt_free)
        return True
    except Exception as readiness_err:
        msg = str(readiness_err)
        if "-2015" in msg or "Invalid API-key" in msg or "permissions" in msg:
            logger.error(
                "Live mode blocked: Binance auth failed (%s). Verify API key/secret, required permissions, and IP whitelist.",
                readiness_err,
            )
            return False
        logger.error("Live mode blocked: readiness check failed: %s", readiness_err)
        return False


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
    asyncio.run(run_bot_async(mode=cl_args.mode))
