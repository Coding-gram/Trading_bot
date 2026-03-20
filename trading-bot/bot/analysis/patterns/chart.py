"""
Chart Pattern Detection – identifies major structural chart patterns:
Head & Shoulders, Double Top/Bottom, Triangles, Flags, Wedges, Cup & Handle
"""
import logging
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

logger = logging.getLogger(__name__)


def find_pivots(df: pd.DataFrame, order: int = 5):
    """Find local swing highs and lows using argrelextrema."""
    highs = df["high"].values
    lows = df["low"].values

    high_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    low_idx = argrelextrema(lows, np.less_equal, order=order)[0]

    swing_highs = [(df.index[i], highs[i]) for i in high_idx]
    swing_lows = [(df.index[i], lows[i]) for i in low_idx]
    return swing_highs, swing_lows


def detect_all_chart_patterns(df: pd.DataFrame) -> dict:
    """
    Run all chart pattern detectors.
    Returns dict: {pattern_name: {"detected": bool, "direction": str, "confidence": float}}
    """
    results = {}
    if len(df) < 50:
        return results

    sh, sl = find_pivots(df, order=5)

    results["double_top"] = _double_top(sh)
    results["double_bottom"] = _double_bottom(sl)
    results["head_and_shoulders"] = _head_and_shoulders(sh)
    results["inv_head_shoulders"] = _inv_head_shoulders(sl)
    results["ascending_triangle"] = _ascending_triangle(sh, sl)
    results["descending_triangle"] = _descending_triangle(sh, sl)
    results["bull_flag"] = _bull_flag(df)
    results["bear_flag"] = _bear_flag(df)
    results["rising_wedge"] = _rising_wedge(sh, sl)
    results["falling_wedge"] = _falling_wedge(sh, sl)

    return results


def get_pattern_signal(patterns: dict) -> tuple:
    """
    Combine pattern results into a single direction and confidence.
    Returns: (direction, confidence)
    """
    bull_conf = 0.0
    bear_conf = 0.0

    bpats = ["double_bottom", "inv_head_shoulders", "ascending_triangle", "bull_flag", "falling_wedge"]
    spats = ["double_top", "head_and_shoulders", "descending_triangle", "bear_flag", "rising_wedge"]

    for p in bpats:
        if patterns.get(p, {}).get("detected"):
            bull_conf += patterns[p].get("confidence", 0.5)

    for p in spats:
        if patterns.get(p, {}).get("detected"):
            bear_conf += patterns[p].get("confidence", 0.5)

    if bull_conf == 0 and bear_conf == 0:
        return "neutral", 0.0
    if bull_conf > bear_conf:
        return "long", min(bull_conf, 1.0)
    return "short", min(bear_conf, 1.0)


# ────────────────────────────────────────────────────────────────────────────────
# Pattern Implementations
# ────────────────────────────────────────────────────────────────────────────────

def _double_top(sh):
    if len(sh) < 2:
        return {"detected": False}
    h1, h2 = sh[-2][1], sh[-1][1]
    if abs(h1 - h2) / h1 < 0.015 and h2 < h1 * 1.005:
        return {"detected": True, "direction": "short", "confidence": 0.75}
    return {"detected": False}


def _double_bottom(sl):
    if len(sl) < 2:
        return {"detected": False}
    l1, l2 = sl[-2][1], sl[-1][1]
    if abs(l1 - l2) / l1 < 0.015 and l2 > l1 * 0.995:
        return {"detected": True, "direction": "long", "confidence": 0.75}
    return {"detected": False}


def _head_and_shoulders(sh):
    if len(sh) < 3:
        return {"detected": False}
    left, head, right = sh[-3][1], sh[-2][1], sh[-1][1]
    if head > left and head > right and abs(left - right) / head < 0.05:
        return {"detected": True, "direction": "short", "confidence": 0.85}
    return {"detected": False}


def _inv_head_shoulders(sl):
    if len(sl) < 3:
        return {"detected": False}
    left, head, right = sl[-3][1], sl[-2][1], sl[-1][1]
    if head < left and head < right and abs(left - right) / left < 0.05:
        return {"detected": True, "direction": "long", "confidence": 0.85}
    return {"detected": False}


def _ascending_triangle(sh, sl):
    if len(sh) < 2 or len(sl) < 2:
        return {"detected": False}
    flat_top = abs(sh[-1][1] - sh[-2][1]) / sh[-1][1] < 0.02
    rising_lows = sl[-1][1] > sl[-2][1]
    if flat_top and rising_lows:
        return {"detected": True, "direction": "long", "confidence": 0.70}
    return {"detected": False}


def _descending_triangle(sh, sl):
    if len(sh) < 2 or len(sl) < 2:
        return {"detected": False}
    flat_bottom = abs(sl[-1][1] - sl[-2][1]) / sl[-1][1] < 0.02
    falling_highs = sh[-1][1] < sh[-2][1]
    if flat_bottom and falling_highs:
        return {"detected": True, "direction": "short", "confidence": 0.70}
    return {"detected": False}


def _bull_flag(df):
    if len(df) < 30:
        return {"detected": False}
    pole = df.iloc[-30:-15]
    flag = df.iloc[-15:]
    pole_move = (pole["close"].max() - pole["close"].min()) / pole["close"].min()
    # Normalise flag range vs the first candle in the flag (baseline price)
    flag_base = flag["close"].iloc[0]
    flag_move = (flag["close"].max() - flag["close"].min()) / flag_base if flag_base > 0 else 1
    if pole["close"].iloc[-1] > pole["close"].iloc[0] and pole_move > 0.04 and flag_move < 0.05:
        return {"detected": True, "direction": "long", "confidence": 0.72}
    return {"detected": False}


def _bear_flag(df):
    if len(df) < 30:
        return {"detected": False}
    pole = df.iloc[-30:-15]
    flag = df.iloc[-15:]
    pole_move = (pole["close"].max() - pole["close"].min()) / pole["close"].max()
    # Normalise flag range vs the first candle in the flag (baseline price)
    flag_base = flag["close"].iloc[0]
    flag_move = (flag["close"].max() - flag["close"].min()) / flag_base if flag_base > 0 else 1
    if pole["close"].iloc[-1] < pole["close"].iloc[0] and pole_move > 0.04 and flag_move < 0.05:
        return {"detected": True, "direction": "short", "confidence": 0.72}
    return {"detected": False}


def _rising_wedge(sh, sl):
    if len(sh) < 3 or len(sl) < 3:
        return {"detected": False}
    highs_rising = sh[-1][1] > sh[-2][1] > sh[-3][1]
    lows_rising = sl[-1][1] > sl[-2][1] > sl[-3][1]
    h_slope = sh[-1][1] - sh[-3][1]
    l_slope = sl[-1][1] - sl[-3][1]
    if highs_rising and lows_rising and l_slope > h_slope:
        return {"detected": True, "direction": "short", "confidence": 0.68}
    return {"detected": False}


def _falling_wedge(sh, sl):
    if len(sh) < 3 or len(sl) < 3:
        return {"detected": False}
    highs_falling = sh[-1][1] < sh[-2][1] < sh[-3][1]
    lows_falling = sl[-1][1] < sl[-2][1] < sl[-3][1]
    h_slope = sh[-1][1] - sh[-3][1]
    l_slope = sl[-1][1] - sl[-3][1]
    if highs_falling and lows_falling and l_slope > h_slope:
        return {"detected": True, "direction": "long", "confidence": 0.68}
    return {"detected": False}
