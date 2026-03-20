"""
Deterministic scoring replay utility.

Capture a fixed candle snapshot once, then replay scoring from the same files to
verify score stability after code changes.
"""
import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from bot.config import WATCHLIST, PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME
from bot.data.fetcher import get_exchange, fetch_multi_timeframe
from bot.strategy.scorer import score_signal


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot_name() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _save_df(df: pd.DataFrame, file_path: Path) -> None:
    df.to_csv(file_path)


def _load_df(file_path: Path) -> pd.DataFrame:
    return pd.read_csv(file_path, index_col=0, parse_dates=True)


def _sanitize_symbol(symbol: str) -> str:
    return symbol.replace("/", "_")


def _score_from_frames(symbol: str, df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> dict:
    signal = score_signal(df_1h, df_4h, symbol)
    details = {k: str(v) for k, v in signal.get("details", {}).items()}
    return {
        "symbol": symbol,
        "score": int(signal.get("score", 0)),
        "direction": str(signal.get("direction", "neutral")),
        "send_alert": bool(signal.get("send_alert", False)),
        "details": details,
    }


def capture_snapshot(output_root: Path, symbols: list[str]) -> Path:
    snapshot_dir = _ensure_dir(output_root / _snapshot_name())
    exchange = get_exchange(paper_mode=True)

    baseline = {"created_at_utc": datetime.now(UTC).isoformat(), "results": []}

    for symbol in symbols:
        dfs = fetch_multi_timeframe(exchange, symbol, [PRIMARY_TIMEFRAME, CONFIRM_TIMEFRAME])
        df_1h = dfs.get(PRIMARY_TIMEFRAME)
        df_4h = dfs.get(CONFIRM_TIMEFRAME)

        if df_1h is None or df_4h is None or df_1h.empty or df_4h.empty:
            continue

        sym = _sanitize_symbol(symbol)
        _save_df(df_1h, snapshot_dir / f"{sym}_{PRIMARY_TIMEFRAME}.csv")
        _save_df(df_4h, snapshot_dir / f"{sym}_{CONFIRM_TIMEFRAME}.csv")

        baseline["results"].append(_score_from_frames(symbol, df_1h, df_4h))

    (snapshot_dir / "baseline.json").write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    return snapshot_dir


def replay_snapshot(snapshot_dir: Path) -> int:
    baseline_path = snapshot_dir / "baseline.json"
    if not baseline_path.exists():
        raise FileNotFoundError(f"Missing baseline file: {baseline_path}")

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    expected = {item["symbol"]: item for item in baseline.get("results", [])}

    mismatches = []
    checked = 0

    for symbol, exp in expected.items():
        sym = _sanitize_symbol(symbol)
        one_h_file = snapshot_dir / f"{sym}_{PRIMARY_TIMEFRAME}.csv"
        four_h_file = snapshot_dir / f"{sym}_{CONFIRM_TIMEFRAME}.csv"

        if not one_h_file.exists() or not four_h_file.exists():
            mismatches.append((symbol, "missing_snapshot_files"))
            continue

        df_1h = _load_df(one_h_file)
        df_4h = _load_df(four_h_file)
        actual = _score_from_frames(symbol, df_1h, df_4h)
        checked += 1

        if (
            actual["score"] != exp.get("score")
            or actual["direction"] != exp.get("direction")
            or actual["send_alert"] != exp.get("send_alert")
        ):
            mismatches.append((
                symbol,
                {
                    "expected": {
                        "score": exp.get("score"),
                        "direction": exp.get("direction"),
                        "send_alert": exp.get("send_alert"),
                    },
                    "actual": {
                        "score": actual["score"],
                        "direction": actual["direction"],
                        "send_alert": actual["send_alert"],
                    },
                },
            ))

    print(f"Checked symbols: {checked}")
    if not mismatches:
        print("Replay result: PASS (no scoring drift)")
        return 0

    print("Replay result: FAIL")
    for item in mismatches:
        print(item)
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic scorer replay checker")
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="Capture market snapshot + baseline scores")
    cap.add_argument("--output-root", default="replay_artifacts", help="Snapshot root directory")
    cap.add_argument("--limit", type=int, default=8, help="Number of watchlist symbols to capture")

    rep = sub.add_parser("replay", help="Replay snapshot and check score drift")
    rep.add_argument("--snapshot-dir", required=True, help="Path to captured snapshot directory")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "capture":
        root = Path(args.output_root)
        symbols = WATCHLIST[: max(args.limit, 1)]
        snapshot_dir = capture_snapshot(root, symbols)
        print(f"Snapshot created: {snapshot_dir}")
        return 0

    if args.command == "replay":
        return replay_snapshot(Path(args.snapshot_dir))

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
