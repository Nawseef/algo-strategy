"""
Research database — dual-mode persistence for the 150K variant engine.

Production (VM): PostgreSQL via psycopg2
Local / Tests:   SQLite fallback (zero config)

Switch via environment variable:
    DATABASE_URL=postgresql://user:pass@localhost:5432/research  → PostgreSQL
    DATABASE_URL=sqlite:///data/research.db                     → SQLite
    (unset)                                                     → SQLite default

Tables (9 total):
    PERMANENT:
    1. trades              — One row per triggered entry (~750/day)
    2. exit_results        — 57 exit model PnLs per trade (1:1 with trades)
    3. variant_scores      — Scoring results per variant per period
    4. variant_definitions — Maps variant_id to full setup (JSON filters)
    5. variant_regime_scores — Regime breakdown per variant per period
    6. historical_candles  — 5m candle data for backtesting (multi-year)
    7. backtest_runs       — Metadata about each backtest execution
    8. fetch_progress      — Tracks which dates have been fetched per instrument

    TEMPORARY:
    9. candle_cache        — Today's live candles for exit sim (auto-cleanup)
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from app.utils.logger import get_logger
from app.variants.models import TradeRecord

logger = get_logger(__name__)

# ─── Detect database mode ────────────────────────────────────────────────────

# Ensure .env is loaded before reading DATABASE_URL
# (only if DATABASE_URL isn't already set in environment)
if not os.getenv("DATABASE_URL"):
    from pathlib import Path as _Path
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).parent.parent.parent / ".env")

_DATABASE_URL = os.getenv("DATABASE_URL", "")
_USE_POSTGRES = _DATABASE_URL.startswith("postgresql://") or _DATABASE_URL.startswith("postgres://")

DEFAULT_SQLITE_PATH = Path(__file__).parent.parent.parent / "data" / "research.db"

# ─── Schema: PostgreSQL ──────────────────────────────────────────────────────

POSTGRES_SCHEMA = """
-- ═══════════════════════════════════════════════════════════
-- TRADES TABLE — One row per triggered entry
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    trade_id TEXT UNIQUE NOT NULL,
    variant_id TEXT NOT NULL,

    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    instrument TEXT NOT NULL,

    direction TEXT NOT NULL,
    entry_time_ms BIGINT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,

    atr_entry DOUBLE PRECISION DEFAULT 0,
    adx_entry DOUBLE PRECISION DEFAULT 0,
    rsi_entry DOUBLE PRECISION DEFAULT 0,
    vix_entry DOUBLE PRECISION DEFAULT 0,
    volume_ratio_entry DOUBLE PRECISION DEFAULT 0,
    vwap_entry DOUBLE PRECISION DEFAULT 0,

    gap_size DOUBLE PRECISION DEFAULT 0,
    gap_direction TEXT DEFAULT '',
    session TEXT DEFAULT '',
    day_of_week TEXT DEFAULT '',
    month TEXT DEFAULT '',
    market_structure TEXT DEFAULT '',
    volatility_regime TEXT DEFAULT '',
    htf_trend_1h TEXT DEFAULT '',
    ema_20_slope DOUBLE PRECISION DEFAULT 0,
    ema_50_slope DOUBLE PRECISION DEFAULT 0,
    opening_range_size DOUBLE PRECISION DEFAULT 0,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_variant ON trades(variant_id);
CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time_ms);
CREATE INDEX IF NOT EXISTS idx_trades_direction ON trades(direction);
CREATE INDEX IF NOT EXISTS idx_trades_variant_instrument ON trades(variant_id, instrument);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_timeframe ON trades(strategy, timeframe);


-- ═══════════════════════════════════════════════════════════
-- EXIT RESULTS TABLE — One row per trade (~57 exit model columns)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS exit_results (
    id BIGSERIAL PRIMARY KEY,
    trade_id TEXT UNIQUE NOT NULL,

    rr1_result DOUBLE PRECISION DEFAULT NULL,
    rr1_5_result DOUBLE PRECISION DEFAULT NULL,
    rr2_result DOUBLE PRECISION DEFAULT NULL,
    rr2_5_result DOUBLE PRECISION DEFAULT NULL,
    rr3_result DOUBLE PRECISION DEFAULT NULL,
    rr5_result DOUBLE PRECISION DEFAULT NULL,
    rr10_result DOUBLE PRECISION DEFAULT NULL,

    atr_stop_result DOUBLE PRECISION DEFAULT NULL,
    swing_stop_result DOUBLE PRECISION DEFAULT NULL,
    fixed_stop_result DOUBLE PRECISION DEFAULT NULL,

    atr_trail_result DOUBLE PRECISION DEFAULT NULL,
    ema_trail_result DOUBLE PRECISION DEFAULT NULL,
    swing_trail_result DOUBLE PRECISION DEFAULT NULL,

    partial_a_result DOUBLE PRECISION DEFAULT NULL,
    partial_b_result DOUBLE PRECISION DEFAULT NULL,
    partial_c_result DOUBLE PRECISION DEFAULT NULL,

    time_15m_result DOUBLE PRECISION DEFAULT NULL,
    time_30m_result DOUBLE PRECISION DEFAULT NULL,
    time_1h_result DOUBLE PRECISION DEFAULT NULL,
    time_2h_result DOUBLE PRECISION DEFAULT NULL,
    time_4h_result DOUBLE PRECISION DEFAULT NULL,

    session_morning_result DOUBLE PRECISION DEFAULT NULL,
    session_midday_result DOUBLE PRECISION DEFAULT NULL,
    session_afternoon_result DOUBLE PRECISION DEFAULT NULL,
    session_preclose_result DOUBLE PRECISION DEFAULT NULL,

    dead_30m_result DOUBLE PRECISION DEFAULT NULL,
    dead_1h_result DOUBLE PRECISION DEFAULT NULL,
    dead_2h_result DOUBLE PRECISION DEFAULT NULL,

    be_atr_trail_result DOUBLE PRECISION DEFAULT NULL,
    be_tight_trail_result DOUBLE PRECISION DEFAULT NULL,
    be_wide_trail_result DOUBLE PRECISION DEFAULT NULL,
    be_ema_trail_result DOUBLE PRECISION DEFAULT NULL,
    be_rr2_target_result DOUBLE PRECISION DEFAULT NULL,
    be_rr3_target_result DOUBLE PRECISION DEFAULT NULL,
    be_rr5_target_result DOUBLE PRECISION DEFAULT NULL,

    chandelier_2x_result DOUBLE PRECISION DEFAULT NULL,
    chandelier_3x_result DOUBLE PRECISION DEFAULT NULL,
    chandelier_4x_result DOUBLE PRECISION DEFAULT NULL,
    pct_trail_05_result DOUBLE PRECISION DEFAULT NULL,
    pct_trail_1_result DOUBLE PRECISION DEFAULT NULL,
    pct_trail_15_result DOUBLE PRECISION DEFAULT NULL,
    pct_trail_2_result DOUBLE PRECISION DEFAULT NULL,
    step_trail_1r_result DOUBLE PRECISION DEFAULT NULL,
    step_trail_05r_result DOUBLE PRECISION DEFAULT NULL,
    delayed_chand_2x_result DOUBLE PRECISION DEFAULT NULL,
    delayed_chand_3x_result DOUBLE PRECISION DEFAULT NULL,
    delayed_chand_4x_result DOUBLE PRECISION DEFAULT NULL,

    vwap_cross_result DOUBLE PRECISION DEFAULT NULL,
    ema9_cross_result DOUBLE PRECISION DEFAULT NULL,
    ema13_cross_result DOUBLE PRECISION DEFAULT NULL,
    ema20_cross_result DOUBLE PRECISION DEFAULT NULL,
    ema50_cross_result DOUBLE PRECISION DEFAULT NULL,
    rsi_70_exit_result DOUBLE PRECISION DEFAULT NULL,
    rsi_75_exit_result DOUBLE PRECISION DEFAULT NULL,
    rsi_80_exit_result DOUBLE PRECISION DEFAULT NULL,
    ema_9_21_xover_result DOUBLE PRECISION DEFAULT NULL,
    ema_9_50_xover_result DOUBLE PRECISION DEFAULT NULL,

    mfe DOUBLE PRECISION DEFAULT 0,
    mae DOUBLE PRECISION DEFAULT 0,

    best_exit_model TEXT DEFAULT '',
    best_pnl DOUBLE PRECISION DEFAULT 0,
    worst_exit_model TEXT DEFAULT '',
    worst_pnl DOUBLE PRECISION DEFAULT 0,

    processed_at TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT fk_exit_trade FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);

CREATE INDEX IF NOT EXISTS idx_exit_trade ON exit_results(trade_id);


-- ═══════════════════════════════════════════════════════════
-- CANDLE CACHE — Temporary intraday storage (auto-cleanup)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS candle_cache (
    id BIGSERIAL PRIMARY KEY,
    instrument TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp_ms BIGINT NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume INTEGER DEFAULT 0,
    session_date DATE NOT NULL,

    UNIQUE(instrument, timeframe, timestamp_ms)
);

CREATE INDEX IF NOT EXISTS idx_candle_instrument_tf ON candle_cache(instrument, timeframe);
CREATE INDEX IF NOT EXISTS idx_candle_session ON candle_cache(session_date);
CREATE INDEX IF NOT EXISTS idx_candle_lookup ON candle_cache(instrument, timeframe, timestamp_ms);


-- ═══════════════════════════════════════════════════════════
-- VARIANT SCORES — Periodic scoring results
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS variant_scores (
    id BIGSERIAL PRIMARY KEY,
    variant_id TEXT NOT NULL,
    scoring_period TEXT NOT NULL,

    trade_count INTEGER DEFAULT 0,
    win_rate DOUBLE PRECISION DEFAULT 0,
    avg_win DOUBLE PRECISION DEFAULT 0,
    avg_loss DOUBLE PRECISION DEFAULT 0,
    expectancy DOUBLE PRECISION DEFAULT 0,
    profit_factor DOUBLE PRECISION DEFAULT 0,
    net_pnl DOUBLE PRECISION DEFAULT 0,
    max_drawdown DOUBLE PRECISION DEFAULT 0,
    recovery_factor DOUBLE PRECISION DEFAULT 0,

    stability_score DOUBLE PRECISION DEFAULT 0,

    best_exit_model TEXT DEFAULT '',
    best_exit_expectancy DOUBLE PRECISION DEFAULT 0,

    scored_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(variant_id, scoring_period)
);

CREATE INDEX IF NOT EXISTS idx_scores_variant ON variant_scores(variant_id);
CREATE INDEX IF NOT EXISTS idx_scores_period ON variant_scores(scoring_period);
CREATE INDEX IF NOT EXISTS idx_scores_expectancy ON variant_scores(expectancy DESC);


-- ═══════════════════════════════════════════════════════════
-- HISTORICAL CANDLES — For backtesting (multi-year storage)
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS historical_candles (
    id BIGSERIAL PRIMARY KEY,
    instrument TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp_ms BIGINT NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume BIGINT DEFAULT 0,
    session_date DATE NOT NULL,

    UNIQUE(instrument, timeframe, timestamp_ms)
);

CREATE INDEX IF NOT EXISTS idx_hist_lookup ON historical_candles(instrument, timeframe, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_hist_date ON historical_candles(instrument, session_date);
CREATE INDEX IF NOT EXISTS idx_hist_instrument ON historical_candles(instrument, timeframe);


-- ═══════════════════════════════════════════════════════════
-- BACKTEST RUNS — Metadata about each backtest execution
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS backtest_runs (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT UNIQUE NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    instruments TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    trades_generated INTEGER DEFAULT 0,
    days_processed INTEGER DEFAULT 0,
    total_days INTEGER DEFAULT 0,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ DEFAULT NULL,
    error_message TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_backtest_status ON backtest_runs(status);


-- ═══════════════════════════════════════════════════════════
-- FETCH PROGRESS — Tracks historical data download progress
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS fetch_progress (
    id BIGSERIAL PRIMARY KEY,
    instrument TEXT NOT NULL,
    session_date DATE NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '5m',
    candles_fetched INTEGER DEFAULT 0,
    fetched_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(instrument, session_date, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_fetch_instrument ON fetch_progress(instrument, session_date);


-- ═══════════════════════════════════════════════════════════
-- VARIANT DEFINITIONS — Maps variant_id to full setup (JSON)
-- Future-proof: adding/removing filters doesn't break schema
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS variant_definitions (
    variant_id TEXT PRIMARY KEY,
    generation INTEGER DEFAULT 1,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    entry_mode TEXT NOT NULL,
    filters JSONB NOT NULL DEFAULT '{}',
    short_name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    retired_at TIMESTAMPTZ DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_vardef_strategy ON variant_definitions(strategy);
CREATE INDEX IF NOT EXISTS idx_vardef_generation ON variant_definitions(generation);
CREATE INDEX IF NOT EXISTS idx_vardef_retired ON variant_definitions(retired_at);


-- ═══════════════════════════════════════════════════════════
-- VARIANT REGIME SCORES — Per variant × period × dimension
-- Stores breakdown of how a variant performs in each regime
-- ═══════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS variant_regime_scores (
    id BIGSERIAL PRIMARY KEY,
    variant_id TEXT NOT NULL,
    scoring_period TEXT NOT NULL,
    exit_model TEXT NOT NULL,

    dimension TEXT NOT NULL,
    dimension_value TEXT NOT NULL,

    trade_count INTEGER DEFAULT 0,
    win_rate DOUBLE PRECISION DEFAULT 0,
    expectancy DOUBLE PRECISION DEFAULT 0,
    profit_factor DOUBLE PRECISION DEFAULT 0,
    net_pnl DOUBLE PRECISION DEFAULT 0,

    scored_at TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(variant_id, scoring_period, exit_model, dimension, dimension_value)
);

CREATE INDEX IF NOT EXISTS idx_regime_variant ON variant_regime_scores(variant_id, scoring_period);
CREATE INDEX IF NOT EXISTS idx_regime_dimension ON variant_regime_scores(dimension, dimension_value);
CREATE INDEX IF NOT EXISTS idx_regime_expectancy ON variant_regime_scores(expectancy DESC);
"""

# ─── Schema: SQLite (for local testing) ─────────────────────────────────────

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE NOT NULL,
    variant_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_time_ms REAL NOT NULL,
    entry_price REAL NOT NULL,
    atr_entry REAL DEFAULT 0,
    adx_entry REAL DEFAULT 0,
    rsi_entry REAL DEFAULT 0,
    vix_entry REAL DEFAULT 0,
    volume_ratio_entry REAL DEFAULT 0,
    vwap_entry REAL DEFAULT 0,
    gap_size REAL DEFAULT 0,
    gap_direction TEXT DEFAULT '',
    session TEXT DEFAULT '',
    day_of_week TEXT DEFAULT '',
    month TEXT DEFAULT '',
    market_structure TEXT DEFAULT '',
    volatility_regime TEXT DEFAULT '',
    htf_trend_1h TEXT DEFAULT '',
    ema_20_slope REAL DEFAULT 0,
    ema_50_slope REAL DEFAULT 0,
    opening_range_size REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trades_variant ON trades(variant_id);
CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time_ms);
CREATE INDEX IF NOT EXISTS idx_trades_direction ON trades(direction);

CREATE TABLE IF NOT EXISTS exit_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT UNIQUE NOT NULL,
    rr1_result REAL, rr1_5_result REAL, rr2_result REAL, rr2_5_result REAL,
    rr3_result REAL, rr5_result REAL, rr10_result REAL,
    atr_stop_result REAL, swing_stop_result REAL, fixed_stop_result REAL,
    atr_trail_result REAL, ema_trail_result REAL, swing_trail_result REAL,
    partial_a_result REAL, partial_b_result REAL, partial_c_result REAL,
    time_15m_result REAL, time_30m_result REAL, time_1h_result REAL,
    time_2h_result REAL, time_4h_result REAL,
    session_morning_result REAL, session_midday_result REAL,
    session_afternoon_result REAL, session_preclose_result REAL,
    dead_30m_result REAL, dead_1h_result REAL, dead_2h_result REAL,
    be_atr_trail_result REAL, be_tight_trail_result REAL, be_wide_trail_result REAL,
    be_ema_trail_result REAL, be_rr2_target_result REAL, be_rr3_target_result REAL,
    be_rr5_target_result REAL,
    chandelier_2x_result REAL, chandelier_3x_result REAL, chandelier_4x_result REAL,
    pct_trail_05_result REAL, pct_trail_1_result REAL, pct_trail_15_result REAL,
    pct_trail_2_result REAL, step_trail_1r_result REAL, step_trail_05r_result REAL,
    delayed_chand_2x_result REAL, delayed_chand_3x_result REAL, delayed_chand_4x_result REAL,
    vwap_cross_result REAL, ema9_cross_result REAL, ema13_cross_result REAL,
    ema20_cross_result REAL, ema50_cross_result REAL,
    rsi_70_exit_result REAL, rsi_75_exit_result REAL, rsi_80_exit_result REAL,
    ema_9_21_xover_result REAL, ema_9_50_xover_result REAL,
    mfe REAL DEFAULT 0, mae REAL DEFAULT 0,
    best_exit_model TEXT DEFAULT '', best_pnl REAL DEFAULT 0,
    worst_exit_model TEXT DEFAULT '', worst_pnl REAL DEFAULT 0,
    processed_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
);
CREATE INDEX IF NOT EXISTS idx_exit_trade ON exit_results(trade_id);

CREATE TABLE IF NOT EXISTS candle_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL, timeframe TEXT NOT NULL,
    timestamp_ms REAL NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume INTEGER DEFAULT 0, session_date TEXT NOT NULL,
    UNIQUE(instrument, timeframe, timestamp_ms)
);
CREATE INDEX IF NOT EXISTS idx_candle_instrument ON candle_cache(instrument, timeframe);
CREATE INDEX IF NOT EXISTS idx_candle_session ON candle_cache(session_date);
CREATE INDEX IF NOT EXISTS idx_candle_time ON candle_cache(timestamp_ms);

CREATE TABLE IF NOT EXISTS variant_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id TEXT NOT NULL, scoring_period TEXT NOT NULL,
    trade_count INTEGER DEFAULT 0, win_rate REAL DEFAULT 0,
    avg_win REAL DEFAULT 0, avg_loss REAL DEFAULT 0,
    expectancy REAL DEFAULT 0, profit_factor REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0, max_drawdown REAL DEFAULT 0,
    recovery_factor REAL DEFAULT 0, stability_score REAL DEFAULT 0,
    best_exit_model TEXT DEFAULT '', best_exit_expectancy REAL DEFAULT 0,
    scored_at TEXT DEFAULT (datetime('now')),
    UNIQUE(variant_id, scoring_period)
);
CREATE INDEX IF NOT EXISTS idx_scores_variant ON variant_scores(variant_id);
CREATE INDEX IF NOT EXISTS idx_scores_period ON variant_scores(scoring_period);
CREATE INDEX IF NOT EXISTS idx_scores_expectancy ON variant_scores(expectancy);

CREATE TABLE IF NOT EXISTS historical_candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL, timeframe TEXT NOT NULL,
    timestamp_ms REAL NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume INTEGER DEFAULT 0, session_date TEXT NOT NULL,
    UNIQUE(instrument, timeframe, timestamp_ms)
);
CREATE INDEX IF NOT EXISTS idx_hist_lookup ON historical_candles(instrument, timeframe, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_hist_date ON historical_candles(instrument, session_date);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,
    start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    instruments TEXT NOT NULL, status TEXT DEFAULT 'running',
    trades_generated INTEGER DEFAULT 0, days_processed INTEGER DEFAULT 0,
    total_days INTEGER DEFAULT 0,
    started_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT DEFAULT NULL, error_message TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS fetch_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument TEXT NOT NULL, session_date TEXT NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '5m',
    candles_fetched INTEGER DEFAULT 0,
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(instrument, session_date, timeframe)
);

CREATE TABLE IF NOT EXISTS variant_definitions (
    variant_id TEXT PRIMARY KEY,
    generation INTEGER DEFAULT 1,
    strategy TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    entry_mode TEXT NOT NULL,
    filters TEXT NOT NULL DEFAULT '{}',
    short_name TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    retired_at TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_vardef_strategy ON variant_definitions(strategy);
CREATE INDEX IF NOT EXISTS idx_vardef_generation ON variant_definitions(generation);

CREATE TABLE IF NOT EXISTS variant_regime_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id TEXT NOT NULL,
    scoring_period TEXT NOT NULL,
    exit_model TEXT NOT NULL,
    dimension TEXT NOT NULL,
    dimension_value TEXT NOT NULL,
    trade_count INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    expectancy REAL DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    net_pnl REAL DEFAULT 0,
    scored_at TEXT DEFAULT (datetime('now')),
    UNIQUE(variant_id, scoring_period, exit_model, dimension, dimension_value)
);
CREATE INDEX IF NOT EXISTS idx_regime_variant ON variant_regime_scores(variant_id, scoring_period);
CREATE INDEX IF NOT EXISTS idx_regime_dimension ON variant_regime_scores(dimension, dimension_value);
"""


# ─── ResearchStore Class ─────────────────────────────────────────────────────


class ResearchStore:
    """
    Dual-mode research database.

    Production: PostgreSQL (set DATABASE_URL env var)
    Testing:    SQLite (default, or pass db_path)

    All public methods work identically regardless of backend.
    The store auto-detects which backend to use from DATABASE_URL.
    """

    def __init__(self, db_path: Path | None = None, database_url: str | None = None) -> None:
        """
        Args:
            db_path: SQLite file path (used only for SQLite mode / tests).
                     If provided, forces SQLite mode regardless of DATABASE_URL.
            database_url: Override DATABASE_URL (for programmatic use).
        """
        # If db_path is explicitly passed, force SQLite (for tests)
        if db_path is not None:
            self._database_url = ""
            self._use_postgres = False
        else:
            self._database_url = database_url or _DATABASE_URL
            self._use_postgres = (
                self._database_url.startswith("postgresql://")
                or self._database_url.startswith("postgres://")
            )

        # SQLite state
        self._sqlite_path = db_path or DEFAULT_SQLITE_PATH
        self._sqlite_conn = None

        # PostgreSQL state (connection pool)
        self._pg_pool = None

        self._lock = threading.Lock()

    @property
    def is_postgres(self) -> bool:
        return self._use_postgres

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Initialize database connection and create tables."""
        if self._use_postgres:
            self._start_postgres()
        else:
            self._start_sqlite()

    def stop(self) -> None:
        """Close database connection."""
        if self._use_postgres:
            if self._pg_pool:
                self._pg_pool.closeall()
                self._pg_pool = None
        else:
            if self._sqlite_conn:
                self._sqlite_conn.close()
                self._sqlite_conn = None
        logger.info("ResearchStore stopped")

    def _start_postgres(self) -> None:
        """Connect to PostgreSQL and create schema."""
        try:
            import psycopg2
            import psycopg2.extras
            import psycopg2.pool
        except ImportError:
            raise ImportError(
                "psycopg2 is required for PostgreSQL mode. "
                "Install with: pip install psycopg2-binary"
            )

        # Use a threaded connection pool for thread safety
        self._pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=self._database_url,
        )

        # Create tables using a dedicated connection
        conn = self._pg_pool.getconn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(POSTGRES_SCHEMA)
        finally:
            self._pg_pool.putconn(conn)

        logger.info("ResearchStore started (PostgreSQL, pool=2-10)")

    def _start_sqlite(self) -> None:
        """Connect to SQLite and create schema."""
        import sqlite3
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._sqlite_conn = sqlite3.connect(str(self._sqlite_path), check_same_thread=False)
        self._sqlite_conn.executescript(SQLITE_SCHEMA)
        self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
        self._sqlite_conn.execute("PRAGMA synchronous=NORMAL")
        self._sqlite_conn.execute("PRAGMA cache_size=-64000")
        logger.info("ResearchStore started (SQLite: %s)", self._sqlite_path)

    # ─── Trade Recording ─────────────────────────────────────────────────────

    def write_trade(self, trade: TradeRecord) -> None:
        """Write a single trade record."""
        params = (
            trade.trade_id, trade.variant_id, trade.strategy,
            trade.timeframe, trade.instrument,
            trade.direction, trade.entry_time_ms, trade.entry_price,
            trade.atr_entry, trade.adx_entry, trade.rsi_entry,
            trade.vix_entry, trade.volume_ratio_entry, trade.vwap_entry,
            trade.gap_size, trade.gap_direction, trade.session,
            trade.day_of_week, trade.month,
            trade.market_structure, trade.volatility_regime,
            trade.htf_trend_1h,
            trade.ema_20_slope, trade.ema_50_slope,
            trade.opening_range_size,
        )

        if self._use_postgres:
            sql = """INSERT INTO trades (
                trade_id, variant_id, strategy, timeframe, instrument,
                direction, entry_time_ms, entry_price,
                atr_entry, adx_entry, rsi_entry, vix_entry,
                volume_ratio_entry, vwap_entry,
                gap_size, gap_direction, session, day_of_week, month,
                market_structure, volatility_regime, htf_trend_1h,
                ema_20_slope, ema_50_slope, opening_range_size
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (trade_id) DO NOTHING"""
        else:
            sql = """INSERT OR IGNORE INTO trades (
                trade_id, variant_id, strategy, timeframe, instrument,
                direction, entry_time_ms, entry_price,
                atr_entry, adx_entry, rsi_entry, vix_entry,
                volume_ratio_entry, vwap_entry,
                gap_size, gap_direction, session, day_of_week, month,
                market_structure, volatility_regime, htf_trend_1h,
                ema_20_slope, ema_50_slope, opening_range_size
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

        self._execute(sql, params)

    def write_trades_batch(self, trades: list[TradeRecord]) -> int:
        """Write multiple trade records in a single transaction."""
        if not trades:
            return 0

        rows = [
            (
                t.trade_id, t.variant_id, t.strategy,
                t.timeframe, t.instrument,
                t.direction, t.entry_time_ms, t.entry_price,
                t.atr_entry, t.adx_entry, t.rsi_entry,
                t.vix_entry, t.volume_ratio_entry, t.vwap_entry,
                t.gap_size, t.gap_direction, t.session,
                t.day_of_week, t.month,
                t.market_structure, t.volatility_regime,
                t.htf_trend_1h,
                t.ema_20_slope, t.ema_50_slope,
                t.opening_range_size,
            )
            for t in trades
        ]

        if self._use_postgres:
            sql = """INSERT INTO trades (
                trade_id, variant_id, strategy, timeframe, instrument,
                direction, entry_time_ms, entry_price,
                atr_entry, adx_entry, rsi_entry, vix_entry,
                volume_ratio_entry, vwap_entry,
                gap_size, gap_direction, session, day_of_week, month,
                market_structure, volatility_regime, htf_trend_1h,
                ema_20_slope, ema_50_slope, opening_range_size
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (trade_id) DO NOTHING"""
        else:
            sql = """INSERT OR IGNORE INTO trades (
                trade_id, variant_id, strategy, timeframe, instrument,
                direction, entry_time_ms, entry_price,
                atr_entry, adx_entry, rsi_entry, vix_entry,
                volume_ratio_entry, vwap_entry,
                gap_size, gap_direction, session, day_of_week, month,
                market_structure, volatility_regime, htf_trend_1h,
                ema_20_slope, ema_50_slope, opening_range_size
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

        try:
            with self._lock:
                if self._use_postgres:
                    conn = self._pg_pool.getconn()
                    try:
                        with conn.cursor() as cur:
                            cur.executemany(sql, rows)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        self._pg_pool.putconn(conn)
                else:
                    self._sqlite_conn.executemany(sql, rows)
                    self._sqlite_conn.commit()
                return len(rows)
        except Exception as e:
            logger.error("Batch trade write error: %s", e)
            return 0

    # ─── Candle Cache ────────────────────────────────────────────────────────

    def cache_candle(
        self, instrument: str, timeframe: str, timestamp_ms: float,
        o: float, h: float, l: float, c: float, volume: int, session_date: str,
    ) -> None:
        """Cache a candle for later exit simulation."""
        if self._use_postgres:
            sql = """INSERT INTO candle_cache
                (instrument, timeframe, timestamp_ms, open, high, low, close, volume, session_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (instrument, timeframe, timestamp_ms) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume"""
        else:
            sql = """INSERT OR REPLACE INTO candle_cache
                (instrument, timeframe, timestamp_ms, open, high, low, close, volume, session_date)
                VALUES (?,?,?,?,?,?,?,?,?)"""

        self._execute(sql, (instrument, timeframe, timestamp_ms, o, h, l, c, volume, session_date))

    def get_cached_candles(self, instrument: str, timeframe: str, start_ms: float, end_ms: float) -> list[dict]:
        """Get cached candles for exit simulation."""
        if self._use_postgres:
            sql = """SELECT * FROM candle_cache
                WHERE instrument=%s AND timeframe=%s AND timestamp_ms >= %s AND timestamp_ms <= %s
                ORDER BY timestamp_ms ASC"""
        else:
            sql = """SELECT * FROM candle_cache
                WHERE instrument=? AND timeframe=? AND timestamp_ms >= ? AND timestamp_ms <= ?
                ORDER BY timestamp_ms ASC"""
        return self._query(sql, (instrument, timeframe, start_ms, end_ms))

    def cleanup_candle_cache(self, before_date: str) -> int:
        """Delete cached candles older than given date."""
        if self._use_postgres:
            sql = "DELETE FROM candle_cache WHERE session_date < %s"
        else:
            sql = "DELETE FROM candle_cache WHERE session_date < ?"

        try:
            with self._lock:
                if self._use_postgres:
                    conn = self._pg_pool.getconn()
                    try:
                        with conn.cursor() as cur:
                            cur.execute(sql, (before_date,))
                            count = cur.rowcount
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        self._pg_pool.putconn(conn)
                    return count
                else:
                    cursor = self._sqlite_conn.execute(sql, (before_date,))
                    self._sqlite_conn.commit()
                    return cursor.rowcount
        except Exception as e:
            logger.error("Candle cache cleanup error: %s", e)
            return 0

    # ─── Historical Candles (for backtesting) ────────────────────────────────

    def write_historical_candles_batch(
        self, candles: list[tuple], instrument: str, timeframe: str = "5m",
    ) -> int:
        """
        Batch insert historical candles.
        Each tuple: (timestamp_ms, open, high, low, close, volume, session_date)
        """
        if not candles:
            return 0

        rows = [(instrument, timeframe, *c) for c in candles]

        if self._use_postgres:
            sql = """INSERT INTO historical_candles
                (instrument, timeframe, timestamp_ms, open, high, low, close, volume, session_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (instrument, timeframe, timestamp_ms) DO NOTHING"""
        else:
            sql = """INSERT OR IGNORE INTO historical_candles
                (instrument, timeframe, timestamp_ms, open, high, low, close, volume, session_date)
                VALUES (?,?,?,?,?,?,?,?,?)"""

        try:
            with self._lock:
                if self._use_postgres:
                    conn = self._pg_pool.getconn()
                    try:
                        with conn.cursor() as cur:
                            cur.executemany(sql, rows)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        self._pg_pool.putconn(conn)
                else:
                    self._sqlite_conn.executemany(sql, rows)
                    self._sqlite_conn.commit()
                return len(rows)
        except Exception as e:
            logger.error("Historical candle write error: %s", e)
            return 0

    def get_historical_candles(
        self, instrument: str, timeframe: str, start_ms: float, end_ms: float,
    ) -> list[dict]:
        """Get historical candles for backtesting."""
        if self._use_postgres:
            sql = """SELECT * FROM historical_candles
                WHERE instrument=%s AND timeframe=%s AND timestamp_ms >= %s AND timestamp_ms <= %s
                ORDER BY timestamp_ms ASC"""
        else:
            sql = """SELECT * FROM historical_candles
                WHERE instrument=? AND timeframe=? AND timestamp_ms >= ? AND timestamp_ms <= ?
                ORDER BY timestamp_ms ASC"""
        return self._query(sql, (instrument, timeframe, start_ms, end_ms))

    def get_historical_dates(self, instrument: str, timeframe: str = "5m") -> list[str]:
        """Get all unique dates that have historical data for an instrument."""
        if self._use_postgres:
            sql = """SELECT DISTINCT session_date FROM historical_candles
                WHERE instrument=%s AND timeframe=%s ORDER BY session_date"""
        else:
            sql = """SELECT DISTINCT session_date FROM historical_candles
                WHERE instrument=? AND timeframe=? ORDER BY session_date"""
        rows = self._query(sql, (instrument, timeframe))
        return [str(r["session_date"]) for r in rows]

    # ─── Fetch Progress ──────────────────────────────────────────────────────

    def mark_fetched(self, instrument: str, session_date: str, candles_fetched: int, timeframe: str = "5m") -> None:
        """Mark a date as fetched for an instrument."""
        if self._use_postgres:
            sql = """INSERT INTO fetch_progress (instrument, session_date, timeframe, candles_fetched)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (instrument, session_date, timeframe) DO UPDATE SET
                candles_fetched=EXCLUDED.candles_fetched, fetched_at=NOW()"""
        else:
            sql = """INSERT OR REPLACE INTO fetch_progress
                (instrument, session_date, timeframe, candles_fetched)
                VALUES (?,?,?,?)"""
        self._execute(sql, (instrument, session_date, timeframe, candles_fetched))

    def is_date_fetched(self, instrument: str, session_date: str, timeframe: str = "5m") -> bool:
        """Check if a date has already been fetched."""
        if self._use_postgres:
            sql = "SELECT 1 FROM fetch_progress WHERE instrument=%s AND session_date=%s AND timeframe=%s"
        else:
            sql = "SELECT 1 FROM fetch_progress WHERE instrument=? AND session_date=? AND timeframe=?"
        rows = self._query(sql, (instrument, session_date, timeframe))
        return len(rows) > 0

    # ─── Backtest Runs ───────────────────────────────────────────────────────

    def create_backtest_run(self, run_id: str, start_date: str, end_date: str, instruments: str, total_days: int) -> None:
        """Create a backtest run record."""
        if self._use_postgres:
            sql = """INSERT INTO backtest_runs (run_id, start_date, end_date, instruments, total_days)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT (run_id) DO NOTHING"""
        else:
            sql = """INSERT OR IGNORE INTO backtest_runs (run_id, start_date, end_date, instruments, total_days)
                VALUES (?,?,?,?,?)"""
        self._execute(sql, (run_id, start_date, end_date, instruments, total_days))

    def update_backtest_progress(self, run_id: str, days_processed: int, trades_generated: int) -> None:
        """Update backtest run progress."""
        if self._use_postgres:
            sql = "UPDATE backtest_runs SET days_processed=%s, trades_generated=%s WHERE run_id=%s"
        else:
            sql = "UPDATE backtest_runs SET days_processed=?, trades_generated=? WHERE run_id=?"
        self._execute(sql, (days_processed, trades_generated, run_id))

    def complete_backtest_run(self, run_id: str, status: str = "complete", error: str | None = None) -> None:
        """Mark a backtest run as complete or failed."""
        if self._use_postgres:
            sql = "UPDATE backtest_runs SET status=%s, completed_at=NOW(), error_message=%s WHERE run_id=%s"
        else:
            sql = "UPDATE backtest_runs SET status=?, completed_at=datetime('now'), error_message=? WHERE run_id=?"
        self._execute(sql, (status, error, run_id))

    # ─── Query Methods ───────────────────────────────────────────────────────

    def get_trades_for_date(self, date_str: str) -> list[dict]:
        """Get all trades for a specific date (for exit processing)."""
        from datetime import datetime
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        start_ms = dt.timestamp() * 1000
        end_ms = (dt.timestamp() + 86400) * 1000

        if self._use_postgres:
            sql = "SELECT * FROM trades WHERE entry_time_ms >= %s AND entry_time_ms < %s ORDER BY entry_time_ms"
        else:
            sql = "SELECT * FROM trades WHERE entry_time_ms >= ? AND entry_time_ms < ? ORDER BY entry_time_ms"
        return self._query(sql, (start_ms, end_ms))

    def get_trades_for_variant(self, variant_id: str, limit: int = 1000) -> list[dict]:
        """Get all trades for a specific variant."""
        if self._use_postgres:
            sql = "SELECT * FROM trades WHERE variant_id=%s ORDER BY entry_time_ms DESC LIMIT %s"
        else:
            sql = "SELECT * FROM trades WHERE variant_id=? ORDER BY entry_time_ms DESC LIMIT ?"
        return self._query(sql, (variant_id, limit))

    def get_trade_count_today(self) -> int:
        """Get the number of trades recorded today."""
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        dt = datetime.strptime(today, "%Y-%m-%d")
        start_ms = dt.timestamp() * 1000
        end_ms = (dt.timestamp() + 86400) * 1000

        if self._use_postgres:
            sql = "SELECT COUNT(*) as cnt FROM trades WHERE entry_time_ms >= %s AND entry_time_ms < %s"
        else:
            sql = "SELECT COUNT(*) as cnt FROM trades WHERE entry_time_ms >= ? AND entry_time_ms < ?"
        result = self._query(sql, (start_ms, end_ms))
        return result[0]["cnt"] if result else 0

    def get_total_trade_count(self) -> int:
        """Total trades in the database."""
        result = self._query("SELECT COUNT(*) as cnt FROM trades")
        return result[0]["cnt"] if result else 0

    def get_unique_variants_triggered(self) -> int:
        """Count of unique variants that have produced at least one trade."""
        result = self._query("SELECT COUNT(DISTINCT variant_id) as cnt FROM trades")
        return result[0]["cnt"] if result else 0

    # ─── Exit Results ────────────────────────────────────────────────────────

    def write_exit_result(self, trade_id: str, results: dict) -> None:
        """Write exit simulation results for a trade. Handles all exit columns dynamically."""
        _DIRECT_COLS = ("mfe", "mae", "best_exit_model", "best_pnl", "worst_exit_model", "worst_pnl")

        columns = ["trade_id"]
        values: list[Any] = [trade_id]

        for key, val in results.items():
            if key in _DIRECT_COLS:
                columns.append(key)
                values.append(val)
            else:
                col_name = f"{key}_result"
                columns.append(col_name)
                values.append(val)

        col_str = ", ".join(columns)

        if self._use_postgres:
            placeholders = ", ".join(["%s"] * len(values))
            # UPSERT: on conflict update all columns
            update_cols = [c for c in columns if c != "trade_id"]
            update_set = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
            sql = f"INSERT INTO exit_results ({col_str}) VALUES ({placeholders}) ON CONFLICT (trade_id) DO UPDATE SET {update_set}"
        else:
            placeholders = ", ".join(["?"] * len(values))
            sql = f"INSERT OR REPLACE INTO exit_results ({col_str}) VALUES ({placeholders})"

        try:
            with self._lock:
                if self._use_postgres:
                    conn = self._pg_pool.getconn()
                    try:
                        with conn.cursor() as cur:
                            cur.execute(sql, values)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        self._pg_pool.putconn(conn)
                else:
                    self._sqlite_conn.execute(sql, values)
                    self._sqlite_conn.commit()
        except Exception as e:
            logger.error("Exit result write error: %s (trade=%s)", e, trade_id)

    # ─── Variant Scores ──────────────────────────────────────────────────────

    def write_variant_score(self, variant_id: str, period: str, metrics: dict) -> None:
        """Write or update a variant's scoring results."""
        params = (
            variant_id, period,
            metrics.get("trade_count", 0), metrics.get("win_rate", 0),
            metrics.get("avg_win", 0), metrics.get("avg_loss", 0),
            metrics.get("expectancy", 0), metrics.get("profit_factor", 0),
            metrics.get("net_pnl", 0), metrics.get("max_drawdown", 0),
            metrics.get("recovery_factor", 0), metrics.get("stability_score", 0),
            metrics.get("best_exit_model", ""), metrics.get("best_exit_expectancy", 0),
        )

        if self._use_postgres:
            sql = """INSERT INTO variant_scores (
                variant_id, scoring_period, trade_count, win_rate, avg_win, avg_loss,
                expectancy, profit_factor, net_pnl, max_drawdown, recovery_factor,
                stability_score, best_exit_model, best_exit_expectancy
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (variant_id, scoring_period) DO UPDATE SET
                trade_count=EXCLUDED.trade_count, win_rate=EXCLUDED.win_rate,
                avg_win=EXCLUDED.avg_win, avg_loss=EXCLUDED.avg_loss,
                expectancy=EXCLUDED.expectancy, profit_factor=EXCLUDED.profit_factor,
                net_pnl=EXCLUDED.net_pnl, max_drawdown=EXCLUDED.max_drawdown,
                recovery_factor=EXCLUDED.recovery_factor, stability_score=EXCLUDED.stability_score,
                best_exit_model=EXCLUDED.best_exit_model, best_exit_expectancy=EXCLUDED.best_exit_expectancy,
                scored_at=NOW()"""
        else:
            sql = """INSERT OR REPLACE INTO variant_scores (
                variant_id, scoring_period, trade_count, win_rate, avg_win, avg_loss,
                expectancy, profit_factor, net_pnl, max_drawdown, recovery_factor,
                stability_score, best_exit_model, best_exit_expectancy
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

        self._execute(sql, params)

    def get_top_variants(self, period: str, limit: int = 50) -> list[dict]:
        """Get top-scoring variants for a period."""
        if self._use_postgres:
            sql = """SELECT * FROM variant_scores
                WHERE scoring_period=%s AND trade_count >= 10
                ORDER BY expectancy DESC LIMIT %s"""
        else:
            sql = """SELECT * FROM variant_scores
                WHERE scoring_period=? AND trade_count >= 10
                ORDER BY expectancy DESC LIMIT ?"""
        return self._query(sql, (period, limit))

    # ─── Variant Definitions ─────────────────────────────────────────────────

    def register_variants(self, variants: list, generation: int = 1) -> int:
        """
        Register variant definitions (bulk upsert).
        Each variant is a Variant object from models.py.
        Stores filters as JSON for future-proofing.

        Returns count registered.
        """
        import json

        rows = []
        for v in variants:
            filters_json = json.dumps(v.filters.to_dict())
            rows.append((
                v.variant_id,
                generation,
                v.strategy.value,
                v.timeframe.value,
                v.entry_mode.value,
                filters_json,
                v.short_name(),
            ))

        if not rows:
            return 0

        if self._use_postgres:
            sql = """INSERT INTO variant_definitions
                (variant_id, generation, strategy, timeframe, entry_mode, filters, short_name)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (variant_id) DO UPDATE SET
                generation=EXCLUDED.generation, short_name=EXCLUDED.short_name"""
        else:
            sql = """INSERT OR REPLACE INTO variant_definitions
                (variant_id, generation, strategy, timeframe, entry_mode, filters, short_name)
                VALUES (?,?,?,?,?,?,?)"""

        try:
            with self._lock:
                if self._use_postgres:
                    conn = self._pg_pool.getconn()
                    try:
                        with conn.cursor() as cur:
                            cur.executemany(sql, rows)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        self._pg_pool.putconn(conn)
                else:
                    self._sqlite_conn.executemany(sql, rows)
                    self._sqlite_conn.commit()
                return len(rows)
        except Exception as e:
            logger.error("Variant registration error: %s", e)
            return 0

    def retire_variants(self, variant_ids: list[str]) -> int:
        """Mark variants as retired (no longer generated). Historical data preserved."""
        if not variant_ids:
            return 0

        if self._use_postgres:
            placeholders = ",".join(["%s"] * len(variant_ids))
            sql = f"UPDATE variant_definitions SET retired_at=NOW() WHERE variant_id IN ({placeholders})"
        else:
            placeholders = ",".join(["?"] * len(variant_ids))
            sql = f"UPDATE variant_definitions SET retired_at=datetime('now') WHERE variant_id IN ({placeholders})"

        self._execute(sql, tuple(variant_ids))
        return len(variant_ids)

    def get_variant_definition(self, variant_id: str) -> dict | None:
        """Look up what a variant_id represents."""
        if self._use_postgres:
            sql = "SELECT * FROM variant_definitions WHERE variant_id=%s"
        else:
            sql = "SELECT * FROM variant_definitions WHERE variant_id=?"
        rows = self._query(sql, (variant_id,))
        return rows[0] if rows else None

    def get_variant_definitions_by_strategy(self, strategy: str) -> list[dict]:
        """Get all variant definitions for a strategy."""
        if self._use_postgres:
            sql = "SELECT * FROM variant_definitions WHERE strategy=%s AND retired_at IS NULL"
        else:
            sql = "SELECT * FROM variant_definitions WHERE strategy=? AND retired_at IS NULL"
        return self._query(sql, (strategy,))

    # ─── Variant Regime Scores ───────────────────────────────────────────────

    def write_regime_scores(
        self,
        variant_id: str,
        scoring_period: str,
        exit_model: str,
        regime_data: dict[str, dict[str, dict]],
    ) -> int:
        """
        Write regime breakdown scores for a variant.

        Args:
            variant_id: The variant being scored.
            scoring_period: Period label (e.g. "2024-01-01_to_2024-12-31").
            exit_model: Which exit model was used.
            regime_data: Nested dict like:
                {
                    "session": {
                        "MORNING": {"trade_count": 45, "win_rate": 0.72, "expectancy": 38.5, ...},
                        "MIDDAY": {"trade_count": 30, "win_rate": 0.40, "expectancy": -8.0, ...},
                    },
                    "volatility_regime": {
                        "HIGH": {...}, "LOW": {...}
                    }
                }
        """
        rows = []
        for dimension, values in regime_data.items():
            for dim_value, metrics in values.items():
                rows.append((
                    variant_id, scoring_period, exit_model,
                    dimension, dim_value,
                    metrics.get("trade_count", 0),
                    metrics.get("win_rate", 0),
                    metrics.get("expectancy", 0),
                    metrics.get("profit_factor", 0),
                    metrics.get("net_pnl", 0),
                ))

        if not rows:
            return 0

        if self._use_postgres:
            sql = """INSERT INTO variant_regime_scores
                (variant_id, scoring_period, exit_model, dimension, dimension_value,
                 trade_count, win_rate, expectancy, profit_factor, net_pnl)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (variant_id, scoring_period, exit_model, dimension, dimension_value)
                DO UPDATE SET trade_count=EXCLUDED.trade_count, win_rate=EXCLUDED.win_rate,
                    expectancy=EXCLUDED.expectancy, profit_factor=EXCLUDED.profit_factor,
                    net_pnl=EXCLUDED.net_pnl, scored_at=NOW()"""
        else:
            sql = """INSERT OR REPLACE INTO variant_regime_scores
                (variant_id, scoring_period, exit_model, dimension, dimension_value,
                 trade_count, win_rate, expectancy, profit_factor, net_pnl)
                VALUES (?,?,?,?,?,?,?,?,?,?)"""

        try:
            with self._lock:
                if self._use_postgres:
                    conn = self._pg_pool.getconn()
                    try:
                        with conn.cursor() as cur:
                            cur.executemany(sql, rows)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        self._pg_pool.putconn(conn)
                else:
                    self._sqlite_conn.executemany(sql, rows)
                    self._sqlite_conn.commit()
                return len(rows)
        except Exception as e:
            logger.error("Regime scores write error: %s", e)
            return 0

    def get_regime_scores(self, variant_id: str, scoring_period: str) -> list[dict]:
        """Get all regime scores for a variant in a specific period."""
        if self._use_postgres:
            sql = """SELECT * FROM variant_regime_scores
                WHERE variant_id=%s AND scoring_period=%s
                ORDER BY dimension, expectancy DESC"""
        else:
            sql = """SELECT * FROM variant_regime_scores
                WHERE variant_id=? AND scoring_period=?
                ORDER BY dimension, expectancy DESC"""
        return self._query(sql, (variant_id, scoring_period))

    def get_best_regimes(self, dimension: str, dimension_value: str, period: str, limit: int = 20) -> list[dict]:
        """Find variants that perform best in a specific regime."""
        if self._use_postgres:
            sql = """SELECT * FROM variant_regime_scores
                WHERE dimension=%s AND dimension_value=%s AND scoring_period=%s
                  AND trade_count >= 5
                ORDER BY expectancy DESC LIMIT %s"""
        else:
            sql = """SELECT * FROM variant_regime_scores
                WHERE dimension=? AND dimension_value=? AND scoring_period=?
                  AND trade_count >= 5
                ORDER BY expectancy DESC LIMIT ?"""
        return self._query(sql, (dimension, dimension_value, period, limit))

    # ─── Internal: Unified Execute/Query ─────────────────────────────────────

    def _execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a write query (works for both backends)."""
        try:
            with self._lock:
                if self._use_postgres:
                    conn = self._pg_pool.getconn()
                    try:
                        with conn.cursor() as cur:
                            cur.execute(sql, params)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    finally:
                        self._pg_pool.putconn(conn)
                else:
                    self._sqlite_conn.execute(sql, params)
                    self._sqlite_conn.commit()
        except Exception as e:
            logger.error("ResearchStore write error: %s (sql=%s)", e, sql[:80])

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute a read query and return results as list of dicts."""
        try:
            with self._lock:
                if self._use_postgres:
                    import psycopg2.extras
                    conn = self._pg_pool.getconn()
                    try:
                        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                            cur.execute(sql, params)
                            return [dict(row) for row in cur.fetchall()]
                    finally:
                        self._pg_pool.putconn(conn)
                else:
                    import sqlite3
                    self._sqlite_conn.row_factory = sqlite3.Row
                    cursor = self._sqlite_conn.execute(sql, params)
                    return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error("ResearchStore read error: %s", e)
            return []
