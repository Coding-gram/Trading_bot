"""
Unit Test - mimics one full scan across the watchlist.
"""
import logging
import sys
import traceback
from bot.config import (
    WATCHLIST, PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME
)
from bot.analysis import indicators
from bot.data.fetcher import (
    get_exchange, fetch_multi_timeframe, fetch_ticker
)
from bot.main import is_spread_ok, is_volatility_normal
from bot.strategy.scorer import score_signal
from bot.execution.paper_trade import PaperTradeEngine

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


def test_run():
    """Execute a simulated scan across all watched symbols."""
    logger.info("Testing Bot Logic...")
    exchange = get_exchange(paper_mode=True)
    engine = PaperTradeEngine(starting_capital=1000.0)
    known_error_count = 0
    unexpected_error_count = 0

    for symbol in WATCHLIST:
        logger.info("\n--- Testing %s ---", symbol)
        try:
            dfs = fetch_multi_timeframe(
                exchange, symbol, [PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME]
            )
            df_1h = dfs.get(PRIMARY_TIMEFRAME)
            df_4h = dfs.get(CONFIRM_TIMEFRAME)

            if df_1h is None or df_4h is None or df_1h.empty:
                logger.warning("Skipping %s: Empty dataframe", symbol)
                continue

            ticker = fetch_ticker(exchange, symbol)
            if ticker and not is_spread_ok(ticker, symbol):
                logger.warning("Skipping %s: Spread not OK", symbol)
                continue

            df_1h_with_indicators = indicators.compute_all(df_1h)
            if not is_volatility_normal(df_1h_with_indicators, symbol):
                logger.warning("Skipping %s: Volatility spike check failed", symbol)
                continue

            # Core scoring logic
            signal = score_signal(df_1h, df_4h, symbol)
            logger.info(
                "Signal for %s: %s/100 | Direction: %s",
                symbol, signal['score'], signal['direction']
            )

        except (RuntimeError, ValueError) as err:
            logger.error("Known error on %s: %s", symbol, err)
            known_error_count += 1
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error("Unexpected error on %s: %s", symbol, err)
            traceback.print_exc()
            unexpected_error_count += 1

    total_errors = known_error_count + unexpected_error_count
    if total_errors > 0:
        logger.error(
            "Test run failed: %d known errors, %d unexpected errors",
            known_error_count,
            unexpected_error_count,
        )
        return 1

    logger.info("Test run passed: no symbol errors")
    return 0


if __name__ == '__main__':
    raise SystemExit(test_run())
