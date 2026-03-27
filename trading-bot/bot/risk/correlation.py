"""
Correlation Risk Management
Prevents overconcentration in highly-correlated assets.
"""
import logging
import pandas as pd
from bot.config import CORRELATION_THRESHOLD, MAX_CORRELATED_POSITIONS

logger = logging.getLogger(__name__)


def compute_correlation_matrix(close_prices: dict[str, pd.Series], lookback: int = 30) -> pd.DataFrame:
    """
    Compute pairwise correlation matrix from close price series.
    
    Args:
        close_prices: {symbol: pd.Series of close prices}
        lookback: Number of bars to use for correlation calculation.
    
    Returns:
        DataFrame correlation matrix.
    """
    if not close_prices or len(close_prices) < 2:
        return pd.DataFrame()

    combined = pd.DataFrame(close_prices)
    # Use percentage returns for more stable correlation
    returns = combined.pct_change().dropna()
    if len(returns) < max(lookback, 10):
        return pd.DataFrame()

    return returns.iloc[-lookback:].corr()


def check_portfolio_correlation(
    new_symbol: str,
    open_symbols: list[str],
    correlation_matrix: pd.DataFrame,
    threshold: float | None = None,
    max_correlated: int | None = None,
) -> tuple[bool, str]:
    """
    Check if adding new_symbol would violate correlation limits.
    
    Returns:
        (allowed: bool, reason: str)
    """
    if threshold is None:
        threshold = CORRELATION_THRESHOLD
    if max_correlated is None:
        max_correlated = MAX_CORRELATED_POSITIONS

    if correlation_matrix.empty or new_symbol not in correlation_matrix.columns:
        # Can't check — allow through
        return True, "OK"

    highly_correlated_count = 0
    correlated_with = []

    for existing_sym in open_symbols:
        if existing_sym not in correlation_matrix.columns:
            continue
        corr = abs(float(correlation_matrix.loc[new_symbol, existing_sym]))
        if corr >= threshold:
            highly_correlated_count += 1
            correlated_with.append(f"{existing_sym}(r={corr:.2f})")

    if highly_correlated_count >= max_correlated:
        reason = (
            f"Correlation limit: {new_symbol} highly correlated with "
            f"{', '.join(correlated_with)} (max {max_correlated} correlated positions allowed)"
        )
        logger.warning("[RISK] %s", reason)
        return False, reason

    return True, "OK"
