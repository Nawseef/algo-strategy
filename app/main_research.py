"""
Research Engine — main entry point for the 150K variant system.

Implements the full pipeline from File 4/5:
    Live Feed → Candle Builder → Indicator Engine → Variant Evaluator
    → Armed State → Grouping → Tick Trigger → Trade Recording → DB

Coexists with app/main.py (paper trading mode).
Run with: python -m app.main_research

Architecture:
    - Per-instrument independent evaluation streams
    - Per-timeframe candle counters (for correct armed expiry)
    - India VIX subscription for filter evaluation
    - Batch trade recording (no DB writes in tick loop)
    - Candle caching for post-market exit simulation
"""

import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, time as dtime

from app.broker.base import Instrument, Tick
from app.broker.groww import GrowwBroker, GrowwFeedClient, is_index_token
from app.broker.reconnect import ReconnectingFeed
from app.core.candle_builder import CandleBuilder
from app.core.events import EventBus
from app.core.models import Candle, Timeframe
from app.db.research_store import ResearchStore
from app.execution.armed_state import ArmedStateManager
from app.execution.candle_cache import CandleCache
from app.execution.grouping import GroupingEngine
from app.execution.tick_engine import TickTriggerEngine
from app.execution.trade_recorder import TradeRecorder
from app.indicators.engine import IndicatorEngine
from app.utils.config import load_config
from app.utils.instruments import get_instrument_map
from app.utils.logger import get_logger
from app.utils.market_hours import is_within_active_window, seconds_until_market_open
from app.variants.config import load_research_config
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

logger = get_logger("research")

# Map string timeframe to enums
TIMEFRAME_MAP = {
    "5m": Timeframe.M5,
    "15m": Timeframe.M15,
    "30m": Timeframe.M30,
}

RESEARCH_TIMEFRAME_MAP = {
    Timeframe.M5: ResearchTimeframe.M5,
    Timeframe.M15: ResearchTimeframe.M15,
    Timeframe.M30: ResearchTimeframe.M30,
}


class ResearchOrchestrator:
    """
    Orchestrates the full 150K variant research pipeline.

    Wires together all Phase 1-4 components and manages:
    - Per-timeframe candle counters
    - Candle close → evaluate → arm → group flow
    - Tick → trigger → trade recording flow
    - Daily reset/cleanup
    """

    def __init__(self) -> None:
        # ─── Configuration ───────────────────────────────────────────────
        self._config = load_config()
        self._research_config = load_research_config()

        # ─── Event Bus ───────────────────────────────────────────────────
        self._event_bus = EventBus()

        # ─── Candle Builder ──────────────────────────────────────────────
        timeframes = [TIMEFRAME_MAP[tf] for tf in self._research_config.timeframes]
        self._candle_builder = CandleBuilder(self._event_bus, timeframes=timeframes)

        # ─── Indicator Engine ────────────────────────────────────────────
        self._indicator_engine = IndicatorEngine(self._candle_builder)

        # ─── Variant Generation ──────────────────────────────────────────
        logger.info("Generating 150K variants...")
        t0 = time.time()
        self._variants = generate_all_variants()
        logger.info("Generated %d variants in %.1fs", len(self._variants), time.time() - t0)

        # Register variant definitions in DB (for lookup/audit trail)
        # Deferred until after store.start() — see start() method

        # ─── Strategy Templates ──────────────────────────────────────────
        self._templates = {
            StrategyType.ORB: ORBTemplate(),
            StrategyType.BOLLINGER_BANDS: BBTemplate(),
            StrategyType.VPA: VPATemplate(),
            StrategyType.TREND_FOLLOWING: TrendTemplate(),
            StrategyType.MEAN_REVERSION: MeanReversionTemplate(),
        }

        # ─── Variant Evaluator ───────────────────────────────────────────
        self._evaluator = VariantEvaluator(self._variants, self._templates)

        # ─── Execution Layer ─────────────────────────────────────────────
        self._armed_state = ArmedStateManager(
            max_armed_per_instrument=self._research_config.max_armed_per_instrument
        )
        self._grouping_engine = GroupingEngine()
        self._tick_engine = TickTriggerEngine(self._armed_state, self._grouping_engine)

        # ─── Database ────────────────────────────────────────────────────
        self._store = ResearchStore()

        # ─── Trade Recorder ──────────────────────────────────────────────
        self._trade_recorder = TradeRecorder(
            store=self._store,
            flush_interval_seconds=self._research_config.trade_flush_interval,
        )

        # ─── Candle Cache ────────────────────────────────────────────────
        self._candle_cache = CandleCache(self._store)

        # ─── Per-timeframe candle counters (for correct armed expiry) ────
        # Key: (instrument, ResearchTimeframe) → counter
        self._candle_counters: dict[tuple[str, ResearchTimeframe], int] = defaultdict(int)

        # ─── India VIX tracking ──────────────────────────────────────────
        # Default to 14.0 (historical average) until first VIX tick arrives.
        # Prevents VIX-filtered variants from being silently suppressed if feed drops.
        self._vix_value: float = 14.0
        self._indicator_engine.update_vix(14.0)

        # ─── Stats ───────────────────────────────────────────────────────
        self._total_evaluations: int = 0
        self._total_candles_processed: int = 0
        self._running = False

        # ─── Daily reset tracking ────────────────────────────────────────
        self._current_trading_date: str = ""

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start all components."""
        self._store.start()
        self._trade_recorder.start()

        # Register variant definitions in DB
        # Compares current variant set with DB, registers new ones, retires old ones.
        current_ids = {v.variant_id for v in self._variants}
        sample_ids = [self._variants[0].variant_id, self._variants[-1].variant_id, self._variants[len(self._variants)//2].variant_id]
        all_exist = all(self._store.get_variant_definition(vid) is not None for vid in sample_ids)

        if not all_exist:
            logger.info("Variant set changed — syncing definitions (%d variants)...", len(self._variants))

            # Register all current variants (upsert)
            batch_size = 10000
            total_registered = 0
            for i in range(0, len(self._variants), batch_size):
                batch = self._variants[i:i + batch_size]
                total_registered += self._store.register_variants(batch, generation=1)

            # Retire variants that are no longer generated
            from app.db.research_store import ResearchStore
            if self._store.is_postgres:
                all_db_ids_raw = self._store._query("SELECT variant_id FROM variant_definitions WHERE retired_at IS NULL")
            else:
                all_db_ids_raw = self._store._query("SELECT variant_id FROM variant_definitions WHERE retired_at IS NULL")
            db_ids = {row["variant_id"] for row in all_db_ids_raw}
            to_retire = db_ids - current_ids
            if to_retire:
                self._store.retire_variants(list(to_retire))
                logger.info("Retired %d old variants no longer in generator", len(to_retire))

            logger.info("Registered %d variant definitions, %d retired", total_registered, len(to_retire) if to_retire else 0)
        else:
            logger.info("Variant definitions up to date (%d active)", len(self._variants))

        # Subscribe to events
        self._event_bus.subscribe("tick", self._on_tick)
        self._event_bus.subscribe("candle", self._on_candle)

        # Also subscribe candle builder to ticks
        self._event_bus.subscribe("tick", self._candle_builder.on_tick)

        self._running = True
        logger.info("ResearchOrchestrator started")

    def stop(self) -> None:
        """Stop all components and flush remaining data."""
        self._running = False

        # Flush tick engine trades
        remaining_trades = self._tick_engine.flush_trades()
        if remaining_trades:
            self._trade_recorder.record_tick_trades(remaining_trades)

        self._trade_recorder.stop()
        self._store.stop()

        # Log final stats
        self._log_summary()
        logger.info("ResearchOrchestrator stopped")

    # ─── Event Handlers ──────────────────────────────────────────────────────

    def _on_tick(self, tick: Tick) -> None:
        """
        Handle tick event.
        Routes to tick trigger engine for armed group checking.
        Also updates VIX if this is the VIX instrument.
        """
        if not self._running:
            return

        # ─── Daily reset check (detect new trading day from tick timestamp) ──
        self._check_daily_reset(tick.timestamp_ms)

        # Skip pre-market ticks (before 9:15)
        tick_time = datetime.fromtimestamp(tick.timestamp_ms / 1000).time()
        if tick_time < dtime(9, 15):
            return

        # Update VIX if this tick is from India VIX
        if tick.exchange_token == self._research_config.vix_token:
            self._vix_value = tick.ltp
            self._indicator_engine.update_vix(tick.ltp)
            return  # VIX doesn't need trigger checking

        # Tick trigger engine — check if price crosses any group levels
        trades_fired = self._tick_engine.on_tick(tick)

        # If trades were fired, flush from tick engine to recorder
        if trades_fired > 0:
            triggered_trades = self._tick_engine.flush_trades()
            self._trade_recorder.record_tick_trades(triggered_trades)

    def _check_daily_reset(self, timestamp_ms: float) -> None:
        """
        Detect day change from tick/candle timestamp and trigger daily reset.
        This ensures proper state cleanup even if the process runs 24/7
        across multiple trading days.
        """
        tick_date = datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")

        if not self._current_trading_date:
            # First tick of the session
            self._current_trading_date = tick_date
            return

        if tick_date != self._current_trading_date:
            # New day detected — reset everything
            logger.info("New trading day detected (%s → %s) — running daily reset",
                       self._current_trading_date, tick_date)
            self.reset_daily()
            self._current_trading_date = tick_date

    def _on_candle(self, candle: Candle) -> None:
        """
        Handle candle close event — THE MAIN EVALUATION PIPELINE.

        This is where the heavy work happens (every 5/15/30 min):
        1. Cache candle for exit simulation
        2. Compute indicators
        3. Evaluate all variants for this instrument + timeframe
        4. Record immediate trades (CANDLE_CLOSE mode)
        5. Arm new variants (INTRABAR mode)
        6. Cleanup expired armed variants
        7. Rebuild trigger groups
        """
        if not self._running:
            return

        # Only process research timeframes
        rtf = RESEARCH_TIMEFRAME_MAP.get(candle.timeframe)
        if rtf is None:
            return

        # Skip non-research instruments
        token = candle.exchange_token
        if token not in self._research_config.instruments:
            return

        self._total_candles_processed += 1

        # ─── Step 1: Cache candle for exit simulation ────────────────────
        self._candle_cache.on_candle(candle)

        # ─── Step 2: Compute indicators ──────────────────────────────────
        snapshot = self._indicator_engine.on_candle(candle)
        if snapshot is None:
            return  # Not enough history yet

        # Update tick engine's snapshot reference
        self._tick_engine.update_snapshot(token, snapshot)

        # ─── Step 3: Get metadata ────────────────────────────────────────
        metadata = self._indicator_engine.get_metadata(token)
        self._tick_engine.update_metadata(token, metadata)

        # ─── Step 4: Update candle counter for this instrument+timeframe ─
        counter_key = (token, rtf)
        self._candle_counters[counter_key] += 1
        current_candle_index = self._candle_counters[counter_key]

        # ─── Step 5: Cleanup expired armed variants ──────────────────────
        # Pass timeframe so only variants of THIS timeframe get expired
        # (prevents 5m counter from expiring 30m variants)
        expired = self._armed_state.cleanup_expired(token, current_candle_index, timeframe=rtf.value)

        # ─── Step 6: Evaluate all variants ───────────────────────────────
        history = self._candle_builder.get_history(token, candle.timeframe)

        result = self._evaluator.evaluate(
            instrument=token,
            timeframe=rtf,
            candle=candle,
            history=history,
            snapshot=snapshot,
            metadata=metadata,
            candle_index=current_candle_index,
        )

        self._total_evaluations += 1

        # ─── Step 7: Record immediate trades (CANDLE_CLOSE) ──────────────
        if result.immediate_trades:
            recorded = self._trade_recorder.record_immediate_trades(
                variants_and_signals=result.immediate_trades,
                instrument=token,
                timestamp_ms=candle.timestamp_ms,
                snapshot=snapshot,
                metadata=metadata,
            )
            if recorded > 0:
                logger.info(
                    "IMMEDIATE | %s %s | %d trades recorded (VPA/MR)",
                    token, rtf.value, recorded,
                )

        # ─── Step 8: Arm new variants (INTRABAR) ─────────────────────────
        if result.armed_variants:
            added = self._armed_state.arm(result.armed_variants)
            if added > 0:
                logger.info(
                    "ARMED | %s %s | %d new variants armed (total: %d)",
                    token, rtf.value, added,
                    self._armed_state.get_armed_count(token),
                )

        # ─── Step 9: Rebuild trigger groups for this instrument ──────────
        all_armed_for_instrument = self._armed_state.get_armed(token)
        group_count = self._grouping_engine.rebuild(token, all_armed_for_instrument)

        # Log evaluation summary (not per-variant, just summary)
        if result.candidates_produced > 0:
            logger.debug(
                "EVAL | %s %s | candle#%d | signals=%d filters_passed=%d "
                "armed=%d immediate=%d groups=%d | %.1fms",
                token, rtf.value, current_candle_index,
                result.candidates_produced, result.filters_passed,
                len(result.armed_variants), len(result.immediate_trades),
                group_count, result.eval_time_ms,
            )

    # ─── Daily Management ────────────────────────────────────────────────────

    def reset_daily(self) -> None:
        """Reset all daily state. Called at start of new trading day."""
        self._armed_state.reset_daily()
        self._grouping_engine.clear()
        self._tick_engine.reset_daily()
        self._trade_recorder.reset_daily()
        self._candle_cache.reset_daily()
        self._indicator_engine.reset_daily()
        self._candle_counters.clear()

        # Reset strategy template state
        for template in self._templates.values():
            if hasattr(template, '_maybe_reset_daily'):
                template._maybe_reset_daily()

        logger.info("Daily reset complete")

    # ─── Stats / Summary ─────────────────────────────────────────────────────

    def _log_summary(self) -> None:
        """Log session summary."""
        logger.info("=" * 60)
        logger.info("RESEARCH ENGINE SESSION SUMMARY")
        logger.info("=" * 60)
        logger.info("  Candles processed:   %d", self._total_candles_processed)
        logger.info("  Evaluations run:     %d", self._total_evaluations)
        logger.info("  Trades in DB:        %d", self._store.get_trade_count_today())
        logger.info("  Candles cached:      %d", self._candle_cache.candles_cached_today)
        logger.info("  Tick engine stats:   %s", self._tick_engine.get_stats())
        logger.info("  Trade recorder:      %s", self._trade_recorder.get_stats())
        logger.info("  Armed state:         %s", self._armed_state.get_stats())
        logger.info("=" * 60)


# ─── Main Entry Point ────────────────────────────────────────────────────────


def main() -> None:
    """Main execution flow for the research engine."""
    logger.info("=" * 60)
    logger.info("150K VARIANT RESEARCH ENGINE")
    logger.info("=" * 60)

    # ─── Market Hours Guard ──────────────────────────────────────────
    if not is_within_active_window():
        sleep_seconds = seconds_until_market_open()
        if sleep_seconds > 0:
            from datetime import timedelta
            wake_time = datetime.now() + timedelta(seconds=sleep_seconds)
            logger.info(
                "Market closed. Sleeping until %s (%.1f hours)...",
                wake_time.strftime("%Y-%m-%d %I:%M %p"),
                sleep_seconds / 3600,
            )
            # Sleep in chunks for signal handling
            while sleep_seconds > 0:
                chunk = min(sleep_seconds, 60)
                time.sleep(chunk)
                sleep_seconds -= chunk
            logger.info("Waking up — market is about to open!")

    # ─── Initialize Orchestrator ─────────────────────────────────────
    orchestrator = ResearchOrchestrator()

    # ─── Broker Authentication ───────────────────────────────────────
    config = load_config()
    research_config = load_research_config()

    broker = GrowwBroker(config.groww)
    try:
        broker.authenticate()
    except Exception as e:
        logger.error("Authentication failed: %s", e)
        sys.exit(1)

    # ─── Historical Warmup ───────────────────────────────────────────
    if research_config.warmup_enabled:
        logger.info("─── Starting Warmup ───")
        from app.warmup.data_manager import DataManager

        instrument_map = get_instrument_map()

        # Create a minimal strategy list for warmup config
        # (warmup needs to know how many candles to fetch)
        from app.strategy.base import BaseStrategy
        class _WarmupProxy(BaseStrategy):
            @property
            def name(self): return "warmup"
            @property
            def warmup_config(self): return {"5m": 50, "15m": 50, "30m": 50}
            def on_candle(self, c, h): return None

        data_manager = DataManager(
            broker=broker,
            candle_builder=orchestrator._candle_builder,
            concurrency=config.warmup.concurrency,
            delay_between_requests_ms=config.warmup.delay_ms,
            max_retries=config.warmup.max_retries,
        )
        warmup_result = data_manager.warmup(
            strategies=[_WarmupProxy()],
            exchange_tokens=research_config.instruments,
            instrument_map=instrument_map,
        )
        logger.info("Warmup: %s", warmup_result.summary())

    # ─── Start Orchestrator ──────────────────────────────────────────
    orchestrator.start()

    # ─── Build Instrument List ───────────────────────────────────────
    # Research instruments + VIX
    instruments = [
        Instrument(exchange="NSE", segment="CASH", exchange_token=token)
        for token in research_config.instruments
    ]
    # Add India VIX subscription
    instruments.append(
        Instrument(
            exchange=research_config.vix_exchange,
            segment=research_config.vix_segment,
            exchange_token=research_config.vix_token,
        )
    )

    logger.info("Subscribing to %d instruments + India VIX", len(research_config.instruments))

    # ─── Feed Setup ──────────────────────────────────────────────────
    feed = GrowwFeedClient(broker)

    def emit_tick(tick: Tick) -> None:
        orchestrator._event_bus.emit("tick", tick)

    reconnecting_feed = ReconnectingFeed(
        feed=feed,
        event_bus=orchestrator._event_bus,
        max_retries=config.reconnect.max_retries,
        broker=broker,
    )
    reconnecting_feed.subscribe_ltp(instruments, on_tick=emit_tick)

    # ─── Graceful Shutdown ───────────────────────────────────────────
    def shutdown(signum, frame):
        logger.info("Shutdown signal received...")
        orchestrator.stop()
        reconnecting_feed.stop()
        logger.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ─── Start ───────────────────────────────────────────────────────
    logger.info("Pipeline ready. Starting feed...")
    logger.info("  Feed → Ticks → Candles → Indicators → 150K Eval → Armed → Groups → Triggers → DB")
    logger.info("  Instruments: %s", research_config.instruments)
    logger.info("  VIX: %s", research_config.vix_token)
    logger.info("Press Ctrl+C to stop")

    try:
        reconnecting_feed.start_blocking()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
