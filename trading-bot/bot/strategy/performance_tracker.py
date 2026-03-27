"""
Performance Feedback Loop — tracks which scoring sub-components predicted
winning vs losing trades and suggests weight adjustments.

Usage:
    tracker = PerformanceTracker()
    tracker.record_entry(trade_id, signal_details)   # at open
    tracker.record_outcome(trade_id, pnl)            # at close
    report = tracker.get_indicator_report()           # anytime
    suggestions = tracker.suggest_weight_adjustments()
"""
import json
import logging
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone
from bot.config import (
    DB_PATH,
    AUTO_WEIGHT_FEEDBACK_ENABLED,
    AUTO_WEIGHT_MIN_CLOSED_TRADES,
    AUTO_WEIGHT_RECALIBRATE_EVERY,
    AUTO_WEIGHT_MIN_INDICATOR_TRADES,
    AUTO_WEIGHT_MIN_EDGE_PCT,
    AUTO_WEIGHT_LEARNING_RATE,
    AUTO_WEIGHT_MAX_STEP_PCT,
    AUTO_WEIGHT_MIN_WEIGHT,
    AUTO_WEIGHT_MAX_WEIGHT,
    AUTO_WEIGHT_REQUIRE_TELEGRAM_APPROVAL,
)

logger = logging.getLogger(__name__)

_TRACKED_INDICATORS = [
    "trend", "macd", "rsi", "stochastic", "candle_pattern",
    "chart_pattern", "volume", "adx", "obv", "vwap",
    "sr", "4h_confluence", "4h_sr", "daily_alignment",
    "ranging_mean_reversion",
]

_TRACKED_PENALTIES = [
    "penalty_rsi_div", "penalty_contra_sr",
    "penalty_vol_decline", "penalty_htf", "penalty_daily_htf",
]

_INDICATOR_TO_WEIGHT_KEY = {
    "trend": "trend_alignment",
    "macd": "macd_signal",
    "rsi": "rsi_zone",
    "stochastic": "stochastic",
    "candle_pattern": "candlestick_pattern",
    "chart_pattern": "chart_pattern",
    "volume": "volume_confirm",
    "adx": "adx_strength",
    "obv": "obv_confirm",
    "vwap": "vwap_bias",
    "sr": "sr_proximity",
    "4h_confluence": "confluence_4h_bonus",
    "4h_sr": "confluence_4h_sr",
    "daily_alignment": "daily_trend_bonus",
    "ranging_mean_reversion": "ranging_mean_reversion",
}

_WEIGHT_KEY_ALIASES = {
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
    "ranging_mean_reversion": ["mean_reversion"],
}


class PerformanceTracker:
    """Tracks per-indicator win rates and suggests weight adjustments.

    Records which scoring sub-components were active at trade entry,
    then correlates them with trade outcomes (win/loss) to compute
    per-indicator win rates.  The ``suggest_weight_adjustments`` method
    compares each indicator's win-rate against the portfolio average
    and recommends increasing or decreasing its weight.

    All data is persisted in the same SQLite database used by the bot.
    """

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or DB_PATH
        self._init_db()

    def _db_connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_indicator_snapshots (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT,
                    direction TEXT,
                    score INTEGER,
                    active_indicators TEXT,
                    active_penalties TEXT,
                    details_json TEXT,
                    entry_time TEXT,
                    pnl REAL DEFAULT NULL,
                    outcome TEXT DEFAULT NULL,
                    close_time TEXT DEFAULT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS performance_tracker_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                )
            """)

    def record_entry(self, trade_id: str, signal: dict) -> None:
        """Record which indicators were active when a trade was opened.

        Args:
            trade_id: Unique identifier for the trade.
            signal: The full signal dict from ``score_signal()``, which
                    must contain ``details``, ``direction``, ``score``,
                    and ``symbol`` keys.
        """
        details = signal.get("details", {})
        active = [k for k in _TRACKED_INDICATORS if k in details]
        penalties = [k for k in _TRACKED_PENALTIES if k in details]

        try:
            with self._db_connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO trade_indicator_snapshots
                    (trade_id, symbol, direction, score, active_indicators,
                     active_penalties, details_json, entry_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    trade_id,
                    signal.get("symbol", ""),
                    signal.get("direction", ""),
                    int(signal.get("score", 0)),
                    json.dumps(active),
                    json.dumps(penalties),
                    json.dumps(details, default=str),
                    datetime.now(timezone.utc).isoformat(),
                ))
        except Exception as e:
            logger.error("PerformanceTracker.record_entry failed: %s", e)

    def record_outcome(self, trade_id: str, pnl: float) -> None:
        """Record the PnL outcome when a trade is closed.

        Args:
            trade_id: Must match a previously recorded entry.
            pnl: Realized profit/loss for the trade.
        """
        outcome = "win" if pnl > 0 else "loss"
        try:
            with self._db_connect() as conn:
                conn.execute("""
                    UPDATE trade_indicator_snapshots
                    SET pnl = ?, outcome = ?, close_time = ?
                    WHERE trade_id = ?
                """, (pnl, outcome, datetime.now(timezone.utc).isoformat(), trade_id))
            self._maybe_auto_adjust_weights()
        except Exception as e:
            logger.error("PerformanceTracker.record_outcome failed: %s", e)

    def _get_meta_int(self, key: str, default: int = 0) -> int:
        with self._db_connect() as conn:
            row = conn.execute(
                "SELECT value FROM performance_tracker_meta WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return int(default)
        try:
            return int(str(row["value"]).strip())
        except Exception:
            return int(default)

    def _get_meta_text(self, key: str, default: str = "") -> str:
        with self._db_connect() as conn:
            row = conn.execute(
                "SELECT value FROM performance_tracker_meta WHERE key = ?",
                (key,),
            ).fetchone()
        if not row:
            return str(default)
        value = row["value"]
        if value is None:
            return str(default)
        return str(value)

    def _set_meta_value(self, key: str, value: str) -> None:
        with self._db_connect() as conn:
            conn.execute(
                """
                INSERT INTO performance_tracker_meta (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (key, str(value), datetime.now(timezone.utc).isoformat()),
            )

    def _closed_trades_count(self) -> int:
        with self._db_connect() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM trade_indicator_snapshots WHERE outcome IS NOT NULL"
                ).fetchone()[0]
            )

    def _send_tg_approval_request(self, request_id: str, closed_trades: int, preview: list[dict]) -> bool:
        from bot.notifications.telegram import send_weight_tuning_approval_request

        return bool(send_weight_tuning_approval_request(request_id, closed_trades, preview))

    def _poll_tg_approval_decision(self, request_id: str) -> str | None:
        from bot.notifications.telegram import poll_weight_tuning_decision

        last_update_id = self._get_meta_int("telegram_last_update_id", default=0)
        result = poll_weight_tuning_decision(last_update_id=last_update_id, request_id=request_id)
        if not isinstance(result, dict):
            return None

        next_update_id = int(result.get("last_update_id") or 0)
        if next_update_id > last_update_id:
            self._set_meta_value("telegram_last_update_id", str(next_update_id))

        decision = str(result.get("decision") or "").strip().lower()
        if decision in {"approve", "reject"}:
            return decision
        return None

    def _clear_pending_approval(self) -> None:
        for key in (
            "pending_auto_adjust_request_id",
            "pending_auto_adjust_closed_count",
            "pending_auto_adjust_created_at",
            "pending_auto_adjust_preview_json",
        ):
            self._set_meta_value(key, "")

    def _finalize_auto_adjust_report(
        self,
        *,
        closed_trades: int,
        adjustments: list[dict],
        approval_required: bool,
        approval_status: str,
        request_id: str | None,
    ) -> None:
        report_payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "closed_trades": int(closed_trades),
            "changed_weights": len(adjustments),
            "adjustments": adjustments,
            "min_indicator_trades": int(AUTO_WEIGHT_MIN_INDICATOR_TRADES),
            "min_edge_pct": float(AUTO_WEIGHT_MIN_EDGE_PCT),
            "learning_rate": float(AUTO_WEIGHT_LEARNING_RATE),
            "max_step_pct": float(AUTO_WEIGHT_MAX_STEP_PCT),
            "min_weight": int(AUTO_WEIGHT_MIN_WEIGHT),
            "max_weight": int(AUTO_WEIGHT_MAX_WEIGHT),
            "approval_required": bool(approval_required),
            "approval_status": str(approval_status),
            "request_id": str(request_id or ""),
        }
        self._set_meta_value("latest_auto_adjust_report", json.dumps(report_payload))

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    @staticmethod
    def _resolve_weight_key_for_file(weights_data: dict, canonical_key: str) -> str:
        if canonical_key in weights_data:
            return canonical_key
        for alias in _WEIGHT_KEY_ALIASES.get(canonical_key, []):
            if alias in weights_data:
                return alias
        return canonical_key

    def _maybe_auto_adjust_weights(self) -> None:
        if not AUTO_WEIGHT_FEEDBACK_ENABLED:
            return

        pending_request_id = self._get_meta_text("pending_auto_adjust_request_id", default="").strip()
        if pending_request_id:
            decision = self._poll_tg_approval_decision(pending_request_id)
            if decision == "approve":
                pending_closed_count = self._get_meta_int("pending_auto_adjust_closed_count", default=0)
                adjustments = self.apply_weight_feedback(
                    min_indicator_trades=AUTO_WEIGHT_MIN_INDICATOR_TRADES,
                    min_edge_pct=AUTO_WEIGHT_MIN_EDGE_PCT,
                    learning_rate=AUTO_WEIGHT_LEARNING_RATE,
                    max_step_pct=AUTO_WEIGHT_MAX_STEP_PCT,
                    min_weight=AUTO_WEIGHT_MIN_WEIGHT,
                    max_weight=AUTO_WEIGHT_MAX_WEIGHT,
                    persist=True,
                )
                self._finalize_auto_adjust_report(
                    closed_trades=max(self._closed_trades_count(), pending_closed_count),
                    adjustments=adjustments,
                    approval_required=True,
                    approval_status="approved",
                    request_id=pending_request_id,
                )
                self._set_meta_value(
                    "last_auto_adjust_closed_count",
                    str(max(self._closed_trades_count(), pending_closed_count)),
                )
                self._clear_pending_approval()
                if adjustments:
                    logger.info(
                        "Approved auto-adjust applied (%d changes) for request %s.",
                        len(adjustments),
                        pending_request_id,
                    )
            elif decision == "reject":
                pending_closed_count = self._get_meta_int("pending_auto_adjust_closed_count", default=0)
                preview_json = self._get_meta_text("pending_auto_adjust_preview_json", default="")
                preview = []
                if preview_json:
                    try:
                        parsed = json.loads(preview_json)
                        if isinstance(parsed, list):
                            preview = parsed
                    except Exception:
                        preview = []
                self._finalize_auto_adjust_report(
                    closed_trades=max(self._closed_trades_count(), pending_closed_count),
                    adjustments=preview,
                    approval_required=True,
                    approval_status="rejected",
                    request_id=pending_request_id,
                )
                self._set_meta_value(
                    "last_auto_adjust_closed_count",
                    str(max(self._closed_trades_count(), pending_closed_count)),
                )
                self._clear_pending_approval()
                logger.info("Auto-adjust request %s was rejected via Telegram.", pending_request_id)
            return

        closed_trades = self._closed_trades_count()
        if closed_trades < max(AUTO_WEIGHT_MIN_CLOSED_TRADES, 1):
            return

        cadence = max(AUTO_WEIGHT_RECALIBRATE_EVERY, 1)
        if closed_trades % cadence != 0:
            return

        last_applied = self._get_meta_int("last_auto_adjust_closed_count", default=0)
        if closed_trades <= last_applied:
            return

        if AUTO_WEIGHT_REQUIRE_TELEGRAM_APPROVAL:
            preview = self.apply_weight_feedback(
                min_indicator_trades=AUTO_WEIGHT_MIN_INDICATOR_TRADES,
                min_edge_pct=AUTO_WEIGHT_MIN_EDGE_PCT,
                learning_rate=AUTO_WEIGHT_LEARNING_RATE,
                max_step_pct=AUTO_WEIGHT_MAX_STEP_PCT,
                min_weight=AUTO_WEIGHT_MIN_WEIGHT,
                max_weight=AUTO_WEIGHT_MAX_WEIGHT,
                persist=False,
            )

            request_id = f"adj-{closed_trades}-{int(time.time())}"
            sent = self._send_tg_approval_request(request_id, closed_trades, preview)
            if sent:
                self._set_meta_value("pending_auto_adjust_request_id", request_id)
                self._set_meta_value("pending_auto_adjust_closed_count", str(closed_trades))
                self._set_meta_value("pending_auto_adjust_created_at", datetime.now(timezone.utc).isoformat())
                self._set_meta_value("pending_auto_adjust_preview_json", json.dumps(preview))
                self._finalize_auto_adjust_report(
                    closed_trades=closed_trades,
                    adjustments=preview,
                    approval_required=True,
                    approval_status="pending",
                    request_id=request_id,
                )
                logger.info(
                    "Sent Telegram approval request for auto-adjust (%s, closed_trades=%d).",
                    request_id,
                    closed_trades,
                )
            else:
                logger.warning(
                    "Auto-adjust approval requested but Telegram send failed; waiting for next cadence."
                )
            return

        adjustments = self.apply_weight_feedback(
            min_indicator_trades=AUTO_WEIGHT_MIN_INDICATOR_TRADES,
            min_edge_pct=AUTO_WEIGHT_MIN_EDGE_PCT,
            learning_rate=AUTO_WEIGHT_LEARNING_RATE,
            max_step_pct=AUTO_WEIGHT_MAX_STEP_PCT,
            min_weight=AUTO_WEIGHT_MIN_WEIGHT,
            max_weight=AUTO_WEIGHT_MAX_WEIGHT,
            persist=True,
        )
        self._finalize_auto_adjust_report(
            closed_trades=closed_trades,
            adjustments=adjustments,
            approval_required=False,
            approval_status="not_required",
            request_id=None,
        )
        self._set_meta_value("last_auto_adjust_closed_count", str(closed_trades))

        if adjustments:
            logger.info(
                "Auto-adjusted scorer weights from closed trade feedback (%d changes at %d closed trades).",
                len(adjustments),
                closed_trades,
            )

    def get_latest_auto_adjust_report(self) -> dict:
        """Return the latest persisted automatic weight adjustment report."""
        with self._db_connect() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM performance_tracker_meta WHERE key = ?",
                ("latest_auto_adjust_report",),
            ).fetchone()

        if not row:
            return {
                "enabled": bool(AUTO_WEIGHT_FEEDBACK_ENABLED),
                "has_report": False,
                "last_auto_adjust_closed_count": self._get_meta_int("last_auto_adjust_closed_count", default=0),
            }

        raw_value = row["value"]
        parsed = None
        if raw_value not in (None, ""):
            try:
                parsed = json.loads(raw_value)
            except Exception:
                parsed = None

        if not isinstance(parsed, dict):
            parsed = {}

        parsed["enabled"] = bool(AUTO_WEIGHT_FEEDBACK_ENABLED)
        parsed["has_report"] = True
        parsed["meta_updated_at"] = row["updated_at"]
        parsed["last_auto_adjust_closed_count"] = self._get_meta_int("last_auto_adjust_closed_count", default=0)
        return parsed

    def apply_weight_feedback(
        self,
        min_indicator_trades: int = AUTO_WEIGHT_MIN_INDICATOR_TRADES,
        min_edge_pct: float = AUTO_WEIGHT_MIN_EDGE_PCT,
        learning_rate: float = AUTO_WEIGHT_LEARNING_RATE,
        max_step_pct: float = AUTO_WEIGHT_MAX_STEP_PCT,
        min_weight: int = AUTO_WEIGHT_MIN_WEIGHT,
        max_weight: int = AUTO_WEIGHT_MAX_WEIGHT,
        persist: bool = True,
    ) -> list[dict]:
        """Apply outcome-driven weight updates based on per-indicator edge.

        Each indicator's win-rate is compared against the portfolio baseline
        win-rate from closed trades. Positive edge increases the associated
        scorer weight; negative edge reduces it.
        """
        report = self.get_indicator_report(min_trades=max(min_indicator_trades, 1))
        if not report:
            return []

        with self._db_connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                    COUNT(*) AS total
                FROM trade_indicator_snapshots
                WHERE outcome IS NOT NULL
                """
            ).fetchone()

        total = int((row["total"] or 0) if row is not None else 0)
        wins = int((row["wins"] or 0) if row is not None else 0)
        if total <= 0:
            return []

        baseline_win_rate = (wins / total) * 100.0

        from bot.strategy import scorer

        weights_on_disk = None
        if persist:
            try:
                with open(scorer._WEIGHTS_PATH, encoding="utf-8") as f:
                    weights_on_disk = json.load(f)
            except Exception as read_err:
                logger.warning("Could not read weights file for auto-adjust: %s", read_err)
                persist = False

        adjustments: list[dict] = []
        for indicator, stats in sorted(report.items(), key=lambda item: item[0]):
            canonical_key = _INDICATOR_TO_WEIGHT_KEY.get(indicator)
            if not canonical_key:
                continue

            old_weight = scorer.WEIGHTS.get(canonical_key)
            if not isinstance(old_weight, (int, float)):
                continue
            if old_weight <= 0:
                continue

            win_rate = float(stats.get("win_rate", 0.0))
            edge = win_rate - baseline_win_rate
            if abs(edge) < float(min_edge_pct):
                continue

            raw_step = (edge / 100.0) * float(learning_rate)
            step = self._clamp(raw_step, -abs(float(max_step_pct)), abs(float(max_step_pct)))

            new_weight = int(round(float(old_weight) * (1.0 + step)))
            new_weight = int(self._clamp(new_weight, int(min_weight), int(max_weight)))

            if new_weight == int(old_weight):
                continue

            scorer.WEIGHTS[canonical_key] = new_weight

            if persist and isinstance(weights_on_disk, dict):
                key_on_disk = self._resolve_weight_key_for_file(weights_on_disk, canonical_key)
                weights_on_disk[key_on_disk] = new_weight

            adjustments.append({
                "indicator": indicator,
                "weight_key": canonical_key,
                "old_weight": int(old_weight),
                "new_weight": new_weight,
                "edge": round(edge, 2),
                "win_rate": round(win_rate, 1),
                "baseline_win_rate": round(baseline_win_rate, 1),
                "trades": int(stats.get("total", 0)),
            })

        if persist and adjustments and isinstance(weights_on_disk, dict):
            try:
                with open(scorer._WEIGHTS_PATH, "w", encoding="utf-8") as f:
                    json.dump(weights_on_disk, f, indent=4)
            except Exception as write_err:
                logger.error("Failed to persist auto-adjusted weights: %s", write_err)

        return adjustments

    def get_indicator_report(self, min_trades: int = 5) -> dict:
        """Compute per-indicator win rates from closed trades.

        Args:
            min_trades: Minimum number of trades an indicator must appear
                        in before its stats are reported.

        Returns:
            Dict mapping indicator name to ``{wins, losses, total, win_rate}``.
        """
        with self._db_connect() as conn:
            rows = conn.execute(
                "SELECT active_indicators, outcome FROM trade_indicator_snapshots WHERE outcome IS NOT NULL"
            ).fetchall()

        stats: dict[str, dict] = {}
        for row in rows:
            indicators = json.loads(row["active_indicators"])
            outcome = row["outcome"]
            for ind in indicators:
                if ind not in stats:
                    stats[ind] = {"wins": 0, "losses": 0, "total": 0}
                stats[ind]["total"] += 1
                if outcome == "win":
                    stats[ind]["wins"] += 1
                else:
                    stats[ind]["losses"] += 1

        # Compute win rates and filter by minimum trades
        report = {}
        for ind, s in stats.items():
            if s["total"] >= min_trades:
                s["win_rate"] = round(s["wins"] / s["total"] * 100, 1)
                report[ind] = s

        return report

    def suggest_weight_adjustments(self, min_trades: int = 10) -> list[dict]:
        """Suggest weight changes based on indicator performance vs average.

        Compares each indicator's win rate against the portfolio-wide average
        and recommends scaling weights up or down accordingly.

        Args:
            min_trades: Minimum trades before an indicator is eligible.

        Returns:
            List of dicts: ``{indicator, win_rate, avg_win_rate, suggestion, factor}``
            sorted by deviation from average (worst performers first).
        """
        report = self.get_indicator_report(min_trades=min_trades)
        if not report:
            return []

        # Portfolio-wide average win rate
        total_wins = sum(s["wins"] for s in report.values())
        total_trades = sum(s["total"] for s in report.values())
        avg_wr = total_wins / total_trades * 100 if total_trades > 0 else 50.0

        suggestions = []
        for ind, s in report.items():
            wr = s["win_rate"]
            deviation = wr - avg_wr

            if deviation > 10:
                suggestion = "INCREASE weight"
                factor = round(1 + (deviation / 100), 2)
            elif deviation < -10:
                suggestion = "DECREASE weight"
                factor = round(1 - (abs(deviation) / 100), 2)
            else:
                suggestion = "keep"
                factor = 1.0

            suggestions.append({
                "indicator": ind,
                "win_rate": wr,
                "avg_win_rate": round(avg_wr, 1),
                "trades": s["total"],
                "suggestion": suggestion,
                "factor": factor,
            })

        # Sort: worst performers first
        suggestions.sort(key=lambda x: x["win_rate"])
        return suggestions

    def get_summary(self) -> dict:
        """Quick summary of tracked trade count and outcomes.

        Returns:
            Dict with ``total``, ``with_outcome``, ``wins``, ``losses`` counts.
        """
        with self._db_connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM trade_indicator_snapshots").fetchone()[0]
            with_outcome = conn.execute(
                "SELECT COUNT(*) FROM trade_indicator_snapshots WHERE outcome IS NOT NULL"
            ).fetchone()[0]
            wins = conn.execute(
                "SELECT COUNT(*) FROM trade_indicator_snapshots WHERE outcome = 'win'"
            ).fetchone()[0]
            losses = conn.execute(
                "SELECT COUNT(*) FROM trade_indicator_snapshots WHERE outcome = 'loss'"
            ).fetchone()[0]
        return {"total": total, "with_outcome": with_outcome, "wins": wins, "losses": losses}
