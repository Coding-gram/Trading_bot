"""
Diagnostic — shows last 300 lines of bot.log cleanly, and runs a live signal check.
Run: python diag.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# ── 1. Show last 300 log lines ────────────────────────────────────────────────
log_path = os.path.join(os.path.dirname(__file__), "bot.log")
if os.path.exists(log_path):
    with open(log_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    print("=" * 60)
    print(f"bot.log  (total {len(lines)} lines) — last 100 shown:")
    print("=" * 60)
    print("".join(lines[-100:]))
else:
    print("bot.log not found")

# ── 2. Live signal scan ───────────────────────────────────────────────────────
import pandas as pd
if not hasattr(pd.DataFrame, "applymap"):
    pd.DataFrame.applymap = pd.DataFrame.map

from bot.data.fetcher import get_exchange, fetch_multi_timeframe
from bot.config import WATCHLIST, PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME, MIN_SIGNAL_SCORE
from bot.analysis import indicators
from bot.analysis.patterns import candlestick
from bot.strategy.scorer import score_signal

print("\n" + "=" * 60)
print("LIVE SIGNAL SCAN")
print("=" * 60)
print(f"Watchlist: {WATCHLIST}")
print(f"MIN_SIGNAL_SCORE: {MIN_SIGNAL_SCORE}")
print()

exchange = get_exchange(paper_mode=True)
print("Exchange loaded OK\n")

for sym in WATCHLIST:
    try:
        dfs = fetch_multi_timeframe(exchange, sym, [PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME])
        df1h = dfs.get(PRIMARY_TIMEFRAME)
        df4h = dfs.get(CONFIRM_TIMEFRAME)

        r1h = len(df1h) if df1h is not None and not df1h.empty else 0
        r4h = len(df4h) if df4h is not None and not df4h.empty else 0
        print(f"[{sym}] 1h={r1h} candles, 4h={r4h} candles", end="")

        if r1h < 60 or r4h == 0:
            print("  ← INSUFFICIENT DATA")
            continue

        signal = score_signal(df1h, df4h, sym)

        adx = float(df1h.iloc[-1].get("adx", 0) or 0) if "adx" in df1h.columns else "?"
        print(f"  | Score={signal['score']} | Dir={signal['direction']} | Alert={signal['send_alert']}")
        if signal.get("details"):
            for k, v in signal["details"].items():
                print(f"    {k}: {v}")
    except Exception as e:
        print(f"\n  !! EXCEPTION: {e}")
    print()
