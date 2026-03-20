# WebSocket Streaming for Low Latency

## Why WebSockets?
- REST polling can miss fast price moves and is less efficient.
- WebSockets provide real-time updates for price, candles, and order book.

## Implementation Plan
1. Use `python-binance` or `ccxt.pro` for WebSocket support.
2. Subscribe to relevant streams (e.g., kline/candlestick, ticker) for all symbols in your watchlist.
3. On each new candle or price update, trigger your scoring and signal logic.
4. Maintain a local cache of recent candles for each symbol.
5. Fall back to REST polling if the WebSocket connection drops.

## Example (Pseudo-code)
```python
from binance import AsyncClient, BinanceSocketManager
import asyncio

async def main():
    client = await AsyncClient.create()
    bm = BinanceSocketManager(client)
    async with bm.kline_socket('btcusdt', interval='1m') as stream:
        while True:
            msg = await stream.recv()
            # Process msg, update local candle cache, run scoring
    await client.close_connection()

asyncio.run(main())
```

## Notes
- WebSocket code is more complex than REST, but essential for high-frequency or low-latency strategies.
- Test thoroughly before using for live trading.
