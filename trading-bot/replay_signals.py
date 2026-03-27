"""
Backtest Signal Replay (Suggestion #14)
Reads saved signals from the structured JSONL signal log and replays them
through the paper trade engine to evaluate strategy changes.
"""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_LOG = str(Path(__file__).resolve().parents[0] / "bot" / "logs" / "signals.jsonl")


def load_signals(filepath: str | None = None) -> list[dict]:
    """Load signals from a JSONL signal log file.

    Args:
        filepath: Path to the signals.jsonl file.

    Returns:
        List of signal dicts, ordered chronologically.
    """
    path = filepath or DEFAULT_SIGNAL_LOG
    if not os.path.isfile(path):
        logger.warning("Signal log not found: %s", path)
        return []

    signals = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                signals.append(record)
            except json.JSONDecodeError as e:
                logger.warning("Skipping invalid JSON at line %d: %s", line_num, e)

    logger.info("Loaded %d signals from %s", len(signals), path)
    return signals


def replay_signals(
    signals: list[dict],
    starting_capital: float = 1000.0,
    min_score: int = 0,
) -> dict:
    """Replay saved signals through a fresh paper trade engine.

    Args:
        signals: List of signal records (from load_signals or JSONL).
        starting_capital: Initial capital for the paper engine.
        min_score: Minimum score threshold to accept a signal.

    Returns:
        Dict with replay stats: trades_opened, trades_closed, final_capital, pnl, etc.
    """
    # Import here to avoid circular imports at module level
    from bot.execution.paper_trade import PaperTradeEngine

    engine = PaperTradeEngine(starting_capital=starting_capital)
    stats = {
        "total_signals": len(signals),
        "signals_accepted": 0,
        "signals_rejected": 0,
        "trades_opened": 0,
        "trades_closed": 0,
        "final_capital": starting_capital,
        "total_pnl": 0.0,
        "errors": 0,
    }

    for record in signals:
        try:
            score = int(record.get("score", 0) or 0)
            direction = record.get("direction", "neutral")

            if direction == "neutral" or score < min_score:
                stats["signals_rejected"] += 1
                continue

            risk = record.get("risk", {})
            if not risk or not risk.get("sl") or not risk.get("tp1"):
                stats["signals_rejected"] += 1
                continue

            signal_dict = {
                "symbol": record.get("symbol", "UNKNOWN/USDT"),
                "direction": direction,
                "score": score,
                "price": record.get("price", 0),
            }

            risk_params = {
                "sl": float(risk.get("sl", 0)),
                "tp1": float(risk.get("tp1", 0)),
                "tp2": float(risk.get("tp2", risk.get("tp1", 0))),
                "qty": float(risk.get("qty", 0)),
            }

            if risk_params["qty"] <= 0:
                stats["signals_rejected"] += 1
                continue

            result = engine.open_position(signal_dict, risk_params)
            if result:
                stats["trades_opened"] += 1
                stats["signals_accepted"] += 1
            else:
                stats["signals_rejected"] += 1

            # Simulate price hit based on signal price
            price = float(record.get("price", 0) or 0)
            if price > 0:
                closed = engine.update_positions({record["symbol"]: price})
                stats["trades_closed"] += len(closed)

        except Exception as e:
            stats["errors"] += 1
            logger.debug("Replay error: %s", e)

    stats["final_capital"] = round(engine.capital, 2)
    stats["total_pnl"] = round(engine.capital - starting_capital, 2)

    return stats


if __name__ == "__main__":
    import sys

    filepath = sys.argv[1] if len(sys.argv) > 1 else None
    min_score = int(sys.argv[2]) if len(sys.argv) > 2 else 65

    sigs = load_signals(filepath)
    if sigs:
        result = replay_signals(sigs, min_score=min_score)
        print(json.dumps(result, indent=2))
    else:
        print("No signals found to replay.")
