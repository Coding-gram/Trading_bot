"""
Optimizer for Trading Strategy Weights
Performs grid search or genetic algorithm to optimize weights.json for best backtest performance.
"""
import json
import itertools
import numpy as np
from pathlib import Path
from bot.strategy.scorer import score_signal
from bot.data.fetcher import fetch_multi_timeframe

WEIGHTS_PATH = Path(__file__).parent.parent / "strategy" / "weights.json"

# Example: grid search over MACD and RSI weights
SEARCH_SPACE = {
    "macd_signal": [10, 15, 20],
    "rsi_zone": [10, 15, 20],
    # Add more weights as needed
}


def grid_search_optimize(exchange, symbol, timeframes, n_candles=500):
    best_score = -np.inf
    best_weights = None
    all_keys = list(SEARCH_SPACE.keys())
    for values in itertools.product(*SEARCH_SPACE.values()):
        weights = dict(zip(all_keys, values))
        # Patch weights.json
        with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump(weights, f, indent=2)
        # Run backtest
        dfs = fetch_multi_timeframe(exchange, symbol, timeframes)
        df_1h = dfs.get("1h")
        df_4h = dfs.get("4h")
        if df_1h is None or df_4h is None:
            continue
        # Simulate signals
        signals = [score_signal(df_1h.iloc[:i+1], df_4h.iloc[:i+1], symbol) for i in range(n_candles)]
        # Calculate performance metric (e.g., sum of scores)
        perf = sum(s["score"] for s in signals if s["send_alert"])
        if perf > best_score:
            best_score = perf
            best_weights = weights.copy()
    print("Best weights:", best_weights)
    print("Best score:", best_score)
    # Save best weights
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(best_weights, f, indent=2)

if __name__ == "__main__":
    # Example usage
    import ccxt
    exchange = ccxt.binance()
    symbol = "BTC/USDT"
    timeframes = ["1h", "4h"]
    grid_search_optimize(exchange, symbol, timeframes)
