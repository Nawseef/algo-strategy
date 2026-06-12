"""
Variant generator — creates all 150K+ variant combinations.

Cartesian product of:
    5 Strategies × 3 Timeframes × Filter Combinations

Filter dimensions:
    ATR:    5 values (None, >10, >15, >20, >25)
    ADX:    5 values (None, >15, >20, >25, >30)
    VIX:    4 values (None, >12, >15, >18)
    Volume: 4 values (None, >1.2x, >1.5x, >2x)
    RSI:    5 values (None, <30, <35, >65, >70)
    VWAP:   5 values (None, Above, Below, Dist>0.5ATR, Dist>1ATR)

Total filters: 5 × 5 × 4 × 4 × 5 × 5 = 10,000
Total variants: 5 strategies × 3 timeframes × 10,000 = 150,000

Entry mode is determined by strategy type:
    ORB → INTRABAR (waits for price breakout)
    BB  → INTRABAR (waits for band touch/breakout)
    VPA → CANDLE_CLOSE (pattern detected on candle close)
    TREND → INTRABAR (waits for pullback entry level)
    MR  → CANDLE_CLOSE (RSI crossover detected on candle close)
"""

from __future__ import annotations

from itertools import product

from app.variants.models import (
    ADXFilter,
    ATRFilter,
    EntryMode,
    FilterSet,
    RSIFilter,
    ResearchTimeframe,
    StrategyType,
    Variant,
    VIXFilter,
    VolumeFilter,
    VWAPFilter,
)

# ─── Entry mode mapping per strategy ────────────────────────────────────────

STRATEGY_ENTRY_MODE: dict[StrategyType, EntryMode] = {
    StrategyType.ORB: EntryMode.INTRABAR,
    StrategyType.BOLLINGER_BANDS: EntryMode.INTRABAR,
    StrategyType.VPA: EntryMode.CANDLE_CLOSE,
    StrategyType.TREND_FOLLOWING: EntryMode.INTRABAR,
    StrategyType.MEAN_REVERSION: EntryMode.CANDLE_CLOSE,
}

# ─── Validity windows (max candles an armed variant stays active) ────────────

STRATEGY_EXPIRY_CANDLES: dict[StrategyType, int] = {
    StrategyType.ORB: 3,  # ORB breakout should happen within 3 candles
    StrategyType.BOLLINGER_BANDS: 3,  # BB touch/breakout within 3 candles
    StrategyType.VPA: 1,  # VPA is candle-close, no arming needed
    StrategyType.TREND_FOLLOWING: 5,  # Trend pullback may take longer
    StrategyType.MEAN_REVERSION: 1,  # MR is candle-close, no arming needed
}


def generate_all_filter_sets() -> list[FilterSet]:
    """
    Generate all possible filter combinations.
    Returns ~10,000 unique FilterSet objects.
    """
    filter_sets = []

    for atr, adx, vix, vol, rsi, vwap in product(
        ATRFilter,
        ADXFilter,
        VIXFilter,
        VolumeFilter,
        RSIFilter,
        VWAPFilter,
    ):
        filter_sets.append(
            FilterSet(atr=atr, adx=adx, vix=vix, volume=vol, rsi=rsi, vwap=vwap)
        )

    return filter_sets


def generate_all_variants() -> list[Variant]:
    """
    Generate all ~150,000 variant definitions.

    Each variant is:
        Strategy + Timeframe + FilterSet + EntryMode

    Returns a list of Variant objects with deterministic IDs.
    """
    filter_sets = generate_all_filter_sets()
    variants: list[Variant] = []

    for strategy in StrategyType:
        entry_mode = STRATEGY_ENTRY_MODE[strategy]

        for timeframe in ResearchTimeframe:
            for filters in filter_sets:
                variant = Variant(
                    strategy=strategy,
                    timeframe=timeframe,
                    filters=filters,
                    entry_mode=entry_mode,
                )
                variants.append(variant)

    return variants


def generate_variant_index() -> dict[str, Variant]:
    """
    Generate all variants and return as a dict keyed by variant_id.
    Used for O(1) lookup during trade recording.
    """
    variants = generate_all_variants()
    return {v.variant_id: v for v in variants}


def get_variant_count() -> dict[str, int]:
    """Return breakdown of variant counts by dimension."""
    return {
        "strategies": len(StrategyType),
        "timeframes": len(ResearchTimeframe),
        "atr_filters": len(ATRFilter),
        "adx_filters": len(ADXFilter),
        "vix_filters": len(VIXFilter),
        "volume_filters": len(VolumeFilter),
        "rsi_filters": len(RSIFilter),
        "vwap_filters": len(VWAPFilter),
        "total_filter_combos": (
            len(ATRFilter)
            * len(ADXFilter)
            * len(VIXFilter)
            * len(VolumeFilter)
            * len(RSIFilter)
            * len(VWAPFilter)
        ),
        "total_variants": (
            len(StrategyType)
            * len(ResearchTimeframe)
            * len(ATRFilter)
            * len(ADXFilter)
            * len(VIXFilter)
            * len(VolumeFilter)
            * len(RSIFilter)
            * len(VWAPFilter)
        ),
    }


# ─── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("150K VARIANT GENERATOR")
    print("=" * 60)
    print()

    counts = get_variant_count()
    print("Dimension breakdown:")
    print(f"  Strategies:      {counts['strategies']}")
    print(f"  Timeframes:      {counts['timeframes']}")
    print(f"  ATR filters:     {counts['atr_filters']}")
    print(f"  ADX filters:     {counts['adx_filters']}")
    print(f"  VIX filters:     {counts['vix_filters']}")
    print(f"  Volume filters:  {counts['volume_filters']}")
    print(f"  RSI filters:     {counts['rsi_filters']}")
    print(f"  VWAP filters:    {counts['vwap_filters']}")
    print()
    print(f"  Filter combos:   {counts['total_filter_combos']:,}")
    print(f"  Total variants:  {counts['total_variants']:,}")
    print()

    print("Generating all variants...")
    variants = generate_all_variants()
    print(f"Generated {len(variants):,} variants")
    print()

    # Show a few examples
    print("Sample variants:")
    for v in variants[:5]:
        print(f"  [{v.variant_id}] {v.short_name()}")
    print("  ...")
    for v in variants[-5:]:
        print(f"  [{v.variant_id}] {v.short_name()}")
    print()

    # Verify uniqueness
    ids = [v.variant_id for v in variants]
    unique_ids = set(ids)
    if len(unique_ids) == len(ids):
        print(f"✅ All {len(ids):,} variant IDs are unique")
    else:
        print(f"⚠️  ID collisions detected: {len(ids) - len(unique_ids)}")

    # Memory estimate
    # Each Variant is ~200 bytes (frozen dataclass with enums)
    mem_mb = (len(variants) * 200) / (1024 * 1024)
    print(f"Estimated memory: ~{mem_mb:.0f} MB for variant definitions")
