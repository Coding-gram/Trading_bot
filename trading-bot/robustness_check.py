"""
Strategy robustness checker.

Runs lightweight walk-forward validation over recent candles and estimates
risk with Monte Carlo resampling of trade outcomes.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

import numpy as np

import bot.strategy.scorer as scorer
from bot.config import WATCHLIST
from bot.data.fetcher import fetch_ohlcv, get_exchange
from bot.risk.risk_manager import calculate_position_size, calculate_take_profits, choose_stop_loss


@dataclass
class BacktestResult:
    metrics: dict
    trades: list[float]
    equity_curve: list[float]


def _scale_weight(value, multiplier: float, minimum_abs: int = 1) -> int:
    sign = -1 if value < 0 else 1
    scaled_abs = max(int(round(abs(value) * multiplier)), minimum_abs)
    return sign * scaled_abs


def apply_analysis_weight_profile(
    trend_mult: float,
    macd_mult: float,
    penalty_mult: float,
) -> dict:
    """Apply robustness-only weight multipliers and return a snapshot for restore."""
    snapshot = dict(scorer.WEIGHTS)

    if "trend_alignment" in scorer.WEIGHTS:
        scorer.WEIGHTS["trend_alignment"] = _scale_weight(
            int(scorer.WEIGHTS["trend_alignment"]),
            trend_mult,
            minimum_abs=1,
        )

    if "macd_signal" in scorer.WEIGHTS:
        scorer.WEIGHTS["macd_signal"] = _scale_weight(
            int(scorer.WEIGHTS["macd_signal"]),
            macd_mult,
            minimum_abs=1,
        )

    penalty_keys = [
        "penalty_rsi_divergence",
        "penalty_contra_sr",
        "penalty_declining_volume",
        "penalty_htf_disagreement",
        "penalty_time_decay",
        "penalty_overextended",
        "penalty_htf_disagree",
    ]
    for key in penalty_keys:
        if key in scorer.WEIGHTS:
            scorer.WEIGHTS[key] = _scale_weight(
                int(scorer.WEIGHTS[key]),
                penalty_mult,
                minimum_abs=1,
            )

    return snapshot


def _align_until(df, ts):
    aligned = df[df.index <= ts]
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
        returns = np.array(trades, dtype=float) / max(starting_capital, 1.0)
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
    }


def run_backtest(df_1h, df_4h, df_daily, symbol: str, starting_capital: float = 1000.0) -> BacktestResult:
    capital = starting_capital
    equity_curve = [capital]
    trades: list[float] = []
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
        df_4h_slice = _align_until(df_4h, ts)
        df_daily_slice = _align_until(df_daily, ts)
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

    metrics = _compute_metrics(starting_capital, equity_curve, trades)
    return BacktestResult(metrics=metrics, trades=trades, equity_curve=equity_curve)


def monte_carlo_from_trades(
    trades: Iterable[float],
    starting_capital: float = 1000.0,
    paths: int = 2000,
    seed: int = 42,
) -> dict:
    trade_array = np.array(list(trades), dtype=float)
    if trade_array.size == 0:
        return {
            "paths": paths,
            "trades_per_path": 0,
            "p_loss": 0.0,
            "p_ruin_10pct": 0.0,
            "p_drawdown_20pct": 0.0,
            "median_final_capital": round(float(starting_capital), 2),
            "p05_final_capital": round(float(starting_capital), 2),
        }

    rng = np.random.default_rng(seed)
    n = trade_array.size
    samples = rng.choice(trade_array, size=(paths, n), replace=True)

    final_caps = []
    dd_hits_20 = 0
    loss_hits = 0
    ruin_hits_10 = 0

    for row in samples:
        eq = starting_capital
        peak = eq
        max_dd = 0.0
        for pnl in row:
            eq += float(pnl)
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
        final_caps.append(eq)
        if eq < starting_capital:
            loss_hits += 1
        if eq <= starting_capital * 0.90:
            ruin_hits_10 += 1
        if max_dd >= 0.20:
            dd_hits_20 += 1

    final_caps_arr = np.array(final_caps, dtype=float)
    return {
        "paths": int(paths),
        "trades_per_path": int(n),
        "p_loss": round(loss_hits / paths, 4),
        "p_ruin_10pct": round(ruin_hits_10 / paths, 4),
        "p_drawdown_20pct": round(dd_hits_20 / paths, 4),
        "median_final_capital": round(float(np.median(final_caps_arr)), 2),
        "p05_final_capital": round(float(np.percentile(final_caps_arr, 5)), 2),
    }


def run_walk_forward_robustness(
    symbols: list[str],
    n_candles: int,
    folds: int,
    mc_paths: int,
    seed: int,
) -> dict:
    exchange = get_exchange(paper_mode=True)
    report = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "config": {
            "symbols": symbols,
            "n_candles": n_candles,
            "folds": folds,
            "mc_paths": mc_paths,
            "seed": seed,
        },
        "symbols": {},
        "portfolio_summary": {},
    }

    all_val_trades: list[float] = []

    # Ensure fetched history can satisfy requested validation window.
    # Previously this path always fetched 300 candles per timeframe.
    one_h_limit = max(int(n_candles), 300)
    four_h_limit = max(int(n_candles // 4) + 120, 300)
    one_d_limit = max(int(n_candles // 24) + 120, 300)

    for symbol in symbols:
        df_1h = fetch_ohlcv(exchange, symbol, timeframe="1h", limit=one_h_limit)
        time.sleep(0.3)
        df_4h = fetch_ohlcv(exchange, symbol, timeframe="4h", limit=four_h_limit)
        time.sleep(0.3)
        df_daily = fetch_ohlcv(exchange, symbol, timeframe="1d", limit=one_d_limit)

        if df_1h is None or df_4h is None or df_daily is None or df_1h.empty or df_4h.empty or df_daily.empty:
            report["symbols"][symbol] = {"error": "missing timeframe data"}
            continue

        if n_candles > 0 and len(df_1h) > n_candles:
            df_1h = df_1h.iloc[-n_candles:]

        if len(df_1h) < 180:
            report["symbols"][symbol] = {"error": f"insufficient 1h candles ({len(df_1h)})"}
            continue

        split_grid = np.linspace(0.55, 0.8, num=max(folds, 1))
        fold_results = []
        symbol_val_trades: list[float] = []

        for split_ratio in split_grid:
            split_idx = int(len(df_1h) * float(split_ratio))
            split_idx = max(split_idx, 120)
            split_idx = min(split_idx, len(df_1h) - 60)
            split_ts = df_1h.index[split_idx]

            val_1h = df_1h.iloc[split_idx:]
            # Keep full HTF history for indicator/state context while only
            # evaluating trades on forward 1h validation bars.
            val_4h = df_4h
            val_daily = df_daily

            if val_1h.empty or val_4h.empty or val_daily.empty:
                continue

            val_result = run_backtest(val_1h, val_4h, val_daily, symbol)
            fold_results.append(
                {
                    "split_ratio": round(float(split_ratio), 3),
                    "metrics": val_result.metrics,
                }
            )
            symbol_val_trades.extend(val_result.trades)

        mc = monte_carlo_from_trades(symbol_val_trades, paths=mc_paths, seed=seed)
        all_val_trades.extend(symbol_val_trades)

        avg_sharpe = float(np.mean([f["metrics"]["sharpe"] for f in fold_results])) if fold_results else 0.0
        avg_pf = float(np.mean([f["metrics"]["profit_factor"] for f in fold_results])) if fold_results else 0.0
        avg_mdd = float(np.mean([f["metrics"]["max_drawdown"] for f in fold_results])) if fold_results else 0.0

        report["symbols"][symbol] = {
            "folds": fold_results,
            "aggregate": {
                "avg_sharpe": round(avg_sharpe, 4),
                "avg_profit_factor": round(avg_pf, 4),
                "avg_max_drawdown": round(avg_mdd, 4),
                "validation_trades": len(symbol_val_trades),
                "warning": (
                    "no validation trades; increase n-candles or lower signal threshold for analysis"
                    if len(symbol_val_trades) == 0
                    else ""
                ),
            },
            "monte_carlo": mc,
        }

    portfolio_warning = (
        "no validation trades across selected symbols; Monte Carlo risk stats are not informative"
        if len(all_val_trades) == 0
        else ""
    )

    report["portfolio_summary"] = {
        "validation_trades_total": len(all_val_trades),
        "warning": portfolio_warning,
        "monte_carlo": monte_carlo_from_trades(all_val_trades, paths=mc_paths, seed=seed),
    }

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward + Monte Carlo robustness checker")
    parser.add_argument("--symbols-limit", type=int, default=6, help="How many watchlist symbols to evaluate")
    parser.add_argument("--n-candles", type=int, default=700, help="1h candles to use per symbol")
    parser.add_argument("--folds", type=int, default=4, help="Number of walk-forward validation folds")
    parser.add_argument("--mc-paths", type=int, default=2000, help="Monte Carlo paths")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--output", default="robustness_report.json", help="Output report path")
    parser.add_argument(
        "--signal-score-override",
        type=int,
        default=None,
        help="Override scorer MIN_SIGNAL_SCORE for robustness analysis only",
    )
    parser.add_argument(
        "--trend-weight-mult",
        type=float,
        default=None,
        help="Robustness-only multiplier for trend_alignment weight",
    )
    parser.add_argument(
        "--macd-weight-mult",
        type=float,
        default=None,
        help="Robustness-only multiplier for macd_signal weight",
    )
    parser.add_argument(
        "--penalty-weight-mult",
        type=float,
        default=None,
        help="Robustness-only multiplier for penalty magnitudes",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    score_override = args.signal_score_override
    if score_override is None:
        env_override = os.getenv("ROBUSTNESS_SIGNAL_SCORE_OVERRIDE")
        if env_override is not None:
            try:
                score_override = int(env_override)
            except ValueError:
                score_override = None

    if score_override is not None:
        scorer.MIN_SIGNAL_SCORE = max(score_override, 0)

    trend_mult = args.trend_weight_mult
    if trend_mult is None:
        raw = os.getenv("ROBUSTNESS_TREND_WEIGHT_MULT")
        if raw is not None:
            try:
                trend_mult = float(raw)
            except ValueError:
                trend_mult = None

    macd_mult = args.macd_weight_mult
    if macd_mult is None:
        raw = os.getenv("ROBUSTNESS_MACD_WEIGHT_MULT")
        if raw is not None:
            try:
                macd_mult = float(raw)
            except ValueError:
                macd_mult = None

    penalty_mult = args.penalty_weight_mult
    if penalty_mult is None:
        raw = os.getenv("ROBUSTNESS_PENALTY_WEIGHT_MULT")
        if raw is not None:
            try:
                penalty_mult = float(raw)
            except ValueError:
                penalty_mult = None

    trend_mult = max(trend_mult if trend_mult is not None else 1.0, 0.1)
    macd_mult = max(macd_mult if macd_mult is not None else 1.0, 0.1)
    penalty_mult = max(penalty_mult if penalty_mult is not None else 1.0, 0.1)

    weights_snapshot = apply_analysis_weight_profile(
        trend_mult=trend_mult,
        macd_mult=macd_mult,
        penalty_mult=penalty_mult,
    )

    symbols = WATCHLIST[: max(args.symbols_limit, 1)]

    report = run_walk_forward_robustness(
        symbols=symbols,
        n_candles=max(args.n_candles, 200),
        folds=max(args.folds, 1),
        mc_paths=max(args.mc_paths, 100),
        seed=args.seed,
    )
    report.setdefault("config", {})["signal_score_override"] = score_override
    report.setdefault("config", {})["trend_weight_mult"] = round(float(trend_mult), 4)
    report.setdefault("config", {})["macd_weight_mult"] = round(float(macd_mult), 4)
    report.setdefault("config", {})["penalty_weight_mult"] = round(float(penalty_mult), 4)

    scorer.WEIGHTS.clear()
    scorer.WEIGHTS.update(weights_snapshot)

    output_path = Path(args.output)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Robustness report written to: {output_path.resolve()}")
    print("Portfolio Monte Carlo:", report["portfolio_summary"]["monte_carlo"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
