"""
Pre-run health gate.
Runs key checks and exits non-zero if any gate fails.

Usage:
  python health_gate.py
"""
import subprocess
import sys
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file(ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _to_float(value, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run_step(label: str, args: list[str]) -> bool:
    print(f"\n=== {label} ===")
    proc = subprocess.run(args, cwd=ROOT)
    if proc.returncode == 0:
        print(f"[PASS] {label}")
        return True
    print(f"[FAIL] {label} (exit {proc.returncode})")
    return False


def run_live_fetch_probe() -> bool:
    print("\n=== Live fetch probe (1 symbol / 2 timeframes) ===")
    code = (
        "from bot.data.fetcher import get_exchange, fetch_multi_timeframe;"
        "ex=get_exchange(paper_mode=True);"
        "dfs=fetch_multi_timeframe(ex,'BTC/USDT',['1h','4h']);"
        "ok=all(dfs.get(tf) is not None and not dfs[tf].empty for tf in ['1h','4h']);"
        "print('probe_ok=', ok);"
        "raise SystemExit(0 if ok else 1)"
    )
    proc = subprocess.run([PY, "-c", code], cwd=ROOT)
    if proc.returncode == 0:
        print("[PASS] Live fetch probe")
        return True
    print(f"[FAIL] Live fetch probe (exit {proc.returncode})")
    return False


def run_robustness_gate() -> bool:
    print("\n=== Robustness threshold gate ===")

    if not _env_bool("ROBUSTNESS_GATE_ENABLED", False):
        print("[SKIP] Robustness gate disabled (set ROBUSTNESS_GATE_ENABLED=true to enforce)")
        return True

    symbols_limit = max(_env_int("ROBUSTNESS_GATE_SYMBOLS_LIMIT", 6), 1)
    n_candles = max(_env_int("ROBUSTNESS_GATE_N_CANDLES", 700), 200)
    folds = max(_env_int("ROBUSTNESS_GATE_FOLDS", 4), 1)
    mc_paths = max(_env_int("ROBUSTNESS_GATE_MC_PATHS", 1200), 100)
    seed = _env_int("ROBUSTNESS_GATE_SEED", 42)
    signal_score_override = _env_int("ROBUSTNESS_GATE_SIGNAL_SCORE_OVERRIDE", -1)
    trend_weight_mult = _env_float("ROBUSTNESS_GATE_TREND_WEIGHT_MULT", 1.0)
    macd_weight_mult = _env_float("ROBUSTNESS_GATE_MACD_WEIGHT_MULT", 1.0)
    penalty_weight_mult = _env_float("ROBUSTNESS_GATE_PENALTY_WEIGHT_MULT", 1.0)
    report_rel_path = os.getenv("ROBUSTNESS_GATE_REPORT_PATH", "robustness_report.json")
    report_path = (ROOT / report_rel_path).resolve()

    cmd = [
        PY,
        "robustness_check.py",
        "--symbols-limit",
        str(symbols_limit),
        "--n-candles",
        str(n_candles),
        "--folds",
        str(folds),
        "--mc-paths",
        str(mc_paths),
        "--seed",
        str(seed),
        "--output",
        str(report_path),
    ]

    if signal_score_override >= 0:
        cmd.extend(["--signal-score-override", str(signal_score_override)])
    cmd.extend(["--trend-weight-mult", str(max(trend_weight_mult, 0.1))])
    cmd.extend(["--macd-weight-mult", str(max(macd_weight_mult, 0.1))])
    cmd.extend(["--penalty-weight-mult", str(max(penalty_weight_mult, 0.1))])

    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        print(f"[FAIL] Robustness runner failed (exit {proc.returncode})")
        return False

    if not report_path.exists():
        print(f"[FAIL] Robustness report not found: {report_path}")
        return False

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[FAIL] Robustness report parse error: {exc}")
        return False

    min_total_trades = max(_env_int("ROBUSTNESS_GATE_MIN_TOTAL_TRADES", 300), 1)
    min_symbol_trades = max(_env_int("ROBUSTNESS_GATE_MIN_SYMBOL_TRADES", 100), 1)
    min_symbols_pass = max(_env_int("ROBUSTNESS_GATE_MIN_SYMBOLS_PASS", 3), 1)
    min_avg_pf = _env_float("ROBUSTNESS_GATE_MIN_AVG_PF", 1.35)
    min_avg_sharpe = _env_float("ROBUSTNESS_GATE_MIN_AVG_SHARPE", 1.2)
    max_avg_mdd = _env_float("ROBUSTNESS_GATE_MAX_AVG_MDD", 0.12)
    max_p_loss = _env_float("ROBUSTNESS_GATE_MAX_P_LOSS", 0.45)
    max_p_drawdown20 = _env_float("ROBUSTNESS_GATE_MAX_P_DRAWDOWN_20", 0.35)

    symbols_data = report.get("symbols", {}) if isinstance(report, dict) else {}
    if not isinstance(symbols_data, dict) or not symbols_data:
        print("[FAIL] Robustness report contains no symbol results")
        return False

    passing_symbols = 0
    evaluated_symbols = 0

    for symbol, payload in symbols_data.items():
        if not isinstance(payload, dict) or payload.get("error"):
            continue

        aggregate = payload.get("aggregate", {})
        if not isinstance(aggregate, dict):
            continue

        evaluated_symbols += 1
        trades = int(aggregate.get("validation_trades", 0) or 0)
        avg_pf = _to_float(aggregate.get("avg_profit_factor"), 0.0)
        avg_sharpe = _to_float(aggregate.get("avg_sharpe"), 0.0)
        avg_mdd = _to_float(aggregate.get("avg_max_drawdown"), 1.0)

        if (
            trades >= min_symbol_trades
            and avg_pf >= min_avg_pf
            and avg_sharpe >= min_avg_sharpe
            and avg_mdd <= max_avg_mdd
        ):
            passing_symbols += 1
        else:
            print(
                f"[INFO] Symbol below threshold: {symbol} "
                f"(trades={trades}, pf={avg_pf:.3f}, sharpe={avg_sharpe:.3f}, mdd={avg_mdd:.3f})"
            )

    portfolio = report.get("portfolio_summary", {}) if isinstance(report, dict) else {}
    portfolio_mc = portfolio.get("monte_carlo", {}) if isinstance(portfolio, dict) else {}
    total_trades = int(portfolio.get("validation_trades_total", 0) or 0)
    p_loss = _to_float(portfolio_mc.get("p_loss"), 1.0)
    p_drawdown20 = _to_float(portfolio_mc.get("p_drawdown_20pct"), 1.0)

    print(
        "Robustness summary: "
        f"passing_symbols={passing_symbols}/{max(evaluated_symbols, 1)}, "
        f"total_trades={total_trades}, p_loss={p_loss:.3f}, p_dd20={p_drawdown20:.3f}"
    )

    failures = []
    if evaluated_symbols < min_symbols_pass:
        failures.append(
            f"evaluated_symbols={evaluated_symbols} < required={min_symbols_pass}"
        )
    if passing_symbols < min_symbols_pass:
        failures.append(
            f"passing_symbols={passing_symbols} < required={min_symbols_pass}"
        )
    if total_trades < min_total_trades:
        failures.append(f"total_trades={total_trades} < required={min_total_trades}")
    if p_loss > max_p_loss:
        failures.append(f"p_loss={p_loss:.3f} > max={max_p_loss:.3f}")
    if p_drawdown20 > max_p_drawdown20:
        failures.append(f"p_drawdown_20pct={p_drawdown20:.3f} > max={max_p_drawdown20:.3f}")

    if failures:
        print("[FAIL] Robustness thresholds not met:")
        for item in failures:
            print(f"  - {item}")
        return False

    print("[PASS] Robustness thresholds")
    return True


def main() -> int:
    checks = [
        run_step("Smoke test", [PY, "smoke_test.py"]),
        run_step("Fix verification", [PY, "verify_fixes.py"]),
        run_step("Runtime safety verification", [PY, "verify_runtime_safety.py"]),
        run_step("Chaos reconcile verification", [PY, "chaos_reconcile_test.py"]),
        run_live_fetch_probe(),
        run_robustness_gate(),
    ]
    passed = sum(1 for x in checks if x)
    total = len(checks)
    print(f"\nHealth gate: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
