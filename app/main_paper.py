"""
Paper Trading Runner — f50a2b57 MR 15m on 6 Validated Instruments.

Runs the proven regime-scored variant on live Groww WebSocket feed.
Each instrument has per-instrument regime gating based on forward-walk
validation from regime scorer (2025-01-01 to 2026-06-12).

Instruments: BANKNIFTY, NIFTY, TCS, RELIANCE, BHARTIARTL, INFY
Strategy: Mean Reversion 15m (f50a2b57d7a1dac5)
Exit: time_1h (60 min after entry)
Session: MORNING only (all validated conditions are morning)
No SL: MR trades recover from MAE (verified in backtest)

Architecture:
    Groww WebSocket → Ticks → CandleBuilder → 5m/15m candles
    → IndicatorEngine (RSI, EMA20, ATR, ADX, VIX)
    → RegimeDetector (session/volatility/structure)
    → VariantEvaluator (check each variant's conditions)
    → TradeManager (track positions, manage exits)
    → PnLTracker (per-variant capital)
    → TelegramNotifier (alerts on entry/exit)

Usage:
    python -m app.main_paper
"""

from __future__ import annotations

import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from enum import Enum
from typing import Any

from app.broker.base import Instrument, Tick
from app.broker.groww import GrowwBroker, GrowwFeedClient
from app.broker.reconnect import ReconnectingFeed
from app.core.candle_builder import CandleBuilder
from app.core.events import EventBus
from app.core.models import Candle, Timeframe
from app.strategy.indicators import atr, ema, rsi as compute_rsi, adx as compute_adx
from app.telegram.notifier import TelegramNotifier
from app.utils.config import load_config, GrowwConfig, TelegramConfig
from app.utils.instruments import get_instrument_short_name
from app.utils.logger import get_logger
from app.utils.market_hours import (
    is_within_active_window,
    seconds_until_market_open,
    is_trading_day,
    should_square_off,
)

logger = get_logger("main_paper")


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — f50a2b57 on BNF + NF + TCS + RELIANCE + BHARTIARTL + INFY
# ═══════════════════════════════════════════════════════════════════════════════

# Capital per variant
VARIANT_CAPITAL = 500_000.0  # ₹5L

# Instruments to trade (exchange_token → name, lot_size)
# 6 instruments validated after futures costs via regime scoring
INSTRUMENTS = {
    "26009": {"name": "BANKNIFTY", "lot_size": 15, "type": "index_fut"},
    "26000": {"name": "NIFTY", "lot_size": 25, "type": "index_fut"},
    "11536": {"name": "TCS", "lot_size": 175, "type": "stock_fut"},
    "2885": {"name": "RELIANCE", "lot_size": 250, "type": "stock_fut"},
    "10604": {"name": "BHARTIARTL", "lot_size": 457, "type": "stock_fut"},
    "1594": {"name": "INFY", "lot_size": 300, "type": "stock_fut"},
}

# VIX token (for regime detection only, not traded)
VIX_TOKEN = "26017"

# All tokens to subscribe (tradeable + VIX)
ALL_TOKENS = list(INSTRUMENTS.keys()) + [VIX_TOKEN]

# Futures costs (in points) for PnL calculation
FUTURES_COSTS = {
    "26009": 28,   # BANKNIFTY
    "26000": 17,   # NIFTY
    "11536": 5,    # TCS
    "2885": 5,     # RELIANCE
    "10604": 5,    # BHARTIARTL
    "1594": 5,     # INFY
}

# No disaster SL — MR trades go against you before reversing (verified in backtest)
# 43/54 BNF trades that touched -150pt MAE still recovered to profit at 1h exit
DISASTER_SL_POINTS: dict[str, float] = {}

# Not used (no TREND variant)
TREND_SL_ATR_MULTIPLIER = 1.5


# ─── Session / Regime Enums ──────────────────────────────────────────────────

class Session(Enum):
    MORNING = "MORNING"      # 9:15 - 11:30
    MIDDAY = "MIDDAY"        # 11:30 - 13:30
    CLOSING = "CLOSING"      # 13:30 - 15:30


class Volatility(Enum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"


class Structure(Enum):
    TRENDING = "TRENDING"       # ADX > 25
    TRANSITIONING = "TRANSITIONING"  # ADX 15-25
    RANGING = "RANGING"         # ADX < 15


# ─── Session Boundaries ──────────────────────────────────────────────────────

MORNING_START = dtime(9, 15)
MORNING_END = dtime(11, 30)
MIDDAY_START = dtime(11, 30)
MIDDAY_END = dtime(13, 30)
CLOSING_START = dtime(13, 30)
CLOSING_END = dtime(15, 30)


# ═══════════════════════════════════════════════════════════════════════════════
# VARIANT DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class VariantConfig:
    """Definition of a single paper trading variant."""
    variant_id: str
    number: int
    name: str
    strategy: str           # MR, VPA, TREND, BB
    timeframe: Timeframe    # M5 or M15
    session: Session
    filter_name: str        # NONE, ATR>20, VIX>12, VIX>15, ADX>15, VWAP>0.5ATR
    instruments: list[str]  # exchange_tokens to trade
    exit_type: str          # time_1h, session_preclose, fixed_stop, adaptive
    category: str           # EXECUTE or MONITOR
    regime_conditions: dict[str, Any] = field(default_factory=dict)


# Single validated variant: f50a2b57 — MR 15m on BNF, NF, TCS, RELIANCE, BHARTIARTL, INFY
# Regime conditions are per-instrument based on forward-walk validation:
#   - MORNING only: all validated conditions are morning session
#   - Per-instrument regime gating via ALLOWED_REGIMES dict
#   - RANGING excluded: not validated in any instrument's forward walk
#
# Per-instrument ALLOWED regime conditions (from regime scorer validation data):
# Only conditions that PASSED forward-walk validation (2025-01-01 to 2026-06-12)
# are included. This ensures we only trade in conditions with proven OOS edge.
ALLOWED_REGIMES: dict[str, list[tuple[str, str, str]]] = {
    # BNF: Validated in NORMAL TRENDING/TRANSITIONING MORNING (E=250-316 pts)
    # MIDDAY: BNF profitable in validation (NORMAL TRENDING +55, TRANS +26, LOW TREND +76)
    "26009": [
        ("MORNING", "HIGH",   "TRENDING"),
        ("MORNING", "HIGH",   "TRANSITIONING"),
        ("MORNING", "NORMAL", "TRENDING"),
        ("MORNING", "NORMAL", "TRANSITIONING"),
        ("MORNING", "LOW",    "TRENDING"),
        ("MORNING", "LOW",    "TRANSITIONING"),
        ("MIDDAY",  "HIGH",   "TRENDING"),
        ("MIDDAY",  "NORMAL", "TRENDING"),
        ("MIDDAY",  "NORMAL", "TRANSITIONING"),
        ("MIDDAY",  "LOW",    "TRENDING"),
    ],
    # NF: Validated in HIGH TRANSITIONING (E=88), NORMAL TRENDING (E=188),
    #     NORMAL TRANSITIONING (E=125)
    # MIDDAY: NF validation mixed, but NORMAL TRANS +41 WR=80% and LOW TRENDING +51 WR=67%
    "26000": [
        ("MORNING", "HIGH",   "TRENDING"),
        ("MORNING", "HIGH",   "TRANSITIONING"),
        ("MORNING", "NORMAL", "TRENDING"),
        ("MORNING", "NORMAL", "TRANSITIONING"),
        ("MORNING", "LOW",    "TRENDING"),
        ("MORNING", "LOW",    "TRANSITIONING"),
        ("MIDDAY",  "NORMAL", "TRANSITIONING"),
        ("MIDDAY",  "LOW",    "TRENDING"),
    ],
    # TCS: Validated NORMAL TRENDING (E=26.7), LOW TRENDING (E=21.1),
    #      NORMAL TRANSITIONING (E=19.9), HIGH TRANSITIONING (E=9.8)
    # MIDDAY: TCS HIGH TRENDING validated (E=19.3, WR=100%, N=5)
    "11536": [
        ("MORNING", "HIGH",   "TRENDING"),
        ("MORNING", "HIGH",   "TRANSITIONING"),
        ("MORNING", "NORMAL", "TRENDING"),
        ("MORNING", "NORMAL", "TRANSITIONING"),
        ("MORNING", "LOW",    "TRENDING"),
        ("MORNING", "LOW",    "TRANSITIONING"),
        ("MIDDAY",  "HIGH",   "TRENDING"),
    ],
    # RELIANCE: Validated NORMAL TRENDING (E=12.6), HIGH TRENDING (E=12.6),
    #           NORMAL TRANSITIONING (E=8.8), LOW TRENDING (E=9.7)
    "2885": [
        ("MORNING", "HIGH",   "TRENDING"),
        ("MORNING", "HIGH",   "TRANSITIONING"),
        ("MORNING", "NORMAL", "TRENDING"),
        ("MORNING", "NORMAL", "TRANSITIONING"),
        ("MORNING", "LOW",    "TRENDING"),
        ("MORNING", "LOW",    "TRANSITIONING"),
    ],
    # BHARTIARTL: Validated NORMAL TRENDING (E=16.3), HIGH TRENDING (E=15.6),
    #             NORMAL TRANSITIONING (E=12.0), HIGH TRANSITIONING (E=6.6)
    "10604": [
        ("MORNING", "HIGH",   "TRENDING"),
        ("MORNING", "HIGH",   "TRANSITIONING"),
        ("MORNING", "NORMAL", "TRENDING"),
        ("MORNING", "NORMAL", "TRANSITIONING"),
        ("MORNING", "LOW",    "TRENDING"),
        ("MORNING", "LOW",    "TRANSITIONING"),
    ],
    # INFY: Validated NORMAL TRENDING (E=14.3), HIGH TRENDING (E=11.3),
    #       NORMAL TRANSITIONING (E=8.1), HIGH TRANSITIONING (E=8.1)
    # MIDDAY: INFY NORMAL TRENDING validated (E=10.5, WR=90%, N=10)
    "1594": [
        ("MORNING", "HIGH",   "TRENDING"),
        ("MORNING", "HIGH",   "TRANSITIONING"),
        ("MORNING", "NORMAL", "TRENDING"),
        ("MORNING", "NORMAL", "TRANSITIONING"),
        ("MORNING", "LOW",    "TRENDING"),
        ("MORNING", "LOW",    "TRANSITIONING"),
        ("MIDDAY",  "NORMAL", "TRENDING"),
    ],
}

# Per-instrument BEST exit models (from validation period 2025+ analysis)
# Used by Variant 2 to compare against Variant 1's uniform time_1h
BEST_EXIT_PER_INSTRUMENT: dict[str, str] = {
    "26009": "time_1h",          # BNF: time_1h is already best (238.7 avg)
    "26000": "rr3",              # NF: rr3 slightly better (103.7 vs 100.9)
    "11536": "time_2h",          # TCS: time_2h slightly better (20.1 vs 19.7)
    "2885": "ema13_cross",       # RELIANCE: ema13_cross tied (9.7 vs 9.6)
    "10604": "session_morning",  # BHARTIARTL: session_morning slightly better (13.2 vs 12.8)
    "1594": "time_1h",           # INFY: time_1h is already best (10.4)
}


VARIANTS: list[VariantConfig] = [
    # Variant 1: Uniform time_1h exit for all instruments
    VariantConfig(
        variant_id="f50a2b57d7a1dac5",
        number=1,
        name="MR 15m time_1h",
        strategy="MR",
        timeframe=Timeframe.M15,
        session=Session.MORNING,
        filter_name="NONE",
        instruments=list(INSTRUMENTS.keys()),  # BNF, NF, TCS, RELIANCE, BHARTIARTL, INFY
        exit_type="time_1h",
        category="EXECUTE",
        regime_conditions={},
    ),
    # Variant 2: Per-instrument best exit (A/B test against V1)
    VariantConfig(
        variant_id="f50a2b57d7a1dac5",
        number=2,
        name="MR 15m best_exit",
        strategy="MR",
        timeframe=Timeframe.M15,
        session=Session.MORNING,
        filter_name="NONE",
        instruments=list(INSTRUMENTS.keys()),  # BNF, NF, TCS, RELIANCE, BHARTIARTL, INFY
        exit_type="per_instrument",  # Special: uses BEST_EXIT_PER_INSTRUMENT dict
        category="EXECUTE",
        regime_conditions={},
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# REGIME DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════

class RegimeDetector:
    """
    Determines current market regime from indicator values.
    Updated on each candle close.
    """

    def __init__(self) -> None:
        self._vix: float = 0.0
        # Per-instrument ATR history for relative volatility
        self._atr_history: dict[str, list[float]] = {}
        self._current_adx: dict[str, float] = {}
        self._current_atr: dict[str, float] = {}

    def update_vix(self, vix_value: float) -> None:
        self._vix = vix_value

    def update_indicators(self, token: str, atr_val: float, adx_val: float) -> None:
        """Update indicator cache after candle close."""
        self._current_atr[token] = atr_val
        self._current_adx[token] = adx_val
        if token not in self._atr_history:
            self._atr_history[token] = []
        self._atr_history[token].append(atr_val)
        if len(self._atr_history[token]) > 20:
            self._atr_history[token].pop(0)

    def get_session(self) -> Session:
        """Determine current session from IST time."""
        now = datetime.now().time()
        if MORNING_START <= now < MORNING_END:
            return Session.MORNING
        elif MIDDAY_START <= now < MIDDAY_END:
            return Session.MIDDAY
        else:
            return Session.CLOSING

    def get_volatility(self, token: str) -> Volatility:
        """Determine volatility regime for an instrument."""
        history = self._atr_history.get(token, [])
        if len(history) < 5:
            return Volatility.NORMAL
        avg_atr = sum(history) / len(history)
        current = self._current_atr.get(token, avg_atr)
        if current > avg_atr * 1.5:
            return Volatility.HIGH
        elif current < avg_atr * 0.7:
            return Volatility.LOW
        return Volatility.NORMAL

    def get_structure(self, token: str) -> Structure:
        """Determine market structure from ADX."""
        adx_val = self._current_adx.get(token, 20.0)
        if adx_val > 25:
            return Structure.TRENDING
        elif adx_val < 15:
            return Structure.RANGING
        return Structure.TRANSITIONING

    @property
    def vix(self) -> float:
        return self._vix

    def check_regime(self, variant: VariantConfig, token: str) -> bool:
        """
        Check if current regime is allowed for this instrument.
        Uses ALLOWED_REGIMES per-instrument table from backtest scoring.
        Falls back to session-only check if instrument not in table.
        """
        now = datetime.now().time()

        # Determine current session
        if MORNING_START <= now < MORNING_END:
            session = "MORNING"
        elif MIDDAY_START <= now < MIDDAY_END:
            session = "MIDDAY"
        else:
            return False  # CLOSING session — no new entries

        # Check per-instrument allowed regimes
        allowed = ALLOWED_REGIMES.get(token)
        if allowed:
            volatility = self.get_volatility(token).value
            structure = self.get_structure(token).value
            return (session, volatility, structure) in allowed

        # Fallback: no restriction beyond session
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE — PostgreSQL trade store for multi-day carry-forward
# ═══════════════════════════════════════════════════════════════════════════════

import json
import os
from pathlib import Path

PAPER_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id SERIAL PRIMARY KEY,
    position_id TEXT NOT NULL,
    variant_number INTEGER NOT NULL,
    token TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    exit_price DOUBLE PRECISION NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP NOT NULL,
    pnl_points DOUBLE PRECISION NOT NULL,
    pnl_after_costs DOUBLE PRECISION NOT NULL,
    cost_deducted DOUBLE PRECISION NOT NULL,
    lot_size INTEGER NOT NULL,
    exit_reason TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_variant ON paper_trades(variant_number);
CREATE INDEX IF NOT EXISTS idx_paper_trades_exit_time ON paper_trades(exit_time);
"""


class PaperTradeStore:
    """
    PostgreSQL persistence for paper trades.
    Writes each closed trade immediately. On startup, loads cumulative PnL.

    Falls back to JSON file if DATABASE_URL is not set (local dev).
    """

    def __init__(self) -> None:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / ".env")

        self._database_url = os.getenv("DATABASE_URL", "")
        self._use_postgres = (
            self._database_url.startswith("postgresql://")
            or self._database_url.startswith("postgres://")
        )
        self._pool = None
        self._json_path = Path(__file__).parent.parent / "data" / "paper_trades.json"

        if self._use_postgres:
            self._init_postgres()
        else:
            logger.info("PaperTradeStore: using JSON fallback (no DATABASE_URL)")
            self._json_path.parent.mkdir(parents=True, exist_ok=True)

    def _init_postgres(self) -> None:
        """Initialize Postgres connection pool and create table."""
        try:
            import psycopg2
            import psycopg2.pool
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1, maxconn=5, dsn=self._database_url,
            )
            conn = self._pool.getconn()
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(PAPER_TRADES_TABLE)
            finally:
                self._pool.putconn(conn)
            logger.info("PaperTradeStore: PostgreSQL connected")
        except Exception as e:
            logger.error("PaperTradeStore: Postgres init failed (%s), falling back to JSON", e)
            self._use_postgres = False
            self._pool = None

    def save_trade(self, trade_data: dict) -> None:
        """Persist a single closed trade."""
        if self._use_postgres:
            self._save_postgres(trade_data)
        else:
            self._save_json(trade_data)

    def get_all_variant_pnls(self) -> dict[int, float]:
        """Get cumulative PnL per variant (all time)."""
        if self._use_postgres:
            return self._get_pnls_postgres()
        return self._get_pnls_json()

    # ─── PostgreSQL methods ──────────────────────────────────────

    def _save_postgres(self, t: dict) -> None:
        """INSERT trade into postgres."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO paper_trades
                       (position_id, variant_number, token, instrument, direction,
                        entry_price, exit_price, entry_time, exit_time,
                        pnl_points, pnl_after_costs, cost_deducted, lot_size, exit_reason)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        t["position_id"], t["variant_number"], t["token"],
                        t["instrument"], t["direction"],
                        t["entry_price"], t["exit_price"],
                        t["entry_time"], t["exit_time"],
                        t["pnl_points"], t["pnl_after_costs"],
                        t["cost_deducted"], t["lot_size"], t["exit_reason"],
                    ),
                )
            conn.commit()
        except Exception as e:
            logger.error("Postgres save failed: %s", e)
            conn.rollback()
        finally:
            self._pool.putconn(conn)

    def _get_pnls_postgres(self) -> dict[int, float]:
        """Query cumulative PnL per variant from postgres."""
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT variant_number, SUM(pnl_after_costs) as total "
                    "FROM paper_trades GROUP BY variant_number"
                )
                rows = cur.fetchall()
            return {row[0]: row[1] for row in rows}
        except Exception as e:
            logger.error("Postgres query failed: %s", e)
            return {}
        finally:
            self._pool.putconn(conn)

    # ─── JSON fallback methods ───────────────────────────────────

    def _save_json(self, trade_data: dict) -> None:
        """Append trade to JSON file."""
        trades = self._load_json()
        trades.append(trade_data)
        try:
            with open(self._json_path, "w") as f:
                json.dump(trades, f, indent=2, default=str)
        except IOError as e:
            logger.error("JSON save failed: %s", e)

    def _get_pnls_json(self) -> dict[int, float]:
        """Calculate cumulative PnL from JSON file."""
        trades = self._load_json()
        pnls: dict[int, float] = {}
        for t in trades:
            vn = t.get("variant_number", 0)
            pnls[vn] = pnls.get(vn, 0.0) + t.get("pnl_after_costs", 0.0)
        return pnls

    def _load_json(self) -> list[dict]:
        """Load trades from JSON file."""
        if not self._json_path.exists():
            return []
        try:
            with open(self._json_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []



# ═══════════════════════════════════════════════════════════════════════════════
# TRADE MANAGER — Position tracking, entries, and exits
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PaperPosition:
    """A single open paper trade."""
    position_id: str
    variant_number: int
    token: str
    direction: str        # "LONG" or "SHORT"
    entry_price: float
    entry_time: datetime
    lot_size: int
    exit_type: str        # time_1h, time_2h, session_morning, rr3, ema13_cross, etc.
    stop_loss: float = 0.0
    # Time-based exit: when to exit
    exit_at: datetime | None = None
    # RR3 target price (only used when exit_type == "rr3")
    rr3_target: float = 0.0
    # Tracking
    max_price: float = 0.0
    min_price: float = 0.0


@dataclass
class ClosedTrade:
    """A completed trade record."""
    position_id: str
    variant_number: int
    token: str
    direction: str
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    pnl_points: float
    pnl_after_costs: float
    lot_size: int
    exit_reason: str
    max_favorable: float = 0.0   # best price reached during trade
    max_adverse: float = 0.0     # worst price reached during trade


class TradeManager:
    """
    Manages open positions and exits for all variants.
    Tracks PnL per variant separately.
    Persists every closed trade to JSON for multi-day carry-forward.
    """

    def __init__(self, store: PaperTradeStore | None = None, candle_builder=None) -> None:
        self._store = store or PaperTradeStore()
        self._candle_builder = candle_builder

        # Open positions: variant_number → list of PaperPosition
        self.open_positions: dict[int, list[PaperPosition]] = {
            v.number: [] for v in VARIANTS
        }
        # Closed trades (today's session only, in-memory)
        self.closed_trades: dict[int, list[ClosedTrade]] = {
            v.number: [] for v in VARIANTS
        }
        # Cumulative PnL from ALL historical trades (loaded from disk)
        self._historical_pnl: dict[int, float] = self._store.get_all_variant_pnls()
        # Latest prices
        self._prices: dict[str, float] = {}

        if any(pnl != 0 for pnl in self._historical_pnl.values()):
            logger.info("Loaded historical PnL:")
            for vn, pnl in sorted(self._historical_pnl.items()):
                if pnl != 0:
                    logger.info("  V%d: %+.1f pts cumulative", vn, pnl)

    def update_price(self, token: str, price: float) -> None:
        self._prices[token] = price

    def get_price(self, token: str) -> float:
        return self._prices.get(token, 0.0)

    def has_open_position(self, variant_number: int, token: str, direction: str) -> bool:
        """Check if variant already has an open position for this token+direction."""
        for pos in self.open_positions[variant_number]:
            if pos.token == token and pos.direction == direction:
                return True
        return False

    def open_trade(
        self,
        variant: VariantConfig,
        token: str,
        direction: str,
        entry_price: float,
        atr_val: float,
    ) -> PaperPosition:
        """Open a new paper position."""
        now = datetime.now()
        lot_size = INSTRUMENTS[token]["lot_size"]

        # Calculate stop loss — only for TREND variant (V8)
        # MR/VPA/BB variants do NOT use SL (mean reversion trades go against you before reversing)
        stop_loss = 0.0
        if variant.strategy == "TREND":
            sl_distance = atr_val * TREND_SL_ATR_MULTIPLIER
            stop_loss = entry_price - sl_distance if direction == "LONG" else entry_price + sl_distance

        # Calculate exit time based on exit_type
        exit_at = None
        actual_exit_type = variant.exit_type

        # Per-instrument exit: resolve the real exit type for this token
        if variant.exit_type == "per_instrument":
            actual_exit_type = BEST_EXIT_PER_INSTRUMENT.get(token, "time_1h")

        if actual_exit_type == "time_1h":
            exit_at = now + timedelta(minutes=60)
        elif actual_exit_type == "time_2h":
            exit_at = now + timedelta(minutes=120)
        elif actual_exit_type == "session_morning":
            exit_at = datetime.combine(now.date(), MORNING_END)  # 11:30
            if now >= exit_at:
                exit_at = now + timedelta(minutes=60)  # fallback if entered after 11:30
        elif actual_exit_type == "session_preclose":
            exit_at = datetime.combine(now.date(), dtime(15, 15))
        elif actual_exit_type == "adaptive":
            exit_at = now + timedelta(minutes=90)
        elif actual_exit_type == "rr3":
            # RR3: exit at 3× ATR from entry. Set exit_at as max time (2h fallback)
            exit_at = now + timedelta(minutes=120)  # max hold time fallback
        elif actual_exit_type == "ema13_cross":
            # EMA13 cross: no fixed time, use 4h max fallback
            exit_at = now + timedelta(minutes=240)  # max hold time fallback
        # else: no time exit (relies on other checks)

        # RR3 target price
        rr3_target = 0.0
        if actual_exit_type == "rr3":
            risk = atr_val  # 1R = 1 ATR
            if direction == "LONG":
                rr3_target = entry_price + (3.0 * risk)
            else:
                rr3_target = entry_price - (3.0 * risk)

        position = PaperPosition(
            position_id=f"PP-{uuid.uuid4().hex[:8]}",
            variant_number=variant.number,
            token=token,
            direction=direction,
            entry_price=entry_price,
            entry_time=now,
            lot_size=lot_size,
            exit_type=actual_exit_type,
            stop_loss=stop_loss,
            exit_at=exit_at,
            max_price=entry_price,
            min_price=entry_price,
            rr3_target=rr3_target,
        )
        self.open_positions[variant.number].append(position)
        return position

    def close_trade(self, position: PaperPosition, exit_price: float, reason: str) -> ClosedTrade:
        """Close a position, record PnL, and persist to disk."""
        now = datetime.now()

        # PnL in points
        if position.direction == "LONG":
            pnl_points = exit_price - position.entry_price
        else:
            pnl_points = position.entry_price - exit_price

        # Deduct futures costs
        cost = FUTURES_COSTS.get(position.token, 5)
        pnl_after_costs = pnl_points - cost

        trade = ClosedTrade(
            position_id=position.position_id,
            variant_number=position.variant_number,
            token=position.token,
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=exit_price,
            entry_time=position.entry_time,
            exit_time=now,
            pnl_points=pnl_points,
            pnl_after_costs=pnl_after_costs,
            lot_size=position.lot_size,
            exit_reason=reason,
            max_favorable=position.max_price if position.direction == "LONG" else position.min_price,
            max_adverse=position.min_price if position.direction == "LONG" else position.max_price,
        )
        self.closed_trades[position.variant_number].append(trade)

        # Update cumulative historical PnL
        vn = position.variant_number
        self._historical_pnl[vn] = self._historical_pnl.get(vn, 0.0) + pnl_after_costs

        # Persist to disk immediately
        self._store.save_trade({
            "position_id": trade.position_id,
            "variant_number": trade.variant_number,
            "token": trade.token,
            "instrument": INSTRUMENTS.get(trade.token, {}).get("name", trade.token),
            "direction": trade.direction,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "entry_time": trade.entry_time.isoformat(),
            "exit_time": trade.exit_time.isoformat(),
            "pnl_points": round(trade.pnl_points, 2),
            "pnl_after_costs": round(trade.pnl_after_costs, 2),
            "cost_deducted": cost,
            "lot_size": trade.lot_size,
            "exit_reason": trade.exit_reason,
        })

        # Remove from open
        self.open_positions[position.variant_number] = [
            p for p in self.open_positions[position.variant_number]
            if p.position_id != position.position_id
        ]
        return trade

    def check_exits(self) -> list[tuple[PaperPosition, float, str]]:
        """Check all open positions for exit conditions. Returns list of (pos, price, reason)."""
        exits = []
        now = datetime.now()

        for variant_num, positions in self.open_positions.items():
            for pos in list(positions):
                price = self._prices.get(pos.token, 0.0)
                if price <= 0:
                    continue

                # Update max/min tracking
                pos.max_price = max(pos.max_price, price)
                pos.min_price = min(pos.min_price, price) if pos.min_price > 0 else price

                # 1. Stop-loss check (only if SL is set — MR variants have SL=0)
                if pos.stop_loss > 0:
                    if pos.direction == "LONG" and price <= pos.stop_loss:
                        exits.append((pos, price, "disaster_sl"))
                        continue
                    if pos.direction == "SHORT" and price >= pos.stop_loss:
                        exits.append((pos, price, "disaster_sl"))
                        continue

                # 2. RR3 target hit (for rr3 exit type)
                if pos.exit_type == "rr3" and pos.rr3_target > 0:
                    if pos.direction == "LONG" and price >= pos.rr3_target:
                        exits.append((pos, price, "rr3_target"))
                        continue
                    if pos.direction == "SHORT" and price <= pos.rr3_target:
                        exits.append((pos, price, "rr3_target"))
                        continue

                # 3. EMA13 cross exit (check on tick)
                if pos.exit_type == "ema13_cross":
                    # Only check after minimum 15 min hold (avoid whipsaw)
                    hold_minutes = (now - pos.entry_time).total_seconds() / 60
                    if hold_minutes >= 15:
                        ema13 = self._get_ema13(pos.token)
                        if ema13 and ema13 > 0:
                            if pos.direction == "LONG" and price < ema13:
                                exits.append((pos, price, "ema13_cross"))
                                continue
                            if pos.direction == "SHORT" and price > ema13:
                                exits.append((pos, price, "ema13_cross"))
                                continue

                # 4. Time-based exit (fallback for all types)
                if pos.exit_at and now >= pos.exit_at:
                    exits.append((pos, price, f"time_exit ({pos.exit_type})"))
                    continue

        return exits

    def _get_ema13(self, token: str) -> float | None:
        """Get current EMA13 for ema13_cross exit. Uses candle builder history."""
        if not self._candle_builder:
            return None
        history = self._candle_builder.get_history(token, Timeframe.M5)
        if len(history) < 15:
            return None
        closes = [c.close for c in history[-30:]]
        return ema(closes, 13)

    def square_off_all(self) -> list[tuple[PaperPosition, float, str]]:
        """Force close all open positions (end of day)."""
        exits = []
        for variant_num, positions in self.open_positions.items():
            for pos in list(positions):
                price = self._prices.get(pos.token, pos.entry_price)
                exits.append((pos, price, "eod_square_off"))
        return exits

    def get_variant_pnl(self, variant_number: int) -> float:
        """Total cumulative PnL in points for a variant (all days)."""
        return self._historical_pnl.get(variant_number, 0.0)

    def get_variant_today_pnl(self, variant_number: int) -> float:
        """Today's session PnL only."""
        return sum(t.pnl_after_costs for t in self.closed_trades[variant_number])

    def get_variant_stats(self, variant_number: int) -> dict:
        """Get stats for a variant (today's session + cumulative)."""
        trades = self.closed_trades[variant_number]
        today_pnl = sum(t.pnl_after_costs for t in trades)
        cumulative_pnl = self._historical_pnl.get(variant_number, 0.0)
        if not trades:
            return {
                "trades": 0, "win_rate": 0, "pnl_today": 0,
                "pnl_cumulative": cumulative_pnl,
                "open": len(self.open_positions[variant_number]),
            }
        wins = sum(1 for t in trades if t.pnl_after_costs > 0)
        return {
            "trades": len(trades),
            "win_rate": wins / len(trades) * 100,
            "pnl_today": today_pnl,
            "pnl_cumulative": cumulative_pnl,
            "open": len(self.open_positions[variant_number]),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL EVALUATOR — Strategy logic per variant type
# ═══════════════════════════════════════════════════════════════════════════════

class SignalEvaluator:
    """
    Evaluates entry signals on each candle close.
    Implements the specific strategies for each variant type.
    """

    def __init__(self, candle_builder: CandleBuilder, regime: RegimeDetector) -> None:
        self._candle_builder = candle_builder
        self._regime = regime
        # Store previous RSI per (token, timeframe) for crossover detection
        self._prev_rsi: dict[tuple[str, str], float] = {}

    def evaluate_mr(
        self, token: str, timeframe: Timeframe, variant: VariantConfig
    ) -> str | None:
        """
        Mean Reversion (CANDLE_CLOSE mode):
        - LONG: price < EMA20 by 0.3×ATR AND prev_RSI < 35 AND curr_RSI >= 35
        - SHORT: price > EMA20 by 0.3×ATR AND prev_RSI > 65 AND curr_RSI <= 65

        Returns "LONG", "SHORT", or None.
        """
        history = self._candle_builder.get_history(token, timeframe)
        if len(history) < 30:
            return None

        candles = history[-50:]
        closes = [c.close for c in candles]
        price = closes[-1]

        # Compute indicators
        ema20_val = ema(closes, 20)
        atr_val = atr(candles, 14)
        rsi_val = compute_rsi(closes, 14)

        if ema20_val is None or atr_val is None or rsi_val is None:
            return None

        # Get previous RSI (from last candle evaluation)
        key = (token, timeframe.value)
        prev_rsi_val = self._prev_rsi.get(key)
        self._prev_rsi[key] = rsi_val

        if prev_rsi_val is None:
            return None

        # Update regime detector
        adx_val = compute_adx(candles, 14) or 20.0
        self._regime.update_indicators(token, atr_val, adx_val)

        # Apply variant-specific filter
        if variant.filter_name == "ATR>20" and atr_val <= 20:
            return None
        if variant.filter_name == "VIX>12" and self._regime.vix < 12:
            return None
        if variant.filter_name == "VIX>15" and self._regime.vix < 15:
            return None

        # LONG: price < EMA20 by 0.3×ATR AND RSI crosses UP through 35
        distance = abs(price - ema20_val)
        threshold = 0.3 * atr_val

        if price < ema20_val and distance >= threshold:
            if prev_rsi_val < 35 and rsi_val >= 35:
                return "LONG"

        # SHORT: price > EMA20 by 0.3×ATR AND RSI crosses DOWN through 65
        if price > ema20_val and distance >= threshold:
            if prev_rsi_val > 65 and rsi_val <= 65:
                return "SHORT"

        return None

    def evaluate_vpa(
        self, token: str, timeframe: Timeframe, variant: VariantConfig
    ) -> str | None:
        """
        Volume Price Analysis:
        - LONG: Price near support (BB lower) + volume spike + RSI < 40
        - SHORT: Price near resistance (BB upper) + volume spike + RSI > 60
        """
        history = self._candle_builder.get_history(token, timeframe)
        if len(history) < 30:
            return None

        candles = history[-50:]
        closes = [c.close for c in candles]
        price = closes[-1]

        from app.strategy.indicators import bollinger_bands, vwap as compute_vwap

        bb = bollinger_bands(candles, 20, 2.0)
        rsi_val = compute_rsi(closes, 14)
        atr_val = atr(candles, 14)
        adx_val = compute_adx(candles, 14) or 20.0

        if bb is None or rsi_val is None or atr_val is None:
            return None

        self._regime.update_indicators(token, atr_val, adx_val)

        bb_upper, bb_middle, bb_lower = bb

        # VWAP filter for variant #10
        if variant.filter_name == "VWAP>0.5ATR":
            vwap_val = compute_vwap(candles[-20:])
            if vwap_val is None:
                return None
            if abs(price - vwap_val) < 0.5 * atr_val:
                return None

        # Volume spike: current volume > 1.5x average
        recent_vols = [c.volume for c in candles[-20:-1]]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 1
        current_vol = candles[-1].volume
        vol_spike = current_vol > avg_vol * 1.5

        if not vol_spike:
            return None

        # LONG: Price near BB lower + RSI < 40
        if price <= bb_lower + 0.1 * (bb_middle - bb_lower) and rsi_val < 40:
            return "LONG"

        # SHORT: Price near BB upper + RSI > 60
        if price >= bb_upper - 0.1 * (bb_upper - bb_middle) and rsi_val > 60:
            return "SHORT"

        return None

    def evaluate_trend(
        self, token: str, timeframe: Timeframe, variant: VariantConfig
    ) -> str | None:
        """
        Trend Following:
        - LONG: EMA9 > EMA21 AND ADX > 25 AND price above EMA9
        - SHORT: EMA9 < EMA21 AND ADX > 25 AND price below EMA9
        """
        history = self._candle_builder.get_history(token, timeframe)
        if len(history) < 30:
            return None

        candles = history[-50:]
        closes = [c.close for c in candles]
        price = closes[-1]

        ema9_val = ema(closes, 9)
        ema21_val = ema(closes, 21)
        adx_val = compute_adx(candles, 14)
        atr_val = atr(candles, 14)

        if ema9_val is None or ema21_val is None or adx_val is None or atr_val is None:
            return None

        self._regime.update_indicators(token, atr_val, adx_val)

        if adx_val < 25:
            return None

        if ema9_val > ema21_val and price > ema9_val:
            return "LONG"
        if ema9_val < ema21_val and price < ema9_val:
            return "SHORT"

        return None

    def evaluate_bb(
        self, token: str, timeframe: Timeframe, variant: VariantConfig
    ) -> str | None:
        """
        Bollinger Band Squeeze Breakout:
        - Detect squeeze (BB inside KC) then breakout direction
        """
        history = self._candle_builder.get_history(token, timeframe)
        if len(history) < 30:
            return None

        candles = history[-50:]
        closes = [c.close for c in candles]
        price = closes[-1]

        from app.strategy.indicators import bollinger_bands, is_squeeze

        bb = bollinger_bands(candles, 20, 2.0)
        squeeze = is_squeeze(candles)
        adx_val = compute_adx(candles, 14)
        atr_val = atr(candles, 14)

        if bb is None or squeeze is None or adx_val is None or atr_val is None:
            return None

        self._regime.update_indicators(token, atr_val, adx_val)

        # ADX filter for variant #9
        if variant.filter_name == "ADX>15" and adx_val <= 15:
            return None

        bb_upper, bb_middle, bb_lower = bb

        # Look for squeeze release: was in squeeze previously, now breaking out
        prev_candles = candles[:-1]
        prev_squeeze = is_squeeze(prev_candles) if len(prev_candles) >= 20 else None

        if prev_squeeze and not squeeze:
            # Squeeze just released! Check direction
            if price > bb_upper:
                return "LONG"
            elif price < bb_lower:
                return "SHORT"

        return None

    def evaluate(self, variant: VariantConfig, token: str) -> str | None:
        """Route to correct strategy evaluator."""
        if variant.strategy == "MR":
            return self.evaluate_mr(token, variant.timeframe, variant)
        elif variant.strategy == "VPA":
            return self.evaluate_vpa(token, variant.timeframe, variant)
        elif variant.strategy == "TREND":
            return self.evaluate_trend(token, variant.timeframe, variant)
        elif variant.strategy == "BB":
            return self.evaluate_bb(token, variant.timeframe, variant)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM ALERTS — Paper Trading specific
# ═══════════════════════════════════════════════════════════════════════════════

class PaperTelegramNotifier:
    """
    Sends Telegram alerts for the paper trading system.
    Includes: entry/exit, MFE analysis, streaks, risk warnings,
    periodic summaries, and EOD report.
    """

    def __init__(self, bot_token: str, chat_ids: list[str]) -> None:
        self._bot_token = bot_token
        self._chat_ids = [cid for cid in chat_ids if cid]
        self._enabled = bool(bot_token and self._chat_ids)
        self._session_start = time.time()
        self._trades_opened = 0
        self._trades_closed = 0
        self._eod_sent_today = False

    def send(self, text: str) -> None:
        """Send message to all configured chat IDs."""
        if not self._enabled:
            return
        import json as _json
        import urllib.request as _urllib
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        for chat_id in self._chat_ids:
            try:
                payload = _json.dumps({
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                }).encode("utf-8")
                req = _urllib.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                _urllib.urlopen(req, timeout=10)
            except Exception as e:
                logger.error("Telegram send failed: %s", e)

    def notify_entry(self, variant: VariantConfig, pos: PaperPosition) -> None:
        self._trades_opened += 1
        name = INSTRUMENTS[pos.token]["name"]
        msg = (
            f"{'─'*25}\n"
            f"📥 ENTRY — V{variant.number} [{variant.category}]\n"
            f"{'─'*25}\n\n"
            f"Variant: {variant.name}\n"
            f"Instrument: {name}\n"
            f"Direction: {pos.direction}\n"
            f"Price: {pos.entry_price:.2f}\n"
            f"SL: {pos.stop_loss:.2f}\n"
            f"Exit: {pos.exit_type}\n"
            f"Time: {pos.entry_time.strftime('%H:%M:%S')}\n"
            f"\n(Trade #{self._trades_opened} today)"
        )
        self.send(msg)

    def notify_exit(self, variant: VariantConfig, trade: ClosedTrade, trade_mgr: TradeManager) -> None:
        self._trades_closed += 1
        name = INSTRUMENTS[trade.token]["name"]
        emoji = "✅" if trade.pnl_after_costs > 0 else "❌"
        hold_min = (trade.exit_time - trade.entry_time).total_seconds() / 60

        # MFE analysis
        mfe_info = ""
        if trade.max_favorable > 0:
            if trade.direction == "LONG":
                peak_pnl = trade.max_favorable - trade.entry_price
                mfe_info = f"Peak: +{peak_pnl:.1f} pts (gave back {peak_pnl - trade.pnl_points:.1f})\n"
            else:
                peak_pnl = trade.entry_price - trade.max_favorable
                mfe_info = f"Peak: +{peak_pnl:.1f} pts (gave back {peak_pnl - trade.pnl_points:.1f})\n"

        # Streak for this variant
        streak = self._get_streak(trade_mgr.closed_trades[variant.number])

        # Running totals for this variant
        v_stats = trade_mgr.get_variant_stats(variant.number)

        msg = (
            f"{'─'*25}\n"
            f"{emoji} EXIT — V{variant.number} [{variant.category}]\n"
            f"{'─'*25}\n\n"
            f"Variant: {variant.name}\n"
            f"Instrument: {name}\n"
            f"Direction: {trade.direction}\n"
            f"Entry: {trade.entry_price:.2f}\n"
            f"Exit: {trade.exit_price:.2f}\n"
            f"{mfe_info}"
            f"PnL: {trade.pnl_points:+.1f} pts (net: {trade.pnl_after_costs:+.1f})\n"
            f"Reason: {trade.exit_reason}\n"
            f"Hold: {hold_min:.0f} min\n"
        )

        # Add variant running stats
        msg += (
            f"\n{'─'*25}\n"
            f"V{variant.number} today: {v_stats['trades']} trades | "
            f"WR {v_stats['win_rate']:.0f}% | {v_stats['pnl_today']:+.0f} pts\n"
        )
        if streak:
            msg += f"Streak: {streak}\n"

        # Risk warning if big drawdown
        total_today = sum(
            trade_mgr.get_variant_stats(v.number)['pnl_today'] for v in VARIANTS
        )
        if total_today < -100:
            msg += f"\n⚠️ RISK: Total down {total_today:.0f} pts today!"

        self.send(msg)

    def notify_startup(self, trade_mgr: TradeManager) -> None:
        now = datetime.now()
        # Show cumulative PnL if any
        pnls = trade_mgr._historical_pnl
        cumulative_total = sum(pnls.values())
        cum_info = ""
        if cumulative_total != 0:
            cum_info = f"\nCumulative PnL: {cumulative_total:+.0f} pts\n"

        msg = (
            f"{'═'*30}\n"
            f"📊 PAPER TRADER STARTED\n"
            f"{now.strftime('%d %b %Y')} | {now.strftime('%H:%M')}\n"
            f"{'═'*30}\n\n"
            f"Variant: f50a2b57 (MR 15m)\n"
            f"Instruments: {', '.join(v['name'] for v in INSTRUMENTS.values())}\n"
            f"Sessions: Morning (all) + Midday (BNF, NF, TCS, INFY)\n"
            f"Exit: time_1h (60 min)\n"
            f"Regime: per-instrument validated conditions\n"
            f"{cum_info}\n"
            f"Waiting for candle data..."
        )
        self.send(msg)

    def notify_summary(self, trade_mgr: TradeManager) -> None:
        """Periodic portfolio summary."""
        now = datetime.now()
        msg = (
            f"{'═'*30}\n"
            f"📊 PAPER TRADING UPDATE — {now.strftime('%H:%M')}\n"
            f"{'═'*30}\n\n"
            f"{'V#':<4}{'Name':<20}{'Tr':<4}{'WR':<6}{'Today':<8}{'Total':<8}\n"
            f"{'─'*50}\n"
        )
        total_today = 0.0
        total_cumulative = 0.0
        for v in VARIANTS:
            stats = trade_mgr.get_variant_stats(v.number)
            today_str = f"{stats['pnl_today']:+.0f}" if stats['trades'] > 0 else "—"
            cum_str = f"{stats['pnl_cumulative']:+.0f}" if stats['pnl_cumulative'] != 0 else "0"
            wr_str = f"{stats['win_rate']:.0f}%" if stats['trades'] > 0 else "—"
            marker = "⚡" if v.category == "EXECUTE" else "👁"
            msg += f"{marker}{v.number:<3}{v.name[:18]:<20}{stats['trades']:<4}{wr_str:<6}{today_str:<8}{cum_str}\n"
            total_today += stats['pnl_today']
            total_cumulative += stats['pnl_cumulative']

        msg += f"{'─'*50}\n"
        msg += f"Today: {total_today:+.1f} pts | Cumulative: {total_cumulative:+.1f} pts\n"
        open_count = sum(len(positions) for positions in trade_mgr.open_positions.values())
        msg += f"Open positions: {open_count}\n"

        # Risk warning
        if total_today < -100:
            msg += f"\n⚠️ DOWN {total_today:.0f} pts today — check regime conditions"

        self.send(msg)

    def notify_eod(self, trade_mgr: TradeManager) -> None:
        """End-of-day report at 3:35 PM."""
        if self._eod_sent_today:
            return
        self._eod_sent_today = True

        now = datetime.now()
        msg = (
            f"{'═'*30}\n"
            f"📊 END OF DAY REPORT\n"
            f"{now.strftime('%d %b %Y')} | Market Closed\n"
            f"{'═'*30}\n\n"
            f"{'V#':<4}{'Name':<20}{'Tr':<4}{'WR':<6}{'PnL':<8}\n"
            f"{'─'*46}\n"
        )

        total_today = 0.0
        best_variant = (0, -9999.0)
        worst_variant = (0, 9999.0)

        for v in VARIANTS:
            stats = trade_mgr.get_variant_stats(v.number)
            pnl = stats['pnl_today']
            total_today += pnl
            if pnl > best_variant[1]:
                best_variant = (v.number, pnl)
            if pnl < worst_variant[1]:
                worst_variant = (v.number, pnl)

            pnl_str = f"{pnl:+.0f}" if stats['trades'] > 0 else "—"
            wr_str = f"{stats['win_rate']:.0f}%" if stats['trades'] > 0 else "—"
            marker = "⚡" if v.category == "EXECUTE" else "👁"
            msg += f"{marker}{v.number:<3}{v.name[:18]:<20}{stats['trades']:<4}{wr_str:<6}{pnl_str}\n"

        msg += f"{'─'*46}\n"

        # Verdict
        if total_today > 0:
            verdict = "🟢 GREEN DAY"
        elif total_today < 0:
            verdict = "🔴 RED DAY"
        else:
            verdict = "⚪ FLAT DAY"

        msg += (
            f"\n{verdict}: {total_today:+.1f} pts net\n\n"
            f"Best: V{best_variant[0]} ({best_variant[1]:+.0f} pts)\n"
            f"Worst: V{worst_variant[0]} ({worst_variant[1]:+.0f} pts)\n\n"
            f"Trades today: {self._trades_opened} opened, {self._trades_closed} closed\n"
            f"Cumulative: {sum(trade_mgr._historical_pnl.values()):+.0f} pts (all time)\n"
            f"\n{'═'*30}\n"
            f"See you tomorrow."
        )
        self.send(msg)

    def notify_shutdown(self, trade_mgr: TradeManager) -> None:
        """Message when bot stops."""
        duration_min = (time.time() - self._session_start) / 60
        msg = (
            f"{'═'*30}\n"
            f"🛑 PAPER TRADER STOPPED\n"
            f"{'═'*30}\n\n"
            f"Session: {duration_min:.0f} min\n"
            f"Trades: {self._trades_opened} opened, {self._trades_closed} closed\n"
            f"Cumulative: {sum(trade_mgr._historical_pnl.values()):+.0f} pts\n"
        )
        self.send(msg)

    def check_eod(self, trade_mgr: TradeManager) -> None:
        """Check if it's time to send EOD report (3:35 PM)."""
        now = datetime.now().time()
        if dtime(15, 35) <= now <= dtime(15, 37) and not self._eod_sent_today:
            self.notify_eod(trade_mgr)

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of new day)."""
        self._trades_opened = 0
        self._trades_closed = 0
        self._eod_sent_today = False

    @staticmethod
    def _get_streak(closed_trades: list[ClosedTrade]) -> str:
        """Get current win/loss streak."""
        if not closed_trades:
            return ""
        streak_count = 0
        streak_type = None
        for trade in reversed(closed_trades):
            current = "W" if trade.pnl_after_costs > 0 else "L"
            if streak_type is None:
                streak_type = current
                streak_count = 1
            elif current == streak_type:
                streak_count += 1
            else:
                break
        if streak_count >= 2:
            label = "wins" if streak_type == "W" else "losses"
            return f"{streak_count} {label} in a row"
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

class PaperTradingOrchestrator:
    """
    Wires everything together. Handles:
    - Tick ingestion via EventBus
    - Candle completion events
    - Signal evaluation per variant
    - Exit management
    - Telegram notifications
    """

    def __init__(self) -> None:
        self._config = load_config()
        self._event_bus = EventBus()
        self._candle_builder = CandleBuilder(
            self._event_bus,
            timeframes=[Timeframe.M5, Timeframe.M15],
        )
        self._regime = RegimeDetector()
        self._signal_evaluator = SignalEvaluator(self._candle_builder, self._regime)
        self._trade_mgr = TradeManager(candle_builder=self._candle_builder)
        self._telegram = PaperTelegramNotifier(
            bot_token=self._config.telegram.bot_token,
            chat_ids=self._config.telegram.chat_ids,
        )
        self._running = False
        self._last_summary_time = 0.0
        self._summary_interval = 1800  # 30 minutes
        self._squared_off_today = False
        self._current_date = datetime.now().date()  # Track day for reset

    def start(self) -> None:
        """Initialize and start the paper trading system."""
        self._running = True

        # Subscribe to events
        self._event_bus.subscribe("tick", self._on_tick)
        self._event_bus.subscribe("candle", self._on_candle)

        # Wire candle builder
        self._event_bus.subscribe("tick", self._candle_builder.on_tick)

        self._telegram.notify_startup(self._trade_mgr)
        self._last_summary_time = time.time()
        logger.info("PaperTradingOrchestrator started")

    def stop(self) -> None:
        """Shutdown gracefully."""
        self._running = False
        self._event_bus.unsubscribe("tick", self._on_tick)
        self._event_bus.unsubscribe("candle", self._on_candle)
        self._event_bus.unsubscribe("tick", self._candle_builder.on_tick)

        # Send final summary + shutdown message
        self._telegram.notify_summary(self._trade_mgr)
        self._telegram.notify_shutdown(self._trade_mgr)
        logger.info("PaperTradingOrchestrator stopped")

    def _on_tick(self, tick: Tick) -> None:
        """Process each incoming tick."""
        # Daily reset check (if process runs across days)
        today = datetime.now().date()
        if today != self._current_date:
            self._current_date = today
            self._squared_off_today = False
            self._telegram.reset_daily()
            logger.info("New trading day detected — counters reset")

        # Update VIX
        if tick.exchange_token == VIX_TOKEN:
            self._regime.update_vix(tick.ltp)
            return

        # Update price in trade manager
        if tick.exchange_token in INSTRUMENTS:
            self._trade_mgr.update_price(tick.exchange_token, tick.ltp)

        # Check exit conditions on every tick
        self._check_exits()

        # Check for end-of-day square off
        if should_square_off() and not self._squared_off_today:
            self._square_off_all()
            self._squared_off_today = True

        # Periodic summary
        now = time.time()
        if now - self._last_summary_time >= self._summary_interval:
            self._last_summary_time = now
            if is_trading_day() and MORNING_START <= datetime.now().time() <= CLOSING_END:
                self._telegram.notify_summary(self._trade_mgr)

        # EOD report at 3:35 PM
        self._telegram.check_eod(self._trade_mgr)

    def _on_candle(self, candle: Candle) -> None:
        """
        On each 15m candle close, evaluate f50a2b57 for each instrument.
        Session (MORNING/MIDDAY) and regime (volatility/structure) are checked
        per-instrument via ALLOWED_REGIMES before taking any trade.
        """
        if not self._running:
            return

        token = candle.exchange_token
        if token not in INSTRUMENTS:
            return

        # Determine which variants to evaluate based on timeframe
        if candle.timeframe == Timeframe.M15:
            target_variants = [v for v in VARIANTS if v.timeframe == Timeframe.M15]
        elif candle.timeframe == Timeframe.M5:
            target_variants = [v for v in VARIANTS if v.timeframe == Timeframe.M5]
        else:
            return

        for variant in target_variants:
            # Skip if this token isn't in variant's instrument list
            if token not in variant.instruments:
                continue

            # Check regime conditions
            if not self._regime.check_regime(variant, token):
                continue

            # Skip if already has a position for this token+variant
            # (1 position at a time per variant per instrument, any direction)
            if (self._trade_mgr.has_open_position(variant.number, token, "LONG") or
                    self._trade_mgr.has_open_position(variant.number, token, "SHORT")):
                continue

            # Evaluate signal
            direction = self._signal_evaluator.evaluate(variant, token)
            if direction is None:
                continue

            # Don't open if already have same direction
            if self._trade_mgr.has_open_position(variant.number, token, direction):
                continue

            # Get entry price (candle close)
            entry_price = candle.close

            # Get ATR for SL calculation
            history = self._candle_builder.get_history(token, candle.timeframe)
            atr_val = atr(history[-50:], 14) if len(history) >= 15 else 50.0

            # Open trade
            position = self._trade_mgr.open_trade(
                variant=variant,
                token=token,
                direction=direction,
                entry_price=entry_price,
                atr_val=atr_val or 50.0,
            )

            inst_name = INSTRUMENTS[token]["name"]
            logger.info(
                "ENTRY | V%d %s | %s %s @ %.2f | SL=%.2f | exit=%s",
                variant.number, variant.name, direction, inst_name,
                entry_price, position.stop_loss, position.exit_type,
            )
            self._telegram.notify_entry(variant, position)

    def _check_exits(self) -> None:
        """Check and execute any pending exits."""
        exits = self._trade_mgr.check_exits()
        for pos, price, reason in exits:
            variant = next(v for v in VARIANTS if v.number == pos.variant_number)
            trade = self._trade_mgr.close_trade(pos, price, reason)
            inst_name = INSTRUMENTS[pos.token]["name"]
            logger.info(
                "EXIT | V%d %s | %s %s @ %.2f | PnL=%.1f (net=%.1f) | %s",
                variant.number, variant.name, trade.direction, inst_name,
                price, trade.pnl_points, trade.pnl_after_costs, reason,
            )
            self._telegram.notify_exit(variant, trade, self._trade_mgr)

    def _square_off_all(self) -> None:
        """End of day: close all open positions."""
        exits = self._trade_mgr.square_off_all()
        if not exits:
            return
        logger.info("EOD SQUARE OFF: Closing %d positions", len(exits))
        for pos, price, reason in exits:
            variant = next(v for v in VARIANTS if v.number == pos.variant_number)
            trade = self._trade_mgr.close_trade(pos, price, reason)
            logger.info(
                "SQUARED OFF | V%d | %s %s @ %.2f | PnL=%.1f",
                variant.number, INSTRUMENTS[pos.token]["name"],
                trade.direction, price, trade.pnl_after_costs,
            )
        self._telegram.notify_summary(self._trade_mgr)
        self._telegram.send("📊 End of day — all positions squared off.")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

_shutdown_requested = False


def main() -> None:
    """Main execution flow for paper trading f50a2b57 on 6 instruments."""
    global _shutdown_requested

    logger.info("=" * 60)
    logger.info("PAPER TRADER — f50a2b57 MR 15m on 6 Instruments")
    logger.info("=" * 60)

    # ─── Market Hours Guard ──────────────────────────────────────
    def _sleep_shutdown_handler(signum, frame):
        global _shutdown_requested
        _shutdown_requested = True
        logger.info("Shutdown signal received during sleep.")
        sys.exit(0)

    if not is_within_active_window():
        sleep_seconds = seconds_until_market_open()
        if sleep_seconds > 0:
            import socket
            wake_time = datetime.now() + timedelta(seconds=sleep_seconds)
            logger.info(
                "Market closed. Sleeping until %s (%.1f hours)...",
                wake_time.strftime("%Y-%m-%d %H:%M"),
                sleep_seconds / 3600,
            )
            signal.signal(signal.SIGINT, _sleep_shutdown_handler)
            signal.signal(signal.SIGTERM, _sleep_shutdown_handler)

            # Sleep in 60s chunks
            while sleep_seconds > 0 and not _shutdown_requested:
                chunk = min(sleep_seconds, 60)
                time.sleep(chunk)
                sleep_seconds -= chunk

            if _shutdown_requested:
                sys.exit(0)
            logger.info("Waking up — market is about to open!")

    # ─── Configuration ───────────────────────────────────────────
    config = load_config()
    logger.info("Config loaded: auth=%s", config.groww.auth_method)

    # ─── Broker Authentication ───────────────────────────────────
    broker = GrowwBroker(config.groww)
    try:
        broker.authenticate()
        logger.info("Groww authentication successful")
    except Exception as e:
        logger.error("Authentication failed: %s", e)
        sys.exit(1)

    # ─── Orchestrator Setup ──────────────────────────────────────
    orchestrator = PaperTradingOrchestrator()

    # ─── Historical Data Warmup ──────────────────────────────────
    # Indicators need ~50 candles of history. Without warmup, RSI/EMA/ATR
    # return None and no signals fire until enough live candles accumulate.
    logger.info("─── Starting Warmup (seeding indicators) ───")
    try:
        from app.warmup import DataManager
        from app.utils.instruments import get_instrument_map

        instrument_map = get_instrument_map()

        # Create a minimal "strategy" stub to tell DataManager what we need
        class _WarmupStub:
            name = "paper_variants"
            warmup_config = {"5m": 60, "15m": 60}  # 60 candles each = ~5h of 5m, ~15h of 15m

        data_manager = DataManager(
            broker=broker,
            candle_builder=orchestrator._candle_builder,
            concurrency=config.warmup.concurrency,
            delay_between_requests_ms=config.warmup.delay_ms,
            max_retries=config.warmup.max_retries,
            retry_backoff_base=config.warmup.retry_backoff_base,
        )

        # Only warm up tradeable tokens (not VIX — it's an index, no historical candles)
        tradeable_tokens = list(INSTRUMENTS.keys())
        warmup_result = data_manager.warmup(
            strategies=[_WarmupStub()],
            exchange_tokens=tradeable_tokens,
            instrument_map=instrument_map,
        )
        logger.info("Warmup: %s", warmup_result.summary())
        if warmup_result.errors:
            for err in warmup_result.errors[:5]:
                logger.warning("  Warmup error: %s", err)
    except Exception as e:
        logger.warning("Warmup failed (will rely on live candles): %s", e)

    # Start orchestrator (after warmup so indicators have history)
    orchestrator.start()

    # ─── Feed Setup ──────────────────────────────────────────────
    instruments = [
        Instrument(exchange="NSE", segment="CASH", exchange_token=token)
        for token in ALL_TOKENS
    ]
    feed = GrowwFeedClient(broker)

    def emit_tick(tick: Tick) -> None:
        orchestrator._event_bus.emit("tick", tick)

    reconnecting_feed = ReconnectingFeed(
        feed=feed,
        event_bus=orchestrator._event_bus,
        max_retries=0,  # unlimited
        broker=broker,
    )

    def on_reconnect(info: dict) -> None:
        logger.warning(
            "RECONNECT | attempt=%d backoff=%.1fs",
            info["attempt"], info["backoff_s"],
        )
        if info["attempt"] == 1:
            orchestrator._telegram.send(
                f"⚠️ Feed disconnected. Reconnecting (attempt {info['attempt']})..."
            )

    orchestrator._event_bus.subscribe("reconnect", on_reconnect)
    reconnecting_feed.subscribe_ltp(instruments, on_tick=emit_tick)

    # ─── Graceful Shutdown ───────────────────────────────────────
    def shutdown(signum, frame):
        logger.info("Shutdown signal received. Cleaning up...")
        reconnecting_feed.stop()
        orchestrator.stop()

        # Print final summary to logs
        logger.info("=" * 50)
        logger.info("FINAL PAPER TRADING SUMMARY")
        logger.info("=" * 50)
        for v in VARIANTS:
            stats = orchestrator._trade_mgr.get_variant_stats(v.number)
            logger.info(
                "  V%d %-25s | Trades=%d WR=%.0f%% PnL=%+.1f pts",
                v.number, v.name, stats["trades"], stats["win_rate"], stats["pnl"],
            )
        logger.info("=" * 50)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ─── Start Feed (blocking) ───────────────────────────────────
    logger.info("Pipeline ready:")
    logger.info("  Feed → Ticks → CandleBuilder(5m,15m) → SignalEvaluator → TradeManager → Telegram")
    logger.info("  Subscribed to %d instruments (%d tradeable + VIX)", len(ALL_TOKENS), len(INSTRUMENTS))
    logger.info("  Variants: 6 EXECUTE + 4 MONITOR = 10 total")
    logger.info("Press Ctrl+C to stop")

    try:
        reconnecting_feed.start_blocking()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
