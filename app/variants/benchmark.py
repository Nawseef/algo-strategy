"""
Performance benchmark for the variant evaluation system.

Tests:
1. Filter evaluation speed (150K variants against a snapshot)
2. Variant generation time
3. Memory usage

Run: python -m app.variants.benchmark
"""

import time
import sys

from app.variants.generator import generate_all_variants
from app.variants.filter_engine import evaluate_filters
from app.variants.models import IndicatorSnapshot


def benchmark_filter_evaluation():
    """Benchmark: evaluate 150K filter sets against a single snapshot."""
    print("=" * 60)
    print("BENCHMARK: Filter Evaluation (150K variants)")
    print("=" * 60)

    # Generate all variants
    print("\n1. Generating variants...")
    t0 = time.perf_counter()
    variants = generate_all_variants()
    gen_time = time.perf_counter() - t0
    print(f"   Generated {len(variants):,} variants in {gen_time:.2f}s")

    # Create a realistic indicator snapshot
    snapshot = IndicatorSnapshot(
        atr=18.5,
        adx=23.0,
        rsi=52.0,
        vwap=24850.0,
        volume_ratio=1.3,
        vix=13.5,
        ema_9=24900.0,
        ema_21=24820.0,
        ema_20=24830.0,
        ema_50=24700.0,
        ema_20_slope=2.5,
        ema_50_slope=1.2,
        bb_upper=25100.0,
        bb_middle=24850.0,
        bb_lower=24600.0,
        bb_squeeze=False,
        price_vs_vwap=0.4,
    )

    # Benchmark: evaluate all 150K filter sets
    print("\n2. Evaluating 150K filter sets...")
    t0 = time.perf_counter()
    pass_count = 0
    for variant in variants:
        if evaluate_filters(variant.filters, snapshot):
            pass_count += 1
    eval_time = time.perf_counter() - t0

    print(f"   Evaluation time: {eval_time:.3f}s")
    print(f"   Passed filters:  {pass_count:,} / {len(variants):,} ({pass_count/len(variants)*100:.1f}%)")
    print(f"   Per variant:     {eval_time/len(variants)*1_000_000:.1f} µs")
    print(f"   Throughput:      {len(variants)/eval_time:,.0f} variants/sec")

    # Simulate 5 instruments
    print("\n3. Simulating 5 instruments (5 × 150K evaluations)...")
    t0 = time.perf_counter()
    total_pass = 0
    for _ in range(5):
        for variant in variants:
            if evaluate_filters(variant.filters, snapshot):
                total_pass += 1
    multi_time = time.perf_counter() - t0
    print(f"   Total time (5 instruments): {multi_time:.2f}s")
    print(f"   Per instrument: {multi_time/5:.2f}s")
    print(f"   Total passed: {total_pass:,}")

    # Memory usage
    print("\n4. Memory usage...")
    size_bytes = sys.getsizeof(variants)
    # Approximate: each variant object + its filter set
    approx_bytes = len(variants) * 300  # rough estimate per variant object
    print(f"   List object: {size_bytes / 1024 / 1024:.1f} MB")
    print(f"   Estimated total: ~{approx_bytes / 1024 / 1024:.0f} MB")

    # Summary
    print("\n" + "=" * 60)
    if eval_time < 5.0:
        print(f"✅ PASS: {eval_time:.2f}s per instrument (target: <5s)")
    else:
        print(f"⚠️  SLOW: {eval_time:.2f}s per instrument (target: <5s)")
    if multi_time < 30.0:
        print(f"✅ PASS: {multi_time:.2f}s for 5 instruments (target: <30s)")
    else:
        print(f"⚠️  SLOW: {multi_time:.2f}s for 5 instruments (target: <30s)")
    print("=" * 60)


if __name__ == "__main__":
    benchmark_filter_evaluation()
