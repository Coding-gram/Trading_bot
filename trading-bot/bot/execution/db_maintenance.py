import logging
import sqlite3
from datetime import datetime, timedelta, timezone


logger = logging.getLogger(__name__)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def prune_old_trades(db_path: str, retention_days: int = 180) -> dict:
    """Delete closed trades older than retention_days and vacuum DB if rows were deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(retention_days, 1))
    cutoff_iso = cutoff.isoformat()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    cur = conn.cursor()

    paper_deleted = 0
    live_deleted = 0

    if _table_exists(conn, "paper_trades"):
        cur.execute(
            """
            DELETE FROM paper_trades
            WHERE status LIKE 'closed%'
              AND COALESCE(exit_time, entry_time) < ?
            """,
            (cutoff_iso,),
        )
        paper_deleted = cur.rowcount if cur.rowcount is not None else 0

    if _table_exists(conn, "live_trades"):
        cur.execute(
            """
            DELETE FROM live_trades
            WHERE status != 'open'
              AND COALESCE(close_time, open_time) < ?
            """,
            (cutoff_iso,),
        )
        live_deleted = cur.rowcount if cur.rowcount is not None else 0

    deleted_total = max(paper_deleted, 0) + max(live_deleted, 0)
    conn.commit()

    if deleted_total > 0:
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
            conn.execute("VACUUM;")
            conn.commit()
        except Exception as vacuum_err:
            logger.warning("VACUUM skipped/failed after cleanup: %s", vacuum_err)

    conn.close()

    return {
        "cutoff": cutoff_iso,
        "paper_deleted": max(paper_deleted, 0),
        "live_deleted": max(live_deleted, 0),
        "deleted_total": deleted_total,
    }
