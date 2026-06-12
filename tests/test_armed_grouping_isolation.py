"""
TEST: Armed State + Grouping isolation across instruments and timeframes.

Verifies:
1. Armed variants for NIFTY don't interfere with BANKNIFTY
2. Expiry of 5m variants doesn't affect 15m/30m variants
3. Triggering on one instrument doesn't clear another
4. Grouping rebuilds per instrument are independent
5. Mixed timeframe variants expire correctly with timeframe-scoped counters
6. Daily reset clears everything

Run: python -m tests.test_armed_grouping_isolation
"""

import time
from app.execution.armed_state import ArmedStateManager
from app.execution.grouping import GroupingEngine, PriceGroup
from app.execution.tick_engine import TickTriggerEngine
from app.broker.base import Tick
from app.variants.models import (
    ArmedVariant,
    Direction,
    EntryMode,
    FilterSet,
    IndicatorSnapshot,
    MetadataSnapshot,
    ResearchTimeframe,
    StrategyType,
    TriggerType,
    Variant,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def make_variant(strategy: StrategyType, timeframe: ResearchTimeframe, idx: int = 0) -> Variant:
    """Create a variant with a unique filter combo based on idx."""
    from app.variants.models import ATRFilter, ADXFilter, VIXFilter, VolumeFilter, RSIFilter, VWAPFilter
    # Use idx to pick different filter combos for uniqueness
    atr_vals = list(ATRFilter)
    adx_vals = list(ADXFilter)
    filters = FilterSet(
        atr=atr_vals[idx % len(atr_vals)],
        adx=adx_vals[(idx // len(atr_vals)) % len(adx_vals)],
    )
    return Variant(strategy=strategy, timeframe=timeframe, filters=filters, entry_mode=EntryMode.INTRABAR)


def make_armed(
    variant: Variant,
    instrument: str,
    direction: Direction,
    trigger_value: float,
    armed_at_candle: int,
    expiry: int = 3,
) -> ArmedVariant:
    """Create an ArmedVariant."""
    return ArmedVariant(
        variant=variant,
        instrument=instrument,
        direction=direction,
        trigger_type=TriggerType.PRICE_LEVEL,
        trigger_value=trigger_value,
        armed_at_candle=armed_at_candle,
        expiry_candles=expiry,
    )


def print_section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def check(condition, msg):
    if condition:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
    return condition


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_multi_instrument_isolation():
    """Armed state for different instruments must be completely independent."""
    print_section("TEST 1: Multi-Instrument Armed State Isolation")

    armed_state = ArmedStateManager(max_armed_per_instrument=1000)
    all_passed = True

    # Create armed variants for NIFTY (ORB 5m, expiry 3 candles)
    nifty_variants = []
    for i in range(10):
        v = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=i)
        av = make_armed(v, "NIFTY", Direction.LONG, 25000.0 + i, armed_at_candle=1, expiry=3)
        nifty_variants.append(av)

    # Create armed variants for BANKNIFTY (BB 5m, expiry 3 candles)
    bnf_variants = []
    for i in range(8):
        v = make_variant(StrategyType.BOLLINGER_BANDS, ResearchTimeframe.M5, idx=i + 20)
        av = make_armed(v, "BANKNIFTY", Direction.SHORT, 52000.0 - i * 10, armed_at_candle=1, expiry=3)
        bnf_variants.append(av)

    # Create armed variants for RELIANCE (TREND 15m, expiry 5 candles)
    rel_variants = []
    for i in range(5):
        v = make_variant(StrategyType.TREND_FOLLOWING, ResearchTimeframe.M15, idx=i + 40)
        av = make_armed(v, "2885", Direction.LONG, 1420.0 + i, armed_at_candle=1, expiry=5)
        rel_variants.append(av)

    # Arm all
    armed_state.arm(nifty_variants)
    armed_state.arm(bnf_variants)
    armed_state.arm(rel_variants)

    all_passed &= check(armed_state.get_armed_count("NIFTY") == 10, "NIFTY has 10 armed")
    all_passed &= check(armed_state.get_armed_count("BANKNIFTY") == 8, "BANKNIFTY has 8 armed")
    all_passed &= check(armed_state.get_armed_count("2885") == 5, "RELIANCE has 5 armed")
    all_passed &= check(armed_state.get_armed_count() == 23, "Total armed: 23")

    # Disarm triggered on NIFTY — should NOT affect BANKNIFTY or RELIANCE
    triggered_ids = [nifty_variants[0].variant_id, nifty_variants[1].variant_id]
    armed_state.disarm_triggered("NIFTY", triggered_ids)

    all_passed &= check(armed_state.get_armed_count("NIFTY") == 8, "NIFTY dropped to 8 after trigger")
    all_passed &= check(armed_state.get_armed_count("BANKNIFTY") == 8, "BANKNIFTY still 8 (unaffected)")
    all_passed &= check(armed_state.get_armed_count("2885") == 5, "RELIANCE still 5 (unaffected)")

    # Expire NIFTY at candle 5 (expiry=3, armed_at=1, so expired at candle 4+)
    expired = armed_state.cleanup_expired("NIFTY", current_candle=5)
    all_passed &= check(expired == 8, f"NIFTY: all 8 remaining expired (got {expired})")
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 0, "NIFTY now empty")
    all_passed &= check(armed_state.get_armed_count("BANKNIFTY") == 8, "BANKNIFTY still 8 (untouched)")
    all_passed &= check(armed_state.get_armed_count("2885") == 5, "RELIANCE still 5 (untouched)")

    return all_passed


def test_timeframe_scoped_expiry():
    """5m candle counter must NOT expire 15m or 30m variants."""
    print_section("TEST 2: Timeframe-Scoped Expiry")

    armed_state = ArmedStateManager(max_armed_per_instrument=1000)
    all_passed = True

    # Mix of timeframes on same instrument
    # 5m variants (expiry=3)
    v5m_a = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=0)
    v5m_b = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=1)
    av5m_a = make_armed(v5m_a, "NIFTY", Direction.LONG, 25000.0, armed_at_candle=1, expiry=3)
    av5m_b = make_armed(v5m_b, "NIFTY", Direction.SHORT, 24900.0, armed_at_candle=1, expiry=3)

    # 15m variants (expiry=4)
    v15m = make_variant(StrategyType.BOLLINGER_BANDS, ResearchTimeframe.M15, idx=10)
    av15m = make_armed(v15m, "NIFTY", Direction.LONG, 25050.0, armed_at_candle=1, expiry=4)

    # 30m variants (expiry=5)
    v30m = make_variant(StrategyType.TREND_FOLLOWING, ResearchTimeframe.M30, idx=20)
    av30m = make_armed(v30m, "NIFTY", Direction.SHORT, 24800.0, armed_at_candle=1, expiry=5)

    armed_state.arm([av5m_a, av5m_b, av15m, av30m])
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 4, "NIFTY starts with 4 armed (mixed TFs)")

    # Simulate: 5m candle counter reaches 4 → only 5m should expire
    # (armed_at=1, expiry=3, current=4 → 4-1=3 >= 3 → expired)
    expired = armed_state.cleanup_expired("NIFTY", current_candle=4, timeframe="5m")
    all_passed &= check(expired == 2, f"5m expiry at candle 4: only 5m variants expired (got {expired})")

    # 15m and 30m should still be there
    remaining = armed_state.get_armed("NIFTY")
    remaining_tfs = [av.variant.timeframe.value for av in remaining]
    all_passed &= check("15m" in remaining_tfs, "15m variant still armed")
    all_passed &= check("30m" in remaining_tfs, "30m variant still armed")
    all_passed &= check("5m" not in remaining_tfs, "5m variants gone")

    # Now simulate 15m counter reaches 5 → 15m expires (armed_at=1, expiry=4, 5-1=4 >= 4)
    expired = armed_state.cleanup_expired("NIFTY", current_candle=5, timeframe="15m")
    all_passed &= check(expired == 1, f"15m expiry at candle 5: expired (got {expired})")

    remaining = armed_state.get_armed("NIFTY")
    remaining_tfs = [av.variant.timeframe.value for av in remaining]
    all_passed &= check("30m" in remaining_tfs, "30m still armed")
    all_passed &= check(len(remaining) == 1, "Only 30m left")

    # 30m counter reaches 6 → 30m expires (armed_at=1, expiry=5, 6-1=5 >= 5)
    expired = armed_state.cleanup_expired("NIFTY", current_candle=6, timeframe="30m")
    all_passed &= check(expired == 1, f"30m expiry at candle 6: expired (got {expired})")
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 0, "All expired — NIFTY empty")

    return all_passed


def test_grouping_per_instrument():
    """Grouping engine must maintain separate groups per instrument."""
    print_section("TEST 3: Grouping Engine Per-Instrument Isolation")

    grouping = GroupingEngine()
    armed_state = ArmedStateManager()
    all_passed = True

    # NIFTY armed variants — 2 price levels
    nifty_armed = []
    for i in range(6):
        v = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=i)
        level = 25000.0 if i < 3 else 24900.0
        direction = Direction.LONG if i < 3 else Direction.SHORT
        av = make_armed(v, "NIFTY", direction, level, armed_at_candle=1)
        nifty_armed.append(av)

    # BANKNIFTY armed variants — different levels
    bnf_armed = []
    for i in range(4):
        v = make_variant(StrategyType.BOLLINGER_BANDS, ResearchTimeframe.M5, idx=i + 50)
        av = make_armed(v, "BANKNIFTY", Direction.LONG, 52500.0, armed_at_candle=1)
        bnf_armed.append(av)

    armed_state.arm(nifty_armed)
    armed_state.arm(bnf_armed)

    # Rebuild groups per instrument
    nifty_groups = grouping.rebuild("NIFTY", armed_state.get_armed("NIFTY"))
    bnf_groups = grouping.rebuild("BANKNIFTY", armed_state.get_armed("BANKNIFTY"))

    all_passed &= check(nifty_groups == 2, f"NIFTY has 2 groups (got {nifty_groups})")
    all_passed &= check(bnf_groups == 1, f"BANKNIFTY has 1 group (got {bnf_groups})")

    # Check a tick on NIFTY doesn't fire BANKNIFTY groups
    triggered = grouping.check_triggers("NIFTY", 25001.0)
    all_passed &= check(len(triggered) == 1, f"NIFTY tick fires 1 group (got {len(triggered)})")
    all_passed &= check(triggered[0].count == 3, f"NIFTY group has 3 members (got {triggered[0].count})")

    # BANKNIFTY untouched
    bnf_check = grouping.check_triggers("BANKNIFTY", 25001.0)
    all_passed &= check(len(bnf_check) == 0, "BANKNIFTY NOT triggered by NIFTY's price")

    # BANKNIFTY triggers at its own level
    bnf_triggered = grouping.check_triggers("BANKNIFTY", 52501.0)
    all_passed &= check(len(bnf_triggered) == 1, "BANKNIFTY triggers at 52500")
    all_passed &= check(bnf_triggered[0].count == 4, f"BANKNIFTY group has 4 members (got {bnf_triggered[0].count})")

    return all_passed


def test_trigger_clears_only_fired_group():
    """Triggering one price group must NOT clear other groups on same instrument."""
    print_section("TEST 4: Trigger Clears Only Fired Group")

    armed_state = ArmedStateManager()
    grouping = GroupingEngine()
    tick_engine = TickTriggerEngine(armed_state, grouping)
    tick_engine.update_snapshot("NIFTY", IndicatorSnapshot())
    tick_engine.update_metadata("NIFTY", MetadataSnapshot())
    all_passed = True

    # Two distinct price groups: 25000 (LONG) and 24900 (SHORT)
    armed = []
    for i in range(5):
        v = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=i)
        av = make_armed(v, "NIFTY", Direction.LONG, 25000.0, armed_at_candle=1)
        armed.append(av)
    for i in range(3):
        v = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=i + 10)
        av = make_armed(v, "NIFTY", Direction.SHORT, 24900.0, armed_at_candle=1)
        armed.append(av)

    armed_state.arm(armed)
    grouping.rebuild("NIFTY", armed_state.get_armed("NIFTY"))

    all_passed &= check(grouping.get_group_count("NIFTY") == 2, "2 groups before trigger")
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 8, "8 armed before trigger")

    # Fire LONG group (price crosses 25000)
    tick = Tick(exchange="NSE", segment="CASH", exchange_token="NIFTY", ltp=25001.0, timestamp_ms=1000000.0)
    fired = tick_engine.on_tick(tick)

    all_passed &= check(fired == 5, f"Fired 5 LONG variants (got {fired})")
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 3, "3 SHORT still armed")
    all_passed &= check(grouping.get_group_count("NIFTY") == 1, "1 group remaining (SHORT)")

    # SHORT group still works
    tick2 = Tick(exchange="NSE", segment="CASH", exchange_token="NIFTY", ltp=24899.0, timestamp_ms=1000001.0)
    fired2 = tick_engine.on_tick(tick2)

    all_passed &= check(fired2 == 3, f"Fired 3 SHORT variants (got {fired2})")
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 0, "All cleared now")
    all_passed &= check(grouping.get_group_count("NIFTY") == 0, "No groups left")

    return all_passed


def test_re_arm_prevention():
    """A variant that triggered today must NOT be re-armed within same session."""
    print_section("TEST 5: Re-Arm Prevention (same session)")

    armed_state = ArmedStateManager()
    all_passed = True

    v = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=0)
    av = make_armed(v, "NIFTY", Direction.LONG, 25000.0, armed_at_candle=1)

    armed_state.arm([av])
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 1, "Initially armed")

    # Trigger it
    armed_state.disarm_triggered("NIFTY", [av.variant_id])
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 0, "Disarmed after trigger")

    # Try to re-arm same variant
    av2 = make_armed(v, "NIFTY", Direction.LONG, 25050.0, armed_at_candle=2)
    armed_state.arm([av2])
    all_passed &= check(
        armed_state.get_armed_count("NIFTY") == 0,
        "Cannot re-arm same variant in same session"
    )

    # Different instrument is fine
    av3 = make_armed(v, "BANKNIFTY", Direction.LONG, 52000.0, armed_at_candle=2)
    armed_state.arm([av3])
    all_passed &= check(
        armed_state.get_armed_count("BANKNIFTY") == 1,
        "Same variant on DIFFERENT instrument CAN be armed"
    )

    # After daily reset, can re-arm
    armed_state.reset_daily()
    av4 = make_armed(v, "NIFTY", Direction.LONG, 25000.0, armed_at_candle=1)
    armed_state.arm([av4])
    all_passed &= check(
        armed_state.get_armed_count("NIFTY") == 1,
        "After daily reset, variant can be re-armed"
    )

    return all_passed


def test_max_armed_bound():
    """Armed state must reject new variants when instrument hits its bound."""
    print_section("TEST 6: Max Armed Per Instrument Bound")

    armed_state = ArmedStateManager(max_armed_per_instrument=20)
    all_passed = True

    # Try to arm 30 variants for NIFTY (max=20)
    armed = []
    for i in range(30):
        v = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=i)
        av = make_armed(v, "NIFTY", Direction.LONG, 25000.0 + i, armed_at_candle=1)
        armed.append(av)

    added = armed_state.arm(armed)
    all_passed &= check(added == 20, f"Only 20 armed (max bound hit), got {added}")
    all_passed &= check(armed_state.get_armed_count("NIFTY") == 20, "Count capped at 20")

    # BANKNIFTY is independent — gets its own 20
    bnf_armed = []
    for i in range(25):
        v = make_variant(StrategyType.BOLLINGER_BANDS, ResearchTimeframe.M5, idx=i + 100)
        av = make_armed(v, "BANKNIFTY", Direction.SHORT, 52000.0 - i, armed_at_candle=1)
        bnf_armed.append(av)

    added2 = armed_state.arm(bnf_armed)
    all_passed &= check(added2 == 20, f"BANKNIFTY also capped at 20, got {added2}")

    return all_passed


def test_grouping_rebuild_replaces():
    """Rebuilding groups for an instrument fully replaces previous groups."""
    print_section("TEST 7: Grouping Rebuild Replaces (Not Accumulates)")

    grouping = GroupingEngine()
    all_passed = True

    # First build: 3 groups
    armed_a = []
    for i in range(3):
        v = make_variant(StrategyType.ORB, ResearchTimeframe.M5, idx=i)
        av = make_armed(v, "NIFTY", Direction.LONG, 25000.0 + i * 100, armed_at_candle=1)
        armed_a.append(av)

    count_a = grouping.rebuild("NIFTY", armed_a)
    all_passed &= check(count_a == 3, f"First build: 3 groups (got {count_a})")

    # Second build: completely different — only 1 group
    armed_b = []
    for i in range(5):
        v = make_variant(StrategyType.BOLLINGER_BANDS, ResearchTimeframe.M15, idx=i + 50)
        av = make_armed(v, "NIFTY", Direction.SHORT, 24500.0, armed_at_candle=2)
        armed_b.append(av)

    count_b = grouping.rebuild("NIFTY", armed_b)
    all_passed &= check(count_b == 1, f"Rebuild: now 1 group (got {count_b})")

    # Old levels should not trigger
    triggered = grouping.check_triggers("NIFTY", 25001.0)
    all_passed &= check(len(triggered) == 0, "Old 25000 level no longer active")

    # New level should trigger
    triggered2 = grouping.check_triggers("NIFTY", 24499.0)
    all_passed &= check(len(triggered2) == 1, "New 24500 level active")
    all_passed &= check(triggered2[0].count == 5, f"Group has 5 members (got {triggered2[0].count})")

    return all_passed


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("  ARMED STATE + GROUPING ISOLATION TESTS")
    print("  Multi-instrument, multi-timeframe, edge cases")
    print("=" * 60)

    t0 = time.time()
    all_passed = True

    all_passed &= test_multi_instrument_isolation()
    all_passed &= test_timeframe_scoped_expiry()
    all_passed &= test_grouping_per_instrument()
    all_passed &= test_trigger_clears_only_fired_group()
    all_passed &= test_re_arm_prevention()
    all_passed &= test_max_armed_bound()
    all_passed &= test_grouping_rebuild_replaces()

    total = time.time() - t0
    print(f"\n{'═' * 60}")
    if all_passed:
        print(f"  ✅ ALL 7 TESTS PASSED")
    else:
        print(f"  ❌ SOME TESTS FAILED")
    print(f"  Total time: {total:.2f}s")
    print(f"{'═' * 60}")

    return all_passed


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
