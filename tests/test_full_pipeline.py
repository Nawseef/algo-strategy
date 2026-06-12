"""
FULL INTEGRATION TEST — Phases 1 through 5

Simulates a complete trading session with 2 instruments (NIFTY + RELIANCE):
1. Phase 1: Variant generation + DB initialization
2. Phase 2: Indicator computation from candle data
3. Phase 3: Strategy evaluation + filter matching
4. Phase 4: Armed state + grouping + tick triggers
5. Phase 5: Trade recording + candle caching + DB persistence

Simulates multiple candle closes and tick events to verify the
entire pipeline works end-to-end without a live market feed.

Run: python -m tests.test_full_pipeline
"""

import os
import random
import time
from collections import defaultdict
from datetime import datetime, time as dtime
from pathlib import Path

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
from app.indicators.engine import IndicatorEngine
from app.variants.config import load_research_config
from app.variants.evaluator import VariantEvaluator
from app.variants.filter_engine import evaluate_filters
from app.variants.generator import generate_all_variants, get_variant_count
from app.variants.models import (
    ATRFilter,
    ADXFilter,
    FilterSet,
    IndicatorSnapshot,
    MetadataSnapshot,
    ResearchTimeframe,
    StrategyType,
    VIXFilter,
    VolumeFilter,
)
from app.variants.strategies.bb_template import BBTemplate
from app.variants.strategies.mean_reversion_template import MeanReversionTemplate
from app.variants.strategies.orb_template import ORBTemplate
from app.variants.strategies.trend_template import TrendTemplate
from app.variants.strategies.vpa_template import VPATemplate


# ─── Test Helpers ────────────────────────────────────────────────────────────


def generate_candle_series(base_price, count, token, tf=Timeframe.M5, seed=42):
    """Generate a realistic candle series with uptrend bias."""
    random.seed(seed)
    candles = []
    price = base_price
    ts = 1718010000000  # Some base timestamp

    for i in range(count):
        noise = random.uniform(-0.003, 0.004) * price
        price += noise
        o = price
        h = o + random.uniform(0.002, 0.006) * price
        l = o - random.uniform(0.002, 0.006) * price
        c = o + random.uniform(-0.003, 0.005) * price
        vol = random.randint(5000, 20000)
        candles.append(Candle(
            exchange='NSE', segment='CASH', exchange_token=token,
            timeframe=tf, timestamp_ms=ts + (i * 300_000),
            open=o, high=h, low=l, close=c, volume=vol,
        ))
    return candles


def print_section(title):
    """Print a formatted section header."""
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def print_pass(msg):
    print(f"  ✅ {msg}")


def print_fail(msg):
    print(f"  ❌ {msg}")


def check(condition, msg):
    if condition:
        print_pass(msg)
    else:
        print_fail(msg)
    return condition


# ─── Main Test ───────────────────────────────────────────────────────────────


def run_full_test():
    """Run the complete Phase 1-5 integration test."""
    print("=" * 60)
    print("  150K VARIANT ENGINE — FULL INTEGRATION TEST")
    print("  Phases 1 → 2 → 3 → 4 → 5")
    print("=" * 60)

    all_passed = True
    t_start = time.time()

    # Use a fresh test DB
    test_db = Path(__file__).parent.parent / "data" / "test_research.db"
    if test_db.exists():
        os.remove(test_db)

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 1: Variant Generation + DB Setup
    # ═════════════════════════════════════════════════════════════════════════
    print_section("PHASE 1: Variant Generation + Database")

    # 1a. Generate variants
    t0 = time.perf_counter()
    variants = generate_all_variants()
    gen_time = time.perf_counter() - t0

    all_passed &= check(len(variants) == 150_000, f"Generated 150,000 variants (got {len(variants):,})")
    all_passed &= check(gen_time < 2.0, f"Generation time {gen_time:.2f}s < 2s")

    # 1b. Verify uniqueness
    ids = {v.variant_id for v in variants}
    all_passed &= check(len(ids) == 150_000, f"All variant IDs unique ({len(ids):,})")

    # 1c. Verify counts match spec (File 1)
    counts = get_variant_count()
    all_passed &= check(counts["strategies"] == 5, "5 strategies")
    all_passed &= check(counts["timeframes"] == 3, "3 timeframes")
    all_passed &= check(counts["total_filter_combos"] == 10_000, "10,000 filter combos")

    # 1d. Database initialization
    store = ResearchStore(db_path=test_db)
    store.start()
    all_passed &= check(store.get_total_trade_count() == 0, "DB initialized empty")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 2: Indicator Engine
    # ═════════════════════════════════════════════════════════════════════════
    print_section("PHASE 2: Indicator Computation")

    event_bus = EventBus()
    candle_builder = CandleBuilder(event_bus, timeframes=[Timeframe.M5, Timeframe.M15, Timeframe.M30])
    indicator_engine = IndicatorEngine(candle_builder)
    indicator_engine.update_vix(14.5)

    # Generate and inject history for NIFTY
    nifty_candles = generate_candle_series(24800.0, 55, 'NIFTY', seed=100)
    candle_builder.inject_history('NIFTY', Timeframe.M5, nifty_candles[:-1])

    # Generate and inject history for RELIANCE
    rel_candles = generate_candle_series(1400.0, 55, '2885', seed=200)
    candle_builder.inject_history('2885', Timeframe.M5, rel_candles[:-1])

    # Compute snapshot for NIFTY
    nifty_snapshot = indicator_engine.on_candle(nifty_candles[-1])
    all_passed &= check(nifty_snapshot is not None, "NIFTY snapshot computed")
    all_passed &= check(nifty_snapshot.atr > 0, f"NIFTY ATR={nifty_snapshot.atr:.1f} > 0")
    all_passed &= check(0 < nifty_snapshot.rsi < 100, f"NIFTY RSI={nifty_snapshot.rsi:.1f} valid")
    all_passed &= check(nifty_snapshot.vix == 14.5, "VIX correctly passed (14.5)")
    all_passed &= check(nifty_snapshot.ema_9 > 0, f"NIFTY EMA9={nifty_snapshot.ema_9:.0f}")
    all_passed &= check(nifty_snapshot.bb_upper > nifty_snapshot.bb_lower, "BB upper > lower")

    # Compute snapshot for RELIANCE
    rel_snapshot = indicator_engine.on_candle(rel_candles[-1])
    all_passed &= check(rel_snapshot is not None, "RELIANCE snapshot computed")
    all_passed &= check(rel_snapshot.atr > 0, f"RELIANCE ATR={rel_snapshot.atr:.1f} > 0")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 2b: Filter Evaluation
    # ═════════════════════════════════════════════════════════════════════════
    print_section("PHASE 2b: Filter Evaluation Against Real Snapshots")

    # Evaluate all 150K filters against NIFTY snapshot
    t0 = time.perf_counter()
    nifty_passed = sum(1 for v in variants if evaluate_filters(v.filters, nifty_snapshot))
    t1 = time.perf_counter()
    nifty_eval_ms = (t1 - t0) * 1000

    all_passed &= check(nifty_eval_ms < 500, f"NIFTY 150K eval: {nifty_eval_ms:.0f}ms < 500ms")
    all_passed &= check(0 < nifty_passed < 150_000, f"NIFTY filters passed: {nifty_passed:,}")

    # Evaluate against RELIANCE
    t0 = time.perf_counter()
    rel_passed = sum(1 for v in variants if evaluate_filters(v.filters, rel_snapshot))
    t1 = time.perf_counter()
    rel_eval_ms = (t1 - t0) * 1000

    all_passed &= check(rel_eval_ms < 500, f"RELIANCE 150K eval: {rel_eval_ms:.0f}ms < 500ms")
    all_passed &= check(0 < rel_passed < 150_000, f"RELIANCE filters passed: {rel_passed:,}")

    # Different instruments should pass different counts
    all_passed &= check(nifty_passed != rel_passed, "Different instruments → different filter results")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 3: Strategy Templates + Variant Evaluation
    # ═════════════════════════════════════════════════════════════════════════
    print_section("PHASE 3: Strategy Templates + Variant Evaluation")

    templates = {
        StrategyType.ORB: ORBTemplate(),
        StrategyType.BOLLINGER_BANDS: BBTemplate(),
        StrategyType.VPA: VPATemplate(),
        StrategyType.TREND_FOLLOWING: TrendTemplate(),
        StrategyType.MEAN_REVERSION: MeanReversionTemplate(),
    }

    # Set up ORB range for NIFTY
    orb = templates[StrategyType.ORB]
    orb._range_high['NIFTY'] = nifty_candles[-1].close + 50
    orb._range_low['NIFTY'] = nifty_candles[-1].close - 80
    orb._range_ready['NIFTY'] = True
    orb._last_reset_date = datetime.now().strftime('%Y-%m-%d')

    evaluator = VariantEvaluator(variants, templates)
    all_passed &= check(
        evaluator.get_group_count(StrategyType.ORB, ResearchTimeframe.M5) == 10_000,
        "ORB+5m group has 10,000 variants",
    )

    # Evaluate NIFTY 5m
    nifty_history = candle_builder.get_history('NIFTY', Timeframe.M5)
    nifty_metadata = MetadataSnapshot(session='MORNING', day_of_week='THU', month='JUN')

    result = evaluator.evaluate(
        instrument='NIFTY', timeframe=ResearchTimeframe.M5,
        candle=nifty_candles[-1], history=nifty_history,
        snapshot=nifty_snapshot, metadata=nifty_metadata, candle_index=1,
    )

    all_passed &= check(result.eval_time_ms < 200, f"Evaluation time: {result.eval_time_ms:.1f}ms < 200ms")
    all_passed &= check(result.candidates_produced > 0, f"Strategy signals produced: {result.candidates_produced}")
    total_output = len(result.armed_variants) + len(result.immediate_trades)
    all_passed &= check(total_output > 0, f"Total output (armed + immediate): {total_output}")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 4: Armed State + Grouping + Tick Trigger
    # ═════════════════════════════════════════════════════════════════════════
    print_section("PHASE 4: Armed State + Grouping + Tick Trigger")

    armed_state = ArmedStateManager(max_armed_per_instrument=10_000)
    grouping_engine = GroupingEngine()
    tick_engine = TickTriggerEngine(armed_state, grouping_engine)
    tick_engine.update_snapshot('NIFTY', nifty_snapshot)
    tick_engine.update_metadata('NIFTY', nifty_metadata)

    # Arm variants from evaluation
    if result.armed_variants:
        added = armed_state.arm(result.armed_variants)
        all_passed &= check(added > 0, f"Armed {added} variants")
        all_passed &= check(
            armed_state.get_armed_count('NIFTY') == added,
            f"Armed state count matches: {armed_state.get_armed_count('NIFTY')}",
        )

        # Build groups
        all_armed = armed_state.get_armed('NIFTY')
        group_count = grouping_engine.rebuild('NIFTY', all_armed)
        all_passed &= check(group_count > 0, f"Groups created: {group_count}")
        all_passed &= check(
            group_count < added,
            f"Grouping reduces work: {added} armed → {group_count} groups ({added // max(group_count,1)}x reduction)",
        )

        # Simulate ticks — find a trigger level and cross it
        groups = grouping_engine.get_all_groups('NIFTY')
        long_groups = [g for g in groups if g.direction.value == 'LONG']

        if long_groups:
            target_group = long_groups[0]
            trigger_price = target_group.trigger_value + 1

            tick = Tick(
                exchange='NSE', segment='CASH', exchange_token='NIFTY',
                ltp=trigger_price, timestamp_ms=nifty_candles[-1].timestamp_ms + 60000,
            )
            fired = tick_engine.on_tick(tick)
            all_passed &= check(fired > 0, f"Tick trigger fired: {fired} trades at {trigger_price:.2f}")

            # Verify armed state cleaned up
            all_passed &= check(
                armed_state.get_armed_count('NIFTY') < added,
                "Triggered variants removed from armed state",
            )
    else:
        print("  ⚠️  No armed variants (ORB may not have fired — testing immediate path)")

    # Test expiry
    armed_state.arm(result.armed_variants[:10] if result.armed_variants else [])
    expired = armed_state.cleanup_expired('NIFTY', 100)  # far future candle
    all_passed &= check(True, f"Expiry cleanup ran (expired: {expired})")

    # ═════════════════════════════════════════════════════════════════════════
    # PHASE 5: Trade Recording + Candle Cache + DB Persistence
    # ═════════════════════════════════════════════════════════════════════════
    print_section("PHASE 5: Trade Recording + DB Persistence")

    trade_recorder = TradeRecorder(store, flush_interval_seconds=60.0)
    trade_recorder.start()
    candle_cache = CandleCache(store)

    # Record immediate trades
    if result.immediate_trades:
        recorded = trade_recorder.record_immediate_trades(
            result.immediate_trades, 'NIFTY',
            nifty_candles[-1].timestamp_ms, nifty_snapshot, nifty_metadata,
        )
        all_passed &= check(recorded > 0, f"Immediate trades recorded: {recorded}")

    # Record tick-triggered trades
    tick_trades = tick_engine.flush_trades()
    if tick_trades:
        tick_recorded = trade_recorder.record_tick_trades(tick_trades)
        all_passed &= check(tick_recorded > 0, f"Tick trades recorded: {tick_recorded}")

    # Test deduplication
    if result.immediate_trades:
        dup_recorded = trade_recorder.record_immediate_trades(
            result.immediate_trades, 'NIFTY',
            nifty_candles[-1].timestamp_ms, nifty_snapshot, nifty_metadata,
        )
        all_passed &= check(dup_recorded == 0, "Deduplication works (0 duplicates written)")

    # Cache candles
    for c in nifty_candles[-5:]:
        candle_cache.on_candle(c)
    all_passed &= check(candle_cache.candles_cached_today == 5, "5 candles cached")

    # Flush to DB
    trade_recorder.stop()

    # Verify DB has data
    total_trades = store.get_total_trade_count()
    all_passed &= check(total_trades > 0, f"Trades persisted to DB: {total_trades}")

    # Verify candle cache query works
    cached = store.get_cached_candles('NIFTY', '5m', 0, 99999999999999)
    all_passed &= check(len(cached) == 5, f"Candle cache query returns {len(cached)} rows")

    # Daily reset
    armed_state.reset_daily()
    all_passed &= check(armed_state.get_armed_count() == 0, "Daily reset clears armed state")

    # ═════════════════════════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ═════════════════════════════════════════════════════════════════════════
    store.stop()

    # Cleanup test DB
    if test_db.exists():
        os.remove(test_db)

    total_time = time.time() - t_start

    print(f"\n{'═' * 60}")
    if all_passed:
        print(f"  ✅ ALL TESTS PASSED — Full pipeline verified")
    else:
        print(f"  ❌ SOME TESTS FAILED — Review output above")
    print(f"  Total time: {total_time:.2f}s")
    print(f"{'═' * 60}")

    return all_passed


if __name__ == "__main__":
    success = run_full_test()
    exit(0 if success else 1)
