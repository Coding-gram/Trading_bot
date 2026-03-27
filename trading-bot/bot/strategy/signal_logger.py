"""
Structured Signal Logger (Suggestion #11)
Logs every scored signal as a structured JSON line to a dedicated log file
for offline replay, analysis, and model training.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SIGNAL_LOG_DIR = os.getenv(
    "SIGNAL_LOG_DIR",
    str(Path(__file__).resolve().parents[1] / "logs"),
)
SIGNAL_LOG_FILE = os.path.join(SIGNAL_LOG_DIR, "signals.jsonl")


def _ensure_log_dir() -> None:
    """Create the log directory if it doesn't exist."""
    Path(SIGNAL_LOG_DIR).mkdir(parents=True, exist_ok=True)


def log_signal(signal: dict, risk_params: dict | None = None, extra: dict | None = None) -> None:
    """Append a structured JSON line for a scored signal.

    Args:
        signal: The full signal dict from score_signal().
        risk_params: Optional risk parameters (sl, tp1, tp2, qty, etc.).
        extra: Optional dict with additional context (mode, exchange, etc.).
    """
    try:
        _ensure_log_dir()

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": signal.get("symbol"),
            "direction": signal.get("direction"),
            "score": signal.get("score"),
            "send_alert": signal.get("send_alert"),
            "price": signal.get("price"),
            "market_regime": signal.get("market_regime"),
            "patterns": signal.get("patterns"),
            "details": signal.get("details"),
            "sr_levels": signal.get("sr_levels"),
        }

        if risk_params:
            record["risk"] = {
                "sl": risk_params.get("sl"),
                "tp1": risk_params.get("tp1"),
                "tp2": risk_params.get("tp2"),
                "qty": risk_params.get("qty"),
                "usdt_value": risk_params.get("usdt_value"),
                "risk_pct": risk_params.get("risk_pct"),
            }

        if extra:
            record["extra"] = extra

        with open(SIGNAL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    except Exception as e:
        logger.debug("Signal logging failed: %s", e)
