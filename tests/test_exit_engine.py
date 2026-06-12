"""
TEST: Phase 6 — Post-Market Exit Simulation Engine

Verifies:
1. RR exit models (TP hit, SL hit, EOD close)
2. Stop loss models (ATR, swing, fixed)
3. Trailing models (ATR trail, EMA trail, swing trail)
4. Partial exit models (A, B, C)
5. MFE/MAE computation
6. Full engine pipeline (trade → candle path → all exits → DB write)
7. Determinism (same input → same output)

Run: python -m tests.test_exit_engine
"""

import os
import time
from datetime import datetime
from pathlib import Path

from app.db.research_store import ResearchStore
from app.exit_engine.engine import ExitSimulationEngine
from app.exit_engine.models.partial_exit_models import (
    simulate_all_partials,
    simulate_partial_a,
)
from app.exit_engine.models.rr_exit import ExitResult, simulate_all_rr, simulate_rr_exit
from app.exit_engine.models.stop_loss_models import (
    simulate_all_stops,
    simulate_atr_stop,
    simulate_fixed_stop,
    simulate_swing_stop,
)
from app.exit_engine.models.trailing_models import (
    simulate_all_trails,
    simulate_atr_trail,
    simulate_ema_trail,
    simulate_swing_trail,
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


# ─── Sample Data ─────────────────────────────────────────────────────────────

def make_trending_up_path(entry_price: float, candles: int = 60) -> list[dict]:
    """Generate a path that trends up from entry (LONG wins)."""
    path = []
    price = entry_price
    for i in range(candles):
        drift = 2.0 + (i * 0.3)  # gradually increasing
        noise = 5.0
        o = price
        h = price + drift + noise
        l = price - noise * 0.5
        c = price + drift
        path.append({"open": o, "high": h, "low": l, "close": c, "volume": 10000})
        price = c
    return path


def make_trending_down_path(entry_price: float, candles: int = 60) -> list[dict]:
    """Generate a path that trends down from entry (SHORT wins)."""
    path = []
    price = entry_price
    for i in range(candles):
        drift = 2.0 + (i * 0.3)
        noise = 5.0
        o = price
        h = price + noise * 0.5
        l = price - drift - noise
        c = price - drift
        path.append({"open": o, "high": h, "low": l, "close": c, "volume": 10000})
        price = c
    return path


def make_choppy_path(entry_price: float, candles: int = 60) -> list[dict]:
    """Generate a choppy (range-bound) path."""
    import math
    path = []
    for i in range(candles):
        # Oscillate around entry
        offset = 30.0 * math.sin(i * 0.3)
        o = entry_price + offset
        h = o + 15.0
        l = o - 15.0
        c = entry_price + offset * 0.8
        path.append({"open": o, "high": h, "low": l, "close": c, "volume": 10000})
    return path


def make_spike_then_reverse_path(entry_price: float) -> list[dict]:
    """Path spikes in favor then reverses hard (tests trailing stops)."""
    path = []
    price = entry_price

    # First 20 candles: strong up move
    for i in range(20):
        price += 5.0
        path.append({
            "open": price - 3, "high": price + 2,
            "low": price - 5, "close": price, "volume": 10000
        })

    # Next 40 candles: reverse down
    for i in range(40):
        price -= 4.0
        path.append({
            "open": price + 3, "high": price + 5,
            "low": price - 2, "close": price, "volume": 10000
        })

    return path


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_rr_exits():
    """Test fixed risk-reward exits."""
    print_section("TEST 1: Risk-Reward Exit Models")
    all_passed = True

    entry = 25000.0
    atr = 50.0  # 1R = 50 points

    # LONG trade with trending up path — should hit TPs
    up_path = make_trending_up_path(entry, 60)
    r1 = simulate_rr_exit(entry, "LONG", atr, up_path, 1.0)
    all_passed &= check(r1.exit_reason == "TP_HIT", f"RR1 LONG hits TP (pnl={r1.pnl_points:.1f})")
    all_passed &= check(abs(r1.pnl_points - 50.0) < 0.01, "RR1 PnL = +50 (1 ATR)")

    r2 = simulate_rr_exit(entry, "LONG", atr, up_path, 2.0)
    all_passed &= check(r2.exit_reason == "TP_HIT", "RR2 LONG hits TP")
    all_passed &= check(abs(r2.pnl_points - 100.0) < 0.01, "RR2 PnL = +100 (2 ATR)")

    # SHORT trade with trending up path — should hit SL
    r_short = simulate_rr_exit(entry, "SHORT", atr, up_path, 2.0)
    all_passed &= check(r_short.exit_reason == "SL_HIT", "SHORT against trend hits SL")
    all_passed &= check(r_short.pnl_points < 0, f"SHORT SL is a loss (pnl={r_short.pnl_points:.1f})")

    # Choppy path with large RR — should close at EOD
    choppy = make_choppy_path(entry, 60)
    r10 = simulate_rr_exit(entry, "LONG", atr, choppy, 10.0)
    all_passed &= check(r10.exit_reason == "CLOSE_AT_EOD", "RR10 on choppy → EOD close")

    # Run all RR models
    all_rr = simulate_all_rr(entry, "LONG", atr, up_path)
    all_passed &= check(len(all_rr) == 7, f"7 RR models run (got {len(all_rr)})")
    all_passed &= check(all_rr["rr1"] > 0, f"RR1 positive: {all_rr['rr1']:.1f}")

    return all_passed


def test_stop_loss_models():
    """Test ATR, swing, and fixed stop loss models."""
    print_section("TEST 2: Stop Loss Models")
    all_passed = True

    entry = 25000.0
    atr = 50.0

    # LONG with down path — all stops should hit
    down_path = make_trending_down_path(entry, 60)

    atr_result = simulate_atr_stop(entry, "LONG", atr, down_path)
    all_passed &= check(atr_result.exit_reason == "ATR_SL_HIT", "ATR stop hits on down path")
    all_passed &= check(atr_result.pnl_points < 0, f"ATR stop loss: {atr_result.pnl_points:.1f}")

    fixed_result = simulate_fixed_stop(entry, "LONG", down_path)
    all_passed &= check(fixed_result.exit_reason == "FIXED_SL_HIT", "Fixed stop hits on down path")

    # Swing stop with pre-entry candles
    pre_entry = [
        {"open": 24950, "high": 24980, "low": 24920, "close": 24960},
        {"open": 24960, "high": 24990, "low": 24930, "close": 24970},
        {"open": 24970, "high": 25010, "low": 24940, "close": 24990},
    ]
    swing_result = simulate_swing_stop(entry, "LONG", down_path, pre_entry)
    all_passed &= check(swing_result.exit_reason == "SWING_SL_HIT", "Swing stop hits")
    # Swing low from pre_entry = 24920
    all_passed &= check(
        abs(swing_result.exit_price - 24920.0) < 0.01,
        f"Swing stop at swing low 24920 (got {swing_result.exit_price:.0f})"
    )

    # LONG with up path — stops should NOT hit (EOD close)
    up_path = make_trending_up_path(entry, 60)
    atr_up = simulate_atr_stop(entry, "LONG", atr, up_path)
    all_passed &= check(atr_up.exit_reason == "CLOSE_AT_EOD", "ATR stop NOT hit on up path")
    all_passed &= check(atr_up.pnl_points > 0, f"Profitable at EOD: {atr_up.pnl_points:.1f}")

    # Run all stops
    all_stops = simulate_all_stops(entry, "LONG", atr, down_path, pre_entry)
    all_passed &= check(len(all_stops) == 3, f"3 stop models (got {len(all_stops)})")
    all_passed &= check(all(v < 0 for v in all_stops.values()), "All stops are losses on down path")

    return all_passed


def test_trailing_models():
    """Test ATR, EMA, and swing trailing stop models."""
    print_section("TEST 3: Trailing Stop Models")
    all_passed = True

    entry = 25000.0
    atr = 50.0

    # Spike then reverse — trailing should lock in some profit
    spike_path = make_spike_then_reverse_path(entry)

    atr_trail = simulate_atr_trail(entry, "LONG", atr, spike_path)
    all_passed &= check(
        atr_trail.exit_reason == "ATR_TRAIL_HIT",
        f"ATR trail triggered (pnl={atr_trail.pnl_points:.1f})"
    )
    # Should be positive (trail locked in profit during spike)
    all_passed &= check(atr_trail.pnl_points > 0, "ATR trail locks profit on spike+reverse")

    ema_trail = simulate_ema_trail(entry, "LONG", atr, spike_path)
    all_passed &= check(
        ema_trail.exit_reason in ("EMA_TRAIL_EXIT", "EMA_INITIAL_SL", "CLOSE_AT_EOD"),
        f"EMA trail exits ({ema_trail.exit_reason})"
    )

    swing_trail = simulate_swing_trail(entry, "LONG", atr, spike_path)
    all_passed &= check(
        swing_trail.exit_reason in ("SWING_TRAIL_HIT", "CLOSE_AT_EOD"),
        f"Swing trail exits ({swing_trail.exit_reason})"
    )

    # Trending up strongly — trails should close at EOD with large profit
    strong_up = make_trending_up_path(entry, 60)
    atr_strong = simulate_atr_trail(entry, "LONG", atr, strong_up)
    all_passed &= check(
        atr_strong.pnl_points > 50,
        f"ATR trail on strong trend: large profit ({atr_strong.pnl_points:.1f})"
    )

    # Run all trails
    all_trails = simulate_all_trails(entry, "LONG", atr, spike_path)
    all_passed &= check(len(all_trails) == 3, f"3 trail models (got {len(all_trails)})")

    return all_passed


def test_partial_exits():
    """Test partial exit models."""
    print_section("TEST 4: Partial Exit Models")
    all_passed = True

    entry = 25000.0
    atr = 50.0
    up_path = make_trending_up_path(entry, 60)

    # Partial A on trending up — should hit RR1 then trail
    pa = simulate_partial_a(entry, "LONG", atr, up_path)
    all_passed &= check(pa.exit_reason == "PARTIAL_A_BLENDED", f"Partial A blended exit")
    all_passed &= check(pa.pnl_points > 0, f"Partial A profitable: {pa.pnl_points:.1f}")

    # Partial A on down path — full SL
    down_path = make_trending_down_path(entry, 60)
    pa_loss = simulate_partial_a(entry, "LONG", atr, down_path)
    all_passed &= check(pa_loss.pnl_points < 0, f"Partial A loss on down: {pa_loss.pnl_points:.1f}")

    # All partials
    all_p = simulate_all_partials(entry, "LONG", atr, up_path)
    all_passed &= check(len(all_p) == 3, f"3 partial models (got {len(all_p)})")
    all_passed &= check(all(v > 0 for v in all_p.values()), "All partials profitable on up trend")

    # Partial C should have lower per-unit profit than A (more conservative scaling)
    all_passed &= check(
        all_p["partial_a"] >= all_p["partial_c"] * 0.5,
        f"Partial A >= Partial C × 0.5 (A={all_p['partial_a']:.1f}, C={all_p['partial_c']:.1f})"
    )

    return all_passed


def test_mfe_mae():
    """Test MFE/MAE computation."""
    print_section("TEST 5: MFE / MAE Excursion Analysis")
    all_passed = True

    entry = 25000.0
    spike_path = make_spike_then_reverse_path(entry)

    from app.exit_engine.engine import ExitSimulationEngine
    mfe, mae = ExitSimulationEngine._compute_excursions(entry, "LONG", spike_path)

    all_passed &= check(mfe > 0, f"MFE positive (best unrealized profit): {mfe:.1f}")
    all_passed &= check(mae < 0, f"MAE negative (worst unrealized loss): {mae:.1f}")
    all_passed &= check(mfe > abs(mae), "MFE > |MAE| on spike path (favorable first)")

    # SHORT on down path
    down_path = make_trending_down_path(entry, 60)
    mfe_s, mae_s = ExitSimulationEngine._compute_excursions(entry, "SHORT", down_path)
    all_passed &= check(mfe_s > 0, f"SHORT MFE positive on down path: {mfe_s:.1f}")

    return all_passed


def test_determinism():
    """Same input must always produce same output."""
    print_section("TEST 6: Determinism (same input → same output)")
    all_passed = True

    entry = 25000.0
    atr = 50.0
    up_path = make_trending_up_path(entry, 60)

    # Run twice
    results_1 = simulate_all_rr(entry, "LONG", atr, up_path)
    results_2 = simulate_all_rr(entry, "LONG", atr, up_path)
    all_passed &= check(results_1 == results_2, "RR results identical on re-run")

    trails_1 = simulate_all_trails(entry, "LONG", atr, up_path)
    trails_2 = simulate_all_trails(entry, "LONG", atr, up_path)
    all_passed &= check(trails_1 == trails_2, "Trail results identical on re-run")

    partials_1 = simulate_all_partials(entry, "LONG", atr, up_path)
    partials_2 = simulate_all_partials(entry, "LONG", atr, up_path)
    all_passed &= check(partials_1 == partials_2, "Partial results identical on re-run")

    return all_passed


def test_full_engine_pipeline():
    """Test the full engine: trade → candle path → exits → DB."""
    print_section("TEST 7: Full Exit Engine Pipeline (DB Integration)")
    all_passed = True

    # Setup test DB
    test_db = Path(__file__).parent.parent / "data" / "test_exit_engine.db"
    if test_db.exists():
        os.remove(test_db)

    store = ResearchStore(db_path=test_db)
    store.start()

    # Insert a fake trade
    from app.variants.models import TradeRecord
    import uuid

    now = datetime.now()
    entry_time_ms = now.replace(hour=10, minute=30).timestamp() * 1000

    trade = TradeRecord(
        trade_id=f"T-{uuid.uuid4().hex[:12]}",
        variant_id="test_variant_001",
        strategy="ORB",
        timeframe="5m",
        instrument="NIFTY",
        direction="LONG",
        entry_time_ms=entry_time_ms,
        entry_price=25000.0,
        atr_entry=50.0,
        adx_entry=25.0,
        rsi_entry=55.0,
    )
    store.write_trade(trade)

    # Insert candle path (5m candles from 10:30 to 15:30 = ~60 candles)
    up_path = make_trending_up_path(25000.0, 60)
    for i, candle in enumerate(up_path):
        ts = entry_time_ms + (i * 300_000)  # 5-min intervals
        store.cache_candle(
            instrument="NIFTY", timeframe="5m",
            timestamp_ms=ts, o=candle["open"], h=candle["high"],
            l=candle["low"], c=candle["close"], volume=candle["volume"],
            session_date=now.strftime("%Y-%m-%d"),
        )

    # Run exit engine
    engine = ExitSimulationEngine(store)
    date_str = now.strftime("%Y-%m-%d")
    stats = engine.run_for_date(date_str)

    all_passed &= check(stats.trades_processed == 1, f"1 trade processed (got {stats.trades_processed})")
    all_passed &= check(stats.trades_skipped == 0, "0 skipped")

    # Verify results in DB
    results = store._query(
        "SELECT * FROM exit_results WHERE trade_id=?", (trade.trade_id,)
    )
    all_passed &= check(len(results) == 1, "Exit result row written")

    if results:
        row = results[0]
        all_passed &= check(row["rr1_result"] is not None, f"RR1 result: {row['rr1_result']}")
        all_passed &= check(row["rr2_result"] is not None, f"RR2 result: {row['rr2_result']}")
        all_passed &= check(row["atr_stop_result"] is not None, f"ATR stop result: {row['atr_stop_result']}")
        all_passed &= check(row["atr_trail_result"] is not None, f"ATR trail result: {row['atr_trail_result']}")
        all_passed &= check(row["partial_a_result"] is not None, f"Partial A result: {row['partial_a_result']}")
        all_passed &= check(row["mfe"] > 0, f"MFE positive: {row['mfe']:.1f}")
        all_passed &= check(row["best_exit_model"] != "", f"Best exit: {row['best_exit_model']}")
        all_passed &= check(row["best_pnl"] > 0, f"Best PnL: {row['best_pnl']:.1f}")

    # Re-run (idempotent) — should overwrite, not duplicate
    stats2 = engine.run_for_date(date_str)
    all_passed &= check(stats2.trades_processed == 1, "Re-run processes same trade")
    results2 = store._query("SELECT COUNT(*) as cnt FROM exit_results")
    all_passed &= check(results2[0]["cnt"] == 1, "Still 1 row (UPSERT, not duplicate)")

    # Cleanup
    store.stop()
    if test_db.exists():
        os.remove(test_db)

    return all_passed


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("  PHASE 6: EXIT SIMULATION ENGINE TESTS")
    print("=" * 60)

    t0 = time.time()
    all_passed = True

    all_passed &= test_rr_exits()
    all_passed &= test_stop_loss_models()
    all_passed &= test_trailing_models()
    all_passed &= test_partial_exits()
    all_passed &= test_mfe_mae()
    all_passed &= test_determinism()
    all_passed &= test_full_engine_pipeline()

    total = time.time() - t0
    print(f"\n{'═' * 60}")
    if all_passed:
        print(f"  ✅ ALL PHASE 6 TESTS PASSED")
    else:
        print(f"  ❌ SOME TESTS FAILED")
    print(f"  Total time: {total:.2f}s")
    print(f"{'═' * 60}")

    return all_passed


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
