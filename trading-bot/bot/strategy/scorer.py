"""
Signal Scorer – the core intelligence engine.
Aggregates signals from all analysis modules and produces a 0-100 score.
Only signals above MIN_SIGNAL_SCORE (default 70) are forwarded for notification.
"""

import logging
import pandas as pd
import json
from pathlib import Path
from datetime import datetime, timezone
from bot.config import MIN_SIGNAL_SCORE
from bot.analysis import indicators
from bot.analysis.patterns import candlestick, chart as chart_patterns
from bot.analysis.support_resistance import (
    find_support_resistance, get_nearest_levels, fibonacci_levels
)


logger = logging.getLogger(__name__)



# Load weights from JSON config
_WEIGHTS_PATH = Path(__file__).parent / "weights.json"
with open(_WEIGHTS_PATH, encoding="utf-8") as f:
    WEIGHTS = json.load(f)


def score_signal(df_1h: pd.DataFrame, df_4h: pd.DataFrame, symbol: str) -> dict:
    """
    Full multi-timeframe analysis and scoring.
    Returns a signal dict with score, direction, and all sub-scores.
    """
    result = {
        "symbol": symbol,
        "score": 0,
        "direction": "neutral",
        "details": {},
        "send_alert": False,
    }

    if df_1h.empty or df_4h.empty or len(df_1h) < 60:
        return result

    def _extract_explicit_trend_override(df: pd.DataFrame):
        if "trend_up" not in df.columns or "trend_down" not in df.columns or df.empty:
            return None
        last = df.iloc[-1]
        trend_up = last.get("trend_up")
        trend_down = last.get("trend_down")
        if pd.isna(trend_up) or pd.isna(trend_down):
            return None
        return bool(trend_up), bool(trend_down)

    def _extract_explicit_last_value(df: pd.DataFrame, col: str):
        if col not in df.columns or df.empty:
            return None
        value = df.iloc[-1].get(col)
        if pd.isna(value):
            return None
        return value

    trend_override_1h = _extract_explicit_trend_override(df_1h)
    trend_override_4h = _extract_explicit_trend_override(df_4h)
    adx_override_1h = _extract_explicit_last_value(df_1h, "adx")

    # ── Compute indicators safely ───────────────────────────────────────────
    def compute_latest(df, compute_func):
        # Full-frame recomputation keeps stateful indicators (EMA/MACD/ATR)
        # accurate and avoids NaNs caused by single-row calculations.
        return compute_func(df)

    df_1h = compute_latest(df_1h, indicators.compute_all)
    df_4h = compute_latest(df_4h, indicators.compute_all)
    df_1h = compute_latest(df_1h, candlestick.detect_all)

    c = df_1h.iloc[-1]   # current 1h candle
    h4 = df_4h.iloc[-1]   # current 4h candle

    if trend_override_1h is not None:
        c = c.copy()
        c["trend_up"], c["trend_down"] = trend_override_1h
    if adx_override_1h is not None:
        if not isinstance(c, pd.Series):
            c = pd.Series(c)
        c = c.copy()
        c["adx"] = adx_override_1h
    if trend_override_4h is not None:
        h4 = h4.copy()
        h4["trend_up"], h4["trend_down"] = trend_override_4h

    # ── ADX Range Filter ────────────────────────────────────────────────────
    adx_val = float(c.get("adx", 0) or 0)
    if adx_val < 15:
        logger.info("[%s] ADX too low (%.1f) — ranging market, skipping signal", symbol, adx_val)
        return result

    # ── Primary Scoring ─────────────────────────────────────────────────────
    score = 0
    details = {}
    direction, trend_score, trend_msg = _score_trend(c, h4)
    score += trend_score
    details["trend"] = trend_msg

    if direction == "neutral":
        return result

    # Standard indicators
    score += _score_macd(c, direction, details)
    score += _score_rsi(c, direction, details)
    score += _score_stochastic(df_1h, direction, details)

    # Patterns & Volume
    score += _score_candlestick(c, direction, details)
    score += _score_chart_patterns(df_1h, direction, details)
    score += _score_volume(c, details)

    # Advanced confirmation
    score += _score_adx(adx_val, details)
    score += _score_institutional_bias(df_1h, c, direction, details)

    # Pre-compute S/R once so both scoring and result use the same data.
    sr = find_support_resistance(df_1h)
    score += _score_sr_proximity(sr, c, direction, details)

    # ── Final result ───────────────────────────────────────────────────────
    score = min(score, 100)
    send_alert = score >= MIN_SIGNAL_SCORE

    swing_high = df_1h["high"].rolling(50).max().iloc[-1]
    swing_low = df_1h["low"].rolling(50).min().iloc[-1]
    fib = fibonacci_levels(swing_high, swing_low)

    result.update({
        "score": score,
        "direction": direction,
        "details": details,
        "send_alert": send_alert,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "price": c["close"],
        "atr": c.get("atr", 0),
        "patterns": [k for k, v in chart_patterns.detect_all_chart_patterns(df_1h).items() if v.get("detected")],
        "market_regime": indicators.get_market_regime(df_1h),
        "sr_levels": sr,
        "fib_levels": fib,
    })

    logger.info("[%s] Score: %d/100 | %s | Alert: %s", symbol, score, direction.upper(), send_alert)
    return result


def _score_trend(c, h4):
    """Determine primary direction and EMA trend score."""
    # Explicitly cast to bool: pandas scalars from warm-up rows may be NaN,
    # and bool(np.nan) == True in Python which would generate spurious signals.
    trend_up_1h = c.get("trend_up") == True
    trend_dn_1h = c.get("trend_down") == True
    trend_up_4h = h4.get("trend_up") == True
    trend_dn_4h = h4.get("trend_down") == True

    if trend_up_1h and trend_up_4h:
        return "long", WEIGHTS["trend_alignment"], f"+{WEIGHTS['trend_alignment']} (bull EMA stack, confirmed 4H)"
    if trend_dn_1h and trend_dn_4h:
        return "short", WEIGHTS["trend_alignment"], f"+{WEIGHTS['trend_alignment']} (bear EMA stack, confirmed 4H)"
    if trend_up_1h or trend_up_4h:
        return "long", WEIGHTS["trend_alignment"] // 2, f"+{WEIGHTS['trend_alignment']//2} (partial bull alignment)"
    if trend_dn_1h or trend_dn_4h:
        return "short", WEIGHTS["trend_alignment"] // 2, f"+{WEIGHTS['trend_alignment']//2} (partial bear alignment)"
    return "neutral", 0, "No trend alignment"


def _score_macd(c, direction, details):
    if direction == "long":
        if c["macd_cross_up"]:
            details["macd"] = f"+{WEIGHTS['macd_signal']} (bullish MACD crossover)"
            return WEIGHTS["macd_signal"]
        if c["macd"] > c["macd_signal"]:
            details["macd"] = f"+{WEIGHTS['macd_signal']//2} (MACD above signal)"
            return WEIGHTS["macd_signal"] // 2
    elif direction == "short":
        if c["macd_cross_dn"]:
            details["macd"] = f"+{WEIGHTS['macd_signal']} (bearish MACD crossover)"
            return WEIGHTS["macd_signal"]
        if c["macd"] < c["macd_signal"]:
            details["macd"] = f"+{WEIGHTS['macd_signal']//2} (MACD below signal)"
            return WEIGHTS["macd_signal"] // 2
    return 0


def _score_rsi(c, direction, details):
    rsi = c["rsi"]
    if direction == "long" and (c["rsi_oversold"] or 30 < rsi < 50):
        details["rsi"] = f"+{WEIGHTS['rsi_zone']} (RSI in buy zone: {rsi:.1f})"
        return WEIGHTS["rsi_zone"]
    if direction == "short" and (c["rsi_overbought"] or 50 < rsi < 70):
        details["rsi"] = f"+{WEIGHTS['rsi_zone']} (RSI in sell zone: {rsi:.1f})"
        return WEIGHTS["rsi_zone"]
    return 0


def _score_stochastic(df, direction, details):
    c = df.iloc[-1]
    prev = df.iloc[-2]
    k, d = float(c.get("stoch_k", 50)), float(c.get("stoch_d", 50))
    pk, pd_val = float(prev.get("stoch_k", 50)), float(prev.get("stoch_d", 50))

    if direction == "long":
        if k < 20 and k > d:
            details["stochastic"] = f"+{WEIGHTS['stochastic']} (stoch oversold cross-up K={k:.1f})"
            return WEIGHTS["stochastic"]
        if k > d and pk <= pd_val:
            details["stochastic"] = f"+{WEIGHTS['stochastic']//2} (stoch bullish cross K={k:.1f})"
            return WEIGHTS["stochastic"] // 2
    elif direction == "short":
        if k > 80 and k < d:
            details["stochastic"] = f"+{WEIGHTS['stochastic']} (stoch overbought cross-down K={k:.1f})"
            return WEIGHTS["stochastic"]
        if k < d and pk >= pd_val:
            details["stochastic"] = f"+{WEIGHTS['stochastic']//2} (stoch bearish cross K={k:.1f})"
            return WEIGHTS["stochastic"] // 2
    return 0


def _score_candlestick(c, direction, details):
    bull, bear = c.get("candle_bull_score", 0), c.get("candle_bear_score", 0)
    if direction == "long" and bull > 0:
        contrib = min(WEIGHTS["candlestick_pattern"], bull * 7)
        details["candle_pattern"] = f"+{contrib} (bullish candle pattern x{bull})"
        return contrib
    if direction == "short" and bear > 0:
        contrib = min(WEIGHTS["candlestick_pattern"], bear * 7)
        details["candle_pattern"] = f"+{contrib} (bearish candle pattern x{bear})"
        return contrib
    return 0


def _score_chart_patterns(df, direction, details):
    patterns = chart_patterns.detect_all_chart_patterns(df)
    cp_dir, cp_conf = chart_patterns.get_pattern_signal(patterns)
    if cp_dir == direction and cp_conf > 0:
        contrib = int(WEIGHTS["chart_pattern"] * cp_conf)
        detected = [k for k, v in patterns.items() if v.get("detected")]
        details["chart_pattern"] = f"+{contrib} ({', '.join(detected)})"
        return contrib
    return 0


def _score_volume(c, details):
    if c.get("vol_spike"):
        details["volume"] = f"+{WEIGHTS['volume_confirm']} (volume spike)"
        return WEIGHTS["volume_confirm"]
    return 0


def _score_adx(adx_val, details):
    if adx_val > 25:
        details["adx"] = f"+{WEIGHTS['adx_strength']} (strong trend ADX={adx_val:.1f})"
        return WEIGHTS["adx_strength"]
    if adx_val > 20:
        details["adx"] = f"+{WEIGHTS['adx_strength']//2} (moderate trend ADX={adx_val:.1f})"
        return WEIGHTS["adx_strength"] // 2
    return 0


def _score_institutional_bias(df, c, direction, details):
    score = 0
    # OBV
    obv = df["obv"]
    if len(obv) >= 5:
        if direction == "long" and obv.iloc[-1] > obv.iloc[-5]:
            score += WEIGHTS["obv_confirm"]
            details["obv"] = f"+{WEIGHTS['obv_confirm']} (OBV rising)"
        elif direction == "short" and obv.iloc[-1] < obv.iloc[-5]:
            score += WEIGHTS["obv_confirm"]
            details["obv"] = f"+{WEIGHTS['obv_confirm']} (OBV falling)"

    # VWAP
    vwap = c.get("vwap")
    if vwap is not None and not pd.isna(vwap):
        if direction == "long" and c["close"] > vwap:
            score += WEIGHTS["vwap_bias"]
            details["vwap"] = f"+{WEIGHTS['vwap_bias']} (price > VWAP)"
        elif direction == "short" and c["close"] < vwap:
            score += WEIGHTS["vwap_bias"]
            details["vwap"] = f"+{WEIGHTS['vwap_bias']} (price < VWAP)"
    return score


def _score_sr_proximity(sr, c, direction, details):
    """Score S/R proximity. Receives pre-computed sr dict to avoid a duplicate call."""
    near = get_nearest_levels(c["close"], sr)

    if direction == "long":
        if near.get("dist_support_pct", 1) < 0.02:
            details["sr"] = f"+{WEIGHTS['sr_proximity']} (near support)"
            return WEIGHTS["sr_proximity"]
    elif direction == "short":
        if near.get("dist_resist_pct", 1) < 0.02:
            details["sr"] = f"+{WEIGHTS['sr_proximity']} (near resistance)"
            return WEIGHTS["sr_proximity"]
    return 0
