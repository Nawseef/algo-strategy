"""
Post-Market Exit Simulation Engine.

From File 2:
    "For every trade: Load candle path from Entry_Time → Market Close.
     Run all exits against same path."

Workflow:
1. Load all trades from today (or specified date)
2. For each trade, load candle path from candle_cache
3. Run ALL exit models against the same candle path
4. Compute MFE (max favorable excursion) and MAE (max adverse excursion)
5. Determine best/worst exit model
6. Write results to exit_results table (one row per trade)

This runs AFTER market close — no real-time constraints.
Idempotent: re-running for same day overwrites (UPSERT via INSERT OR REPLACE).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.db.research_store import ResearchStore
from app.exit_engine.models.breakeven_trail import simulate_all_breakeven_trails
from app.exit_engine.models.chandelier_exit import simulate_all_chandelier_exits
from app.exit_engine.models.indicator_exits import simulate_all_indicator_exits
from app.exit_engine.models.partial_exit_models import simulate_all_partials
from app.exit_engine.models.rr_exit import simulate_all_rr
from app.exit_engine.models.stop_loss_models import simulate_all_stops
from app.exit_engine.models.time_exits import (
    simulate_all_dead_trade_exits,
    simulate_all_session_exits,
    simulate_all_time_exits,
)
from app.exit_engine.models.trailing_models import simulate_all_trails
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExitEngineStats:
    """Statistics from an exit simulation run."""

    trades_processed: int = 0
    trades_skipped: int = 0  # no candle path available
    total_time_seconds: float = 0.0
    avg_time_per_trade_ms: float = 0.0


class ExitSimulationEngine:
    """
    Post-market exit simulation engine.

    Loads trades, reconstructs candle paths, runs all exit models,
    writes results to DB.
    """

    def __init__(self, store: ResearchStore) -> None:
        self._store = store

    def run_for_date(self, date_str: str) -> ExitEngineStats:
        """
        Run exit simulation for all trades on a given date.

        Args:
            date_str: Date in "YYYY-MM-DD" format.

        Returns:
            ExitEngineStats with processing summary.
        """
        t0 = time.time()
        stats = ExitEngineStats()

        # Load trades for the date
        trades = self._store.get_trades_for_date(date_str)
        if not trades:
            logger.info("No trades found for %s", date_str)
            return stats

        logger.info("Exit simulation: processing %d trades for %s", len(trades), date_str)

        for trade in trades:
            success = self._process_trade(trade)
            if success:
                stats.trades_processed += 1
            else:
                stats.trades_skipped += 1

        stats.total_time_seconds = time.time() - t0
        if stats.trades_processed > 0:
            stats.avg_time_per_trade_ms = (stats.total_time_seconds / stats.trades_processed) * 1000

        logger.info(
            "Exit simulation complete: %d processed, %d skipped, %.1fs total (%.1fms/trade)",
            stats.trades_processed, stats.trades_skipped,
            stats.total_time_seconds, stats.avg_time_per_trade_ms,
        )

        return stats

    def _process_trade(self, trade: dict) -> bool:
        """
        Process a single trade: load candle path, run exits, write results.
        Returns True if successful, False if skipped.
        """
        trade_id = trade["trade_id"]
        instrument = trade["instrument"]
        timeframe = trade["timeframe"]
        entry_time_ms = trade["entry_time_ms"]
        entry_price = trade["entry_price"]
        direction = trade["direction"]
        atr_at_entry = trade.get("atr_entry", 0.0)

        # Load candle path: entry_time → market close (~15:30 IST)
        # Market close: add ~6.25 hours (22500 seconds) from 9:15 as rough upper bound
        # More precisely: get all candles from entry to end of day
        from datetime import datetime
        entry_dt = datetime.fromtimestamp(entry_time_ms / 1000)
        eod_dt = entry_dt.replace(hour=15, minute=30, second=0, microsecond=0)
        end_ms = eod_dt.timestamp() * 1000

        candle_path_raw = self._store.get_cached_candles(
            instrument, timeframe, entry_time_ms, end_ms
        )

        if not candle_path_raw:
            # Try with just the instrument (timeframe might not match exactly)
            # Also try 5m as default if timeframe doesn't yield results
            candle_path_raw = self._store.get_cached_candles(
                instrument, "5m", entry_time_ms, end_ms
            )

        if not candle_path_raw:
            logger.debug("No candle path for trade %s (%s %s)", trade_id, instrument, timeframe)
            return False

        # Convert to simple dicts with OHLCV keys
        candle_path = [
            {
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c.get("volume", 0),
            }
            for c in candle_path_raw
        ]

        if not candle_path:
            return False

        # Also get candles BEFORE entry for swing stop calculation
        pre_entry_raw = self._store.get_cached_candles(
            instrument, timeframe,
            entry_time_ms - (5 * 300_000),  # ~5 candles before (5m)
            entry_time_ms,
        )
        candles_before_entry = [
            {"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
            for c in pre_entry_raw
        ] if pre_entry_raw else None

        # ─── Run all exit models ─────────────────────────────────────────
        results: dict[str, float] = {}

        # RR exits (7 models)
        rr_results = simulate_all_rr(entry_price, direction, atr_at_entry, candle_path)
        results.update(rr_results)

        # Stop loss models (3 models)
        stop_results = simulate_all_stops(
            entry_price, direction, atr_at_entry, candle_path, candles_before_entry
        )
        results.update(stop_results)

        # Trailing models (3 models)
        trail_results = simulate_all_trails(entry_price, direction, atr_at_entry, candle_path)
        results.update(trail_results)

        # Partial exit models (3 models)
        partial_results = simulate_all_partials(entry_price, direction, atr_at_entry, candle_path)
        results.update(partial_results)

        # Time-based exits (5 models)
        time_results = simulate_all_time_exits(entry_price, direction, candle_path)
        results.update(time_results)

        # Session exits (4 models)
        # Compute entry candle offset from market open (9:15)
        entry_candle_from_open = 0
        market_open = entry_dt.replace(hour=9, minute=15, second=0, microsecond=0)
        if entry_dt > market_open:
            minutes_from_open = (entry_dt - market_open).total_seconds() / 60
            entry_candle_from_open = int(minutes_from_open / 5)

        session_results = simulate_all_session_exits(
            entry_price, direction, candle_path, entry_candle_from_open
        )
        results.update(session_results)

        # Dead trade exits (3 models)
        dead_results = simulate_all_dead_trade_exits(entry_price, direction, candle_path)
        results.update(dead_results)

        # Breakeven + trail combos (7 models)
        be_results = simulate_all_breakeven_trails(entry_price, direction, atr_at_entry, candle_path)
        results.update(be_results)

        # Chandelier / advanced trails (12 models)
        chand_results = simulate_all_chandelier_exits(entry_price, direction, atr_at_entry, candle_path)
        results.update(chand_results)

        # Indicator-based exits (10 models)
        indicator_results = simulate_all_indicator_exits(entry_price, direction, atr_at_entry, candle_path)
        results.update(indicator_results)

        # ─── Compute MFE / MAE ───────────────────────────────────────────
        mfe, mae = self._compute_excursions(entry_price, direction, candle_path)
        results["mfe"] = mfe
        results["mae"] = mae

        # ─── Find best/worst exit ────────────────────────────────────────
        all_pnls = {k: v for k, v in results.items() if k not in ("mfe", "mae")}
        if all_pnls:
            best_model = max(all_pnls, key=all_pnls.get)
            worst_model = min(all_pnls, key=all_pnls.get)
            results["best_exit_model"] = best_model
            results["best_pnl"] = all_pnls[best_model]
            results["worst_exit_model"] = worst_model
            results["worst_pnl"] = all_pnls[worst_model]

        # ─── Write to DB ─────────────────────────────────────────────────
        self._store.write_exit_result(trade_id, results)

        return True

    @staticmethod
    def _compute_excursions(
        entry_price: float, direction: str, candle_path: list[dict]
    ) -> tuple[float, float]:
        """
        Compute Max Favorable Excursion (MFE) and Max Adverse Excursion (MAE).

        MFE: best unrealized profit during the trade (how far it went in your favor)
        MAE: worst unrealized loss during the trade (how far it went against you)

        Returns (mfe_points, mae_points) — both as positive values for favorable,
        negative for adverse.
        """
        if not candle_path:
            return 0.0, 0.0

        if direction == "LONG":
            best_price = max(c["high"] for c in candle_path)
            worst_price = min(c["low"] for c in candle_path)
            mfe = best_price - entry_price  # positive = good
            mae = worst_price - entry_price  # negative = bad
        else:
            best_price = min(c["low"] for c in candle_path)
            worst_price = max(c["high"] for c in candle_path)
            mfe = entry_price - best_price  # positive = good
            mae = entry_price - worst_price  # negative = bad

        return mfe, mae
