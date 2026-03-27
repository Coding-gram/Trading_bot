"""
Telegram Notification System
Sends formatted trade alerts with signal details.
Semi-automatic: bot informs you → you decide to act.
"""
import html
import logging
import time
from collections import defaultdict, deque
from threading import Lock
import requests
from bot.config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_MIN_SECONDS_BETWEEN_MESSAGES,
    TELEGRAM_SIGNAL_ALERT_COOLDOWN_SECONDS,
    TELEGRAM_TRADE_ALERT_COOLDOWN_SECONDS,
    TELEGRAM_RISK_ALERT_COOLDOWN_SECONDS,
    TELEGRAM_PANIC_ALERT_COOLDOWN_SECONDS,
    TELEGRAM_EXEC_REJECT_ALERT_COOLDOWN_SECONDS,
    TELEGRAM_ESCALATION_WINDOW_SECONDS,
    TELEGRAM_ESCALATION_EVENT_THRESHOLD,
    AUTO_WEIGHT_TELEGRAM_APPROVE_COMMAND,
    AUTO_WEIGHT_TELEGRAM_REJECT_COMMAND,
)
from bot import telemetry

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5
_NOTIFY_LOCK = Lock()
_LAST_SENT_AT_BY_CATEGORY = defaultdict(float)
_EVENT_HISTORY_BY_CATEGORY = defaultdict(deque)


def _record_telemetry_event(metric: str, value):
    try:
        telemetry.record_event(metric, value)
    except Exception:
        return


def _telemetry_increment(metric: str, amount: int = 1):
    try:
        telemetry.increment(metric, amount)
    except Exception:
        return


def _message_allowed(category: str, cooldown_seconds: float, force: bool = False) -> bool:
    now = time.time()
    with _NOTIFY_LOCK:
        global_last = float(_LAST_SENT_AT_BY_CATEGORY.get("__global__", 0.0))
        if not force and TELEGRAM_MIN_SECONDS_BETWEEN_MESSAGES > 0 and (now - global_last) < TELEGRAM_MIN_SECONDS_BETWEEN_MESSAGES:
            return False

        last_sent = float(_LAST_SENT_AT_BY_CATEGORY.get(category, 0.0))
        if not force and cooldown_seconds > 0 and (now - last_sent) < cooldown_seconds:
            return False

        _LAST_SENT_AT_BY_CATEGORY["__global__"] = now
        _LAST_SENT_AT_BY_CATEGORY[category] = now
        return True


def _escalation_prefix(category: str) -> str:
    now = time.time()
    window = max(int(TELEGRAM_ESCALATION_WINDOW_SECONDS), 1)
    threshold = max(int(TELEGRAM_ESCALATION_EVENT_THRESHOLD), 1)
    with _NOTIFY_LOCK:
        history = _EVENT_HISTORY_BY_CATEGORY[category]
        history.append(now)
        cutoff = now - window
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= threshold:
            return f"🚨 <b>ESCALATED:</b> {len(history)} {category.upper()} events in {window // 60}m\n\n"
    return ""


def _format_price(value: float) -> str:
    """Format prices with adaptive precision so micro-priced assets remain visible."""
    price = float(value)
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    if price >= 0.01:
        return f"{price:.6f}"
    if price >= 0.0001:
        return f"{price:.8f}"
    return f"{price:.10f}"


def send_message(
    text: str,
    parse_mode: str = "HTML",
    *,
    category: str = "generic",
    cooldown_seconds: float = 0.0,
    force: bool = False,
) -> bool:
    """Send a basic telegram message."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not set. Skipping notification.")
        return False

    if not _message_allowed(category=category, cooldown_seconds=max(float(cooldown_seconds), 0.0), force=force):
        logger.info("Telegram message suppressed by rate limit [category=%s]", category)
        _telemetry_increment("telegram_messages_suppressed")
        _record_telemetry_event("telegram_last_suppressed", {"category": category, "at": time.time()})
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                f"{BASE_URL}/sendMessage",
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                _telemetry_increment("telegram_messages_sent")
                return True

            logger.error(
                "Telegram API Error %s (attempt %d/%d): %s",
                resp.status_code,
                attempt,
                MAX_RETRIES,
                resp.text,
            )

            if resp.status_code in (400, 401, 403):
                return False

        except requests.exceptions.RequestException as err:
            logger.error(
                "Telegram send failed (attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                err,
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    return False


def send_signal_alert(signal: dict, risk: dict):
    """
    Send a fully formatted trade signal alert.
    """
    direction = signal["direction"]
    emoji = "🟢 LONG " if direction == "long" else "🔴 SHORT"
    pats = html.escape(", ".join(signal.get("patterns", [])) or "None")
    breakdown = signal.get("details", {})
    details = "\n".join([f"  • {html.escape(str(k))}: {html.escape(str(v))}" for k, v in breakdown.items()])

    rec_size = risk.get('usdt_value', '?')
    risk_pct = risk.get('risk_pct', '?')
    entry_price = _format_price(signal['price'])
    stop_loss = _format_price(risk['sl'])
    target_1 = _format_price(risk['tp1'])
    target_2 = _format_price(risk['tp2'])

    msg = f"""
<b>🤖 TRADE SIGNAL – {signal['symbol']}</b>

<b>{emoji}</b> | Score: <b>{signal['score']}/100</b>
Market: <b>{signal.get('market_regime', 'N/A').title()}</b>

💰 <b>Recommended Size:</b> Buy <b>${rec_size}</b> (Risks {risk_pct}% of capital)

🟢 <b>Entry:</b> ${entry_price}
🛑 <b>Stop Loss:</b> ${stop_loss}
🎯 <b>Target 1:</b> ${target_1}
🎯 <b>Target 2:</b> ${target_2}

🕯️ <b>Patterns:</b> {pats}

<b>Confluence Breakdown:</b>
{details}

⚠️ <i>Trade at your own risk.</i>
<b>Reply /approve_{signal['symbol'].replace('/', '_')} or /skip.</b>
"""
    return send_message(
        msg.strip(),
        category="signal",
        cooldown_seconds=TELEGRAM_SIGNAL_ALERT_COOLDOWN_SECONDS,
    )


def send_trade_closed(symbol: str, status: str, entry: float, exit_price: float, pnl: float, reason: str):
    """Notify when a trade is closed."""
    emoji = "✅" if pnl > 0 else "❌"
    msg = f"""
{emoji} <b>TRADE CLOSED – {symbol}</b>

Status: <b>{status.upper()}</b>
Reason: <b>{reason.upper()}</b>
Entry: ${entry:.4f} → Exit: ${exit_price:.4f}
PnL: <b>${pnl:+.2f}</b>
"""
    send_message(
        msg.strip(),
        category="trade_closed",
        cooldown_seconds=TELEGRAM_TRADE_ALERT_COOLDOWN_SECONDS,
    )


def send_daily_summary(stats: dict):
    """Send end-of-day performance summary."""
    msg = f"""
📈 <b>Daily Summary</b>

Trades: {stats['total']} | Wins: {stats['wins']} | Losses: {stats['losses']}
Win Rate: <b>{stats['win_rate']}%</b>
Daily PnL: <b>${stats['total_pnl']:+.2f}</b>
Capital: <b>${stats['capital']:.2f}</b> ({stats['return_pct']:+.2f}%)
"""
    send_message(msg.strip(), category="daily_summary", force=True)


def send_risk_alert(reason: str):
    """Alert user when risk limit is hit."""
    prefix = _escalation_prefix("risk")
    msg = f"{prefix}⚠️ <b>RISK ALERT – Trading Halted</b>\n\nReason: {reason}"
    send_message(
        msg,
        category="risk",
        cooldown_seconds=TELEGRAM_RISK_ALERT_COOLDOWN_SECONDS,
    )


def send_execution_rejected(symbol: str, direction: str, score: int, mode: str, reason: str) -> bool:
    """Notify when a signal passed scoring but execution was rejected by safety guards."""
    side = str(direction or "").upper()
    safe_reason = html.escape(str(reason or "Unknown rejection reason"))
    msg = f"""
⚠️ <b>SIGNAL REJECTED – {symbol}</b>

Mode: <b>{html.escape(str(mode).upper())}</b>
Direction: <b>{html.escape(side)}</b>
Score: <b>{int(score or 0)}/100</b>

Reason: <b>{safe_reason}</b>
"""
    return send_message(
        msg.strip(),
        category="execution_rejected",
        cooldown_seconds=TELEGRAM_EXEC_REJECT_ALERT_COOLDOWN_SECONDS,
    )


def send_panic_alert(reason: str):
    """Send a PANIC alert to Telegram for circuit breaker events."""
    prefix = _escalation_prefix("panic")
    msg = f"""
{prefix}🚨 <b>CIRCUIT BREAKER TRIGGERED</b>

Reason: <b>{html.escape(reason)}</b>

All trading halted. Please investigate immediately.
"""
    send_message(
        msg.strip(),
        category="panic",
        cooldown_seconds=TELEGRAM_PANIC_ALERT_COOLDOWN_SECONDS,
        force=True,
    )


def send_weight_tuning_approval_request(request_id: str, closed_trades: int, preview: list[dict]) -> bool:
    """Send Telegram approval request before applying automatic weight tuning."""
    top = list(preview or [])[:5]
    if top:
        lines = []
        for item in top:
            indicator = html.escape(str(item.get("indicator") or "?"))
            old_w = int(item.get("old_weight") or 0)
            new_w = int(item.get("new_weight") or 0)
            edge = float(item.get("edge") or 0.0)
            lines.append(f"  • {indicator}: {old_w} → {new_w} (edge {edge:+.1f}%)")
        preview_text = "\n".join(lines)
    else:
        preview_text = "  • No weight changes proposed (kept for audit run)."

    approve_cmd = html.escape(f"{AUTO_WEIGHT_TELEGRAM_APPROVE_COMMAND} {request_id}")
    reject_cmd = html.escape(f"{AUTO_WEIGHT_TELEGRAM_REJECT_COMMAND} {request_id}")
    msg = f"""
🧠 <b>Auto-Tuning Approval Needed</b>

Request ID: <code>{html.escape(request_id)}</code>
Closed trades evaluated: <b>{int(closed_trades)}</b>

<b>Proposed changes:</b>
{preview_text}

Approve: <code>{approve_cmd}</code>
Reject: <code>{reject_cmd}</code>
"""
    return send_message(
        msg.strip(),
        category="weight_tuning_approval",
        cooldown_seconds=0,
        force=True,
    )


def poll_weight_tuning_decision(last_update_id: int, request_id: str) -> dict:
    """Poll Telegram getUpdates and return approval decision for request_id."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"decision": None, "last_update_id": int(last_update_id or 0)}

    base_update_id = int(last_update_id or 0)
    params = {
        "offset": base_update_id + 1,
        "limit": 50,
        "timeout": 0,
    }

    try:
        resp = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=10)
        if resp.status_code != 200:
            logger.warning("Telegram getUpdates error %s: %s", resp.status_code, resp.text)
            return {"decision": None, "last_update_id": base_update_id}
        payload = resp.json() if resp.content else {}
    except requests.exceptions.RequestException as err:
        logger.warning("Telegram getUpdates failed: %s", err)
        return {"decision": None, "last_update_id": base_update_id}
    except Exception as err:
        logger.warning("Telegram getUpdates parse failed: %s", err)
        return {"decision": None, "last_update_id": base_update_id}

    results = payload.get("result") or []
    if not isinstance(results, list):
        return {"decision": None, "last_update_id": base_update_id}

    approved_cmd = str(AUTO_WEIGHT_TELEGRAM_APPROVE_COMMAND or "/approve_tune").strip().lower()
    rejected_cmd = str(AUTO_WEIGHT_TELEGRAM_REJECT_COMMAND or "/reject_tune").strip().lower()
    expected_chat_id = str(TELEGRAM_CHAT_ID)
    expected_request_id = str(request_id or "").strip()

    decision = None
    max_update_id = base_update_id

    for update in results:
        if not isinstance(update, dict):
            continue
        update_id = int(update.get("update_id") or 0)
        if update_id > max_update_id:
            max_update_id = update_id

        msg = update.get("message") or update.get("edited_message") or {}
        if not isinstance(msg, dict):
            continue

        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if chat_id != expected_chat_id:
            continue

        text = str(msg.get("text") or "").strip()
        if not text:
            continue

        parts = text.split(maxsplit=1)
        command = parts[0].strip().lower()
        cmd_request_id = parts[1].strip() if len(parts) > 1 else ""
        matches_request = (not cmd_request_id) or (cmd_request_id == expected_request_id)

        if command == approved_cmd and matches_request:
            decision = "approve"
        elif command == rejected_cmd and matches_request:
            decision = "reject"

    return {"decision": decision, "last_update_id": max_update_id}


# ── Kill Switch (Suggestion #13) ─────────────────────────────────────────────

_KILL_SWITCH_COMMANDS = {"/stop", "/halt", "/kill", "/shutdown"}


def poll_kill_switch(last_update_id: int = 0) -> dict:
    """Check Telegram for a kill switch command to remotely halt the bot.

    Returns:
        Dict with 'triggered' (bool), 'command' (str or None),
        and 'last_update_id' (int).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"triggered": False, "command": None, "last_update_id": int(last_update_id or 0)}

    base_update_id = int(last_update_id or 0)
    params = {
        "offset": base_update_id + 1,
        "limit": 50,
        "timeout": 0,
    }

    try:
        resp = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=10)
        if resp.status_code != 200:
            return {"triggered": False, "command": None, "last_update_id": base_update_id}
        payload = resp.json() if resp.content else {}
    except Exception:
        return {"triggered": False, "command": None, "last_update_id": base_update_id}

    results = payload.get("result") or []
    if not isinstance(results, list):
        return {"triggered": False, "command": None, "last_update_id": base_update_id}

    expected_chat_id = str(TELEGRAM_CHAT_ID)
    max_update_id = base_update_id
    triggered_command = None

    for update in results:
        if not isinstance(update, dict):
            continue
        update_id = int(update.get("update_id") or 0)
        if update_id > max_update_id:
            max_update_id = update_id

        msg = update.get("message") or update.get("edited_message") or {}
        if not isinstance(msg, dict):
            continue

        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if chat_id != expected_chat_id:
            continue

        text = str(msg.get("text") or "").strip().lower()
        if text in _KILL_SWITCH_COMMANDS:
            triggered_command = text

    return {
        "triggered": triggered_command is not None,
        "command": triggered_command,
        "last_update_id": max_update_id,
    }
