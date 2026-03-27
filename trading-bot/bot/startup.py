"""
Startup Checks – health gate, live arming, exchange readiness.
Extracted from main.py for cleaner module boundaries.
"""
import logging
import subprocess
import sys
from pathlib import Path

from bot.config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    LIVE_CONFIRMATION_PHRASE, LIVE_CONFIRMATION_REQUIRED_VALUE,
    WATCHLIST,
)
from bot.data.fetcher import get_exchange, fetch_balance

logger = logging.getLogger(__name__)


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
