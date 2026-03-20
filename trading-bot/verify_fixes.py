"""
Verification script for all 9 bug fixes.
Run from the trading-bot directory: python verify_fixes.py
"""
import os
import sys
import warnings

# Minimal env setup so config imports don't fail
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import numpy as np
import pandas as pd

PASS = "[PASS]"
FAIL = "[FAIL]"
results = []


def check(label, condition, detail=""):
    tag = PASS if condition else FAIL
    print(f"  {tag} {label}" + (f" — {detail}" if detail else ""))
    results.append((label, condition))


# ─── Build a synthetic OHLCV DataFrame ────────────────────────────────────────
np.random.seed(42)
n = 300
price = pd.Series(np.cumsum(np.random.randn(n)) + 100)
df = pd.DataFrame({
    "open": price * 0.999,
    "high": price * 1.003,
    "low": price * 0.997,
    "close": price,
    "volume": np.random.randint(1000, 8000, n),
})
df.index = pd.date_range("2024-01-01", periods=n, freq="1h")

# ─── Test 1 & 2: indicators.py BB columns + NaN bool flags ────────────────────
print("\n=== Bug 1+2: indicators.py ===")
from bot.analysis import indicators

out = indicators.compute_all(df.copy())
check("bb_lower populated", out["bb_lower"].notna().any(), f"max={out['bb_lower'].dropna().max():.4f}")
check("bb_mid populated", out["bb_mid"].notna().any())
check("bb_upper populated", out["bb_upper"].notna().any())
check("bb_pct populated", "bb_pct" in out.columns)
check("bb columns by name (not position)", "bb_lower" in out.columns and out["bb_lower"].notna().sum() > 0)
check("trend_up dtype is bool", out["trend_up"].dtype == bool, str(out["trend_up"].dtype))
check("trend_down dtype is bool", out["trend_down"].dtype == bool)
check("trend_up no NaN", not out["trend_up"].isna().any())
check("rsi_oversold no NaN", not out["rsi_oversold"].isna().any())
check("macd_cross_up no NaN", "macd_cross_up" not in out.columns or not out["macd_cross_up"].isna().any())

# ─── Test 3 & 4: candlestick.py pattern rules ─────────────────────────────────
print("\n=== Bug 3+4: candlestick Three White Soldiers / Three Black Crows ===")
from bot.analysis.patterns import candlestick

# Craft a perfect 3 white soldiers sequence (opens within prior body)
rows = [
    {"open": 100, "high": 105, "low": 99, "close": 104},  # C1 bullish
    {"open": 102, "high": 108, "low": 101, "close": 107},  # C2: open within [100,104], close higher ✓
    {"open": 105, "high": 113, "low": 104, "close": 112},  # C3: open within [102,107], close higher ✓
]
test_df = pd.DataFrame(rows)
result = candlestick._three_white_soldiers_logic(test_df)
check("Three White Soldiers detects correct pattern", result.iloc[-1] == 1, f"got {result.iloc[-1]}")

# Craft a GAPPING sequence (old code would pass, new correct code should NOT detect)
gap_rows = [
    {"open": 100, "high": 105, "low": 99, "close": 104},
    {"open": 106, "high": 110, "low": 105, "close": 109},  # opens ABOVE C1's close — gap up
    {"open": 111, "high": 115, "low": 110, "close": 114},  # opens ABOVE C2's close — gap up
]
gap_df = pd.DataFrame(gap_rows)
gap_result = candlestick._three_white_soldiers_logic(gap_df)
check("Three White Soldiers rejects gapping candles", gap_result.iloc[-1] == 0, f"got {gap_result.iloc[-1]}")

# Three Black Crows
bear_rows = [
    {"open": 115, "high": 116, "low": 108, "close": 109},  # C1 bearish
    {"open": 112, "high": 113, "low": 105, "close": 106},  # C2: open within [109,115], close lower ✓
    {"open": 109, "high": 110, "low": 103, "close": 104},  # C3: open within [106,112], close lower ✓
]
bear_df = pd.DataFrame(bear_rows)
bear_result = candlestick._three_black_crows_logic(bear_df)
check("Three Black Crows detects correct pattern", bear_result.iloc[-1] == -1, f"got {bear_result.iloc[-1]}")

# ─── Test 5: No SettingWithCopyWarning ────────────────────────────────────────
print("\n=== Bug 5: candlestick.py no SettingWithCopyWarning ===")
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    out_c = candlestick.detect_all(indicators.compute_all(df.copy()))
    sw = [x for x in w if "SettingWithCopy" in str(x.category)]
check("No SettingWithCopyWarning", len(sw) == 0, f"found {len(sw)} warnings")
check("candle_bull_score computed", "candle_bull_score" in out_c.columns)

# ─── Test 6: chart.py flag normalisation ──────────────────────────────────────
print("\n=== Bug 6: chart.py flag normalisation ===")
from bot.analysis.patterns import chart as chart_pat

# Create a very tight flag (range < 5%) after a strong pole (range > 4%)
pole_base = 100.0
pole = pd.DataFrame({
    "open": [pole_base + i * 0.5 for i in range(15)],
    "high": [pole_base + i * 0.5 + 0.8 for i in range(15)],
    "low":  [pole_base + i * 0.5 - 0.2 for i in range(15)],
    "close":[pole_base + i * 0.5 + 0.3 for i in range(15)],
    "volume": [5000] * 15,
})
flag_base = pole["close"].iloc[-1]
# Tight flag: oscillates within ±1% of flag_base
flag = pd.DataFrame({
    "open":  [flag_base + 0.002 * ((-1)**i) for i in range(15)],
    "high":  [flag_base + 0.005 for _ in range(15)],
    "low":   [flag_base - 0.005 for _ in range(15)],
    "close": [flag_base + 0.001 * ((-1)**i) for i in range(15)],
    "volume": [3000] * 15,
})
flag_full = pd.concat([pole, flag], ignore_index=True)
flag_full.index = pd.date_range("2024-01-01", periods=30, freq="1h")
r = chart_pat._bull_flag(flag_full)
check("Bull flag detected with tight flag", r["detected"], str(r))

# ─── Test 7: scorer.py S/R called once ────────────────────────────────────────
print("\n=== Bug 7+8: scorer.py ===")
from bot.strategy import scorer

# Patch find_support_resistance to count calls
call_count = [0]
orig_fsr = scorer.find_support_resistance
def counting_fsr(df):
    call_count[0] += 1
    return orig_fsr(df)
scorer.find_support_resistance = counting_fsr

# Force trend_up=True so direction evaluates to "long" and execution reaches S/R check
sr_df = df.copy()
sr_h4 = df.iloc[::4].copy()
sr_df["trend_up"] = True
sr_df["trend_down"] = False
sr_df["close"] = 100
sr_h4["trend_up"] = True
sr_h4["trend_down"] = False
sr_df["adx"] = 30
try:
    print("  [DEBUG] Calling score_signal...")
    try:
        sig = scorer.score_signal(sr_df, sr_h4, "TEST/USDT")
        print(f"  [DEBUG] score_signal returned direction={sig.get('direction')} score={sig.get('score')}")
    except Exception as e:
        print(f"  [DEBUG] Exception in score_signal: {repr(e)}")
        raise
    check("S/R computed exactly once", call_count[0] == 1, f"called {call_count[0]} times")
    check("Forced trend+ADX path is not neutral", sig["direction"] != "neutral", sig["direction"])
    check("Direction is a valid value", sig["direction"] in ("long", "short", "neutral"), sig["direction"])
    check("Score is in [0, 100]", 0 <= sig["score"] <= 100, str(sig["score"]))
finally:
    scorer.find_support_resistance = orig_fsr

# ─── Test 8: NaN-safe bool cast ───────────────────────────────────────────────
# Simulate a warm-up row where trend_up is NaN
print("\n=== Bug 8: NaN-truthy guard in _score_trend ===")
import pandas as pd_alias
nan_row = pd_alias.Series({"trend_up": float("nan"), "trend_down": float("nan")})
nan_h4 = pd.Series({"trend_up": float("nan"), "trend_down": float("nan")})
direction, _, _ = scorer._score_trend(nan_row, nan_h4)
check("NaN trend_up does NOT produce long/short", direction == "neutral", f"got '{direction}'")

# ─── Test 9: risk_manager ZeroDivision guard ─────────────────────────────────
print("\n=== Bug 9: risk_manager.py ZeroDivisionError guard ===")
from bot.risk.risk_manager import calculate_position_size
r = calculate_position_size(1000.0, 50000.0, 50000.0)  # entry == stop_loss
check("sl_distance=0 returns qty=0", r["qty"] == 0.0, f"qty={r['qty']}")
check("sl_distance=0 returns usdt_risk=0", r["usdt_risk"] == 0.0)

# ─── Summary ──────────────────────────────────────────────────────────────────
print("\n" + "="*50)
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
print(f"Results: {passed} passed, {failed} failed out of {len(results)} checks")
if failed:
    print("\nFailed checks:")
    for label, ok in results:
        if not ok:
            print(f"  {FAIL} {label}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
