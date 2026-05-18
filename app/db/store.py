"""
SQLite persistence layer.

Stores all trading events for historical analysis:
- Signals
- Orders
- Positions (open + close)
- Reconnect events
- Errors

Subscribes to EventBus events and writes them to SQLite.
All writes are synchronous (SQLite is fast enough for this use case).
"""

import sqlite3
from pathlib import Path

from app.core.events import EventBus
from app.core.models import PaperOrder, Position, Signal
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Default database path
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "trades.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms REAL NOT NULL,
    signal_type TEXT NOT NULL,
    exchange TEXT NOT NULL,
    segment TEXT NOT NULL,
    exchange_token TEXT NOT NULL,
    price REAL NOT NULL,
    strategy_name TEXT NOT NULL,
    reason TEXT,
    metadata TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT UNIQUE NOT NULL,
    side TEXT NOT NULL,
    exchange TEXT NOT NULL,
    segment TEXT NOT NULL,
    exchange_token TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    timestamp_ms REAL NOT NULL,
    strategy_name TEXT NOT NULL,
    signal_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT UNIQUE NOT NULL,
    exchange TEXT NOT NULL,
    segment TEXT NOT NULL,
    exchange_token TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    entry_time_ms REAL NOT NULL,
    exit_price REAL,
    exit_time_ms REAL,
    pnl REAL,
    pnl_pct REAL,
    status TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS reconnects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt INTEGER NOT NULL,
    backoff_s REAL NOT NULL,
    timestamp_ms REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_token ON signals(exchange_token);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_name);
CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(exchange_token);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy_name);
"""


class TradeStore:
    """
    SQLite-backed event store.

    Subscribes to EventBus events and persists them.
    Provides query methods for analytics.

    Usage:
        store = TradeStore(event_bus)
        store.start()
    """

    def __init__(self, event_bus: EventBus, db_path: Path | None = None) -> None:
        self._event_bus = event_bus
        self._db_path = db_path or DEFAULT_DB_PATH
        self._conn: sqlite3.Connection | None = None

    def start(self) -> None:
        """Initialize database and subscribe to events."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        # Subscribe to events
        self._event_bus.subscribe("signal", self._on_signal)
        self._event_bus.subscribe("order", self._on_order)
        self._event_bus.subscribe("position_open", self._on_position_open)
        self._event_bus.subscribe("position_close", self._on_position_close)
        self._event_bus.subscribe("reconnect", self._on_reconnect)
        self._event_bus.subscribe("error", self._on_error)

        logger.info("TradeStore started (db=%s)", self._db_path)

    def stop(self) -> None:
        """Unsubscribe and close database."""
        self._event_bus.unsubscribe("signal", self._on_signal)
        self._event_bus.unsubscribe("order", self._on_order)
        self._event_bus.unsubscribe("position_open", self._on_position_open)
        self._event_bus.unsubscribe("position_close", self._on_position_close)
        self._event_bus.unsubscribe("reconnect", self._on_reconnect)
        self._event_bus.unsubscribe("error", self._on_error)

        if self._conn:
            self._conn.close()
            self._conn = None
        logger.info("TradeStore stopped")

    # ─── Event Handlers ──────────────────────────────────────────

    def _on_signal(self, signal: Signal) -> None:
        """Persist a trading signal."""
        import json

        self._execute(
            """INSERT INTO signals (timestamp_ms, signal_type, exchange, segment,
               exchange_token, price, strategy_name, reason, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal.timestamp_ms,
                signal.signal_type.value,
                signal.exchange,
                signal.segment,
                signal.exchange_token,
                signal.price,
                signal.strategy_name,
                signal.reason,
                json.dumps(signal.metadata) if signal.metadata else None,
            ),
        )

    def _on_order(self, order: PaperOrder) -> None:
        """Persist a paper order."""
        self._execute(
            """INSERT INTO orders (order_id, side, exchange, segment, exchange_token,
               quantity, price, timestamp_ms, strategy_name, signal_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order.order_id,
                order.side.value,
                order.exchange,
                order.segment,
                order.exchange_token,
                order.quantity,
                order.price,
                order.timestamp_ms,
                order.strategy_name,
                order.signal_reason,
            ),
        )

    def _on_position_open(self, position: Position) -> None:
        """Persist a newly opened position."""
        self._execute(
            """INSERT INTO positions (position_id, exchange, segment, exchange_token,
               side, quantity, entry_price, entry_time_ms, status, strategy_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position.position_id,
                position.exchange,
                position.segment,
                position.exchange_token,
                position.side.value,
                position.quantity,
                position.entry_price,
                position.entry_time_ms,
                position.status.value,
                position.strategy_name,
            ),
        )

    def _on_position_close(self, position: Position) -> None:
        """Update a closed position."""
        self._execute(
            """UPDATE positions SET exit_price=?, exit_time_ms=?, pnl=?, pnl_pct=?,
               status=?, closed_at=datetime('now')
               WHERE position_id=?""",
            (
                position.exit_price,
                position.exit_time_ms,
                position.pnl,
                position.pnl_pct,
                position.status.value,
                position.position_id,
            ),
        )

    def _on_reconnect(self, info: dict) -> None:
        """Persist a reconnection event."""
        self._execute(
            "INSERT INTO reconnects (attempt, backoff_s, timestamp_ms) VALUES (?, ?, ?)",
            (info["attempt"], info["backoff_s"], info["timestamp"]),
        )

    def _on_error(self, message: str) -> None:
        """Persist an error event."""
        self._execute("INSERT INTO errors (message) VALUES (?)", (str(message),))

    # ─── Query Methods (for analytics) ───────────────────────────

    def get_closed_positions(
        self, strategy_name: str | None = None, limit: int = 1000
    ) -> list[dict]:
        """Fetch closed positions, optionally filtered by strategy."""
        if strategy_name:
            rows = self._query(
                """SELECT * FROM positions WHERE status='CLOSED' AND strategy_name=?
                   ORDER BY closed_at DESC LIMIT ?""",
                (strategy_name, limit),
            )
        else:
            rows = self._query(
                "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            )
        return rows

    def get_signals(self, limit: int = 500) -> list[dict]:
        """Fetch recent signals."""
        return self._query(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def get_all_positions(self) -> list[dict]:
        """Fetch all positions."""
        return self._query("SELECT * FROM positions ORDER BY created_at DESC")

    def get_reconnects(self, limit: int = 100) -> list[dict]:
        """Fetch reconnection events."""
        return self._query(
            "SELECT * FROM reconnects ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    # ─── Internal ────────────────────────────────────────────────

    def _execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a write query with error handling."""
        if not self._conn:
            return
        try:
            self._conn.execute(sql, params)
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("DB write error: %s (sql=%s)", e, sql[:80])

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a read query and return results as dicts."""
        if not self._conn:
            return []
        try:
            self._conn.row_factory = sqlite3.Row
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("DB read error: %s", e)
            return []
