import sqlite3


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    return column_name in cols


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _get_component_version(conn: sqlite3.Connection, component: str) -> int:
    row = conn.execute(
        "SELECT version FROM schema_migrations WHERE component = ?",
        (component,),
    ).fetchone()
    return int(row[0]) if row else 0


def _set_component_version(conn: sqlite3.Connection, component: str, version: int) -> None:
    conn.execute(
        """
        INSERT INTO schema_migrations(component, version)
        VALUES(?, ?)
        ON CONFLICT(component) DO UPDATE SET
            version=excluded.version,
            updated_at=CURRENT_TIMESTAMP
        """,
        (component, int(version)),
    )


def apply_live_trades_migrations(conn: sqlite3.Connection) -> int:
    """Apply deterministic, versioned live_trades schema migrations."""
    component = "live_trades"
    _ensure_migrations_table(conn)
    current_version = _get_component_version(conn, component)

    if current_version < 1:
        if not _column_exists(conn, "live_trades", "stop_loss"):
            conn.execute("ALTER TABLE live_trades ADD COLUMN stop_loss REAL")
        if not _column_exists(conn, "live_trades", "tp1"):
            conn.execute("ALTER TABLE live_trades ADD COLUMN tp1 REAL")
        if not _column_exists(conn, "live_trades", "tp2"):
            conn.execute("ALTER TABLE live_trades ADD COLUMN tp2 REAL")
        _set_component_version(conn, component, 1)
        current_version = 1

    if current_version < 2:
        if not _column_exists(conn, "live_trades", "tp1_hit"):
            conn.execute("ALTER TABLE live_trades ADD COLUMN tp1_hit INTEGER DEFAULT 0")
        _set_component_version(conn, component, 2)
        current_version = 2

    if current_version < 3:
        if not _column_exists(conn, "live_trades", "entry_filled"):
            conn.execute("ALTER TABLE live_trades ADD COLUMN entry_filled INTEGER DEFAULT 0")
        _set_component_version(conn, component, 3)
        current_version = 3

    if current_version < 4:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_trades_status_symbol ON live_trades(status, symbol)"
        )
        _set_component_version(conn, component, 4)
        current_version = 4

    if current_version < 5:
        if not _column_exists(conn, "live_trades", "sync_required"):
            conn.execute("ALTER TABLE live_trades ADD COLUMN sync_required INTEGER DEFAULT 0")
        _set_component_version(conn, component, 5)
        current_version = 5

    if current_version < 6:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_live_trades_order_id_status ON live_trades(order_id, status)"
        )
        _set_component_version(conn, component, 6)
        current_version = 6

    return current_version


def apply_paper_trades_migrations(conn: sqlite3.Connection) -> int:
    """Apply deterministic, versioned paper_trades schema migrations."""
    component = "paper_trades"
    _ensure_migrations_table(conn)
    current_version = _get_component_version(conn, component)

    if current_version < 1:
        if not _column_exists(conn, "paper_trades", "score"):
            conn.execute("ALTER TABLE paper_trades ADD COLUMN score INTEGER")
        if not _column_exists(conn, "paper_trades", "reason"):
            conn.execute("ALTER TABLE paper_trades ADD COLUMN reason TEXT")
        _set_component_version(conn, component, 1)
        current_version = 1

    if current_version < 2:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_trades_status_symbol ON paper_trades(status, symbol)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_paper_trades_entry_time ON paper_trades(entry_time)"
        )
        _set_component_version(conn, component, 2)
        current_version = 2

    if current_version < 3:
        if not _column_exists(conn, "paper_trades", "tp1_hit"):
            conn.execute("ALTER TABLE paper_trades ADD COLUMN tp1_hit INTEGER DEFAULT 0")
        _set_component_version(conn, component, 3)
        current_version = 3

    if current_version < 4:
        if not _column_exists(conn, "paper_trades", "realized_pnl"):
            conn.execute("ALTER TABLE paper_trades ADD COLUMN realized_pnl REAL DEFAULT 0")
        _set_component_version(conn, component, 4)
        current_version = 4

    return current_version
