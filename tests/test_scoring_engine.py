"""
TEST: Phase 7 — Scoring + Stability + Regime + Ranker

Verifies:
1. Core metrics computation (win rate, expectancy, profit factor, drawdown)
2. Stability scoring across time periods
3. Regime analysis (best/worst conditions)
4. Variant ranking with composite scores
5. Full pipeline: trades → exit results → scoring → ranked output

Run: python -m tests.test_scoring_engine
"""

import os
import time
import uuid
import random
from datetime import datetime, timedelta
from pathlib import Path

from app.db.research_store import ResearchStore
from app.scoring.metrics import compute_metrics, VariantMetrics
from app.scoring.stability import compute_stability, StabilityResult
from app.scoring.regime import compute_regime_analysis, compute_regime_summary
from app.scoring.ranker import RankingConfig, VariantRanker, RankedVariant


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


def test_core_metrics():
    """Test metrics computation from PnL lists."""
    print_section("TEST 1: Core Metrics Computation")
    all_passed = True

    # Winning system: 60% WR, 2:1 avg win/loss
    pnls = [100, -50, 80, 120, -40, 90, -60, 110, 75, -45]
    m = compute_metrics(pnls)

    all_passed &= check(m.trade_count == 10, f"Trade count: {m.trade_count}")
    all_passed &= check(m.win_count == 6, f"Win count: {m.win_count}")
    all_passed &= check(abs(m.win_rate - 0.6) < 0.01, f"Win rate: {m.win_rate:.2f}")
    all_passed &= check(m.avg_win > 0, f"Avg win: {m.avg_win:.1f}")
    all_passed &= check(m.avg_loss > 0, f"Avg loss: {m.avg_loss:.1f} (stored positive)")
    all_passed &= check(m.expectancy > 0, f"Expectancy: {m.expectancy:.1f} (positive)")
    all_passed &= check(m.profit_factor > 1.0, f"Profit factor: {m.profit_factor:.2f} > 1.0")
    all_passed &= check(m.net_pnl > 0, f"Net PnL: {m.net_pnl:.1f}")
    all_passed &= check(m.max_drawdown > 0, f"Max drawdown: {m.max_drawdown:.1f}")
    all_passed &= check(m.recovery_factor > 0, f"Recovery factor: {m.recovery_factor:.2f}")
    all_passed &= check(m.sharpe_ratio > 0, f"Sharpe ratio: {m.sharpe_ratio:.2f}")

    # Losing system
    losing_pnls = [-50, -30, 20, -60, -40, 10, -55, -25]
    m2 = compute_metrics(losing_pnls)
    all_passed &= check(m2.expectancy < 0, f"Losing expectancy: {m2.expectancy:.1f}")
    all_passed &= check(m2.profit_factor < 1.0, f"Losing PF: {m2.profit_factor:.2f}")
    all_passed &= check(m2.net_pnl < 0, f"Losing net PnL: {m2.net_pnl:.1f}")

    # Empty
    m3 = compute_metrics([])
    all_passed &= check(m3.trade_count == 0, "Empty: trade_count = 0")

    # Consecutive
    streak_pnls = [10, 20, 30, -5, -10, -15, -20, 50, 60]
    m4 = compute_metrics(streak_pnls)
    all_passed &= check(m4.max_consecutive_wins == 3, f"Max consec wins: {m4.max_consecutive_wins}")
    all_passed &= check(m4.max_consecutive_losses == 4, f"Max consec losses: {m4.max_consecutive_losses}")

    return all_passed


def test_stability():
    """Test stability scoring across periods."""
    print_section("TEST 2: Stability Scoring")
    all_passed = True

    # Create trades spread across multiple weeks
    base_time = datetime(2026, 1, 1, 10, 0).timestamp() * 1000

    # Good stability: consistent profits across weeks
    consistent_trades = []
    for week in range(8):
        for day in range(5):
            t = {
                "entry_time_ms": base_time + (week * 7 + day) * 86400_000,
                "rr2_result": random.uniform(10, 40),  # Consistently positive
            }
            consistent_trades.append(t)

    random.seed(42)
    stab = compute_stability(consistent_trades, "rr2_result", "weekly")
    all_passed &= check(stab.stability_score > 50, f"Consistent system stability: {stab.stability_score:.1f} > 50")
    all_passed &= check(stab.periods_analyzed >= 4, f"Periods analyzed: {stab.periods_analyzed}")
    all_passed &= check(stab.periods_profitable >= 4, f"Profitable periods: {stab.periods_profitable}")

    # Bad stability: one big winning week, rest losing
    random.seed(99)
    inconsistent_trades = []
    for week in range(8):
        for day in range(5):
            if week == 3:  # Only week 3 profitable
                pnl = random.uniform(50, 150)
            else:
                pnl = random.uniform(-20, -5)
            t = {
                "entry_time_ms": base_time + (week * 7 + day) * 86400_000,
                "rr2_result": pnl,
            }
            inconsistent_trades.append(t)

    stab2 = compute_stability(inconsistent_trades, "rr2_result", "weekly")
    all_passed &= check(
        stab2.stability_score < stab.stability_score,
        f"Inconsistent stability ({stab2.stability_score:.1f}) < consistent ({stab.stability_score:.1f})"
    )
    all_passed &= check(stab2.pnl_concentration > 0.5, f"PnL concentrated: {stab2.pnl_concentration:.2f}")

    return all_passed


def test_regime_analysis():
    """Test regime breakdown."""
    print_section("TEST 3: Regime Analysis")
    all_passed = True

    # Create trades with different sessions and conditions
    trades = []
    random.seed(123)

    # Morning trades: profitable
    for i in range(20):
        trades.append({
            "entry_time_ms": 1000000 + i * 1000,
            "session": "MORNING",
            "day_of_week": "MON" if i % 2 == 0 else "TUE",
            "volatility_regime": "HIGH",
            "htf_trend_1h": "BULLISH",
            "gap_direction": "UP",
            "market_structure": "TRENDING",
            "rr2_result": random.uniform(20, 80),
        })

    # Midday trades: losing
    for i in range(20):
        trades.append({
            "entry_time_ms": 2000000 + i * 1000,
            "session": "MIDDAY",
            "day_of_week": "WED" if i % 2 == 0 else "THU",
            "volatility_regime": "LOW",
            "htf_trend_1h": "NEUTRAL",
            "gap_direction": "FLAT",
            "market_structure": "RANGING",
            "rr2_result": random.uniform(-50, -10),
        })

    analysis = compute_regime_analysis(trades, "rr2_result")

    all_passed &= check(analysis.regime_count > 0, f"Regimes found: {analysis.regime_count}")
    all_passed &= check(analysis.profitable_regimes > 0, f"Profitable regimes: {analysis.profitable_regimes}")
    all_passed &= check(
        analysis.best_regime is not None,
        f"Best regime: {analysis.best_regime.regime_name}={analysis.best_regime.regime_value}" if analysis.best_regime else "None"
    )
    all_passed &= check(
        analysis.worst_regime is not None,
        f"Worst regime: {analysis.worst_regime.regime_name}={analysis.worst_regime.regime_value}" if analysis.worst_regime else "None"
    )

    # Best regime should have positive expectancy
    if analysis.best_regime:
        all_passed &= check(
            analysis.best_regime.expectancy > 0,
            f"Best regime expectancy: {analysis.best_regime.expectancy:.1f}"
        )
    if analysis.worst_regime:
        all_passed &= check(
            analysis.worst_regime.expectancy < 0,
            f"Worst regime expectancy: {analysis.worst_regime.expectancy:.1f}"
        )

    # Summary format
    summary = compute_regime_summary(trades, "rr2_result")
    all_passed &= check("session" in summary, "Summary has session dimension")
    all_passed &= check("MORNING" in summary.get("session", {}), "Summary has MORNING")

    return all_passed


def test_full_ranker_pipeline():
    """Test the full ranking pipeline with DB."""
    print_section("TEST 4: Full Ranking Pipeline (DB Integration)")
    all_passed = True

    # Setup test DB
    test_db = Path(__file__).parent.parent / "data" / "test_scoring.db"
    if test_db.exists():
        os.remove(test_db)

    store = ResearchStore(db_path=test_db)
    store.start()

    random.seed(777)
    now = datetime.now()
    base_ms = (now - timedelta(days=20)).timestamp() * 1000

    # Insert trades for 3 different variants
    variant_profiles = {
        "good_variant_01": {"strategy": "ORB", "tf": "5m", "win_pnl_range": (20, 80), "loss_pnl_range": (-30, -10), "win_prob": 0.65},
        "avg_variant_02": {"strategy": "BB", "tf": "15m", "win_pnl_range": (10, 40), "loss_pnl_range": (-35, -15), "win_prob": 0.50},
        "bad_variant_03": {"strategy": "TREND", "tf": "30m", "win_pnl_range": (5, 20), "loss_pnl_range": (-40, -20), "win_prob": 0.35},
    }

    sessions = ["MORNING", "MIDDAY", "CLOSING"]
    trends = ["BULLISH", "BEARISH", "NEUTRAL"]
    vol_regimes = ["LOW", "NORMAL", "HIGH"]

    for variant_id, profile in variant_profiles.items():
        for i in range(30):  # 30 trades per variant
            is_win = random.random() < profile["win_prob"]
            if is_win:
                pnl = random.uniform(*profile["win_pnl_range"])
            else:
                pnl = random.uniform(*profile["loss_pnl_range"])

            trade_id = f"T-{uuid.uuid4().hex[:12]}"
            entry_ms = base_ms + i * 86400_000 / 3  # spread through days

            # Insert trade
            from app.variants.models import TradeRecord
            trade = TradeRecord(
                trade_id=trade_id,
                variant_id=variant_id,
                strategy=profile["strategy"],
                timeframe=profile["tf"],
                instrument="NIFTY",
                direction="LONG",
                entry_time_ms=entry_ms,
                entry_price=25000.0,
                atr_entry=50.0,
                session=random.choice(sessions),
                day_of_week=random.choice(["MON", "TUE", "WED", "THU", "FRI"]),
                volatility_regime=random.choice(vol_regimes),
                htf_trend_1h=random.choice(trends),
                market_structure=random.choice(["TRENDING", "RANGING"]),
            )
            store.write_trade(trade)

            # Insert exit results (simulate multiple exit models)
            exit_results = {
                "rr1": pnl * 0.5,
                "rr2": pnl,
                "rr3": pnl * 1.3,
                "rr5": pnl * 0.8,
                "atr_stop": pnl * 0.9,
                "atr_trail": pnl * 1.1,
                "be_atr_trail": pnl * 1.05,
                "chandelier_3x": pnl * 0.95,
                "mfe": abs(pnl) * 1.5 if pnl > 0 else abs(pnl) * 0.3,
                "mae": -abs(pnl) * 0.5 if pnl > 0 else -abs(pnl) * 1.2,
                "best_exit_model": "rr3" if pnl > 0 else "atr_stop",
                "best_pnl": pnl * 1.3 if pnl > 0 else pnl * 0.9,
                "worst_exit_model": "rr1",
                "worst_pnl": pnl * 0.5,
            }
            store.write_exit_result(trade_id, exit_results)

    total_trades = store.get_total_trade_count()
    all_passed &= check(total_trades == 90, f"90 trades inserted (got {total_trades})")

    # Run ranker
    config = RankingConfig(min_trade_count=10, min_stability=0, top_n=10)
    ranker = VariantRanker(store, config)

    end_ms = now.timestamp() * 1000
    start_ms = base_ms - 86400_000  # buffer

    results = ranker.rank_variants(start_ms, end_ms, "test_period")

    all_passed &= check(len(results) > 0, f"Ranked variants: {len(results)}")

    if results:
        top = results[0]
        all_passed &= check(top.rank == 1, "Top variant has rank 1")
        all_passed &= check(top.composite_score > 0, f"Top composite: {top.composite_score:.1f}")
        all_passed &= check(top.expectancy > 0, f"Top expectancy: {top.expectancy:.1f}")
        all_passed &= check(top.best_exit_model != "", f"Top exit model: {top.best_exit_model}")
        all_passed &= check(top.trade_count >= 10, f"Top trades: {top.trade_count}")

        # The "good" variant should rank highest
        all_passed &= check(
            top.variant_id == "good_variant_01",
            f"Best variant is 'good_variant_01' (got '{top.variant_id}')"
        )

        # Print ranking
        print(f"\n  Ranking results:")
        for rv in results:
            print(
                f"    #{rv.rank} {rv.variant_id:18} | {rv.strategy:5} {rv.timeframe:4} | "
                f"WR={rv.win_rate*100:.0f}% E={rv.expectancy:.1f} "
                f"PF={rv.profit_factor:.2f} Stab={rv.stability_score:.0f} "
                f"Score={rv.composite_score:.1f} | Exit={rv.best_exit_model}"
            )

    # Verify scores saved to DB
    saved = store.get_top_variants("test_period", 10)
    all_passed &= check(len(saved) > 0, f"Scores saved to DB: {len(saved)} rows")

    # Cleanup
    store.stop()
    if test_db.exists():
        os.remove(test_db)

    return all_passed


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("  PHASE 7: SCORING + STABILITY ENGINE TESTS")
    print("=" * 60)

    t0 = time.time()
    all_passed = True

    all_passed &= test_core_metrics()
    all_passed &= test_stability()
    all_passed &= test_regime_analysis()
    all_passed &= test_full_ranker_pipeline()

    total = time.time() - t0
    print(f"\n{'═' * 60}")
    if all_passed:
        print(f"  ✅ ALL PHASE 7 TESTS PASSED")
    else:
        print(f"  ❌ SOME TESTS FAILED")
    print(f"  Total time: {total:.2f}s")
    print(f"{'═' * 60}")

    return all_passed


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
