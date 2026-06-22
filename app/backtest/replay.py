"""
Backtest Replay Engine — replays historical candles through the full pipeline.

Reads from historical_candles table and feeds data through the SAME code path
as live trading: IndicatorEngine → Evaluator → ArmedState → Grouping →
TickEngine → TradeRecorder → ExitEngine.

Processes day-by-day with proper daily reset between days.
Resumable via backtest_runs table.

Architecture:
- Loads 5m candles from DB for a day
- Builds 15m and 30m candles by aggregating 5m
- Simulates intrabar ticks from candle OHLC (for INTRABAR triggers)
- After each day: runs exit engine on that day's trades
- Memory stable: resets state between days
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, time as dtime

from app.broker.base import Tick
from app.core.candle_builder import CandleBuilder
from app.core.events import EventBus
from app.core.models import Candle, Timeframe
from app.db.research_store import ResearchStore
from app.execution.armed_state import ArmedStateManager
from app.execution.candle_cache import CandleCache
from app.execution.grouping import GroupingEngine
from app.execution.tick_engine import TickTriggerEngine
from app.execution.trade_recorder import TradeRecorder
from app.exit_engine.engine import ExitSimulationEngine
from app.indicators.engine import IndicatorEngine
from app.utils.logger import get_logger
from app.variants.evaluator import EvaluationResult, VariantEvaluator
from app.variants.generator import generate_all_variants
from app.variants.models import (
    IndicatorSnapshot,
    MetadataSnapshot,
    ResearchTimeframe,
    StrategyType,
)
from app.variants.strategies.bb_template import BBTemplate
from app.variants.strategies.mean_reversion_template import MeanReversionTemplate
from app.variants.strategies.orb_template import ORBTemplate
from app.variants.strategies.trend_template import TrendTemplate
from app.variants.strategies.vpa_template import VPATemplate

from app.backtest.fetch import INSTRUMENT_MAP

logger = get_logger("backtest.replay")

RESEARCH_TIMEFRAME_MAP = {
    Timeframe.M5: ResearchTimeframe.M5,
    Timeframe.M15: ResearchTimeframe.M15,
    Timeframe.M30: ResearchTimeframe.M30,
}


class BacktestReplayEngine:
    """
    Replays historical candle data through the full research pipeline.

    Processes one day at a time:
    1. Load 5m candles from historical_candles table
    2. Build 15m/30m by aggregating
    3. Feed through indicator engine → evaluator → execution
    4. After all candles: flush trades, run exit engine
    5. Reset for next day
    """

    def __init__(self, store: ResearchStore, instruments: list[str] | None = None) -> None:
        self._store = store

        # Instruments to process (exchange_tokens)
        if instruments:
            self._instruments = []
            for name in instruments:
                info = INSTRUMENT_MAP.get(name.upper())
                if info:
                    self._instruments.append(info["exchange_token"])
                else:
                    logger.warning("Unknown instrument: %s — skipping", name)
        else:
            self._instruments = [info["exchange_token"] for info in INSTRUMENT_MAP.values()]

        # Find VIX token
        self._vix_token = INSTRUMENT_MAP.get("INDIAVIX", {}).get("exchange_token", "26017")

        # Generate variants (once)
        logger.info("Generating variants for backtest...")
        t0 = time.time()
        self._variants = generate_all_variants()
        logger.info("Generated %d variants in %.1fs", len(self._variants), time.time() - t0)

        # Strategy templates
        self._templates = {
            StrategyType.ORB: ORBTemplate(),
            StrategyType.BOLLINGER_BANDS: BBTemplate(),
            StrategyType.VPA: VPATemplate(),
            StrategyType.TREND_FOLLOWING: TrendTemplate(),
            StrategyType.MEAN_REVERSION: MeanReversionTemplate(),
        }

        # Exit engine
        self._exit_engine = ExitSimulationEngine(store)

        # Stats
        self._total_days_processed = 0
        self._total_trades = 0
        self._total_candles = 0

    def run(
        self,
        start_date: date,
        end_date: date,
        run_id: str | None = None,
    ) -> dict[str, int]:
        """
        Run backtest replay for a date range.

        Args:
            start_date: First day to process.
            end_date: Last day to process.
            run_id: Optional run identifier for tracking.

        Returns:
            Stats dict with days_processed, trades, candles.
        """
        if not run_id:
            run_id = f"BT-{uuid.uuid4().hex[:8]}"

        # Calculate trading days
        trading_days = self._get_trading_days(start_date, end_date)
        total_days = len(trading_days)

        logger.info(
            "═══ BACKTEST: %s to %s (%d trading days, %d instruments) ═══",
            start_date, end_date, total_days, len(self._instruments),
        )

        # Track run
        self._store.create_backtest_run(
            run_id=run_id,
            start_date=str(start_date),
            end_date=str(end_date),
            instruments=",".join(self._instruments),
            total_days=total_days,
        )

        t_start = time.time()

        for day_idx, trading_day in enumerate(trading_days, 1):
            day_trades = self._process_day(trading_day)
            self._total_days_processed += 1
            self._total_trades += day_trades

            # Update progress periodically
            if day_idx % 10 == 0 or day_idx == total_days:
                elapsed = time.time() - t_start
                rate = day_idx / elapsed if elapsed > 0 else 0
                eta = (total_days - day_idx) / rate if rate > 0 else 0
                logger.info(
                    "  Progress: %d/%d days (%.0f%%) | %d trades | %.1f days/sec | ETA: %.0fm",
                    day_idx, total_days, day_idx / total_days * 100,
                    self._total_trades, rate, eta / 60,
                )
                self._store.update_backtest_progress(run_id, day_idx, self._total_trades)

        # Complete
        total_time = time.time() - t_start
        self._store.complete_backtest_run(run_id, "complete")

        logger.info(
            "═══ BACKTEST COMPLETE: %d days, %d trades, %d candles in %.1fs ═══",
            self._total_days_processed, self._total_trades, self._total_candles, total_time,
        )

        return {
            "run_id": run_id,
            "days_processed": self._total_days_processed,
            "trades": self._total_trades,
            "candles": self._total_candles,
            "time_seconds": total_time,
        }

    def _process_day(self, trading_day: date) -> int:
        """
        Process one trading day through the full pipeline.
        Returns number of trades generated.

        Warmup strategy (matches live mode):
        - Load 50 candles from PREVIOUS trading day(s) into candle_builder
        - This seeds the indicator engine so it produces valid snapshots
          from the very first candle of the current day (9:15)
        - Then process ALL candles in true chronological order (5m/15m/30m interleaved)
          matching live behavior where candles arrive in real time

        Key fix: candles are merged into a single timeline sorted by timestamp
        so that session/metadata/VIX are evaluated at the correct wall-clock time,
        matching exactly what happens in live trading via the EventBus.
        """
        day_str = trading_day.strftime("%Y-%m-%d")

        # ─── Reset template state for the new day (matches live reset_daily) ──
        # Templates are singletons shared across all days. Without explicit reset,
        # stale state from the last candle of the previous day leaks into today.
        for template in self._templates.values():
            if hasattr(template, '_maybe_reset_daily'):
                template._maybe_reset_daily()

        # ─── Setup fresh pipeline for this day ───────────────────────────
        event_bus = EventBus()
        candle_builder = CandleBuilder(event_bus, timeframes=[Timeframe.M5, Timeframe.M15, Timeframe.M30])
        indicator_engine = IndicatorEngine(candle_builder)

        evaluator = VariantEvaluator(self._variants, self._templates)
        armed_state = ArmedStateManager(max_armed_per_instrument=500000)  # unlimited for backtest
        grouping_engine = GroupingEngine()
        tick_engine = TickTriggerEngine(armed_state, grouping_engine)

        trade_recorder = TradeRecorder(self._store, flush_interval_seconds=0, max_buffer_size=50000)  # No timer, no mid-day flush in backtest

        candle_cache = CandleCache(self._store)
        candle_cache._today_str = day_str

        # Per-timeframe candle counters
        candle_counters: dict[tuple[str, ResearchTimeframe], int] = defaultdict(int)

        # ─── Time boundaries ─────────────────────────────────────────────
        day_start_ms = datetime.combine(trading_day, dtime(9, 15)).timestamp() * 1000
        day_end_ms = datetime.combine(trading_day, dtime(15, 30)).timestamp() * 1000

        # ─── Load VIX candles for this day (used in interleaved timeline) ─
        vix_today = self._store.get_historical_candles(self._vix_token, "5m", day_start_ms, day_end_ms)
        # Build a map: timestamp_ms → vix_value for fast lookup during timeline
        vix_map: dict[float, float] = {}
        if vix_today:
            # Seed initial VIX from first candle
            indicator_engine.update_vix(vix_today[0].get("close", 14.0))
            for vc in vix_today:
                vix_map[vc["timestamp_ms"]] = vc.get("close", 14.0)
        else:
            indicator_engine.update_vix(14.0)

        # ─── Load previous day's close for each instrument ───────────────
        prev_day_end_ms = day_start_ms - 1
        prev_day_start_ms = prev_day_end_ms - (7 * 86400 * 1000)
        prev_closes: dict[str, float] = {}
        for token in self._instruments:
            if token == self._vix_token:
                continue
            prev_candles = self._store.get_historical_candles(token, "5m", prev_day_start_ms, prev_day_end_ms)
            if prev_candles:
                prev_closes[token] = prev_candles[-1].get("close", 0.0)

        # ─── Load candles for each instrument ────────────────────────────
        # Build unified timeline: (timestamp_ms, candle, rtf, core_tf, token)
        # All timeframes for all instruments merged into chronological order.
        timeline: list[tuple[float, Candle, ResearchTimeframe, Timeframe, str]] = []

        # Also store 5m candles per token for synthetic tick generation
        candles_5m_by_token: dict[str, list[Candle]] = {}

        for token in self._instruments:
            if token == self._vix_token:
                continue

            candles_raw = self._store.get_historical_candles(token, "5m", day_start_ms, day_end_ms)
            if not candles_raw:
                continue

            candles_5m: list[Candle] = []
            for c in candles_raw:
                candle = Candle(
                    exchange="NSE", segment="CASH", exchange_token=token,
                    timeframe=Timeframe.M5,
                    timestamp_ms=c["timestamp_ms"],
                    open=c["open"], high=c["high"], low=c["low"], close=c["close"],
                    volume=c.get("volume", 0),
                )
                candles_5m.append(candle)

            if len(candles_5m) < 10:
                continue

            self._total_candles += len(candles_5m)
            candles_5m_by_token[token] = candles_5m

            # ─── Warmup from previous day ────────────────────────────────
            warmup_raw = self._store.get_historical_candles(
                token, "5m", prev_day_start_ms, prev_day_end_ms
            )
            if warmup_raw:
                warmup_raw = warmup_raw[-50:]
                warmup_candles: list[Candle] = []
                for c in warmup_raw:
                    wc = Candle(
                        exchange="NSE", segment="CASH", exchange_token=token,
                        timeframe=Timeframe.M5,
                        timestamp_ms=c["timestamp_ms"],
                        open=c["open"], high=c["high"], low=c["low"], close=c["close"],
                        volume=c.get("volume", 0),
                    )
                    warmup_candles.append(wc)

                # Inject 5m warmup history
                candle_builder.inject_history(token, Timeframe.M5, warmup_candles)

                # Build 15m/30m warmup from the same previous-day candles
                warmup_15m = self._aggregate_candles(warmup_candles, 3, token, Timeframe.M15)
                warmup_30m = self._aggregate_candles(warmup_candles, 6, token, Timeframe.M30)
                if warmup_15m:
                    candle_builder.inject_history(token, Timeframe.M15, warmup_15m)
                    # Run 15m warmup through on_candle to seed EMA slope tracking
                    for wc15 in warmup_15m:
                        indicator_engine.on_candle(wc15)
                if warmup_30m:
                    candle_builder.inject_history(token, Timeframe.M30, warmup_30m)
                    for wc30 in warmup_30m:
                        indicator_engine.on_candle(wc30)

            # ─── Previous close + opening range setup ────────────────────
            prev_close = prev_closes.get(token, candles_5m[0].open)
            indicator_engine.set_prev_day_close(token, prev_close)

            # ─── Add 5m candles to timeline ──────────────────────────────
            for candle in candles_5m:
                timeline.append((candle.timestamp_ms, candle, ResearchTimeframe.M5, Timeframe.M5, token))

            # ─── Aggregate 15m/30m and add to timeline ───────────────────
            candles_15m = self._aggregate_candles(candles_5m, 3, token, Timeframe.M15)
            candles_30m = self._aggregate_candles(candles_5m, 6, token, Timeframe.M30)
            for candle in candles_15m:
                timeline.append((candle.timestamp_ms, candle, ResearchTimeframe.M15, Timeframe.M15, token))
            for candle in candles_30m:
                timeline.append((candle.timestamp_ms, candle, ResearchTimeframe.M30, Timeframe.M30, token))

        # ─── Sort timeline by timestamp (interleaved, matches live behavior) ─
        # When multiple candles share the same timestamp (a 15m and three 5m both
        # close at 9:30), process 5m first then 15m then 30m — matches CandleBuilder
        # emission order in live trading.
        TF_ORDER = {Timeframe.M5: 0, Timeframe.M15: 1, Timeframe.M30: 2}
        timeline.sort(key=lambda x: (x[0], TF_ORDER.get(x[3], 0)))

        # ─── Process unified timeline ─────────────────────────────────────
        # Track 5m candle index per token for synthetic tick generation
        candle_5m_index: dict[str, int] = {t: 0 for t in candles_5m_by_token}

        for ts_ms, candle, rtf, core_tf, token in timeline:
            # Update VIX if we've crossed into this candle's timestamp
            # This replicates live tick-by-tick VIX updates at the right time
            if vix_map and ts_ms in vix_map:
                indicator_engine.update_vix(vix_map[ts_ms])

            # Opening range tracking (9:15–9:30 for metadata)
            candle_dt = datetime.fromtimestamp(ts_ms / 1000)
            if core_tf == Timeframe.M5 and dtime(9, 15) <= candle_dt.time() <= dtime(9, 30):
                indicator_engine.update_opening_range(token, candle)

            # Cache for exit engine
            candle_cache.on_candle(candle)

            # Inject into history
            candle_builder.inject_history(token, core_tf, [candle])

            # Compute indicator snapshot
            snapshot = indicator_engine.on_candle(candle)
            if snapshot is None:
                if core_tf == Timeframe.M5:
                    metadata = indicator_engine.get_metadata(token)
                    history = candle_builder.get_history(token, core_tf)
                    minimal_snapshot = IndicatorSnapshot()
                    counter_key = (token, rtf)
                    candle_counters[counter_key] += 1
                    current_candle_idx = candle_counters[counter_key]
                    armed_state.cleanup_expired(token, current_candle_idx, timeframe=rtf.value)
                    evaluator.evaluate(
                        instrument=token, timeframe=rtf,
                        candle=candle, history=history,
                        snapshot=minimal_snapshot, metadata=metadata,
                        candle_index=current_candle_idx,
                    )
                continue

            metadata = indicator_engine.get_metadata(token)
            tick_engine.update_snapshot(token, snapshot)
            tick_engine.update_metadata(token, metadata)

            # Increment counter and evaluate
            counter_key = (token, rtf)
            candle_counters[counter_key] += 1
            current_candle_idx = candle_counters[counter_key]

            armed_state.cleanup_expired(token, current_candle_idx, timeframe=rtf.value)

            history = candle_builder.get_history(token, core_tf)
            result = evaluator.evaluate(
                instrument=token, timeframe=rtf,
                candle=candle, history=history,
                snapshot=snapshot, metadata=metadata,
                candle_index=current_candle_idx,
            )

            if result.immediate_trades:
                trade_recorder.record_immediate_trades(
                    result.immediate_trades, token,
                    candle.timestamp_ms, snapshot, metadata,
                )

            if result.armed_variants:
                armed_state.arm(result.armed_variants)

            all_armed = armed_state.get_armed(token)
            grouping_engine.rebuild(token, all_armed)

            # Synthetic ticks from NEXT 5m candle (only for 5m candles)
            if core_tf == Timeframe.M5 and token in candles_5m_by_token:
                idx = candle_5m_index[token]
                candle_5m_index[token] = idx + 1
                candles_5m = candles_5m_by_token[token]
                if idx + 1 < len(candles_5m):
                    next_candle = candles_5m[idx + 1]
                    synthetic_ticks = self._generate_synthetic_ticks(token, next_candle)
                    for tick in synthetic_ticks:
                        fired = tick_engine.on_tick(tick)
                        if fired > 0:
                            triggered = tick_engine.flush_trades()
                            trade_recorder.record_tick_trades(triggered)

        # ─── Flush trades and run exit engine ────────────────────────────
        trade_recorder.stop()

        stats = self._exit_engine.run_for_date(day_str)
        return stats.trades_processed if stats.trades_processed > 0 else 0

    def _generate_synthetic_ticks(self, token: str, candle: Candle) -> list[Tick]:
        """
        Generate synthetic tick prices from a candle OHLC.
        Order: Open → (Low or High) → (High or Low) → Close
        Based on candle direction (bullish: O→L→H→C, bearish: O→H→L→C)
        """
        ts = candle.timestamp_ms
        ticks = []

        # Open
        ticks.append(Tick("NSE", "CASH", token, candle.open, ts))

        if candle.close >= candle.open:
            # Bullish: assume price dipped first then rose
            ticks.append(Tick("NSE", "CASH", token, candle.low, ts + 60000))
            ticks.append(Tick("NSE", "CASH", token, candle.high, ts + 180000))
        else:
            # Bearish: assume price rose first then dropped
            ticks.append(Tick("NSE", "CASH", token, candle.high, ts + 60000))
            ticks.append(Tick("NSE", "CASH", token, candle.low, ts + 180000))

        # Close
        ticks.append(Tick("NSE", "CASH", token, candle.close, ts + 240000))

        return ticks

    @staticmethod
    def _aggregate_candles(candles_5m: list[Candle], factor: int, token: str, tf: Timeframe) -> list[Candle]:
        """
        Aggregate 5m candles into higher timeframe (3=15m, 6=30m).

        Uses the LAST 5m candle's timestamp as the aggregate candle's timestamp.
        This correctly represents the candle's close boundary in the timeline,
        ensuring 15m candles sort AFTER the 5m candles they contain.

        Example: 15m candle for 9:15-9:30 gets timestamp 9:25 (last 5m open),
        which places it after the 9:15, 9:20, 9:25 5m candles in the timeline.
        In live trading, the 15m candle only closes at 9:30 (after the 9:25 5m closes).
        """
        aggregated = []
        for i in range(0, len(candles_5m) - factor + 1, factor):
            group = candles_5m[i:i + factor]
            if len(group) < factor:
                break
            agg = Candle(
                exchange="NSE", segment="CASH", exchange_token=token,
                timeframe=tf,
                timestamp_ms=group[-1].timestamp_ms,  # use LAST candle's ts (close boundary)
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
            )
            aggregated.append(agg)
        return aggregated

    @staticmethod
    def _get_trading_days(start: date, end: date) -> list[date]:
        """Get list of trading days between start and end (skip weekends)."""
        from app.utils.market_hours import is_trading_day
        days = []
        current = start
        while current <= end:
            if is_trading_day(current):
                days.append(current)
            current += timedelta(days=1)
        return days
