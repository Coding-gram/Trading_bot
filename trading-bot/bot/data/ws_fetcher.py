"""
Async WebSocket Data Fetcher for Binance using ccxt.pro
Subscribes to 1H and 4H kline streams and triggers callbacks on candle close.
"""
import asyncio
import logging
import random
import ccxt.pro
from bot.config import (
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    EXCHANGE_ID,
    WS_WATCHDOG_TIMEOUT_SECONDS,
    WS_WATCHDOG_MAX_TIMEOUTS,
)

logger = logging.getLogger(__name__)

class WebSocketFetcher:
    def __init__(self, symbols, timeframes, use_private_auth: bool = False):
        self.symbols = symbols
        self.timeframes = timeframes
        self.use_private_auth = use_private_auth
        self.exchange = None
        self._running = False
        self._last_closed_ts = {}
        self._error_counts = {}
        self._stall_counts = {}

    @staticmethod
    def _is_time_sync_error(err: Exception) -> bool:
        msg = str(err).lower()
        return (
            "-1021" in msg
            or "timestamp" in msg
            or "invalidnonce" in msg
            or "ahead of the server" in msg
        )

    @staticmethod
    def _is_auth_error(err: Exception) -> bool:
        msg = str(err).lower()
        return (
            "-2015" in msg
            or "invalid api-key" in msg
            or ("api-key" in msg and "permissions" in msg)
            or "authentication" in msg
        )

    @staticmethod
    def _is_policy_violation(err: Exception) -> bool:
        msg = str(err).lower()
        return "1008" in msg or "policy violation" in msg

    async def _resync_time(self, retries: int = 3) -> bool:
        if not self.exchange:
            return False
        for attempt in range(1, retries + 1):
            try:
                await self.exchange.load_time_difference()
                return True
            except Exception as err:
                wait_s = round(0.8 + random.uniform(0.1, 0.8) * attempt, 2)
                logger.warning("WebSocket time resync failed (%d/%d): %s; retrying in %.2fs", attempt, retries, err, wait_s)
                await asyncio.sleep(wait_s)
        return False

    async def connect(self):
        exchange_class = getattr(ccxt.pro, EXCHANGE_ID)
        config = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True}
        }
        if self.use_private_auth and BINANCE_API_KEY and BINANCE_API_SECRET:
            config["apiKey"] = BINANCE_API_KEY
            config["secret"] = BINANCE_API_SECRET
        self.exchange = exchange_class(config)

        # Retry time sync if nonce/timestamp drift occurs.
        last_error = None
        for attempt in range(5):
            try:
                await self.exchange.load_time_difference()
                await self.exchange.load_markets()
                auth_mode = "private" if self.use_private_auth else "public"
                logger.info("Connected to Binance WebSocket (%s mode).", auth_mode)
                return
            except Exception as e:
                last_error = e
                if self._is_auth_error(e):
                    logger.error("Exchange auth error during WebSocket connect: %s", e)
                    raise ConnectionError("WebSocket authentication failed") from e
                if self._is_time_sync_error(e):
                    logger.warning(f"Time sync issue (attempt {attempt+1}/5): {e}")
                    await self._resync_time()
                    await asyncio.sleep(1.0 + random.uniform(0.3, 1.0))
                else:
                    logger.error(f"Exchange connect error: {e}")
                    await asyncio.sleep(1.0 + random.uniform(0.3, 1.0))
        raise ConnectionError(f"WebSocket connect failed after retries: {last_error}")

    async def subscribe_klines(self, on_candle_close):
        self._running = True
        tasks = []
        for symbol in self.symbols:
            for tf in self.timeframes:
                tasks.append(asyncio.create_task(self._kline_worker(symbol, tf, on_candle_close)))
        await asyncio.gather(*tasks)

    async def _kline_worker(self, symbol, timeframe, on_candle_close):
        key = (symbol, timeframe)
        while self._running:
            try:
                ohlcv = await asyncio.wait_for(
                    self.exchange.watch_ohlcv(symbol, timeframe),
                    timeout=max(15, WS_WATCHDOG_TIMEOUT_SECONDS),
                )
                self._stall_counts[key] = 0
                if not ohlcv:
                    continue

                closed_payload = None
                # Some exchanges include a close flag at index 6 in each candle row.
                # Prefer explicit close-flag semantics when available.
                if len(ohlcv[-1]) > 6 and bool(ohlcv[-1][6]):
                    closed_payload = ohlcv
                elif len(ohlcv) >= 2:
                    # Most streams send the currently-forming candle as the last row.
                    # Use the penultimate row as the most recent fully-closed candle.
                    closed_payload = ohlcv[:-1]

                if not closed_payload:
                    continue

                closed_ts = int(closed_payload[-1][0])
                key = (symbol, timeframe)
                if self._last_closed_ts.get(key) == closed_ts:
                    continue

                self._last_closed_ts[key] = closed_ts
                self._error_counts[key] = 0
                logger.info("Candle closed: %s %s @ %s", symbol, timeframe, closed_ts)
                await on_candle_close(symbol, timeframe, closed_payload)
            except asyncio.CancelledError:
                return
            except asyncio.TimeoutError:
                self._stall_counts[key] = self._stall_counts.get(key, 0) + 1
                timeout_count = self._stall_counts[key]
                logger.warning(
                    "WebSocket stalled for %s %s (timeout %d/%d, %ss)",
                    symbol,
                    timeframe,
                    timeout_count,
                    WS_WATCHDOG_MAX_TIMEOUTS,
                    max(15, WS_WATCHDOG_TIMEOUT_SECONDS),
                )
                if timeout_count >= max(1, WS_WATCHDOG_MAX_TIMEOUTS):
                    self._running = False
                    raise ConnectionError(
                        f"WebSocket stalled for {symbol} {timeframe} after {timeout_count} consecutive timeouts"
                    )
                await asyncio.sleep(1)
                continue
            except Exception as e:
                if self._is_auth_error(e):
                    self._running = False
                    logger.error("WebSocket auth error for %s %s: %s", symbol, timeframe, e)
                    raise ConnectionError(f"WebSocket auth failed for {symbol} {timeframe}") from e
                if self._is_time_sync_error(e):
                    logger.warning(f"WebSocket time sync error for {symbol} {timeframe}: {e}")
                    await self._resync_time()
                    await asyncio.sleep(1.0 + random.uniform(0.5, 1.5))
                    continue
                if self._is_policy_violation(e):
                    self._error_counts[key] = self._error_counts.get(key, 0) + 1
                    count = self._error_counts[key]
                    logger.error("WebSocket policy error for %s %s: %s (count=%d)", symbol, timeframe, e, count)
                    if count >= 3:
                        self._running = False
                        raise ConnectionError(
                            f"WebSocket policy violation persisted for {symbol} {timeframe}"
                        ) from e
                    await asyncio.sleep(2)
                    continue
                logger.error(f"WebSocket error for {symbol} {timeframe}: {e}")
                await asyncio.sleep(5)

    async def close(self):
        self._running = False
        if self.exchange:
            try:
                await self.exchange.close()
            except Exception as e:
                logger.warning("Error closing WebSocket exchange: %s", e)
