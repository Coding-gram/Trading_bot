"""
Pre-run health gate.
Runs key checks and exits non-zero if any gate fails.

Usage:
  python health_gate.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable


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


def main() -> int:
    checks = [
        run_step("Smoke test", [PY, "smoke_test.py"]),
        run_step("Fix verification", [PY, "verify_fixes.py"]),
        run_step("Runtime safety verification", [PY, "verify_runtime_safety.py"]),
        run_live_fetch_probe(),
    ]
    passed = sum(1 for x in checks if x)
    total = len(checks)
    print(f"\nHealth gate: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
