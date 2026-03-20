"""
Data Fetcher – pulls OHLCV candle data from Binance via CCXT
Supports both historical (for backtesting) and live streaming.
"""
import asyncio
import logging
import os
import random
import sys
import time
import pandas as pd
from bot.config import BINANCE_API_KEY, BINANCE_API_SECRET, EXCHANGE_ID


def _import_ccxt_with_quiet_stderr():
    """Import ccxt while suppressing noisy third-party git stderr output."""
    stderr_fd = None
    saved_stderr_fd = None
    null_fd = None
    try:
        stderr_fd = sys.stderr.fileno()
        saved_stderr_fd = os.dup(stderr_fd)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_fd, stderr_fd)
    except Exception:
        stderr_fd = None
        saved_stderr_fd = None
        null_fd = None

    try:
        import ccxt as ccxt_module
        return ccxt_module
    finally:
        if stderr_fd is not None and saved_stderr_fd is not None:
            os.dup2(saved_stderr_fd, stderr_fd)
            os.close(saved_stderr_fd)
        if null_fd is not None:
            os.close(null_fd)


ccxt = _import_ccxt_with_quiet_stderr()

logger = logging.getLogger(__name__)


def _is_time_sync_error(err: Exception) -> bool:
    msg = str(err).lower()
    return (
        "-1021" in msg
        or "timestamp" in msg
        or "invalidnonce" in msg
        or "ahead of the server" in msg
    )


def _resync_exchange_clock(exchange, retries: int = 3) -> bool:
    for attempt in range(1, retries + 1):
        try:
            exchange.load_time_difference()
            return True
        except Exception as err:
            wait_s = round(0.8 + random.uniform(0.1, 0.7) * attempt, 2)
            logger.warning("Time resync failed (%d/%d): %s; retrying in %.2fs", attempt, retries, err, wait_s)
            time.sleep(wait_s)
    return False


def get_exchange(paper_mode: bool = True):
    """Initialize CCXT Binance exchange object."""
    exchange_class = getattr(ccxt, EXCHANGE_ID)

    # Paper mode only needs public market data; avoid attaching invalid API
    # credentials (can cause CCXT/private auth failures even for some public calls).
    exchange_config = {
        "options": {
            "defaultType": "spot",
            "adjustForTimeDifference": True,
        },
        "enableRateLimit": True,
    }
    if not paper_mode:
        exchange_config["apiKey"] = BINANCE_API_KEY
        exchange_config["secret"] = BINANCE_API_SECRET

    exchange = exchange_class(exchange_config)
    
    # Explicitly load time difference to prevent Timestamp out of bounds error
    # Add retry logic because Binance API can intermittently fail/timeout
    for attempt in range(5):
        try:
            exchange.load_time_difference()
            break
        except Exception as e:
            logger.warning(f"Failed to load time difference (attempt {attempt+1}/5): {e}")
            time.sleep(1.0 + random.uniform(0.3, 1.0))

    # Pre-load markets with retry to avoid intermittent NetworkErrors (e.g. dapi/fapi blocks)
    for attempt in range(5):
        try:
            exchange.load_markets()
            break
        except Exception as e:
            logger.warning(f"Failed to load markets (attempt {attempt+1}/5): {e}")
            if _is_time_sync_error(e):
                _resync_exchange_clock(exchange)
            time.sleep(1.0 + random.uniform(0.3, 1.0))

    # Paper mode simulates trade execution locally — we still want
    # real market data (not Binance testnet which has stale/poor data).
    _ = paper_mode  # parameter kept for API compatibility
    return exchange


def fetch_ohlcv(exchange, symbol: str, timeframe: str = "1h", limit: int = 300) -> pd.DataFrame:
    """
    Fetch OHLCV candlestick data.
    Returns DataFrame with columns: open, high, low, close, volume
    """
    for attempt in range(1, 4):
        try:
            raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            df = df.astype(float)
            logger.info("Fetched %d candles for %s [%s]", len(df), symbol, timeframe)
            return df
        except ccxt.BaseError as e:
            if _is_time_sync_error(e):
                logger.warning("Time sync error while fetching %s [%s] (attempt %d/3): %s", symbol, timeframe, attempt, e)
                _resync_exchange_clock(exchange)
                time.sleep(0.4 + random.uniform(0.1, 0.6) * attempt)
                continue
            logger.error("Error fetching OHLCV for %s: %s", symbol, e)
            return pd.DataFrame()
    return pd.DataFrame()


def fetch_multi_timeframe(exchange, symbol: str, timeframes: list) -> dict:
    """
    Fetch OHLCV for multiple timeframes. Returns dict keyed by timeframe.
    """
    result = {}
    for tf in timeframes:
        result[tf] = fetch_ohlcv(exchange, symbol, timeframe=tf)
        time.sleep(0.3)  # respect rate limits
    return result


def fetch_ticker(exchange, symbol: str) -> dict:
    """Get current ticker price and 24h stats."""
    for attempt in range(1, 4):
        try:
            ticker = exchange.fetch_ticker(symbol)
            return {
                "price": ticker["last"],
                "bid": ticker["bid"],
                "ask": ticker["ask"],
                "volume_24h": ticker["quoteVolume"],
                "change_24h": ticker["percentage"],
            }
        except ccxt.BaseError as e:
            if _is_time_sync_error(e):
                logger.warning("Time sync error while fetching ticker %s (attempt %d/3): %s", symbol, attempt, e)
                _resync_exchange_clock(exchange)
                time.sleep(0.3 + random.uniform(0.1, 0.5) * attempt)
                continue
            logger.error("Ticker fetch error for %s: %s", symbol, e)
            return {}
    return {}


def fetch_balance(exchange) -> dict:
    """Get current USDT balance from exchange."""
    try:
        balance = exchange.fetch_balance()
        return {
            "USDT_free": balance["USDT"]["free"],
            "USDT_total": balance["USDT"]["total"],
        }
    except ccxt.BaseError as e:
        logger.error("Balance fetch error: %s", e)
        return {}


async def async_fetch_ohlcv(exchange, symbol: str, timeframe: str = "1h", limit: int = 300):
    """
    Async fetch OHLCV using WebSocket-enabled exchange object.
    Returns DataFrame with columns: open, high, low, close, volume
    """
    _ = limit
    try:
        raw = await exchange.watch_ohlcv(symbol, timeframe)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        logger.info("[WS] Got %d candles for %s [%s]", len(df), symbol, timeframe)
        return df
    except Exception as e:
        logger.error("[WS] Error fetching OHLCV for %s: %s", symbol, e)
        return pd.DataFrame()


async def async_fetch_multi_timeframe(exchange, symbol: str, timeframes: list) -> dict:
    """Async fetch OHLCV for multiple timeframes using WebSocket."""
    result = {}
    for tf in timeframes:
        result[tf] = await async_fetch_ohlcv(exchange, symbol, timeframe=tf)
        await asyncio.sleep(0.1)
    return result
