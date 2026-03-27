"""
Signal Worker – signal deduplication, execution routing, and async worker.
Extracted from main.py for cleaner module boundaries.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from bot.config import (
    SIGNAL_DEDUP_COOLDOWN_SECONDS,
    SIGNAL_DEDUP_MAX_SCORE_DELTA,
    SIGNAL_DEDUP_MIN_PRICE_MOVE_PCT,
    LIVE_ORDER_TYPE,
    MAX_SIGNAL_STALENESS_SECONDS,
)
from bot.execution.trade_cooldown import cooldown_remaining_seconds
from bot.risk.risk_manager import (
    choose_stop_loss, calculate_take_profits, calculate_position_size,
)
from bot.notifications.telegram import (
    send_signal_alert, send_panic_alert, send_execution_rejected,
    send_trade_closed, send_daily_summary, send_risk_alert,
)
from bot.data.fetcher import fetch_balance
from bot import telemetry
from bot.strategy.signal_logger import log_signal as _log_signal_to_file

logger = logging.getLogger(__name__)

_LAST_EMITTED_SIGNAL_BY_SYMBOL: dict[str, dict] = {}



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


def _safe_send_signal_alert(signal: dict, risk_params: dict) -> bool:
    try:
        return send_signal_alert(signal, risk_params)
    except Exception as alert_err:
        logger.error("Signal alert failed for %s: %s", signal.get("symbol"), alert_err)
        _telemetry_increment("api_errors")
        return False


def _safe_send_panic_alert(message: str) -> None:
    try:
        send_panic_alert(message)
    except Exception as alert_err:
        logger.error("Panic alert failed: %s", alert_err)
        _telemetry_increment("api_errors")


def _safe_send_execution_rejected(signal: dict, mode: str, reason: str) -> None:
    try:
        send_execution_rejected(
            symbol=str(signal.get("symbol") or "?"),
            direction=str(signal.get("direction") or "neutral"),
            score=int(signal.get("score") or 0),
            mode=mode,
            reason=reason,
        )
    except Exception as alert_err:
        logger.error("Execution-rejected alert failed for %s: %s", signal.get("symbol"), alert_err)
        _telemetry_increment("api_errors")


def _safe_send_trade_closed(symbol: str, status: str, entry: float, exit_price: float, pnl: float, reason: str) -> None:
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


def _safe_send_risk_alert(reason: str) -> None:
    try:
        send_risk_alert(reason)
    except Exception as alert_err:
        logger.error("Risk alert failed: %s", alert_err)
        _telemetry_increment("api_errors")


def should_emit_signal(signal: dict) -> bool:
    """Check if this signal is sufficiently different from the last emitted one."""
    symbol = str(signal.get("symbol") or "").strip()
    direction = str(signal.get("direction") or "").strip().lower()
    score = int(signal.get("score") or 0)
    price = float(signal.get("price") or 0.0)
    now_ts = time.time()

    if not symbol or direction not in {"long", "short"}:
        return True

    prev = _LAST_EMITTED_SIGNAL_BY_SYMBOL.get(symbol)
    if prev is None:
        _LAST_EMITTED_SIGNAL_BY_SYMBOL[symbol] = {
            "at": now_ts,
            "direction": direction,
            "score": score,
            "price": price,
        }
        return True

    age = now_ts - float(prev.get("at") or 0.0)
    same_direction = direction == str(prev.get("direction") or "")
    prev_score = int(prev.get("score") or 0)
    prev_price = float(prev.get("price") or 0.0)
    score_delta = abs(score - prev_score)
    price_move_pct = abs(price - prev_price) / prev_price if prev_price > 0 else 0.0

    within_cooldown = age < max(SIGNAL_DEDUP_COOLDOWN_SECONDS, 0)
    looks_duplicate = (
        same_direction
        and score_delta <= max(SIGNAL_DEDUP_MAX_SCORE_DELTA, 0)
        and price_move_pct < max(SIGNAL_DEDUP_MIN_PRICE_MOVE_PCT, 0.0)
    )

    if within_cooldown and looks_duplicate:
        _telemetry_increment("signals_suppressed_duplicate")
        logger.info(
            "Suppressing duplicate signal %s (%s): age=%.0fs scoreΔ=%d price_move=%.4f%%",
            symbol,
            direction.upper(),
            age,
            score_delta,
            price_move_pct * 100,
        )
        return False

    _LAST_EMITTED_SIGNAL_BY_SYMBOL[symbol] = {
        "at": now_ts,
        "direction": direction,
        "score": score,
        "price": price,
    }
    return True


def execute_signal(engine, loss_guard, signal, mode, live_engine=None):
    """Calculate risk parameters and execute the trade/alert."""
    symbol = str(signal.get("symbol") or "")
    cooldown_remaining = cooldown_remaining_seconds(symbol)
    if cooldown_remaining > 0:
        minutes = cooldown_remaining // 60
        seconds = cooldown_remaining % 60
        reason = f"symbol cooldown active ({minutes}m {seconds}s remaining)"
        logger.info("Skipping %s signal due to trade cooldown: %s", symbol, reason)
        _telemetry_increment("symbols_skipped_trade_cooldown")
        _safe_send_execution_rejected(signal, mode=mode, reason=reason)
        return

    entry = signal["price"]
    atr = signal["atr"]
    sr = signal.get("sr_levels", {})
    fib = signal.get("fib_levels", {})

    sl = choose_stop_loss(entry, atr, signal["direction"], sr)
    tps = calculate_take_profits(entry, sl, signal["direction"], fib)

    capital_for_sizing = engine.capital
    live_balance_available = True
    if mode == "live" and live_engine is not None:
        try:
            bal = fetch_balance(live_engine.exchange)
            live_free_usdt = float((bal or {}).get("USDT_free") or 0.0)
            if live_free_usdt > 0:
                capital_for_sizing = live_free_usdt
            else:
                live_balance_available = False
                logger.warning("[LIVE] Could not read positive free USDT from exchange; refusing live order sizing")
        except Exception as balance_err:
            live_balance_available = False
            logger.warning("[LIVE] Balance fetch failed; refusing live order sizing (%s)", balance_err)

    if mode == "live" and not live_balance_available:
        _telemetry_increment("live_orders_balance_blocked")
        _safe_send_panic_alert(
            f"Live order blocked for {signal.get('symbol')}: unavailable/zero USDT_free balance"
        )
        return

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

    alert_delivered = _safe_send_signal_alert(signal, risk_params)
    try:
        _log_signal_to_file(signal, risk_params, extra={"mode": mode})
    except Exception:
        pass
    _telemetry_record_event("last_signal", {
        "symbol": signal.get("symbol"),
        "score": signal.get("score"),
        "direction": signal.get("direction"),
    })
    if alert_delivered:
        logger.info("📲 Alert sent for %s | Score: %s", signal['symbol'], signal['score'])
    else:
        logger.warning("📵 Alert delivery failed for %s | Score: %s", signal['symbol'], signal['score'])

    if mode == "paper":
        opened = engine.open_position(signal, risk_params)
        if not opened:
            reason = getattr(engine, "last_rejection_reason", None) or "paper execution guard rejected order"
            _safe_send_execution_rejected(signal, mode="paper", reason=reason)
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
            signal=signal,
        )
        if order is None:
            _telemetry_increment("live_orders_failed")
            reason = getattr(live_engine, "last_rejection_reason", None) or "live execution guard rejected order"
            _safe_send_execution_rejected(signal, mode="live", reason=reason)
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


async def signal_worker_loop(signal_queue: asyncio.Queue, engine, loss_guard, mode, live_engine=None):
    """Async consumer of the signal queue."""
    while True:
        signal = await signal_queue.get()
        try:
            # Drop stale signals that sat in the queue too long
            generated_at = signal.get("generated_at")
            if generated_at:
                try:
                    gen_dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
                    if gen_dt.tzinfo is None:
                        gen_dt = gen_dt.replace(tzinfo=timezone.utc)
                    age_s = (datetime.now(timezone.utc) - gen_dt).total_seconds()
                    if age_s > MAX_SIGNAL_STALENESS_SECONDS:
                        logger.warning(
                            "Dropping stale signal for %s (age=%.0fs > max=%ds)",
                            signal.get("symbol"), age_s, MAX_SIGNAL_STALENESS_SECONDS,
                        )
                        _telemetry_increment("signals_dropped_stale")
                        continue
                except Exception:
                    pass  # If parsing fails, execute anyway

            await asyncio.to_thread(execute_signal, engine, loss_guard, signal, mode, live_engine)
        except Exception as signal_err:
            logger.error("Signal execution failed for %s: %s", signal.get("symbol"), signal_err)
            _telemetry_increment("signal_exec_errors")
            _safe_send_panic_alert(f"Signal execution failed for {signal.get('symbol')}: {signal_err}")
        finally:
            signal_queue.task_done()
            _telemetry_record_event("signal_queue_depth", signal_queue.qsize())
