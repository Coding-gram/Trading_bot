"""
Technical Indicators – computes all TA signals using pandas-ta
"""
import logging
import warnings
import pandas as pd
import pandas_ta as ta

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)

logger = logging.getLogger(__name__)


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    Adds all technical indicators as columns to the DataFrame.
    Input df must have: open, high, low, close, volume columns.
    """
    if df.empty or len(df) < 50:
        logger.warning("Not enough candle data for indicator computation.")
        return df

    df = df.copy()

    _add_trend_indicators(df)
    _add_momentum_indicators(df)
    _add_volatility_indicators(df)
    _add_volume_indicators(df)
    _add_derived_signals(df)

    return df


def _add_trend_indicators(df: pd.DataFrame):
    """Add EMA, SMA, MACD and ADX."""
    df["ema9"] = ta.ema(df["close"], length=9)
    df["ema21"] = ta.ema(df["close"], length=21)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)
    df["sma50"] = ta.sma(df["close"], length=50)

    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd is not None:
        df["macd"] = macd["MACD_12_26_9"]
        df["macd_signal"] = macd["MACDs_12_26_9"]
        df["macd_hist"] = macd["MACDh_12_26_9"]

    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    if adx is not None:
        df["adx"] = adx["ADX_14"]
        df["dmp"] = adx["DMP_14"]   # +DI
        df["dmn"] = adx["DMN_14"]   # -DI


def _add_momentum_indicators(df: pd.DataFrame):
    """Add RSI, Stochastic and Williams %R."""
    df["rsi"] = ta.rsi(df["close"], length=14)

    stoch = ta.stoch(df["high"], df["low"], df["close"])
    if stoch is not None:
        df["stoch_k"] = stoch["STOCHk_14_3_3"]
        df["stoch_d"] = stoch["STOCHd_14_3_3"]

    df["willr"] = ta.willr(df["high"], df["low"], df["close"], length=14)


def _add_volatility_indicators(df: pd.DataFrame):
    """Add ATR and Bollinger Bands."""
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    bb = ta.bbands(df["close"], length=20, std=2)
    if bb is not None:
        # Match columns by name prefix — safe across all pandas-ta versions.
        # pandas-ta names: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0, BBB_20_2.0, BBP_20_2.0
        def _col(prefix):
            matches = [c for c in bb.columns if c.startswith(prefix)]
            return matches[0] if matches else None

        if _col("BBL_"):
            df["bb_lower"] = bb[_col("BBL_")]
        if _col("BBM_"):
            df["bb_mid"] = bb[_col("BBM_")]
        if _col("BBU_"):
            df["bb_upper"] = bb[_col("BBU_")]
        if _col("BBP_"):
            df["bb_pct"] = bb[_col("BBP_")]


def _add_volume_indicators(df: pd.DataFrame):
    """Add OBV and daily-anchored VWAP."""
    df["obv"] = ta.obv(df["close"], df["volume"])
    # Ensure DatetimeIndex for grouping
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception as e:
            logger.warning("Could not convert index to DatetimeIndex for VWAP: %s", e)
            df["vwap"] = None
            return

    # Ensure stable ordering and unique timestamps to avoid reindex/assignment failures.
    if not df.index.is_monotonic_increasing:
        df.sort_index(inplace=True)
    if df.index.has_duplicates:
        dup_count = int(df.index.duplicated(keep="last").sum())
        logger.warning("VWAP: dropping %d duplicate timestamp rows", dup_count)
        df.drop(index=df.index[df.index.duplicated(keep="last")], inplace=True)

    # Daily-anchored VWAP via grouped cumulative sums (robust with duplicate-safe index).
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    session = pd.Series(df.index.date, index=df.index)
    cum_tpv = (typical_price * df["volume"]).groupby(session).cumsum()
    cum_vol = df["volume"].groupby(session).cumsum()
    df["vwap"] = cum_tpv / cum_vol.where(cum_vol != 0)


def _add_derived_signals(df: pd.DataFrame):
    """Add boolean helper flags for strategy decisions."""
    df["trend_up"] = (df["ema9"] > df["ema21"]) & (df["ema21"] > df["ema50"])
    df["trend_down"] = (df["ema9"] < df["ema21"]) & (df["ema21"] < df["ema50"])
    df["strong_trend"] = df["adx"] > 25
    df["rsi_oversold"] = df["rsi"] < 35
    df["rsi_overbought"] = df["rsi"] > 65

    if "macd" in df.columns and "macd_signal" in df.columns:
        mup = (df["macd"] > df["macd_signal"]) & (df["macd"].shift(1) <= df["macd_signal"].shift(1))
        mdn = (df["macd"] < df["macd_signal"]) & (df["macd"].shift(1) >= df["macd_signal"].shift(1))
        df["macd_cross_up"] = mup
        df["macd_cross_dn"] = mdn

    df["vol_spike"] = df["volume"] > df["volume"].rolling(20).mean() * 1.5

    # ── Ensure all boolean flag columns contain True/False, never NaN.
    # NaN propagates through warm-up rows (first N candles before indicators
    # converge) and np.nan is truthy in Python, which can produce spurious
    # "long" or "short" directions before any real alignment exists.
    bool_cols = [
        "trend_up", "trend_down", "strong_trend",
        "rsi_oversold", "rsi_overbought", "vol_spike",
    ]
    if "macd_cross_up" in df.columns:
        bool_cols += ["macd_cross_up", "macd_cross_dn"]
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].fillna(False).astype(bool)


def get_market_regime(df: pd.DataFrame) -> str:
    """
    Classify overall market condition: trending_up, trending_down, ranging.
    Uses EMA alignment + ADX score.
    """
    if df.empty:
        return "unknown"

    last = df.iloc[-1]
    if last.get("adx", 0) <= 25:
        return "ranging"
    if last.get("trend_up", False):
        return "trending_up"
    if last.get("trend_down", False):
        return "trending_down"
    return "ranging"
