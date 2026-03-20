def send_panic_alert(reason: str):
    """Send a PANIC alert to Telegram for circuit breaker events."""
    msg = f"""
🚨 <b>CIRCUIT BREAKER TRIGGERED</b>

Reason: <b>{html.escape(reason)}</b>

All trading halted. Please investigate immediately.
"""
    send_message(msg.strip())
"""
Telegram Notification System
Sends formatted trade alerts with signal details.
Semi-automatic: bot informs you → you decide to act.
"""
import html
import logging
import time
import requests
from bot.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a basic telegram message."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials not set. Skipping notification.")
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

    msg = f"""
<b>🤖 TRADE SIGNAL – {signal['symbol']}</b>

<b>{emoji}</b> | Score: <b>{signal['score']}/100</b>
Market: <b>{signal.get('market_regime', 'N/A').title()}</b>

💰 <b>Recommended Size:</b> Buy <b>${rec_size}</b> (Risks {risk_pct}% of capital)

🟢 <b>Entry:</b> ${signal['price']:.4f}
🛑 <b>Stop Loss:</b> ${risk['sl']:.4f}
🎯 <b>Target 1:</b> ${risk['tp1']:.4f}
🎯 <b>Target 2:</b> ${risk['tp2']:.4f}

🕯️ <b>Patterns:</b> {pats}

<b>Confluence Breakdown:</b>
{details}

⚠️ <i>Trade at your own risk.</i>
<b>Reply /approve_{signal['symbol'].replace('/', '_')} or /skip.</b>
"""
    send_message(msg.strip())


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
    send_message(msg.strip())


def send_daily_summary(stats: dict):
    """Send end-of-day performance summary."""
    msg = f"""
📈 <b>Daily Summary</b>

Trades: {stats['total']} | Wins: {stats['wins']} | Losses: {stats['losses']}
Win Rate: <b>{stats['win_rate']}%</b>
Daily PnL: <b>${stats['total_pnl']:+.2f}</b>
Capital: <b>${stats['capital']:.2f}</b> ({stats['return_pct']:+.2f}%)
"""
    send_message(msg.strip())


def send_risk_alert(reason: str):
    """Alert user when risk limit is hit."""
    msg = f"⚠️ <b>RISK ALERT – Trading Halted</b>\n\nReason: {reason}"
    send_message(msg)
