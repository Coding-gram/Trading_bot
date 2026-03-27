"""
Optimizer for Trading Strategy Weights.
Performs grid search using a lightweight backtest simulator with walk-forward
validation and risk-aware metrics (PnL, Sharpe, profit factor, drawdown, win rate).
"""
import itertools
import json
from pathlib import Path

import numpy as np

from bot.data.fetcher import fetch_multi_timeframe
from bot.risk.risk_manager import calculate_position_size, choose_stop_loss, calculate_take_profits
import bot.strategy.scorer as scorer

WEIGHTS_PATH = Path(__file__).parent.parent / "strategy" / "weights.json"

SEARCH_SPACE = {
    "macd_signal": [10, 15, 20],
    "rsi_zone": [10, 15, 20],
    "trend_alignment": [16, 20, 24],
    "confluence_4h_bonus": [5, 7, 9],
}


def _align_4h_until(df_4h, ts):
    aligned = df_4h[df_4h.index <= ts]
    return aligned if not aligned.empty else None


def _align_daily_until(df_daily, ts):
    aligned = df_daily[df_daily.index <= ts]
    return aligned if not aligned.empty else None


def _compute_metrics(starting_capital: float, equity_curve: list[float], trades: list[float]) -> dict:
    total_pnl = (equity_curve[-1] - starting_capital) if equity_curve else 0.0
    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p < 0]
    gross_profit = float(sum(wins))
    gross_loss = abs(float(sum(losses)))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0

    eq = np.array(equity_curve, dtype=float) if equity_curve else np.array([starting_capital], dtype=float)
    peak = np.maximum.accumulate(eq)
    drawdown = np.where(peak > 0, (peak - eq) / peak, 0.0)
    max_drawdown = float(drawdown.max()) if drawdown.size else 0.0

    if len(trades) >= 2:
        returns = np.array(trades, dtype=float) / starting_capital
        std = float(np.std(returns, ddof=1))
        sharpe = float(np.mean(returns) / std * np.sqrt(len(returns))) if std > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "total_pnl": round(total_pnl, 2),
        "sharpe": round(sharpe, 4),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_drawdown, 4),
        "win_rate": round(win_rate, 2),
        "trades": len(trades),
        "objective": float(
            sharpe * 2.0
            + profit_factor
            + (total_pnl / max(starting_capital, 1.0)) * 10.0
            - max_drawdown * 10.0
        ),
    }


def _run_backtest(df_1h, df_4h, df_daily, symbol: str, starting_capital: float = 1000.0) -> dict:
    capital = starting_capital
    equity_curve = [capital]
    trades = []
    position = None

    warmup = 80
    for i in range(warmup, len(df_1h) - 1):
        row = df_1h.iloc[i]
        ts = df_1h.index[i]

        if position is not None:
            high = float(row["high"])
            low = float(row["low"])
            direction = position["direction"]
            sl = float(position["sl"])
            tp2 = float(position["tp2"])
            qty = float(position["qty"])
            entry = float(position["entry"])

            exit_price = None
            if direction == "long":
                if low <= sl:
                    exit_price = sl
                elif high >= tp2:
                    exit_price = tp2
                pnl = (exit_price - entry) * qty if exit_price is not None else None
            else:
                if high >= sl:
                    exit_price = sl
                elif low <= tp2:
                    exit_price = tp2
                pnl = (entry - exit_price) * qty if exit_price is not None else None

            if pnl is not None:
                capital += pnl
                trades.append(float(pnl))
                equity_curve.append(capital)
                position = None

        if position is not None:
            continue

        df_1h_slice = df_1h.iloc[: i + 1]
        df_4h_slice = _align_4h_until(df_4h, ts)
        df_daily_slice = _align_daily_until(df_daily, ts)
        if df_4h_slice is None or len(df_4h_slice) < 60:
            continue

        signal = scorer.score_signal(df_1h_slice, df_4h_slice, symbol, df_daily=df_daily_slice)
        if not signal.get("send_alert"):
            continue

        direction = signal.get("direction")
        if direction not in {"long", "short"}:
            continue

        entry = float(df_1h.iloc[i + 1]["open"])
        sr = signal.get("sr_levels", {})
        fib = signal.get("fib_levels", {})
        atr = float(signal.get("atr", 0.0) or 0.0)

        sl = choose_stop_loss(entry, atr, direction, sr)
        tps = calculate_take_profits(entry, sl, direction, fib)
        pos = calculate_position_size(capital, entry, sl, consecutive_losses=0)
        qty = float(pos.get("qty") or 0.0)
        if qty <= 0:
            continue

        position = {
            "direction": direction,
            "entry": entry,
            "sl": float(sl),
            "tp2": float(tps["tp2"]),
            "qty": qty,
        }

    if position is not None:
        last_close = float(df_1h.iloc[-1]["close"])
        if position["direction"] == "long":
            pnl = (last_close - position["entry"]) * position["qty"]
        else:
            pnl = (position["entry"] - last_close) * position["qty"]
        capital += pnl
        trades.append(float(pnl))
        equity_curve.append(capital)

    return _compute_metrics(starting_capital, equity_curve, trades)


def _walk_forward_metrics(df_1h, df_4h, df_daily, symbol: str, split_ratio: float = 0.7) -> dict:
    split_idx = int(len(df_1h) * split_ratio)
    split_idx = max(split_idx, 120)
    split_ts = df_1h.index[split_idx]

    train_1h = df_1h.iloc[:split_idx]
    val_1h = df_1h.iloc[split_idx:]
    train_4h = df_4h[df_4h.index <= split_ts]
    val_4h = df_4h[df_4h.index > split_ts]
    train_daily = df_daily[df_daily.index <= split_ts]
    val_daily = df_daily[df_daily.index > split_ts]

    train_metrics = _run_backtest(train_1h, train_4h, train_daily, symbol)
    val_metrics = _run_backtest(val_1h, val_4h, val_daily, symbol)
    return {"train": train_metrics, "val": val_metrics}


def grid_search_optimize(exchange, symbol: str, n_candles: int = 700):
    with open(WEIGHTS_PATH, encoding="utf-8") as f:
        base_weights = json.load(f)

    dfs = fetch_multi_timeframe(exchange, symbol, ["1h", "4h", "1d"])
    df_1h = dfs.get("1h")
    df_4h = dfs.get("4h")
    df_daily = dfs.get("1d")

    if df_1h is None or df_4h is None or df_daily is None or df_1h.empty or df_4h.empty or df_daily.empty:
        raise RuntimeError("Missing timeframe data for optimizer run.")

    if n_candles > 0:
        df_1h = df_1h.iloc[-n_candles:]

    best = {
        "objective": -np.inf,
        "weights": base_weights.copy(),
        "metrics": None,
        "combo": None,
    }

    all_keys = list(SEARCH_SPACE.keys())
    for values in itertools.product(*SEARCH_SPACE.values()):
        candidate = base_weights.copy()
        candidate.update(dict(zip(all_keys, values)))

        scorer.WEIGHTS.clear()
        scorer.WEIGHTS.update(candidate)

        wf_metrics = _walk_forward_metrics(df_1h, df_4h, df_daily, symbol, split_ratio=0.7)
        objective = wf_metrics["val"]["objective"]

        if objective > best["objective"]:
            best.update({
                "objective": objective,
                "weights": candidate.copy(),
                "metrics": wf_metrics,
                "combo": dict(zip(all_keys, values)),
            })

    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(best["weights"], f, indent=2)

    print("Best params:", best["combo"])
    print("Validation metrics:", best["metrics"]["val"])
    print("Training metrics:", best["metrics"]["train"])
    print("Objective:", round(float(best["objective"]), 4))
    return best


if __name__ == "__main__":
    import ccxt

    exchange = ccxt.binance()
    grid_search_optimize(exchange, symbol="BTC/USDT", n_candles=700)
