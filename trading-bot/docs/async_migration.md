# Async Migration Plan for Trading Bot

## Why Async?
- Synchronous ccxt calls are fine for <10 symbols, but slow for 50+ or low timeframes.
- Asyncio + ccxt.async_support allows parallel data fetches, reducing scan time and latency.

## Migration Steps
1. Refactor all data-fetching functions (fetch_ohlcv, fetch_multi_timeframe, fetch_ticker) to async equivalents using ccxt.async_support.
2. Change main bot loop to `async def` and use `await` for all exchange calls.
3. Use `asyncio.gather` to fetch data for all symbols in parallel.
4. Replace `time.sleep()` with `await asyncio.sleep()`.
5. Ensure all DB and file I/O is thread-safe or use async libraries if needed.
6. Test with a large watchlist and short intervals to confirm speedup and stability.

## Example (Pseudo-code)
```python
import asyncio
import ccxt.async_support as ccxt

async def fetch_symbol(exchange, symbol):
    return await exchange.fetch_ohlcv(symbol, timeframe="1h", limit=300)

async def main():
    exchange = ccxt.binance()
    symbols = ["BTC/USDT", "ETH/USDT", ...]
    results = await asyncio.gather(*(fetch_symbol(exchange, s) for s in symbols))
    # ...process results...
    await exchange.close()

asyncio.run(main())
```

## When to Migrate
- Only needed if you want to scan 50+ symbols or run on 1m/5m timeframes.
- For most users, current sync code is sufficient.
