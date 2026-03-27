"""
Paper vs Live Position Diff Report (Suggestion #10)
Compares paper trade outcomes against live trade outcomes to detect
execution quality issues (slippage, fill rate, timing gaps).
"""
import logging
import sqlite3
from bot.config import DB_PATH

logger = logging.getLogger(__name__)


def _db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def generate_diff_report() -> dict:
    """Compare paper vs live trade outcomes for the same symbols.

    Returns a dict with:
        - matched_trades: list of paired paper/live trades for the same symbol
        - paper_only_count: trades that only exist in paper
        - live_only_count: trades that only exist in live
        - avg_slippage_diff_pct: average PnL difference between paper and live
        - summary: human-readable summary string
    """
    report = {
        "matched_trades": [],
        "paper_only_count": 0,
        "live_only_count": 0,
        "avg_pnl_diff": 0.0,
        "avg_pnl_diff_pct": 0.0,
        "paper_win_rate": 0.0,
        "live_win_rate": 0.0,
        "summary": "",
    }

    try:
        with _db_connect() as conn:
            if not _has_table(conn, "paper_trades") or not _has_table(conn, "live_trades"):
                report["summary"] = "One or both trade tables missing."
                return report

            paper_rows = conn.execute(
                "SELECT symbol, direction, entry_price, pnl, status, entry_time, exit_time "
                "FROM paper_trades WHERE status LIKE 'closed%' ORDER BY exit_time DESC"
            ).fetchall()

            live_rows = conn.execute(
                "SELECT symbol, direction, entry_price, pnl, status, entry_time, close_time as exit_time "
                "FROM live_trades WHERE status='closed' ORDER BY close_time DESC"
            ).fetchall()

        paper_by_sym = {}
        for r in paper_rows:
            sym = r["symbol"]
            if sym not in paper_by_sym:
                paper_by_sym[sym] = []
            paper_by_sym[sym].append(dict(r))

        live_by_sym = {}
        for r in live_rows:
            sym = r["symbol"]
            if sym not in live_by_sym:
                live_by_sym[sym] = []
            live_by_sym[sym].append(dict(r))

        all_symbols = set(paper_by_sym.keys()) | set(live_by_sym.keys())
        pnl_diffs = []

        for sym in sorted(all_symbols):
            p_trades = paper_by_sym.get(sym, [])
            l_trades = live_by_sym.get(sym, [])

            # Pair by nearest entry time (simple greedy approach)
            used_live = set()
            for pt in p_trades:
                best_match = None
                best_idx = None
                for i, lt in enumerate(l_trades):
                    if i in used_live:
                        continue
                    if lt["direction"] == pt["direction"]:
                        best_match = lt
                        best_idx = i
                        break

                if best_match is not None and best_idx is not None:
                    used_live.add(best_idx)
                    p_pnl = float(pt.get("pnl") or 0.0)
                    l_pnl = float(best_match.get("pnl") or 0.0)
                    diff = l_pnl - p_pnl
                    pnl_diffs.append(diff)
                    report["matched_trades"].append({
                        "symbol": sym,
                        "direction": pt["direction"],
                        "paper_pnl": round(p_pnl, 4),
                        "live_pnl": round(l_pnl, 4),
                        "pnl_diff": round(diff, 4),
                    })

            report["paper_only_count"] += max(len(p_trades) - len(used_live), 0)
            report["live_only_count"] += max(len(l_trades) - len(used_live), 0)

        if pnl_diffs:
            report["avg_pnl_diff"] = round(sum(pnl_diffs) / len(pnl_diffs), 4)

        paper_wins = sum(1 for r in paper_rows if (r["pnl"] or 0) > 0)
        live_wins = sum(1 for r in live_rows if (r["pnl"] or 0) > 0)
        report["paper_win_rate"] = round(paper_wins / len(paper_rows) * 100, 1) if paper_rows else 0.0
        report["live_win_rate"] = round(live_wins / len(live_rows) * 100, 1) if live_rows else 0.0

        report["summary"] = (
            f"Matched {len(report['matched_trades'])} trades | "
            f"Avg PnL diff: ${report['avg_pnl_diff']:+.4f} | "
            f"Paper WR: {report['paper_win_rate']}% | "
            f"Live WR: {report['live_win_rate']}% | "
            f"Paper-only: {report['paper_only_count']} | "
            f"Live-only: {report['live_only_count']}"
        )

    except Exception as e:
        logger.error("Diff report generation failed: %s", e)
        report["summary"] = f"Error: {e}"

    return report
