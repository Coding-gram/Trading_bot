"""
FastAPI backend – exposes bot stats & trade history to the dashboard.
Run with: uvicorn api.server:app --reload --port 8000
"""
import sqlite3
import json
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from bot.config import (
    DB_PATH,
    TOTAL_CAPITAL_USDT,
    DASHBOARD_ONLY_CURRENT_SESSION,
    AUTO_WEIGHT_FEEDBACK_ENABLED,
)
from bot import telemetry
from bot.strategy.performance_tracker import PerformanceTracker

app = FastAPI(title="Trading Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_API_STARTED_AT_UTC = datetime.now(timezone.utc)


def _get_latest_telemetry_event_value(metric: str):
    """Read the latest telemetry event value for a metric directly from DB.

    Needed because API and bot run in separate processes.
    """
    if not table_exists("telemetry_events"):
        return None
    rows = query_db(
        """
        SELECT value_json, created_at
        FROM telemetry_events
        WHERE metric = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (metric,),
    )
    if not rows:
        return None
    value_json = rows[0].get("value_json")
    if value_json in (None, ""):
        return rows[0].get("created_at")
    try:
        return json.loads(value_json)
    except Exception:
        return value_json


def _dashboard_session_start_utc() -> datetime:
    """Resolve session start for dashboard filtering.

    Prefer bot scan-session start when available; otherwise fall back to API start.
    """
    session_start = _API_STARTED_AT_UTC
    try:
        event_value = _get_latest_telemetry_event_value("bot_session_started_at")
        parsed = _parse_trade_time(event_value)
        if parsed is None:
            snap = telemetry.snapshot()
            event = (snap.get("last_event") or {}).get("bot_session_started_at")
            if isinstance(event, dict):
                parsed = _parse_trade_time(event.get("value") or event.get("at"))
            else:
                parsed = _parse_trade_time(event)
        if parsed is not None and parsed > session_start:
            session_start = parsed
    except Exception:
        pass
    return session_start


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


def _parse_trade_time(value):
    if value in (None, ""):
        return None

    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _filter_to_current_session(rows: list[dict], close_key: str, open_key: str):
    if not DASHBOARD_ONLY_CURRENT_SESSION:
        return rows

    session_start = _dashboard_session_start_utc()
    filtered = []
    for row in rows:
        ts = _parse_trade_time(row.get(close_key) or row.get(open_key))
        if ts is not None and ts >= session_start:
            filtered.append(row)
    return filtered


def _normalize_mode(mode: str | None) -> str:
    normalized = str(mode or "all").strip().lower()
    return normalized if normalized in {"all", "paper", "live"} else "all"


def _filter_rows_by_mode(rows: list[dict], mode: str | None):
    resolved = _normalize_mode(mode)
    if resolved == "all":
        return rows
    return [row for row in rows if str(row.get("mode") or "").lower() == resolved]


def get_closed_trades(limit: int | None = None, mode: str | None = "all"):
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
    rows = _filter_to_current_session(rows, close_key="close_time", open_key="open_time")
    rows = _filter_rows_by_mode(rows, mode)
    rows.sort(key=lambda r: r.get("close_time") or r.get("open_time") or "", reverse=True)
    return rows[:limit] if limit is not None else rows


def get_open_positions_all(mode: str | None = "all"):
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
    rows = _filter_rows_by_mode(rows, mode)
    rows.sort(key=lambda r: r.get("open_time") or "", reverse=True)
    return rows


@app.get("/api/stats")
def get_stats(mode: str = "all"):
    """Get aggregate trading statistics across paper+live closed trades."""
    resolved_mode = _normalize_mode(mode)
    rows = get_closed_trades(mode=resolved_mode)
    if not rows:
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "paper_total": 0,
            "live_total": 0,
            "mode": resolved_mode,
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
        "mode": resolved_mode,
    }


@app.get("/api/trades")
def get_trades(limit: int = 50, mode: str = "all"):
    """Get recent closed trade history across paper+live."""
    return get_closed_trades(limit=max(1, int(limit or 50)), mode=_normalize_mode(mode))


@app.get("/api/open")
def get_open(mode: str = "all"):
    """Get currently open positions across paper+live."""
    return get_open_positions_all(mode=_normalize_mode(mode))


@app.get("/api/pnl-curve")
def get_pnl_curve(mode: str = "all"):
    """Get cumulative PnL data for chart visualization."""
    rows = get_closed_trades(mode=_normalize_mode(mode))
    rows.sort(key=lambda r: r.get("close_time") or r.get("open_time") or "")

    cum_pnl = TOTAL_CAPITAL_USDT
    pnl_curve = []
    for r in rows:
        pnl_value = r.get("pnl") or 0
        trade_time = r.get("close_time") or r.get("open_time")
        cum_pnl += pnl_value
        pnl_curve.append({
            "date": trade_time,
            "capital": round(cum_pnl, 2)
        })
    return pnl_curve


@app.get("/api/quality")
def get_quality_metrics(mode: str = "all"):
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

    rows = get_closed_trades(mode=_normalize_mode(mode))
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
    counters = (snap or {}).get("counters") or {}
    derived = (snap or {}).get("derived") or {}

    api_error_rate_pct = float(derived.get("api_error_rate_pct") or 0.0)
    order_reject_rate_pct = float(derived.get("order_reject_rate_pct") or 0.0)
    desync_events = int(counters.get("live_state_desync_events") or 0)
    queue_drops = int(counters.get("signals_dropped_queue_full") or 0)

    health_score = 100.0
    health_score -= min(api_error_rate_pct * 1.5, 35.0)
    health_score -= min(order_reject_rate_pct * 0.8, 25.0)
    health_score -= min(desync_events * 4.0, 25.0)
    health_score -= min(queue_drops * 6.0, 15.0)
    health_score = round(max(0.0, min(100.0, health_score)), 1)

    return {
        "runtime": snap,
        "health": {
            "score": health_score,
            "api_error_rate_pct": round(api_error_rate_pct, 2),
            "order_reject_rate_pct": round(order_reject_rate_pct, 2),
            "live_state_desync_events": desync_events,
            "signals_dropped_queue_full": queue_drops,
        },
        "db": {
            "open_positions": len(open_rows),
            "open_positions_paper": paper_open,
            "open_positions_live": live_open,
            "closed_last_24h": recent_closed,
        },
    }


@app.get("/api/per-symbol")
def get_per_symbol_stats():
    """Get per-symbol performance breakdown: wins, losses, total PnL per symbol."""
    rows = []
    for tbl in ("paper_trades", "live_trades"):
        if table_exists(tbl):
            rows += query_db(f"SELECT symbol, pnl FROM {tbl} WHERE status='closed'")

    symbol_stats: dict = {}
    for r in rows:
        sym = r.get("symbol") or "UNKNOWN"
        pnl = float(r.get("pnl") or 0)
        if sym not in symbol_stats:
            symbol_stats[sym] = {"symbol": sym, "wins": 0, "losses": 0, "total_pnl": 0.0, "trades": 0}
        symbol_stats[sym]["trades"] += 1
        symbol_stats[sym]["total_pnl"] = round(symbol_stats[sym]["total_pnl"] + pnl, 2)
        if pnl > 0:
            symbol_stats[sym]["wins"] += 1
        else:
            symbol_stats[sym]["losses"] += 1

    for stats in symbol_stats.values():
        total = stats["trades"]
        stats["win_rate"] = round(stats["wins"] / total * 100, 1) if total > 0 else 0.0

    return sorted(symbol_stats.values(), key=lambda x: x["total_pnl"], reverse=True)


@app.get("/api/signal-quality")
def get_signal_quality():
    """Analyse score distribution of winning vs. losing trades."""
    rows = []
    for tbl in ("paper_trades", "live_trades"):
        if table_exists(tbl):
            rows += query_db(f"SELECT score, pnl FROM {tbl} WHERE status='closed' AND score IS NOT NULL")

    winning_scores = []
    losing_scores = []

    for r in rows:
        score = r.get("score")
        if score is None:
            continue
        try:
            score_val = float(score)
        except (ValueError, TypeError):
            continue
        pnl = float(r.get("pnl") or 0)
        if pnl > 0:
            winning_scores.append(score_val)
        else:
            losing_scores.append(score_val)

    def _bucket_scores(scores, buckets=None):
        if buckets is None:
            buckets = [(0, 60), (60, 70), (70, 80), (80, 90), (90, 101)]
        result = {}
        for lo, hi in buckets:
            label = f"{lo}-{hi-1}"
            result[label] = sum(1 for s in scores if lo <= s < hi)
        return result

    return {
        "total_with_score": len(winning_scores) + len(losing_scores),
        "avg_winning_score": round(sum(winning_scores) / len(winning_scores), 1) if winning_scores else 0,
        "avg_losing_score": round(sum(losing_scores) / len(losing_scores), 1) if losing_scores else 0,
        "winning_distribution": _bucket_scores(winning_scores),
        "losing_distribution": _bucket_scores(losing_scores),
    }


@app.get("/api/weight-feedback")
def get_weight_feedback():
    """Return latest auto-adjusted weight feedback report for dashboard visibility."""
    try:
        tracker = PerformanceTracker()
        report = tracker.get_latest_auto_adjust_report()
        if not isinstance(report, dict):
            report = {}
        report.setdefault("enabled", bool(AUTO_WEIGHT_FEEDBACK_ENABLED))
        report.setdefault("has_report", False)
        return report
    except Exception as err:
        return {
            "enabled": bool(AUTO_WEIGHT_FEEDBACK_ENABLED),
            "has_report": False,
            "error": str(err),
        }

