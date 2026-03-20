"""
Backtesting and Weight Optimization Engine
- Simulates trading using historical OHLCV data
- Optimizes weights in weights.json for best performance
"""
import argparse
import json
from pathlib import Path
import pandas as pd
import numpy as np
from bot.strategy import scorer
from bot.config import WATCHLIST, PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME
from bot.data.fetcher import get_exchange, fetch_multi_timeframe


def run_backtest(symbols, weights, limit=300):
    results = []
    exchange = get_exchange(paper_mode=True)
    for symbol in symbols:
        dfs = fetch_multi_timeframe(exchange, symbol, [PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME])
        df_1h = dfs.get(PRIMARY_TIMEFRAME)
        df_4h = dfs.get(CONFIRM_TIMEFRAME)
        if df_1h is None or df_4h is None or df_1h.empty or df_4h.empty:
            continue
        # Simulate rolling window
        for i in range(60, len(df_1h)):
            sub_1h = df_1h.iloc[:i+1]
            sub_4h = df_4h.iloc[:max(1, i//4)+1]
            # Patch weights for this run
            scorer.WEIGHTS = weights
            sig = scorer.score_signal(sub_1h, sub_4h, symbol)
            results.append({"symbol": symbol, "i": i, "score": sig["score"], "direction": sig["direction"], "send_alert": sig["send_alert"], "price": sig.get("price", 0)})
    return results


def evaluate_results(results):
    # Simple metric: count alerts, avg score, etc.
    alerts = [r for r in results if r["send_alert"]]
    avg_score = np.mean([r["score"] for r in results]) if results else 0
    return {"alerts": len(alerts), "avg_score": avg_score}


def optimize_weights(symbols, base_weights, n_iter=20):
    best = base_weights.copy()
    best_score = -np.inf
    for _ in range(n_iter):
        # Randomly perturb weights
        trial = {k: max(1, int(v * np.random.uniform(0.7, 1.3))) for k, v in base_weights.items()}
        results = run_backtest(symbols, trial)
        metrics = evaluate_results(results)
        score = metrics["alerts"] + metrics["avg_score"]
        if score > best_score:
            best = trial.copy()
            best_score = score
    return best, best_score


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--optimize", action="store_true", help="Run weight optimization")
    parser.add_argument("--limit", type=int, default=5, help="Number of symbols to use")
    args = parser.parse_args()

    weights_path = Path("bot/strategy/weights.json")
    with open(weights_path, encoding="utf-8") as f:
        base_weights = json.load(f)

    symbols = WATCHLIST[:args.limit]

    if args.optimize:
        best, score = optimize_weights(symbols, base_weights)
        print("Best weights:", best)
        print("Score:", score)
        with open("bot/strategy/weights_optimized.json", "w", encoding="utf-8") as f:
            json.dump(best, f, indent=2)
    else:
        results = run_backtest(symbols, base_weights)
        metrics = evaluate_results(results)
        print("Backtest metrics:", metrics)

if __name__ == "__main__":
    main()
