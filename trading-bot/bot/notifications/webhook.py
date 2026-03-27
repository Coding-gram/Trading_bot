"""
Webhook Notification Fallback (Suggestion #8)
Sends trade alerts via generic HTTP webhook as a fallback when Telegram is unavailable.
Supports Discord webhooks, Slack incoming webhooks, or any custom HTTP endpoint.
"""
import json
import logging
import os
import time

import requests

from bot import telemetry

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("NOTIFICATION_WEBHOOK_URL", "").strip()
WEBHOOK_TIMEOUT_SECONDS = int(os.getenv("NOTIFICATION_WEBHOOK_TIMEOUT", "10"))
WEBHOOK_MAX_RETRIES = int(os.getenv("NOTIFICATION_WEBHOOK_MAX_RETRIES", "2"))
WEBHOOK_RETRY_BACKOFF = float(os.getenv("NOTIFICATION_WEBHOOK_RETRY_BACKOFF", "1.0"))


def is_webhook_configured() -> bool:
    """Return True if a webhook URL is configured."""
    return bool(WEBHOOK_URL)


def send_webhook(payload: dict, *, url: str | None = None) -> bool:
    """Send a JSON payload to the configured webhook endpoint.

    Args:
        payload: Dict to JSON-encode and POST.
        url: Override URL (defaults to NOTIFICATION_WEBHOOK_URL env var).

    Returns:
        True if the webhook accepted the payload (2xx response).
    """
    target_url = (url or WEBHOOK_URL).strip()
    if not target_url:
        logger.debug("Webhook URL not configured, skipping.")
        return False

    for attempt in range(1, WEBHOOK_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                target_url,
                json=payload,
                timeout=WEBHOOK_TIMEOUT_SECONDS,
                headers={"Content-Type": "application/json"},
            )
            if 200 <= resp.status_code < 300:
                telemetry.increment("webhook_messages_sent")
                return True

            logger.warning(
                "Webhook %s returned %d (attempt %d/%d): %s",
                target_url, resp.status_code, attempt, WEBHOOK_MAX_RETRIES, resp.text[:200],
            )

            # Non-retriable client errors
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                return False

        except requests.exceptions.RequestException as err:
            logger.warning(
                "Webhook request failed (attempt %d/%d): %s",
                attempt, WEBHOOK_MAX_RETRIES, err,
            )

        if attempt < WEBHOOK_MAX_RETRIES:
            time.sleep(WEBHOOK_RETRY_BACKOFF * attempt)

    telemetry.increment("webhook_messages_failed")
    return False


def send_signal_webhook(signal: dict, risk: dict) -> bool:
    """Format and send a trade signal via webhook."""
    payload = {
        "event": "trade_signal",
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "score": signal.get("score"),
        "price": signal.get("price"),
        "stop_loss": risk.get("sl"),
        "tp1": risk.get("tp1"),
        "tp2": risk.get("tp2"),
        "qty": risk.get("qty"),
        "usdt_value": risk.get("usdt_value"),
        "risk_pct": risk.get("risk_pct"),
        "market_regime": signal.get("market_regime"),
        "patterns": signal.get("patterns", []),
        "details": signal.get("details", {}),
        "timestamp": time.time(),
    }
    return send_webhook(payload)


def send_trade_closed_webhook(symbol: str, status: str, entry: float, exit_price: float, pnl: float, reason: str) -> bool:
    """Send trade closed notification via webhook."""
    payload = {
        "event": "trade_closed",
        "symbol": symbol,
        "status": status,
        "entry": entry,
        "exit_price": exit_price,
        "pnl": pnl,
        "reason": reason,
        "timestamp": time.time(),
    }
    return send_webhook(payload)


def send_risk_alert_webhook(reason: str) -> bool:
    """Send risk alert via webhook."""
    payload = {
        "event": "risk_alert",
        "reason": reason,
        "timestamp": time.time(),
    }
    return send_webhook(payload)


def send_panic_alert_webhook(reason: str) -> bool:
    """Send panic/circuit breaker alert via webhook."""
    payload = {
        "event": "panic_alert",
        "reason": reason,
        "severity": "critical",
        "timestamp": time.time(),
    }
    return send_webhook(payload)
