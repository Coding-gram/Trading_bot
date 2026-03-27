"""
Signal Scorer – the core intelligence engine.
Aggregates signals from all analysis modules and produces a 0-100 score.
Includes penalty deductions for contra-signals and regime-adaptive thresholds.
Only signals above the dynamic signal threshold are forwarded for notification.
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


def _normalize_weight_keys() -> None:
    """Backfill legacy key names when weights.json uses newer names."""
    alias_map = {
        "rsi_zone": ["rsi_signal"],
        "stochastic": ["stochastic_signal"],
        "candlestick_pattern": ["candlestick_bonus"],
        "chart_pattern": ["pattern_bonus"],
        "volume_confirm": ["volume_signal"],
        "obv_confirm": ["obv_signal"],
        "vwap_bias": ["vwap_signal"],
        "sr_proximity": ["support_resistance"],
        "confluence_4h_bonus": ["confluence_4h"],
        "daily_trend_bonus": ["daily_filter"],
        "penalty_htf_disagreement": ["penalty_htf_disagree"],
    }

    defaults = {
        "adx_strength": 0,
    }

    for target, candidates in alias_map.items():
        if target in WEIGHTS:
            continue
        for candidate in candidates:
            if candidate in WEIGHTS:
                WEIGHTS[target] = WEIGHTS[candidate]
                break

    for key, default_value in defaults.items():
        WEIGHTS.setdefault(key, default_value)


def _validate_weight_config() -> None:
    """Validate normalized scoring weights to fail fast on config drift."""
    required_numeric_keys = {
        "trend_alignment",
        "macd_signal",
        "rsi_zone",
        "stochastic",
        "candlestick_pattern",
        "chart_pattern",
        "volume_confirm",
        "adx_strength",
        "obv_confirm",
        "vwap_bias",
        "sr_proximity",
    }

    missing = sorted(k for k in required_numeric_keys if k not in WEIGHTS)
    if missing:
        raise ValueError(
            f"weights.json missing required scorer keys: {', '.join(missing)}"
        )

    non_numeric = sorted(
        k for k in required_numeric_keys if not isinstance(WEIGHTS.get(k), (int, float))
    )
    if non_numeric:
        raise ValueError(
            f"weights.json has non-numeric scorer keys: {', '.join(non_numeric)}"
        )


_normalize_weight_keys()
_validate_weight_config()


def _dynamic_signal_threshold(adx_val: float) -> int:
    """Regime-adaptive signal threshold: require higher confluence in weak trends."""
    if adx_val < 20:
        return max(MIN_SIGNAL_SCORE, 80)
    if adx_val < 25:
        return max(MIN_SIGNAL_SCORE, 75)
    return MIN_SIGNAL_SCORE


def score_signal(df_1h: pd.DataFrame, df_4h: pd.DataFrame, symbol: str, df_daily: pd.DataFrame | None = None) -> dict:
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

    # ── Compute indicators safely (skip if already computed) ──────────────
    _INDICATOR_MARKERS = {"ema9", "rsi", "atr", "macd"}

    def _has_indicators(df: pd.DataFrame) -> bool:
        """Check if indicator columns are already present."""
        return not df.empty and _INDICATOR_MARKERS.issubset(df.columns) and not df["rsi"].iloc[-1:].isna().all()

    if not _has_indicators(df_1h):
        df_1h = indicators.compute_all(df_1h)
    if not _has_indicators(df_4h):
        df_4h = indicators.compute_all(df_4h)
    if df_daily is not None and not df_daily.empty and not _has_indicators(df_daily):
        df_daily = indicators.compute_all(df_daily)
    df_1h = candlestick.detect_all(df_1h)

    c = df_1h.iloc[-1]   # current 1h candle
    h4 = df_4h.iloc[-1]   # current 4h candle
    daily = None
    if df_daily is not None and not df_daily.empty:
        daily = df_daily.iloc[-1]

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

    # ── ADX Range Filter / Mean-Reversion Branch ───────────────────────────
    adx_val = float(c.get("adx", 0) or 0)
    if adx_val < 15:
        direction, ranging_score, ranging_detail = _score_ranging_mean_reversion(c)
        if direction == "neutral":
            logger.info("[%s] ADX too low (%.1f) — ranging market, skipping signal", symbol, adx_val)
            return result

        score = ranging_score
        details = {"ranging_mean_reversion": ranging_detail}
        score += _score_stochastic(df_1h, direction, details)
        score += _score_volume(c, details)

        sr = find_support_resistance(df_1h)
        score += _score_sr_proximity(sr, c, direction, details)

        # Apply penalties even in ranging mode
        score += _penalty_declining_volume(df_1h, direction, details)
        score += _penalty_contra_sr(sr, c, direction, details)
        score += _penalty_rsi_divergence(df_1h, c, direction, details)

        score = max(min(score, 100), 0)
        send_alert = score >= _dynamic_signal_threshold(adx_val)

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
        logger.info("[%s] Score: %d/100 | %s | Alert: %s | ranging mean-reversion", symbol, score, direction.upper(), send_alert)
        return result

    # ── Primary Scoring ─────────────────────────────────────────────────────
    score = 0
    details = {}
    direction, trend_score, trend_msg = _score_trend(c, h4, adx_val=adx_val)
    score += trend_score
    details["trend"] = trend_msg

    if direction == "neutral":
        logger.info("[%s] %s — skipping signal", symbol, trend_msg)
        return result

    # Daily big-picture filter (do not trade against daily trend)
    blocked_daily, daily_msg = _daily_trend_blocks_direction(daily, direction)
    if blocked_daily:
        logger.info("[%s] %s — skipping signal", symbol, daily_msg)
        details["daily_filter"] = daily_msg
        return result
    daily_bonus = _score_daily_alignment(daily, direction, details)
    score += daily_bonus

    # Standard indicators
    score += _score_macd(c, direction, details, adx_val=adx_val)
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
    sr_4h = _infer_sr_levels_from_ohlc(df_4h)
    score += _score_4h_sr_confluence(sr_4h, h4, direction, details)

    # ── Multi-Timeframe Confluence Bonus ───────────────────────────────────
    score += _score_4h_confluence(h4, direction, details)

    # ── Penalty Deductions ─────────────────────────────────────────────────
    score += _penalty_rsi_divergence(df_1h, c, direction, details)
    score += _penalty_rsi_divergence_4h(df_4h, h4, direction, details)
    score += _penalty_contra_sr(sr, c, direction, details)
    score += _penalty_declining_volume(df_1h, direction, details)
    score += _penalty_htf_disagreement(h4, direction, details, daily=daily)

    # ── Final result ───────────────────────────────────────────────────────
    score = max(min(score, 100), 0)
    dynamic_threshold = _dynamic_signal_threshold(adx_val)
    send_alert = score >= dynamic_threshold

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

    logger.info("[%s] Score: %d/100 | %s | Alert: %s (threshold=%d)", symbol, score, direction.upper(), send_alert, dynamic_threshold)
    return result


def _score_trend(c, h4, adx_val: float = 25.0):
    """Determine primary direction and EMA trend score."""
    # Explicitly cast to bool: pandas scalars from warm-up rows may be NaN,
    # and bool(np.nan) == True in Python which would generate spurious signals.
    trend_up_1h = c.get("trend_up") == True
    trend_dn_1h = c.get("trend_down") == True
    trend_up_4h = h4.get("trend_up") == True
    trend_dn_4h = h4.get("trend_down") == True

    trend_weight = _scale_weight_by_adx(WEIGHTS["trend_alignment"], adx_val, strong_mult=1.25, weak_mult=0.85)

    if trend_up_1h and trend_up_4h:
        return "long", trend_weight, f"+{trend_weight} (bull EMA stack, confirmed 4H, ADX-adaptive)"
    if trend_dn_1h and trend_dn_4h:
        return "short", trend_weight, f"+{trend_weight} (bear EMA stack, confirmed 4H, ADX-adaptive)"
    if trend_up_1h or trend_up_4h:
        partial = max(trend_weight // 2, 1)
        return "long", partial, f"+{partial} (partial bull alignment, ADX-adaptive)"
    if trend_dn_1h or trend_dn_4h:
        partial = max(trend_weight // 2, 1)
        return "short", partial, f"+{partial} (partial bear alignment, ADX-adaptive)"
    return "neutral", 0, "No trend alignment"


def _score_macd(c, direction, details, adx_val: float = 25.0):
    """Score MACD momentum confirmation.

    Awards full weight for a crossover in the signal direction,
    or half weight if MACD is merely above/below its signal line.
    Weight is scaled by ADX to give stronger signals more influence.

    Returns:
        int: Points to add (0 to ADX-scaled macd_weight).
    """
    macd_weight = _scale_weight_by_adx(WEIGHTS["macd_signal"], adx_val, strong_mult=1.20, weak_mult=0.85)
    if direction == "long":
        if c["macd_cross_up"]:
            details["macd"] = f"+{macd_weight} (bullish MACD crossover, ADX-adaptive)"
            return macd_weight
        if c["macd"] > c["macd_signal"]:
            partial = max(macd_weight // 2, 1)
            details["macd"] = f"+{partial} (MACD above signal, ADX-adaptive)"
            return partial
    elif direction == "short":
        if c["macd_cross_dn"]:
            details["macd"] = f"+{macd_weight} (bearish MACD crossover, ADX-adaptive)"
            return macd_weight
        if c["macd"] < c["macd_signal"]:
            partial = max(macd_weight // 2, 1)
            details["macd"] = f"+{partial} (MACD below signal, ADX-adaptive)"
            return partial
    return 0


def _score_rsi(c, direction, details):
    """Score RSI zone confirmation.

    Awards points when RSI is in a favourable zone for the trade direction:
    - Long: RSI oversold or 30–50 (recovery zone)
    - Short: RSI overbought or 50–70 (exhaustion zone)

    Weight sourced from ``WEIGHTS['rsi_zone']``.

    Returns:
        int: Points to add (0 or rsi_zone weight).
    """
    rsi = c["rsi"]
    if direction == "long" and (c["rsi_oversold"] or 30 < rsi < 50):
        details["rsi"] = f"+{WEIGHTS['rsi_zone']} (RSI in buy zone: {rsi:.1f})"
        return WEIGHTS["rsi_zone"]
    if direction == "short" and (c["rsi_overbought"] or 50 < rsi < 70):
        details["rsi"] = f"+{WEIGHTS['rsi_zone']} (RSI in sell zone: {rsi:.1f})"
        return WEIGHTS["rsi_zone"]
    return 0


def _score_stochastic(df, direction, details):
    """Score Stochastic oscillator confirmation.

    Awards full weight for an oversold/overbought cross, or half
    weight for a simple bullish/bearish %K/%D crossover.

    Weight sourced from ``WEIGHTS['stochastic']``.

    Returns:
        int: Points to add.
    """
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
    """Score candlestick pattern confirmation.

    Awards points proportional to the number of detected bullish/bearish
    candle patterns (capped at ``WEIGHTS['candlestick_pattern']``).

    Returns:
        int: Points to add (0 to candlestick_pattern weight).
    """
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
    """Score chart pattern recognition (head-and-shoulders, wedges, etc.).

    Detects multi-bar patterns via ``chart_patterns.detect_all_chart_patterns``
    and awards ``WEIGHTS['chart_pattern'] × confidence``.

    Returns:
        int: Points to add (0 to chart_pattern weight).
    """
    patterns = chart_patterns.detect_all_chart_patterns(df)
    cp_dir, cp_conf = chart_patterns.get_pattern_signal(patterns)
    if cp_dir == direction and cp_conf > 0:
        contrib = int(WEIGHTS["chart_pattern"] * cp_conf)
        detected = [k for k, v in patterns.items() if v.get("detected")]
        details["chart_pattern"] = f"+{contrib} ({', '.join(detected)})"
        return contrib
    return 0


def _score_volume(c, details):
    """Score volume spike confirmation.

    Awards ``WEIGHTS['volume_confirm']`` when a volume spike is detected,
    indicating institutional participation.

    Returns:
        int: Points to add (0 or volume_confirm weight).
    """
    if c.get("vol_spike"):
        details["volume"] = f"+{WEIGHTS['volume_confirm']} (volume spike)"
        return WEIGHTS["volume_confirm"]
    return 0


def _score_adx(adx_val, details):
    """Score ADX trend strength.

    Awards full weight for strong trends (ADX > 25) or half for moderate
    (ADX 20–25).  Weight is itself ADX-adaptive via ``_scale_weight_by_adx``.

    Returns:
        int: Points to add.
    """
    adx_weight = _scale_weight_by_adx(WEIGHTS["adx_strength"], adx_val, strong_mult=1.20, weak_mult=0.8)
    if adx_val > 25:
        details["adx"] = f"+{adx_weight} (strong trend ADX={adx_val:.1f}, adaptive)"
        return adx_weight
    if adx_val > 20:
        partial = max(adx_weight // 2, 1)
        details["adx"] = f"+{partial} (moderate trend ADX={adx_val:.1f}, adaptive)"
        return partial
    return 0


def _score_institutional_bias(df, c, direction, details):
    """Score institutional flow signals: OBV trend and VWAP bias.

    Checks On-Balance Volume (rising/falling over 5 bars) and price
    position relative to VWAP.  Each sub-signal has its own weight
    from ``WEIGHTS['obv_confirm']`` and ``WEIGHTS['vwap_bias']``.

    Returns:
        int: Combined OBV + VWAP points.
    """
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


def _score_ranging_mean_reversion(c):
    """Low-ADX fallback: fade BB extremes with RSI confirmation."""
    close = float(c.get("close", 0) or 0)
    rsi = float(c.get("rsi", 50) or 50)
    bb_lower = c.get("bb_lower")
    bb_upper = c.get("bb_upper")
    mr_weight = int(WEIGHTS.get("ranging_mean_reversion", 40))

    long_setup = bb_lower is not None and not pd.isna(bb_lower) and close <= float(bb_lower) and rsi <= 40
    if long_setup:
        return "long", mr_weight, f"+{mr_weight} (low ADX mean-reversion long: close<=BB lower, RSI={rsi:.1f})"

    short_setup = bb_upper is not None and not pd.isna(bb_upper) and close >= float(bb_upper) and rsi >= 60
    if short_setup:
        return "short", mr_weight, f"+{mr_weight} (low ADX mean-reversion short: close>=BB upper, RSI={rsi:.1f})"

    return "neutral", 0, "No mean-reversion setup"


# ── Multi-Timeframe Confluence Bonus ──────────────────────────────────────────

def _score_4h_confluence(h4, direction, details):
    """Bonus points when 4H MACD and RSI both confirm the 1H direction."""
    bonus = int(WEIGHTS.get("confluence_4h_bonus", 7))
    h4_rsi = float(h4.get("rsi", 50) or 50)
    h4_macd = float(h4.get("macd", 0) or 0)
    h4_macd_signal = float(h4.get("macd_signal", 0) or 0)

    if direction == "long":
        macd_ok = h4_macd > h4_macd_signal
        rsi_ok = 30 < h4_rsi < 60
        if macd_ok and rsi_ok:
            details["4h_confluence"] = f"+{bonus} (4H MACD bullish + RSI={h4_rsi:.1f})"
            return bonus
    elif direction == "short":
        macd_ok = h4_macd < h4_macd_signal
        rsi_ok = 40 < h4_rsi < 70
        if macd_ok and rsi_ok:
            details["4h_confluence"] = f"+{bonus} (4H MACD bearish + RSI={h4_rsi:.1f})"
            return bonus
    return 0


def _score_4h_sr_confluence(sr_4h, h4, direction, details):
    """Secondary confirmation from 4H support/resistance context."""
    bonus = int(WEIGHTS.get("confluence_4h_sr", 5))
    near = get_nearest_levels(float(h4.get("close", 0) or 0), sr_4h)

    if direction == "long" and near.get("dist_support_pct", 1) < 0.02:
        details["4h_sr"] = f"+{bonus} (4H near support)"
        return bonus
    if direction == "short" and near.get("dist_resist_pct", 1) < 0.02:
        details["4h_sr"] = f"+{bonus} (4H near resistance)"
        return bonus
    return 0


def _infer_sr_levels_from_ohlc(df: pd.DataFrame) -> dict:
    """Infer coarse support/resistance bands from recent OHLC without running the full SR detector."""
    if df is None or df.empty:
        return {"support": [], "resistance": []}

    lookback = min(max(len(df), 20), 120)
    recent = df.tail(lookback)

    lows = recent.get("low")
    highs = recent.get("high")
    if lows is None or highs is None or lows.empty or highs.empty:
        return {"support": [], "resistance": []}

    try:
        support = float(lows.quantile(0.2))
        resistance = float(highs.quantile(0.8))
    except Exception:
        return {"support": [], "resistance": []}

    return {
        "support": [support],
        "resistance": [resistance],
    }


def _scale_weight_by_adx(base_weight: int, adx_val: float, strong_mult: float = 1.2, weak_mult: float = 0.85) -> int:
    """Regime-adaptive weight scaling based on ADX strength."""
    if adx_val > 40:
        return max(int(round(base_weight * strong_mult)), 1)
    if 20 <= adx_val < 25:
        return max(int(round(base_weight * weak_mult)), 1)
    return int(base_weight)


def _daily_trend_blocks_direction(daily, direction: str) -> tuple[bool, str]:
    """Block trades that go against the daily trend when daily context is available."""
    if daily is None:
        return False, ""
    d_up = daily.get("trend_up") == True
    d_dn = daily.get("trend_down") == True
    if direction == "long" and d_dn:
        return True, "Daily trend bearish against long setup"
    if direction == "short" and d_up:
        return True, "Daily trend bullish against short setup"
    return False, ""


def _score_daily_alignment(daily, direction: str, details: dict) -> int:
    """Bonus for alignment with daily trend."""
    if daily is None:
        return 0
    bonus = int(WEIGHTS.get("daily_trend_bonus", 6))
    d_up = daily.get("trend_up") == True
    d_dn = daily.get("trend_down") == True
    if direction == "long" and d_up:
        details["daily_alignment"] = f"+{bonus} (daily trend bullish)"
        return bonus
    if direction == "short" and d_dn:
        details["daily_alignment"] = f"+{bonus} (daily trend bearish)"
        return bonus
    return 0


# ── Penalty Deductions ────────────────────────────────────────────────────────

def _penalty_rsi_divergence(df, c, direction, details):
    """Detect RSI divergence (price vs RSI disagreement) and penalise."""
    penalty_weight = abs(int(WEIGHTS.get("penalty_rsi_divergence", 8)))
    if len(df) < 20 or "rsi" not in df.columns:
        return 0

    # Compare recent 10-bar window: price making new high but RSI not confirming
    recent = df.iloc[-10:]
    price_col = recent["close"]
    rsi_col = recent["rsi"].dropna()
    if len(rsi_col) < 5:
        return 0

    if direction == "long":
        # Bearish divergence: price higher high but RSI lower high
        price_hh = price_col.iloc[-1] >= price_col.iloc[-5:].max()
        rsi_lh = rsi_col.iloc[-1] < rsi_col.iloc[-5:].max() - 3
        if price_hh and rsi_lh:
            details["penalty_rsi_div"] = f"-{penalty_weight} (bearish RSI divergence)"
            return -penalty_weight
    elif direction == "short":
        # Bullish divergence: price lower low but RSI higher low
        price_ll = price_col.iloc[-1] <= price_col.iloc[-5:].min()
        rsi_hl = rsi_col.iloc[-1] > rsi_col.iloc[-5:].min() + 3
        if price_ll and rsi_hl:
            details["penalty_rsi_div"] = f"-{penalty_weight} (bullish RSI divergence)"
            return -penalty_weight
    return 0


def _penalty_contra_sr(sr, c, direction, details):
    """Penalise longs near resistance or shorts near support."""
    penalty_weight = abs(int(WEIGHTS.get("penalty_contra_sr", 7)))
    near = get_nearest_levels(c["close"], sr)

    if direction == "long" and near.get("dist_resist_pct", 1) < 0.015:
        details["penalty_contra_sr"] = f"-{penalty_weight} (long near resistance)"
        return -penalty_weight
    if direction == "short" and near.get("dist_support_pct", 1) < 0.015:
        details["penalty_contra_sr"] = f"-{penalty_weight} (short near support)"
        return -penalty_weight
    return 0


def _penalty_declining_volume(df, direction, details):
    """Penalise if volume is declining over the last 3 bars during a directional move."""
    penalty_weight = abs(int(WEIGHTS.get("penalty_declining_volume", 5)))
    if len(df) < 5 or "volume" not in df.columns:
        return 0

    last3_vol = df["volume"].iloc[-3:]
    if last3_vol.iloc[0] > last3_vol.iloc[1] > last3_vol.iloc[2]:
        # Volume decreasing for 3 consecutive bars
        last3_close = df["close"].iloc[-3:]
        move_with_direction = False
        if direction == "long":
            move_with_direction = last3_close.iloc[-1] > last3_close.iloc[0]
        elif direction == "short":
            move_with_direction = last3_close.iloc[-1] < last3_close.iloc[0]

        if move_with_direction:
            details["penalty_vol_decline"] = f"-{penalty_weight} (declining volume on move)"
            return -penalty_weight
    return 0


def _penalty_htf_disagreement(h4, direction, details, daily=None):
    """Penalise when higher-timeframe trend opposes the 1H signal direction."""
    penalty_weight = abs(int(WEIGHTS.get("penalty_htf_disagreement", 10)))

    h4_trend_up = h4.get("trend_up") == True
    h4_trend_dn = h4.get("trend_down") == True

    if direction == "long" and h4_trend_dn:
        details["penalty_htf"] = f"-{penalty_weight} (4H bearish trend opposes long)"
        return -penalty_weight
    if direction == "short" and h4_trend_up:
        details["penalty_htf"] = f"-{penalty_weight} (4H bullish trend opposes short)"
        return -penalty_weight

    daily_penalty = abs(int(WEIGHTS.get("penalty_daily_disagreement", penalty_weight)))
    if daily is not None:
        d_up = daily.get("trend_up") == True
        d_dn = daily.get("trend_down") == True
        if direction == "long" and d_dn:
            details["penalty_daily_htf"] = f"-{daily_penalty} (daily bearish trend opposes long)"
            return -daily_penalty
        if direction == "short" and d_up:
            details["penalty_daily_htf"] = f"-{daily_penalty} (daily bullish trend opposes short)"
            return -daily_penalty

    return 0


# ── Multi-Timeframe RSI Divergence (Suggestion #7) ───────────────────────────

def _penalty_rsi_divergence_4h(df_4h: pd.DataFrame, h4, direction: str, details: dict) -> int:
    """Detect RSI divergence on the 4H timeframe and penalise.

    Catching divergence on a higher timeframe provides stronger conviction
    that the 1H trend may be exhausting.
    """
    penalty_weight = abs(int(WEIGHTS.get("penalty_rsi_divergence", 8)))
    if df_4h is None or df_4h.empty or len(df_4h) < 15 or "rsi" not in df_4h.columns:
        return 0

    recent = df_4h.iloc[-8:]
    price_col = recent["close"]
    rsi_col = recent["rsi"].dropna()
    if len(rsi_col) < 4:
        return 0

    if direction == "long":
        price_hh = price_col.iloc[-1] >= price_col.iloc[-4:].max()
        rsi_lh = rsi_col.iloc[-1] < rsi_col.iloc[-4:].max() - 3
        if price_hh and rsi_lh:
            details["penalty_rsi_div_4h"] = f"-{penalty_weight} (4H bearish RSI divergence)"
            return -penalty_weight
    elif direction == "short":
        price_ll = price_col.iloc[-1] <= price_col.iloc[-4:].min()
        rsi_hl = rsi_col.iloc[-1] > rsi_col.iloc[-4:].min() + 3
        if price_ll and rsi_hl:
            details["penalty_rsi_div_4h"] = f"-{penalty_weight} (4H bullish RSI divergence)"
            return -penalty_weight
    return 0
