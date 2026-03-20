"""
FastAPI backend – exposes bot stats & trade history to the dashboard.
Run with: uvicorn api.server:app --reload --port 8000
"""
import sqlite3
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from bot.config import DB_PATH, TOTAL_CAPITAL_USDT
from bot import telemetry

app = FastAPI(title="Trading Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def query_db(sql: str, params=()):
    """Helper to execute SQL queries and return results as dicts."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def table_exists(name: str) -> bool:
    rows = query_db("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return bool(rows)


def get_closed_trades(limit: int | None = None):
    rows = []
    if table_exists("paper_trades"):
        rows.extend(
            query_db(
                """
                SELECT
                    id,
                    symbol,
                    direction,
                    entry_price,
                    qty,
                    status,
                    score,
                    reason,
                    entry_time AS open_time,
                    exit_time AS close_time,
                    exit_price,
                    pnl,
                    'paper' AS mode
                FROM paper_trades
                WHERE status LIKE 'closed%'
                """
            )
        )
    if table_exists("live_trades"):
        rows.extend(
            query_db(
                """
                SELECT
                    id,
                    symbol,
                    direction,
                    entry_price,
                    qty,
                    status,
                    NULL AS score,
                    status AS reason,
                    open_time,
                    close_time,
                    NULL AS exit_price,
                    COALESCE(pnl, 0) AS pnl,
                    'live' AS mode
                FROM live_trades
                WHERE status='closed'
                """
            )
        )
    rows.sort(key=lambda r: r.get("close_time") or r.get("open_time") or "", reverse=True)
    return rows[:limit] if limit is not None else rows


def get_open_positions_all():
    rows = []
    if table_exists("paper_trades"):
        rows.extend(
            query_db(
                """
                SELECT
                    id,
                    symbol,
                    direction,
                    entry_price,
                    qty,
                    stop_loss,
                    tp1,
                    tp2,
                    score,
                    status,
                    entry_time AS open_time,
                    'paper' AS mode
                FROM paper_trades
                WHERE status='open'
                """
            )
        )
    if table_exists("live_trades"):
        rows.extend(
            query_db(
                """
                SELECT
                    id,
                    symbol,
                    direction,
                    entry_price,
                    qty,
                    NULL AS stop_loss,
                    NULL AS tp1,
                    NULL AS tp2,
                    NULL AS score,
                    status,
                    open_time,
                    'live' AS mode
                FROM live_trades
                WHERE status='open'
                """
            )
        )
    rows.sort(key=lambda r: r.get("open_time") or "", reverse=True)
    return rows


@app.get("/api/stats")
def get_stats():
    """Get aggregate trading statistics across paper+live closed trades."""
    rows = get_closed_trades()
    if not rows:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "paper_total": 0,
            "live_total": 0,
        }

    wins = [r for r in rows if (r.get("pnl") or 0) > 0]
    losses = [r for r in rows if (r.get("pnl") or 0) <= 0]
    total_trades = len(rows)
    paper_total = sum(1 for r in rows if r.get("mode") == "paper")
    live_total = sum(1 for r in rows if r.get("mode") == "live")

    return {
        "total": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total_trades * 100, 1),
        "total_pnl": round(sum(r.get("pnl") or 0 for r in rows), 2),
        "paper_total": paper_total,
        "live_total": live_total,
    }


@app.get("/api/trades")
def get_trades(limit: int = 50):
    """Get recent closed trade history across paper+live."""
    return get_closed_trades(limit=max(1, int(limit or 50)))


@app.get("/api/open")
def get_open():
    """Get currently open positions across paper+live."""
    return get_open_positions_all()


@app.get("/api/pnl-curve")
def get_pnl_curve():
    """Get cumulative PnL data for chart visualization (paper mode baseline)."""
    rows = []
    if table_exists("paper_trades"):
        rows = query_db(
            "SELECT exit_time, pnl FROM paper_trades WHERE status LIKE 'closed%' ORDER BY exit_time"
        )
    cum_pnl = TOTAL_CAPITAL_USDT
    pnl_curve = []
    for r in rows:
        cum_pnl += r["pnl"]
        pnl_curve.append({
            "date": r["exit_time"],
            "capital": round(cum_pnl, 2)
        })
    return pnl_curve


@app.get("/api/quality")
def get_quality_metrics():
    """Get quality metrics used for operational bot health scoring."""
    def _to_float(value, default=0.0):
        if value is None:
            return default
        if isinstance(value, bytes):
            try:
                return float(value.decode("utf-8"))
            except Exception:
                return default
        try:
            return float(value)
        except Exception:
            return default

    rows = []
    if table_exists("paper_trades"):
        rows.extend(
            query_db(
                """
                SELECT pnl, score, entry_time AS open_time, exit_time AS close_time, status
                FROM paper_trades
                WHERE status LIKE 'closed%'
                """
            )
        )
    if table_exists("live_trades"):
        rows.extend(
            query_db(
                """
                SELECT COALESCE(pnl, 0) AS pnl, NULL AS score, open_time, close_time, status
                FROM live_trades
                WHERE status='closed'
                """
            )
        )
    rows.sort(key=lambda r: r.get("close_time") or r.get("open_time") or "")
    if not rows:
        return {
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "expectancy_per_trade": 0.0,
            "high_conf_precision_pct": 0.0,
            "sample_size": 0,
        }

    pnls = [_to_float(r.get("pnl"), 0.0) for r in rows]
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss_abs = abs(sum(x for x in pnls if x < 0))
    profit_factor = round(gross_profit / gross_loss_abs, 3) if gross_loss_abs > 0 else (999.0 if gross_profit > 0 else 0.0)

    equity = TOTAL_CAPITAL_USDT
    peak = equity
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd

    high_conf = [r for r in rows if _to_float(r.get("score"), 0.0) >= 70]
    high_conf_wins = [r for r in high_conf if _to_float(r.get("pnl"), 0.0) > 0]
    high_conf_precision = round(len(high_conf_wins) / len(high_conf) * 100, 1) if high_conf else 0.0

    return {
        "profit_factor": profit_factor,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "expectancy_per_trade": round(sum(pnls) / len(pnls), 3),
        "high_conf_precision_pct": high_conf_precision,
        "sample_size": len(rows),
    }


@app.get("/api/runtime")
def get_runtime_metrics():
    """Get real-time runtime observability counters and derived health metrics."""
    open_rows = get_open_positions_all()
    recent_closed = 0
    if table_exists("paper_trades"):
        result = query_db(
            """
            SELECT COUNT(*) AS n
            FROM paper_trades
            WHERE status LIKE 'closed%'
              AND COALESCE(exit_time, entry_time) >= datetime('now', '-24 hours')
            """
        )
        recent_closed += int(result[0]["n"]) if result else 0
    if table_exists("live_trades"):
        result = query_db(
            """
            SELECT COUNT(*) AS n
            FROM live_trades
            WHERE status='closed'
              AND COALESCE(close_time, open_time) >= datetime('now', '-24 hours')
            """
        )
        recent_closed += int(result[0]["n"]) if result else 0

    paper_open = sum(1 for row in open_rows if row.get("mode") == "paper")
    live_open = sum(1 for row in open_rows if row.get("mode") == "live")

    snap = telemetry.snapshot()
    return {
        "runtime": snap,
        "db": {
            "open_positions": len(open_rows),
            "open_positions_paper": paper_open,
            "open_positions_live": live_open,
            "closed_last_24h": recent_closed,
        },
    }
