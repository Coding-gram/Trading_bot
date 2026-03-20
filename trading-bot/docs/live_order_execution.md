# Live Order Execution Engine

## Why?
- Enables full automation: bot can place, update, and close real trades on Binance.

## Implementation Plan
1. Use `ccxt` or `python-binance` for authenticated order placement.
2. Add a `LiveTradeEngine` class (parallel to `PaperTradeEngine`) with methods for:
   - Placing market/limit orders
   - Setting stop-loss and take-profit
   - Monitoring open orders and positions
   - Handling order errors, retries, and API rate limits
3. Add a config flag to switch between paper and live mode.
4. Always use API keys with lowest possible permissions and store securely.
5. Add dry-run/test mode for safety.

## Example (Pseudo-code)
```python
import ccxt
exchange = ccxt.binance({
    'apiKey': 'YOUR_KEY',
    'secret': 'YOUR_SECRET',
    'enableRateLimit': True,
})
order = exchange.create_market_buy_order('BTC/USDT', 0.001)
```

## Safety Tips
- Always test with small size and in Binance testnet first.
- Implement error handling for insufficient funds, network errors, etc.
- Log all order actions and responses for audit.
