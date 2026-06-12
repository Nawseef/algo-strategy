"""
Test: Multi-instrument evaluation with grouping analysis.

Simulates a realistic market session where multiple strategies fire
across 2 instruments (NIFTY + RELIANCE), then shows how armed variants
would be grouped for tick monitoring.

We patch datetime.now() to simulate market hours so all strategies can fire.
"""

import time
import random
from collections import defaultdict
from unittest.mock import patch
from datetime import datetime, time as dtime

from app.core.models import Candle, Timeframe
from app.variants.generator import generate_all_variants
from app.variants.models import (
    IndicatorSnapshot, MetadataSnapshot, ResearchTimeframe, StrategyType,
    ArmedVariant, TriggerType, Direction,
)
from app.variants.evaluator import VariantEvaluator
from app.variants.strategies.orb_template import ORBTemplate
from app.variants.strategies.bb_template import BBTemplate
from app.variants.strategies.vpa_template import VPATemplate
from app.variants.strategies.trend_template import TrendTemplate
from app.variants.strategies.mean_reversion_template import MeanReversionTemplate


def build_candle_history(base_price, count, token, timeframe=Timeframe.M5):
    """Generate synthetic candle history with slight uptrend."""
    random.seed(123)
    candles = []
    ts_base = 1718010000000  # ~9:30 AM timestamp
    price = base_price

    for i in range(count):
        noise = random.uniform(-0.003, 0.004) * price
        price += noise
        o = price
        h = o + random.uniform(0.001, 0.005) * price
        l = o - random.uniform(0.001, 0.005) * price
        c = o + random.uniform(-0.003, 0.004) * price
        vol = random.randint(5000, 20000)
        candles.append(Candle(
            exchange='NSE', segment='CASH', exchange_token=token,
            timeframe=timeframe,
            timestamp_ms=ts_base + (i * 300_000),
            open=o, high=h, low=l, close=c, volume=vol,
        ))
    return candles


def make_engulfing_candles(base_price, token):
    """Create history ending with a bullish engulfing pattern."""
    candles = build_candle_history(base_price, 48, token)
    ts = candles[-1].timestamp_ms + 300_000

    # Previous candle: bearish (red)
    prev = Candle(
        exchange='NSE', segment='CASH', exchange_token=token,
        timeframe=Timeframe.M5, timestamp_ms=ts,
        open=base_price + 10, high=base_price + 12,
        low=base_price - 5, close=base_price - 3, volume=8000,
    )
    candles.append(prev)

    # Current candle: bullish engulfing (green, body engulfs previous)
    curr = Candle(
        exchange='NSE', segment='CASH', exchange_token=token,
        timeframe=Timeframe.M5, timestamp_ms=ts + 300_000,
        open=base_price - 5, high=base_price + 20,
        low=base_price - 8, close=base_price + 15, volume=15000,
    )
    return candles, curr


def main():
    print("=" * 70)
    print("MULTI-INSTRUMENT ARMED VARIANT + GROUPING ANALYSIS")
    print("=" * 70)

    # Generate all variants
    variants = generate_all_variants()
    print(f"\nGenerated {len(variants):,} variants")

    # Create strategy templates
    templates = {
        StrategyType.ORB: ORBTemplate(),
        StrategyType.BOLLINGER_BANDS: BBTemplate(),
        StrategyType.VPA: VPATemplate(),
        StrategyType.TREND_FOLLOWING: TrendTemplate(),
        StrategyType.MEAN_REVERSION: MeanReversionTemplate(),
    }

    evaluator = VariantEvaluator(variants, templates)

    # ═══════════════════════════════════════════════════════════════════
    # INSTRUMENT 1: NIFTY
    # Scenario: ORB range set, Trend pullback detected, VPA engulfing
    # ═══════════════════════════════════════════════════════════════════

    nifty_snapshot = IndicatorSnapshot(
        atr=85.0, adx=28.5, rsi=55.0, vwap=24850.0,
        volume_ratio=1.3, vix=13.8,
        ema_9=24920.0, ema_21=24870.0, ema_20=24875.0, ema_50=24750.0,
        ema_20_slope=3.2, ema_50_slope=1.5,
        bb_upper=25050.0, bb_middle=24880.0, bb_lower=24710.0,
        bb_squeeze=False, price_vs_vwap=0.6,
    )

    nifty_metadata = MetadataSnapshot(
        session='MORNING', day_of_week='THU', month='JUN',
        gap_size=0.4, gap_direction='UP',
        opening_range_size=130.0, market_structure='TRENDING',
        volatility_regime='NORMAL', htf_trend_1h='BULLISH',
    )

    # Build history with engulfing pattern for VPA to detect
    nifty_history, nifty_candle = make_engulfing_candles(24900.0, 'NIFTY')

    # Manually set ORB range (simulate 9:15-9:30 already passed)
    orb_template = templates[StrategyType.ORB]
    orb_template._range_high['NIFTY'] = 25000.0
    orb_template._range_low['NIFTY'] = 24750.0
    orb_template._range_ready['NIFTY'] = True
    orb_template._last_reset_date = datetime.now().strftime("%Y-%m-%d")

    # Set BB squeeze state (simulate squeeze just released)
    bb_template = templates[StrategyType.BOLLINGER_BANDS]
    bb_template._was_in_squeeze['NIFTY'] = True
    bb_template._squeeze_count['NIFTY'] = 7  # 7 candles of squeeze
    bb_template._last_reset_date = datetime.now().strftime("%Y-%m-%d")

    # ═══════════════════════════════════════════════════════════════════
    # INSTRUMENT 2: RELIANCE
    # Scenario: Mean reversion firing (RSI recovery), VPA hammer
    # ═══════════════════════════════════════════════════════════════════

    reliance_snapshot = IndicatorSnapshot(
        atr=22.0, adx=16.0, rsi=41.0, vwap=1420.0,
        volume_ratio=1.8, vix=13.8,
        ema_9=1415.0, ema_21=1425.0, ema_20=1424.0, ema_50=1435.0,
        ema_20_slope=-1.2, ema_50_slope=-0.5,
        bb_upper=1445.0, bb_middle=1422.0, bb_lower=1399.0,
        bb_squeeze=False, price_vs_vwap=0.2,  # Slightly above VWAP
    )

    reliance_metadata = MetadataSnapshot(
        session='MORNING', day_of_week='THU', month='JUN',
        gap_size=0.2, gap_direction='DOWN',
        opening_range_size=18.0, market_structure='RANGING',
        volatility_regime='NORMAL', htf_trend_1h='NEUTRAL',
    )

    # Build history with hammer pattern
    rel_candles = build_candle_history(1410.0, 49, '2885')
    # Add hammer candle (long lower wick)
    rel_candle = Candle(
        exchange='NSE', segment='CASH', exchange_token='2885',
        timeframe=Timeframe.M5,
        timestamp_ms=rel_candles[-1].timestamp_ms + 300_000,
        open=1418.0, high=1420.0, low=1405.0, close=1419.0, volume=12000,
    )

    # Set MR prev_rsi to simulate RSI crossing back up from 38 → 41
    mr_template = templates[StrategyType.MEAN_REVERSION]
    mr_template._prev_rsi['2885'] = 38.0  # Was below 40, now snapshot shows 41
    mr_template._last_reset_date = datetime.now().strftime("%Y-%m-%d")

    # ═══════════════════════════════════════════════════════════════════
    # EVALUATE — patch time to simulate 10:00 AM (market hours)
    # ═══════════════════════════════════════════════════════════════════

    fake_now = datetime(2026, 6, 11, 10, 0, 0)  # 10:00 AM

    all_armed: list[ArmedVariant] = []
    all_immediate: list[tuple] = []

    with patch('app.variants.strategies.orb_template.datetime') as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        # Evaluate NIFTY
        print("\n─── NIFTY Evaluation (5m) ───")
        r1 = evaluator.evaluate(
            instrument='NIFTY', timeframe=ResearchTimeframe.M5,
            candle=nifty_candle, history=nifty_history,
            snapshot=nifty_snapshot, metadata=nifty_metadata,
            candle_index=50,
        )
        print(f"  Strategy signals: {r1.candidates_produced}")
        print(f"  Filters passed:   {r1.filters_passed:,}")
        print(f"  Armed:            {len(r1.armed_variants):,}")
        print(f"  Immediate trades: {len(r1.immediate_trades):,}")
        all_armed.extend(r1.armed_variants)
        all_immediate.extend(r1.immediate_trades)

    # Evaluate RELIANCE (MR and VPA don't need ORB time patch)
    print("\n─── RELIANCE Evaluation (5m) ───")
    r2 = evaluator.evaluate(
        instrument='2885', timeframe=ResearchTimeframe.M5,
        candle=rel_candle, history=rel_candles,
        snapshot=reliance_snapshot, metadata=reliance_metadata,
        candle_index=50,
    )
    print(f"  Strategy signals: {r2.candidates_produced}")
    print(f"  Filters passed:   {r2.filters_passed:,}")
    print(f"  Armed:            {len(r2.armed_variants):,}")
    print(f"  Immediate trades: {len(r2.immediate_trades):,}")
    all_armed.extend(r2.armed_variants)
    all_immediate.extend(r2.immediate_trades)

    # ═══════════════════════════════════════════════════════════════════
    # GROUPING ANALYSIS
    # ═══════════════════════════════════════════════════════════════════

    print("\n" + "=" * 70)
    print("GROUPING ANALYSIS")
    print("=" * 70)

    print(f"\nTotal armed variants: {len(all_armed):,}")
    print(f"Total immediate trades: {len(all_immediate):,}")

    # Group by (instrument, direction, trigger_type, trigger_value)
    groups: dict[tuple, list[ArmedVariant]] = defaultdict(list)
    for av in all_armed:
        # Round trigger value to avoid floating point noise
        key = (av.instrument, av.direction.value, av.trigger_type.value, round(av.trigger_value, 2))
        groups[key].append(av)

    print(f"\nUnique trigger groups: {len(groups)}")
    print(f"Reduction: {len(all_armed):,} armed → {len(groups)} groups")
    if len(groups) > 0:
        print(f"Avg variants per group: {len(all_armed) / len(groups):.0f}")

    print("\n─── Groups Detail ───")
    for key, members in sorted(groups.items(), key=lambda x: -len(x[1])):
        instrument, direction, trigger_type, trigger_value = key
        strategies = set(m.variant.strategy.value for m in members)
        print(f"  {instrument} {direction} | {trigger_type} @ {trigger_value:.2f} | "
              f"{len(members)} variants | strategies: {strategies}")

    # Show immediate trades breakdown
    if all_immediate:
        print("\n─── Immediate Trades (CANDLE_CLOSE entries) ───")
        imm_by_strategy = defaultdict(int)
        for variant, candidate in all_immediate:
            imm_by_strategy[variant.strategy.value] += 1
        for strat, count in sorted(imm_by_strategy.items()):
            print(f"  {strat}: {count} trades")

    print("\n" + "=" * 70)
    print("TICK ENGINE WORKLOAD COMPARISON")
    print("=" * 70)
    print(f"\n  WITHOUT grouping: {len(all_armed):,} checks per tick")
    print(f"  WITH grouping:    {len(groups)} checks per tick")
    if len(all_armed) > 0 and len(groups) > 0:
        print(f"  Reduction factor:  {len(all_armed) / len(groups):.0f}x fewer checks")
    print()


if __name__ == "__main__":
    main()
