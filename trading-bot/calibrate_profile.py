"""
Calibrate robustness profile by sweeping score thresholds and selected weight multipliers.

This tool is analysis-only: it does not modify weights.json.
It writes ranked candidates and a best-profile env snippet.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

import bot.strategy.scorer as scorer
from bot.config import WATCHLIST
from bot.data.fetcher import fetch_ohlcv, get_exchange
from robustness_check import run_backtest, monte_carlo_from_trades, apply_analysis_weight_profile


@dataclass
class Candidate:
    min_signal_score: int
    trend_mult: float
    macd_mult: float
    penalty_mult: float


def _parse_float_grid(raw: str) -> list[float]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def _parse_int_grid(raw: str) -> list[int]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def _evaluate_candidate(
    candidate: Candidate,
    market_data: dict,
    n_candles: int,
    folds: int,
    mc_paths: int,
    seed: int,
) -> dict:
    original_min = scorer.MIN_SIGNAL_SCORE
    original_weights = dict(scorer.WEIGHTS)

    scorer.MIN_SIGNAL_SCORE = max(candidate.min_signal_score, 0)
    _ = apply_analysis_weight_profile(
        trend_mult=candidate.trend_mult,
        macd_mult=candidate.macd_mult,
        penalty_mult=candidate.penalty_mult,
    )

    symbols = {}
    all_trades: list[float] = []
    sharpe_values = []
    pf_values = []
    mdd_values = []

    try:
        split_grid = np.linspace(0.55, 0.8, num=max(folds, 1))

        for symbol, frames in market_data.items():
            df_1h = frames["1h"]
            df_4h = frames["4h"]
            df_daily = frames["1d"]

            if n_candles > 0 and len(df_1h) > n_candles:
                df_1h = df_1h.iloc[-n_candles:]

            if len(df_1h) < 180:
                symbols[symbol] = {"error": f"insufficient 1h candles ({len(df_1h)})"}
                continue

            fold_results = []
            symbol_trades: list[float] = []

            for split_ratio in split_grid:
                split_idx = int(len(df_1h) * float(split_ratio))
                split_idx = max(split_idx, 120)
                split_idx = min(split_idx, len(df_1h) - 60)

                val_1h = df_1h.iloc[split_idx:]
                if val_1h.empty:
                    continue

                val_result = run_backtest(val_1h, df_4h, df_daily, symbol)
                fold_results.append(
                    {
                        "split_ratio": round(float(split_ratio), 3),
                        "metrics": val_result.metrics,
                    }
                )
                symbol_trades.extend(val_result.trades)

            mc = monte_carlo_from_trades(symbol_trades, paths=mc_paths, seed=seed)
            all_trades.extend(symbol_trades)

            if fold_results:
                avg_sharpe = float(np.mean([f["metrics"]["sharpe"] for f in fold_results]))
                avg_pf = float(np.mean([f["metrics"]["profit_factor"] for f in fold_results]))
                avg_mdd = float(np.mean([f["metrics"]["max_drawdown"] for f in fold_results]))
            else:
                avg_sharpe, avg_pf, avg_mdd = 0.0, 0.0, 0.0

            sharpe_values.append(avg_sharpe)
            pf_values.append(avg_pf)
            mdd_values.append(avg_mdd)

            symbols[symbol] = {
                "aggregate": {
                    "avg_sharpe": round(avg_sharpe, 4),
                    "avg_profit_factor": round(avg_pf, 4),
                    "avg_max_drawdown": round(avg_mdd, 4),
                    "validation_trades": len(symbol_trades),
                },
                "monte_carlo": mc,
            }

        portfolio_mc = monte_carlo_from_trades(all_trades, paths=mc_paths, seed=seed)
        total_trades = len(all_trades)
        avg_pf_port = float(np.mean(pf_values)) if pf_values else 0.0
        avg_sharpe_port = float(np.mean(sharpe_values)) if sharpe_values else 0.0
        avg_mdd_port = float(np.mean(mdd_values)) if mdd_values else 0.0

        trades_score = min(total_trades / 300.0, 1.0) * 35.0
        loss_score = max(0.0, 1.0 - float(portfolio_mc.get("p_loss", 1.0))) * 20.0
        dd_score = max(0.0, 1.0 - float(portfolio_mc.get("p_drawdown_20pct", 1.0))) * 10.0
        pf_score = min(max(avg_pf_port, 0.0) / 1.5, 1.0) * 20.0
        sharpe_score = min(max(avg_sharpe_port, 0.0) / 1.5, 1.0) * 15.0

        objective = round(trades_score + loss_score + dd_score + pf_score + sharpe_score, 4)

        return {
            "candidate": {
                "min_signal_score": candidate.min_signal_score,
                "trend_mult": candidate.trend_mult,
                "macd_mult": candidate.macd_mult,
                "penalty_mult": candidate.penalty_mult,
            },
            "objective": objective,
            "portfolio": {
                "validation_trades_total": total_trades,
                "avg_profit_factor": round(avg_pf_port, 4),
                "avg_sharpe": round(avg_sharpe_port, 4),
                "avg_max_drawdown": round(avg_mdd_port, 4),
                "monte_carlo": portfolio_mc,
            },
            "symbols": symbols,
        }
    finally:
        scorer.MIN_SIGNAL_SCORE = original_min
        scorer.WEIGHTS.clear()
        scorer.WEIGHTS.update(original_weights)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate profile with threshold and weight sweeps")
    parser.add_argument("--symbols-limit", type=int, default=6)
    parser.add_argument("--n-candles", type=int, default=1200)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--mc-paths", type=int, default=800)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--score-grid", default="52,56,58")
    parser.add_argument("--trend-mults", default="1.0,1.1")
    parser.add_argument("--macd-mults", default="1.0,1.1")
    parser.add_argument("--penalty-mults", default="1.0,1.1")
    parser.add_argument("--output", default="calibration_report.json")
    parser.add_argument("--best-env-output", default="calibration_best.env")
    parser.add_argument("--baseline-report", default="calibration_report.json")
    parser.add_argument("--min-improvement", type=float, default=0.0)
    parser.add_argument("--apply-if-improved", action="store_true")
    parser.add_argument("--target-env", default=".env")
    parser.add_argument("--max-candidates", type=int, default=0)
    parser.add_argument("--checkpoint-every", type=int, default=5)
    return parser.parse_args()


def _load_baseline_objective(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    best = payload.get("best") if isinstance(payload, dict) else None
    if not isinstance(best, dict):
        return None
    objective = best.get("objective")
    try:
        return float(objective)
    except (TypeError, ValueError):
        return None


def _upsert_env_keys(env_path: Path, updates: dict[str, str]) -> None:
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = text.splitlines()

    remaining = dict(updates)
    out_lines: list[str] = []
    for line in lines:
        replaced = False
        for key, value in list(remaining.items()):
            if re.match(rf"^\s*{re.escape(key)}\s*=", line):
                out_lines.append(f"{key}={value}")
                remaining.pop(key, None)
                replaced = True
                break
        if not replaced:
            out_lines.append(line)

    if remaining:
        if out_lines and out_lines[-1].strip() != "":
            out_lines.append("")
        out_lines.append("# Auto-updated by calibrate_profile.py")
        for key, value in remaining.items():
            out_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()

    score_grid = _parse_int_grid(args.score_grid)
    trend_mults = _parse_float_grid(args.trend_mults)
    macd_mults = _parse_float_grid(args.macd_mults)
    penalty_mults = _parse_float_grid(args.penalty_mults)

    symbols = WATCHLIST[: max(args.symbols_limit, 1)]

    exchange = get_exchange(paper_mode=True)
    market_data = {}
    one_h_limit = max(int(args.n_candles), 300)
    four_h_limit = max(int(args.n_candles // 4) + 120, 300)
    one_d_limit = max(int(args.n_candles // 24) + 120, 300)

    for symbol in symbols:
        df_1h = fetch_ohlcv(exchange, symbol, timeframe="1h", limit=one_h_limit)
        time.sleep(0.3)
        df_4h = fetch_ohlcv(exchange, symbol, timeframe="4h", limit=four_h_limit)
        time.sleep(0.3)
        df_daily = fetch_ohlcv(exchange, symbol, timeframe="1d", limit=one_d_limit)
        if df_1h is None or df_4h is None or df_daily is None or df_1h.empty or df_4h.empty or df_daily.empty:
            continue
        market_data[symbol] = {"1h": df_1h, "4h": df_4h, "1d": df_daily}

    if not market_data:
        raise SystemExit("No symbol data available for calibration")

    candidates = [
        Candidate(min_signal_score=s, trend_mult=t, macd_mult=m, penalty_mult=p)
        for s, t, m, p in itertools.product(score_grid, trend_mults, macd_mults, penalty_mults)
    ]
    if args.max_candidates > 0:
        candidates = candidates[: args.max_candidates]

    results = []
    interrupted = False
    try:
        for idx, cand in enumerate(candidates, start=1):
            print(
                f"[{idx}/{len(candidates)}] score={cand.min_signal_score} "
                f"trend={cand.trend_mult} macd={cand.macd_mult} penalty={cand.penalty_mult}"
            )
            results.append(
                _evaluate_candidate(
                    candidate=cand,
                    market_data=market_data,
                    n_candles=max(args.n_candles, 200),
                    folds=max(args.folds, 1),
                    mc_paths=max(args.mc_paths, 100),
                    seed=args.seed,
                )
            )
            if args.checkpoint_every > 0 and idx % args.checkpoint_every == 0:
                checkpoint_payload = {
                    "created_at_utc": datetime.now(UTC).isoformat(),
                    "in_progress": True,
                    "evaluated_candidates": idx,
                    "total_candidates": len(candidates),
                    "top3": sorted(results, key=lambda r: r["objective"], reverse=True)[:3],
                }
                Path(args.output).write_text(json.dumps(checkpoint_payload, indent=2), encoding="utf-8")
    except KeyboardInterrupt:
        interrupted = True
        print("Calibration interrupted; finalizing report with evaluated candidates so far...")

    if not results:
        raise SystemExit("No candidates evaluated; calibration report not generated.")

    ranked = sorted(results, key=lambda r: r["objective"], reverse=True)
    best = ranked[0]
    best_objective = float(best["objective"])
    baseline_path = Path(args.baseline_report)
    baseline_objective = _load_baseline_objective(baseline_path)
    improvement = (
        best_objective - baseline_objective
        if baseline_objective is not None
        else None
    )

    should_apply = False
    if args.apply_if_improved:
        if baseline_objective is None:
            should_apply = True
        else:
            should_apply = improvement is not None and improvement >= float(args.min_improvement)

    report = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "config": {
            "symbols": list(market_data.keys()),
            "n_candles": max(args.n_candles, 200),
            "folds": max(args.folds, 1),
            "mc_paths": max(args.mc_paths, 100),
            "seed": args.seed,
            "score_grid": score_grid,
            "trend_mults": trend_mults,
            "macd_mults": macd_mults,
            "penalty_mults": penalty_mults,
            "total_candidates": len(candidates),
            "evaluated_candidates": len(results),
        },
        "best": best,
        "top5": ranked[:5],
        "baseline": {
            "report_path": str(baseline_path),
            "objective": baseline_objective,
            "min_improvement": float(args.min_improvement),
            "improvement": round(float(improvement), 6) if improvement is not None else None,
            "apply_if_improved": bool(args.apply_if_improved),
            "applied": bool(should_apply),
            "target_env": args.target_env,
            "interrupted": interrupted,
        },
    }

    output_path = Path(args.output)
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    best_candidate = best["candidate"]
    env_lines = [
        f"ROBUSTNESS_GATE_SIGNAL_SCORE_OVERRIDE={best_candidate['min_signal_score']}",
        f"ROBUSTNESS_GATE_TREND_WEIGHT_MULT={best_candidate['trend_mult']}",
        f"ROBUSTNESS_GATE_MACD_WEIGHT_MULT={best_candidate['macd_mult']}",
        f"ROBUSTNESS_GATE_PENALTY_WEIGHT_MULT={best_candidate['penalty_mult']}",
    ]
    best_env_output = Path(args.best_env_output)
    best_env_output.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    if should_apply:
        _upsert_env_keys(
            Path(args.target_env),
            {
                "ROBUSTNESS_GATE_SIGNAL_SCORE_OVERRIDE": str(best_candidate["min_signal_score"]),
                "ROBUSTNESS_GATE_TREND_WEIGHT_MULT": str(best_candidate["trend_mult"]),
                "ROBUSTNESS_GATE_MACD_WEIGHT_MULT": str(best_candidate["macd_mult"]),
                "ROBUSTNESS_GATE_PENALTY_WEIGHT_MULT": str(best_candidate["penalty_mult"]),
            },
        )

    print(f"Calibration report written to: {output_path.resolve()}")
    print(f"Best profile env snippet written to: {best_env_output.resolve()}")
    if args.apply_if_improved:
        if baseline_objective is None:
            print("No baseline objective found; applied best profile to target env.")
        else:
            print(
                "Baseline objective:",
                round(float(baseline_objective), 6),
                "| Best objective:",
                round(float(best_objective), 6),
                "| Improvement:",
                round(float(improvement or 0.0), 6),
                "| Applied:",
                should_apply,
            )
    print("Best candidate:", best_candidate)
    print("Best portfolio:", best["portfolio"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
