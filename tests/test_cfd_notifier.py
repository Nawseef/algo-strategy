"""
Tests for the CFD trade notifier (app.telegram.cfd_notifier).

Verifies message CONTENT via a fake transport that captures sent text:
  * entry alert has direction/lots/entry/SL/TP/RR/risk,
  * exit alert has running day totals, win/loss streak, MFE, and a risk warning,
  * periodic summary and EOD report render per-account and pick best/worst,
  * on_day_reset clears the tally.
"""

from __future__ import annotations

from app.cfd_execution.base import ExitReason, ManagedPosition, PositionStatus
from app.cfd_strategy.base import (
    CFDSignal,
    Direction,
    EntryMode,
    build_rr_exit_plan,
)
from app.telegram.cfd_notifier import CFDTradeNotifier


class _FakeTransport:
    def __init__(self):
        self.messages: list[str] = []
        self._enabled = True

    def send(self, text, block=False):
        self.messages.append(text)


def _guard_summary(account_id="cfd_demo", balance=100_000.0, init=100_000.0,
                   daily_pnl=0.0, dd_used=0.0, status="ACTIVE", trades_today=0):
    return {
        "account_id": account_id, "balance": balance, "initial_balance": init,
        "daily_pnl": daily_pnl, "daily_dd_used_pct": dd_used, "status": status,
        "trades_today": trades_today,
    }


def _gold_long_signal(entry=2400.0, sl=2390.0):
    plan = build_rr_exit_plan(Direction.LONG, entry, sl, rr_targets=[2.0])
    return CFDSignal(
        strategy_id="gold", variant_id="v1", instrument="XAUUSD",
        direction=Direction.LONG, entry_mode=EntryMode.CANDLE_CLOSE,
        entry_price=entry, exit_plan=plan, timestamp_ms=1_000.0,
    )


def _closed_pos(entry=2400.0, exit_price=2420.0, lots=1.0, peak=2425.0,
                entry_ms=1_000_000, exit_ms=3_100_000):
    sig = _gold_long_signal(entry=entry)
    pos = ManagedPosition(
        position_id="p1", strategy_id="gold", variant_id="v1", instrument="XAUUSD",
        direction=Direction.LONG, entry_price=entry, entry_time_ms=entry_ms,
        lots=lots, exit_plan=sig.exit_plan,
    )
    pos.status = PositionStatus.CLOSED
    pos.exit_price = exit_price
    pos.exit_time_ms = exit_ms
    pos.final_reason = ExitReason.TAKE_PROFIT
    pos.max_favorable_price = peak
    return pos


def test_entry_alert_content():
    t = _FakeTransport()
    n = CFDTradeNotifier(t)
    sig = _gold_long_signal()
    pos = _closed_pos()
    pos.status = PositionStatus.OPEN
    n.notify_entry("cfd_demo", pos, sig, risk_usd=1000.0, open_count=1,
                   guard_summary=_guard_summary())
    msg = t.messages[-1]
    assert "ENTRY" in msg and "[cfd_demo]" in msg
    assert "gold" in msg                     # strategy id now in the header
    assert "LONG XAUUSD" in msg
    assert "RR 2.00" in msg
    assert "$1,000.00" in msg
    assert "v1" in msg


def test_exit_alert_running_totals_and_mfe():
    t = _FakeTransport()
    n = CFDTradeNotifier(t)
    pos = _closed_pos(peak=2425.0)   # peak beyond the 2R TP (2420)
    n.notify_exit("cfd_demo", pos, realized_rr=2.0, net_pnl_usd=1993.0,
                  reason=ExitReason.TAKE_PROFIT,
                  guard_summary=_guard_summary(balance=101_993.0))
    msg = t.messages[-1]
    assert "EXIT" in msg and "[cfd_demo]" in msg and "WIN" in msg
    assert "gold" in msg                     # strategy id now in the header
    assert "TAKE_PROFIT" in msg
    assert "hold 35m" in msg                 # (3_100_000-1_000_000)/60000 = 35
    assert "+$1,993.00" in msg               # per-strategy realized PnL
    assert "Account today:" in msg and "+$1,993.00" in msg
    assert "1 trades  W:1 L:0 (100%)" in msg
    assert "Peak" in msg and "beyond furthest TP" in msg


def test_exit_streak_and_risk_warning():
    t = _FakeTransport()
    n = CFDTradeNotifier(t)
    # Two losses in a row, deep enough to trigger the warning (>2% of 100k).
    lose = _closed_pos(exit_price=2390.0, peak=2401.0)
    lose.final_reason = ExitReason.STOP_LOSS
    n.notify_exit("cfd_demo", lose, realized_rr=-1.0, net_pnl_usd=-1200.0,
                  reason=ExitReason.STOP_LOSS, guard_summary=_guard_summary(balance=98_800.0))
    n.notify_exit("cfd_demo", lose, realized_rr=-1.0, net_pnl_usd=-1200.0,
                  reason=ExitReason.STOP_LOSS, guard_summary=_guard_summary(balance=97_600.0))
    msg = t.messages[-1]
    assert "LOSS" in msg
    assert "2 losses in a row" in msg
    assert "down 2.4% today" in msg          # 2400/100000 = 2.4%
    assert "RISK:" in msg


def test_halt_banner_on_blown_account():
    t = _FakeTransport()
    n = CFDTradeNotifier(t)
    pos = _closed_pos(exit_price=2390.0)
    pos.final_reason = ExitReason.STOP_LOSS
    n.notify_exit("cfd_demo", pos, realized_rr=-1.0, net_pnl_usd=-500.0,
                  reason=ExitReason.STOP_LOSS,
                  guard_summary=_guard_summary(status="MAX_DD_BREACH", balance=89_000.0))
    assert "ACCOUNT MAX_DD_BREACH" in t.messages[-1]


def test_periodic_summary_multi_account_best():
    t = _FakeTransport()
    n = CFDTradeNotifier(t)
    # Seed some realized PnL via exits so best/worst is meaningful.
    win = _closed_pos()
    n.notify_exit("demo", win, 2.0, 1000.0, ExitReason.TAKE_PROFIT,
                  _guard_summary(account_id="demo"))
    lose = _closed_pos(exit_price=2390.0); lose.final_reason = ExitReason.STOP_LOSS
    n.notify_exit("ftmo", lose, -1.0, -800.0, ExitReason.STOP_LOSS,
                  _guard_summary(account_id="ftmo"))

    summaries = [
        _guard_summary(account_id="demo", balance=101_000.0, daily_pnl=1000.0, trades_today=1),
        _guard_summary(account_id="ftmo", balance=99_200.0, daily_pnl=-800.0, trades_today=1),
    ]
    n.periodic_summary(summaries, sessions="london+new_york")
    msg = t.messages[-1]
    assert "PORTFOLIO" in msg
    assert "[demo]" in msg and "[ftmo]" in msg
    assert "Best:" in msg and "demo" in msg.split("Best:")[1]


def test_eod_report_and_day_reset():
    t = _FakeTransport()
    n = CFDTradeNotifier(t)
    win = _closed_pos()
    n.notify_exit("demo", win, 2.0, 1500.0, ExitReason.TAKE_PROFIT,
                  _guard_summary(account_id="demo"))
    summaries = [_guard_summary(account_id="demo", balance=101_500.0)]
    n.eod_report(summaries, "2026-08-06")
    msg = t.messages[-1]
    assert "END OF DAY" in msg and "2026-08-06" in msg
    assert "GREEN DAY" in msg
    assert "Day" in msg and "+$1,500.00" in msg

    # After reset the tally is cleared: a fresh EOD shows flat.
    n.on_day_reset()
    n.eod_report(summaries, "2026-08-07")
    assert "FLAT DAY" in t.messages[-1]


def test_alert_trades_false_suppresses_but_still_tallies():
    t = _FakeTransport()
    n = CFDTradeNotifier(t, alert_trades=False)
    win = _closed_pos()
    n.notify_exit("demo", win, 2.0, 1000.0, ExitReason.TAKE_PROFIT,
                  _guard_summary(account_id="demo"))
    # No per-trade message sent...
    assert t.messages == []
    # ...but the day tally still updated, so EOD reflects it.
    n.eod_report([_guard_summary(account_id="demo", balance=101_000.0)], "2026-08-06")
    assert "GREEN DAY" in t.messages[-1]
