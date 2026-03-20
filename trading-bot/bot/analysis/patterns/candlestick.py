"""
Candlestick Pattern Detection
Detects 20+ reversal and continuation patterns using pure price-action logic.
Returns pattern name + direction (+1 bullish, -1 bearish, 0 none).
"""
import pandas as pd


def detect_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    Detect all candlestick patterns and add columns to df.
    Returns df with pattern columns and a combined signal column.
    """
    if len(df) < 5:
        return df

    df = df.copy()

    # ─── Single-candle patterns ─────────────────────────────────────────────
    _add_single_candle_patterns(df)

    # ─── Two-candle patterns ────────────────────────────────────────────────
    _add_two_candle_patterns(df)

    # ─── Three-candle patterns ──────────────────────────────────────────────
    _add_three_candle_patterns(df)

    # ─── Combined pattern signal ────────────────────────────────────────────
    _calculate_combined_scores(df)

    return df


def _body(row) -> float:
    return abs(row["close"] - row["open"])


def _upper_wick(row) -> float:
    return row["high"] - max(row["close"], row["open"])


def _lower_wick(row) -> float:
    return min(row["close"], row["open"]) - row["low"]


def _is_bullish(row) -> bool:
    return row["close"] > row["open"]


def _is_bearish(row) -> bool:
    return row["close"] < row["open"]


def _add_single_candle_patterns(df: pd.DataFrame):
    """Add single-candle patterns like Doji, Hammer, etc."""
    df["p_doji"] = df.apply(_doji_logic, axis=1)
    df["p_hammer"] = _hammer_logic(df)
    df["p_shooting_star"] = _shooting_star_logic(df)
    df["p_spinning_top"] = df.apply(_spinning_top_logic, axis=1)


def _add_two_candle_patterns(df: pd.DataFrame):
    """Add two-candle patterns like Engulfing, Harami, etc."""
    df["p_engulfing"] = _engulfing_logic(df)
    df["p_harami"] = _harami_logic(df)
    df["p_piercing"] = _piercing_logic(df)
    df["p_dark_cloud"] = _dark_cloud_logic(df)
    df["p_tweezer_bottom"] = _tweezer_bottom_logic(df)
    df["p_tweezer_top"] = _tweezer_top_logic(df)


def _add_three_candle_patterns(df: pd.DataFrame):
    """Add three-candle patterns like Morning Star, Three Soldiers, etc."""
    df["p_morning_star"] = _morning_star_logic(df)
    df["p_evening_star"] = _evening_star_logic(df)
    df["p_three_soldiers"] = _three_white_soldiers_logic(df)
    df["p_three_crows"] = _three_black_crows_logic(df)
    df["p_three_inside_up"] = _three_inside_up_logic(df)


def _calculate_combined_scores(df: pd.DataFrame):
    """Compute aggregate bull/bear scores."""
    bullish_cols = [
        "p_hammer", "p_morning_star", "p_three_soldiers",
        "p_three_inside_up", "p_tweezer_bottom", "p_piercing"
    ]
    bearish_cols = [
        "p_shooting_star", "p_evening_star", "p_three_crows",
        "p_dark_cloud", "p_tweezer_top"
    ]

    df["candle_bull_score"] = df[bullish_cols].map(lambda x: 1 if x == 1 else 0).sum(axis=1)
    df["candle_bear_score"] = df[bearish_cols].map(lambda x: 1 if x == -1 else 0).sum(axis=1)

    # Engulfing contributes 2 points
    df.loc[df["p_engulfing"] == 1, "candle_bull_score"] += 2
    df.loc[df["p_engulfing"] == -1, "candle_bear_score"] += 2


def _doji_logic(row) -> int:
    candle_range = row["high"] - row["low"]
    if candle_range == 0:
        return 0
    return 1 if (_body(row) / candle_range) < 0.1 else 0


def _hammer_logic(df: pd.DataFrame) -> pd.Series:
    body = (df["close"] - df["open"]).abs()
    lower = df.apply(_lower_wick, axis=1)
    upper = df.apply(_upper_wick, axis=1)
    prev_bearish = df["close"].shift(1) < df["open"].shift(1)
    mask = (body > 0) & (lower >= 2 * body) & (upper < body * 0.5) & prev_bearish
    result = pd.Series(0, index=df.index)
    result[mask] = 1
    # First row has no previous candle — clear it.
    result.iloc[0] = 0
    return result


def _shooting_star_logic(df: pd.DataFrame) -> pd.Series:
    body = (df["close"] - df["open"]).abs()
    lower = df.apply(_lower_wick, axis=1)
    upper = df.apply(_upper_wick, axis=1)
    prev_bullish = df["close"].shift(1) > df["open"].shift(1)
    mask = (body > 0) & (upper >= 2 * body) & (lower < body * 0.5) & prev_bullish
    result = pd.Series(0, index=df.index)
    result[mask] = -1
    result.iloc[0] = 0
    return result


def _spinning_top_logic(row) -> int:
    body, upper, lower = _body(row), _upper_wick(row), _lower_wick(row)
    if body == 0:
        return 0
    return 1 if (upper > body and lower > body) else 0


def _engulfing_logic(df: pd.DataFrame) -> pd.Series:
    result = pd.Series(0, index=df.index)
    bull_mask = (
        (df["close"].shift(1) < df["open"].shift(1)) &  # prev bearish
        (df["close"] > df["open"]) &                     # cur bullish
        (df["open"] < df["close"].shift(1)) &
        (df["close"] > df["open"].shift(1))
    )
    bear_mask = (
        (df["close"].shift(1) > df["open"].shift(1)) &  # prev bullish
        (df["close"] < df["open"]) &                     # cur bearish
        (df["open"] > df["close"].shift(1)) &
        (df["close"] < df["open"].shift(1))
    )
    result[bull_mask] = 1
    result[bear_mask] = -1
    result.iloc[0] = 0
    return result


def _harami_logic(df: pd.DataFrame) -> pd.Series:
    result = pd.Series(0, index=df.index)
    prev_body = (df["close"] - df["open"]).abs().shift(1)
    prev_lo = df[["open", "close"]].shift(1).min(axis=1)
    prev_hi = df[["open", "close"]].shift(1).max(axis=1)
    contained = (df["open"] > prev_lo) & (df["close"] < prev_hi) & (prev_body > 0)
    bull_mask = contained & (df["close"].shift(1) < df["open"].shift(1)) & (df["close"] > df["open"])
    bear_mask = contained & (df["close"].shift(1) > df["open"].shift(1)) & (df["close"] < df["open"])
    result[bull_mask] = 1
    result[bear_mask] = -1
    result.iloc[0] = 0
    return result


def _piercing_logic(df: pd.DataFrame) -> pd.Series:
    mid_prev = (df["open"].shift(1) + df["close"].shift(1)) / 2
    mask = (
        (df["close"].shift(1) < df["open"].shift(1)) &  # prev bearish
        (df["close"] > df["open"]) &                     # cur bullish
        (df["open"] < df["close"].shift(1)) &
        (df["close"] > mid_prev)
    )
    result = pd.Series(0, index=df.index)
    result[mask] = 1
    result.iloc[0] = 0
    return result


def _dark_cloud_logic(df: pd.DataFrame) -> pd.Series:
    mid_prev = (df["open"].shift(1) + df["close"].shift(1)) / 2
    mask = (
        (df["close"].shift(1) > df["open"].shift(1)) &  # prev bullish
        (df["close"] < df["open"]) &                     # cur bearish
        (df["open"] > df["close"].shift(1)) &
        (df["close"] < mid_prev)
    )
    result = pd.Series(0, index=df.index)
    result[mask] = -1
    result.iloc[0] = 0
    return result


def _tweezer_bottom_logic(df: pd.DataFrame) -> pd.Series:
    mask = (
        ((df["low"] - df["low"].shift(1)).abs() < (df["close"] * 0.001)) &
        (df["close"].shift(1) < df["open"].shift(1)) &  # prev bearish
        (df["close"] > df["open"])                       # cur bullish
    )
    result = pd.Series(0, index=df.index)
    result[mask] = 1
    result.iloc[0] = 0
    return result


def _tweezer_top_logic(df: pd.DataFrame) -> pd.Series:
    mask = (
        ((df["high"] - df["high"].shift(1)).abs() < (df["close"] * 0.001)) &
        (df["close"].shift(1) > df["open"].shift(1)) &  # prev bullish
        (df["close"] < df["open"])                       # cur bearish
    )
    result = pd.Series(0, index=df.index)
    result[mask] = -1
    result.iloc[0] = 0
    return result


def _morning_star_logic(df: pd.DataFrame) -> pd.Series:
    result = pd.Series(0, index=df.index)
    for i in range(2, len(df)):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        if (_is_bearish(c1) and _body(c2) < _body(c1) * 0.3
                and _is_bullish(c3) and c3["close"] > (c1["open"] + c1["close"]) / 2):
            result.iloc[i] = 1
    return result


def _evening_star_logic(df: pd.DataFrame) -> pd.Series:
    result = pd.Series(0, index=df.index)
    for i in range(2, len(df)):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        if (_is_bullish(c1) and _body(c2) < _body(c1) * 0.3
                and _is_bearish(c3) and c3["close"] < (c1["open"] + c1["close"]) / 2):
            result.iloc[i] = -1
    return result


def _three_white_soldiers_logic(df: pd.DataFrame) -> pd.Series:
    result = pd.Series(0, index=df.index)
    for i in range(2, len(df)):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        bullish = all(_is_bullish(c) for c in [c1, c2, c3])
        # Each candle must open WITHIN the prior candle's body (textbook rule).
        # Also closes must step progressively higher.
        c2_open_in_c1 = c1["open"] <= c2["open"] <= c1["close"]
        c3_open_in_c2 = c2["open"] <= c3["open"] <= c2["close"]
        upward = (
            c2_open_in_c1 and c3_open_in_c2
            and c2["close"] > c1["close"] and c3["close"] > c2["close"]
        )
        if bullish and upward:
            result.iloc[i] = 1
    return result


def _three_black_crows_logic(df: pd.DataFrame) -> pd.Series:
    result = pd.Series(0, index=df.index)
    for i in range(2, len(df)):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        bearish = all(_is_bearish(c) for c in [c1, c2, c3])
        # Each candle must open WITHIN the prior candle's body (textbook rule).
        c2_open_in_c1 = c1["close"] <= c2["open"] <= c1["open"]
        c3_open_in_c2 = c2["close"] <= c3["open"] <= c2["open"]
        downward = (
            c2_open_in_c1 and c3_open_in_c2
            and c2["close"] < c1["close"] and c3["close"] < c2["close"]
        )
        if bearish and downward:
            result.iloc[i] = -1
    return result


def _three_inside_up_logic(df: pd.DataFrame) -> pd.Series:
    result = pd.Series(0, index=df.index)
    for i in range(2, len(df)):
        c1, c2, c3 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
        # C1 bearish, C2 bullish and contained within C1's body (harami)
        harami_bull = (_is_bearish(c1) and _is_bullish(c2)
                       and c2["open"] > c1["close"] and c2["close"] < c1["open"])
        # C3 bullish and closes above C1's open (top of C1 body)
        if harami_bull and _is_bullish(c3) and c3["close"] > c1["open"]:
            result.iloc[i] = 1
    return result
