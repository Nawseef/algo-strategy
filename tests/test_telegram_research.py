"""
TEST: Phase 8 — Telegram Research Notifier

Verifies:
1. All message types format without errors
2. Messages contain expected data
3. Works when disabled (no token)
4. Message length reasonable for Telegram (< 4096 chars)

Run: python -m tests.test_telegram_research
"""

import time
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

from app.telegram.research_notifier import ResearchNotifier


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


class MessageCapture:
    """Captures messages instead of sending to Telegram."""

    def __init__(self):
        self.messages: list[str] = []

    def capture(self, text: str):
        self.messages.append(text)

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ""


def test_daily_summary():
    """Test daily trade summary message."""
    print_section("TEST 1: Daily Trade Summary")
    all_passed = True

    notifier = ResearchNotifier("fake_token", ["123456"])
    capture = MessageCapture()
    notifier._send = capture.capture

    notifier.send_daily_summary(
        date_str="2026-06-11",
        total_trades=142,
        trades_by_strategy={"ORB": 45, "BB": 32, "VPA": 28, "TREND": 22, "MR": 15},
        trades_by_instrument={"NIFTY": 80, "BANKNIFTY": 42, "RELIANCE": 20},
        armed_state_stats={"total_armed": 3200, "triggered_today": 142, "instruments_active": 3},
        eval_timing_ms=28.5,
        candles_cached=360,
    )

    msg = capture.last
    all_passed &= check(len(msg) > 0, "Message generated")
    all_passed &= check("142" in msg, "Trade count in message")
    all_passed &= check("ORB" in msg, "Strategy breakdown present")
    all_passed &= check("NIFTY" in msg, "Instrument breakdown present")
    all_passed &= check("28.5" in msg, "Eval timing present")
    all_passed &= check(len(msg) < 4096, f"Length OK: {len(msg)} < 4096")

    return all_passed


def test_exit_report():
    """Test exit simulation report."""
    print_section("TEST 2: Exit Engine Report")
    all_passed = True

    notifier = ResearchNotifier("fake_token", ["123456"])
    capture = MessageCapture()
    notifier._send = capture.capture

    notifier.send_exit_report(
        date_str="2026-06-11",
        trades_processed=142,
        trades_skipped=3,
        processing_time_s=1.8,
        best_exit_summary={
            "be_atr_trail": 35.2,
            "chandelier_3x": 32.1,
            "rr3": 28.5,
            "atr_trail": 25.0,
            "partial_a": 22.8,
        },
        avg_mfe=48.5,
        avg_mae=-22.3,
    )

    msg = capture.last
    all_passed &= check("142" in msg, "Trades processed in message")
    all_passed &= check("1.8" in msg, "Processing time present")
    all_passed &= check("be_atr_trail" in msg, "Top exit model listed")
    all_passed &= check("MFE" in msg or "48.5" in msg, "MFE present")
    all_passed &= check(len(msg) < 4096, f"Length OK: {len(msg)} < 4096")

    return all_passed


def test_scoring_report():
    """Test scoring report with ranked variants."""
    print_section("TEST 3: Scoring Report")
    all_passed = True

    notifier = ResearchNotifier("fake_token", ["123456"])
    capture = MessageCapture()
    notifier._send = capture.capture

    # Mock ranked variants
    @dataclass
    class MockRanked:
        rank: int = 1
        variant_id: str = "a3f82bc01d5e"
        strategy: str = "ORB"
        timeframe: str = "5m"
        best_exit_model: str = "be_atr_trail"
        composite_score: float = 78.5
        win_rate: float = 0.65
        expectancy: float = 32.5
        profit_factor: float = 3.2
        max_drawdown: float = 85.0
        sharpe_ratio: float = 1.45
        stability_score: float = 72.0
        best_regime: str = "session=MORNING"
        worst_regime: str = "volatility_regime=LOW"

    variants = [
        MockRanked(rank=1, variant_id="a3f82bc01d5e", strategy="ORB", expectancy=32.5),
        MockRanked(rank=2, variant_id="b5c91de23f4a", strategy="BB", expectancy=28.0, best_exit_model="chandelier_3x"),
        MockRanked(rank=3, variant_id="c7d02ef34a5b", strategy="TREND", expectancy=25.2, best_exit_model="rr3"),
    ]

    notifier.send_scoring_report(
        period_label="2026-06-W2",
        ranked_variants=variants,
        total_variants_scored=150000,
        total_passed_filters=45,
    )

    msg = capture.last
    all_passed &= check("a3f82bc01d5e" in msg, "Top variant ID in message")
    all_passed &= check("ORB" in msg, "Strategy present")
    all_passed &= check("be_atr_trail" in msg, "Exit model present")
    all_passed &= check("MORNING" in msg, "Best regime present")
    all_passed &= check("150000" in msg or "150,000" in msg, "Total variants mentioned")
    all_passed &= check(len(msg) < 4096, f"Length OK: {len(msg)} < 4096")

    return all_passed


def test_variant_trigger_alert():
    """Test promoted variant trigger notification."""
    print_section("TEST 4: Promoted Variant Trigger Alert")
    all_passed = True

    notifier = ResearchNotifier("fake_token", ["123456"])
    capture = MessageCapture()
    notifier._send = capture.capture

    notifier.send_variant_trigger_alert(
        variant_id="a3f82bc01d5e",
        strategy="ORB",
        timeframe="5m",
        instrument="NIFTY",
        direction="LONG",
        entry_price=24985.50,
        best_exit_model="be_atr_trail",
        expectancy=32.5,
        regime_match="MORNING + HIGH VOL + BULLISH",
    )

    msg = capture.last
    all_passed &= check("PROMOTED" in msg, "Alert type clear")
    all_passed &= check("LONG" in msg, "Direction present")
    all_passed &= check("24,985.50" in msg or "24985.5" in msg, "Entry price present")
    all_passed &= check("be_atr_trail" in msg, "Recommended exit present")
    all_passed &= check("32.5" in msg, "Expectancy present")
    all_passed &= check("MORNING" in msg, "Regime match present")
    all_passed &= check(len(msg) < 4096, f"Length OK: {len(msg)} < 4096")

    return all_passed


def test_health_and_lifecycle():
    """Test health alerts, startup, shutdown."""
    print_section("TEST 5: Health / Startup / Shutdown Messages")
    all_passed = True

    notifier = ResearchNotifier("fake_token", ["123456"])
    capture = MessageCapture()
    notifier._send = capture.capture

    notifier.send_startup(variant_count=150000, instruments=["NIFTY", "BANKNIFTY", "2885"])
    all_passed &= check("150,000" in capture.last, "Startup shows variant count")
    all_passed &= check("NIFTY" in capture.last, "Startup shows instruments")

    notifier.send_health_alert(
        alert_type="WARNING",
        message="Armed state size approaching limit",
        stats={"armed_count": 9500, "max_allowed": 10000},
    )
    all_passed &= check("WARNING" in capture.last, "Health alert type")
    all_passed &= check("9500" in capture.last, "Health stat value")

    notifier.send_shutdown(stats={
        "candles_processed": 1450,
        "trades_recorded": 142,
        "session_duration": "6h 15m",
    })
    all_passed &= check("STOPPED" in capture.last, "Shutdown message")
    all_passed &= check("142" in capture.last, "Shutdown trade count")

    all_passed &= check(len(capture.messages) == 3, f"3 messages sent (got {len(capture.messages)})")

    return all_passed


def test_disabled_notifier():
    """Test that disabled notifier doesn't crash."""
    print_section("TEST 6: Disabled Notifier (no token)")
    all_passed = True

    notifier = ResearchNotifier("", [])
    all_passed &= check(not notifier.enabled, "Notifier reports disabled")

    # Should not crash
    notifier.send_daily_summary("2026-06-11", 0, {}, {}, {})
    notifier.send_exit_report("2026-06-11", 0, 0, 0.0)
    notifier.send_scoring_report("test", [])
    notifier.send_variant_trigger_alert("x", "ORB", "5m", "NIFTY", "LONG", 25000, "rr2", 10)
    notifier.send_health_alert("ERROR", "test")
    notifier.send_startup(150000, ["NIFTY"])
    notifier.send_shutdown({})

    all_passed &= check(True, "All methods run without error when disabled")

    return all_passed


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    print("=" * 60)
    print("  PHASE 8: TELEGRAM RESEARCH NOTIFIER TESTS")
    print("=" * 60)

    t0 = time.time()
    all_passed = True

    all_passed &= test_daily_summary()
    all_passed &= test_exit_report()
    all_passed &= test_scoring_report()
    all_passed &= test_variant_trigger_alert()
    all_passed &= test_health_and_lifecycle()
    all_passed &= test_disabled_notifier()

    total = time.time() - t0
    print(f"\n{'═' * 60}")
    if all_passed:
        print(f"  ✅ ALL PHASE 8 TESTS PASSED")
    else:
        print(f"  ❌ SOME TESTS FAILED")
    print(f"  Total time: {total:.2f}s")
    print(f"{'═' * 60}")

    return all_passed


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
