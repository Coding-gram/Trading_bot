"""
Support & Resistance Detection + Fibonacci Retracement Levels
"""
import logging
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

logger = logging.getLogger(__name__)


def find_support_resistance(df: pd.DataFrame, order: int = 10, n_levels: int = 5) -> dict:
    """
    Detect key S/R levels from recent price history.
    Returns dict with 'support' and 'resistance' lists.
    """
    if len(df) < 30:
        return {"support": [], "resistance": []}

    highs = df["high"].values
    lows = df["low"].values

    sh_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    sl_idx = argrelextrema(lows, np.less_equal, order=order)[0]

    r_raw = [highs[i] for i in sh_idx[-20:]]
    s_raw = [lows[i] for i in sl_idx[-20:]]

    resistance = _cluster_levels(r_raw, n=n_levels)
    support = _cluster_levels(s_raw, n=n_levels)

    return {"support": support, "resistance": resistance}


def _cluster_levels(levels: list, n: int = 5, tolerance: float = 0.005) -> list:
    """Merge nearby levels into clusters and return top N strongest."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters = []
    cluster = [levels[0]]
    for price in levels[1:]:
        if abs(price - cluster[-1]) / cluster[-1] < tolerance:
            cluster.append(price)
        else:
            clusters.append(np.mean(cluster))
            cluster = [price]
    clusters.append(np.mean(cluster))
    return sorted(clusters)[-n:]


def get_nearest_levels(curr_price: float, sr_levels: dict) -> dict:
    """
    Given current price, find nearest S/R levels.
    """
    supports = [s for s in sr_levels.get("support", []) if s < curr_price]
    resists = [r for r in sr_levels.get("resistance", []) if r > curr_price]

    ns = max(supports) if supports else None
    nr = min(resists) if resists else None

    res = {}
    if ns:
        res["nearest_support"] = ns
        res["dist_support_pct"] = (curr_price - ns) / curr_price
    if nr:
        res["nearest_resistance"] = nr
        res["dist_resist_pct"] = (nr - curr_price) / curr_price

    return res


def fibonacci_levels(sh: float, sl: float) -> dict:
    """
    Calculate Fibonacci retracement and extension levels.
    """
    diff = sh - sl

    retrace = {
        "0.0": sh,
        "0.236": sh - diff * 0.236,
        "0.382": sh - diff * 0.382,
        "0.5": sh - diff * 0.5,
        "0.618": sh - diff * 0.618,
        "0.786": sh - diff * 0.786,
        "1.0": sl,
    }

    ext = {
        "1.272": sl - diff * 0.272,
        "1.618": sl - diff * 0.618,
        "2.0": sl - diff * 1.0,
    }

    return {"retracements": retrace, "extensions": ext}


def price_at_fib_level(curr_price: float, fib: dict, tol: float = 0.005) -> str:
    """Check if price is near any Fibonacci level. Returns level name or ''."""
    for level, price in fib["retracements"].items():
        if abs(curr_price - price) / curr_price < tol:
            return f"fib_{level}"
    return ""
