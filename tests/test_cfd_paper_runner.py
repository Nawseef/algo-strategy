"""
Tests for the CFD paper-trading runner glue (app.main_cfd_paper).

These verify the WIRING, not a strategy edge:
  * a candle-close signal from a strategy opens a position via the executor,
  * a subsequent tick manages that position to its SL/TP,
  * intrabar-arm aging happens BEFORE evaluation on each candle (so a fresh arm
    keeps its full expiry window),
  * pre-weekend flatten suppresses NEW entries and flattens open positions,
  * strategy selection from the registry works.

The runner is constructed with an in-memory SQLite store and no notifier, and we
drive it by calling its handlers / emitting events directly (no feed/network).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from app.broker.base import Tick
from app.cfd_strategy.base import (
    CFDSignal,
    CFDStrategy,
    Direction,
    EntryMode,
    StrategyContext,
    build_rr_exit_plan,
)
from app.core.models import Candle, Timeframe
from app.db.research_store import ResearchStore
from app.utils import forex_hours


# ─── Helpers ─────────────────────────────────────────────────────────────────


class _DummyNotifier:
    """No-op notifier implementing the full contract (no real Telegram)."""

    def __init__(self):
        self.entries = 0
        self.exits = 0

    def send(self, text, block=False):
        pass

    def notify_entry(self, **kwargs):
        self.entries += 1

    def notify_exit(self, **kwargs):
        self.exits += 1

    def periodic_summary(self, *a, **k):
        pass

    def eod_report(self, *a, **k):
        pass

    def session_start(self, *a, **k):
        pass

    def session_end(self, *a, **k):
        pass

    def on_day_reset(self):
        pass


def _make_app(store, strategies):
    """Build a runner with an injected store, no archiving, dummy notifier."""
    os.environ["CFD_PAPER_ARCHIVE_CANDLES"] = "false"
    # Import here so the env var above is honoured at construction.
    from app.main_cfd_paper import CFDPaperTradingApp

    app = CFDPaperTradingApp(store=store, notifier=_DummyNotifier())
    app._strategies = strategies          # override registry selection
    app._flatten_weekend = True           # exercise the weekend guard
    app._flatten_reset = False
    app._wire_pipeline()
    return app


def _candle(token, ts_ms, o, h, l, c, volume=10):
    return Candle(
        exchange="ICMARKETS", segment="CFD", exchange_token=token,
        timeframe=Timeframe.M5, timestamp_ms=ts_ms,
        open=o, high=h, low=l, close=c, volume=volume,
    )


class _AlwaysLong(CFDStrategy):
    """Stub: emits one CANDLE_CLOSE long per candle (SL 1% below, 2R TP)."""

    strategy_id = "_stub_always_long"
    name = "stub always long"
    timeframe = Timeframe.M5
    instruments = ("XAUUSD",)
    min_history = 1

    def evaluate(self, ctx: StrategyContext) -> list[CFDSignal]:
        entry = ctx.close
        plan = build_rr_exit_plan(Direction.LONG, entry, entry * 0.99, rr_targets=[2.0])
        return [CFDSignal(
            strategy_id=self.strategy_id, variant_id="default", instrument=ctx.instrument,
            direction=Direction.LONG, entry_mode=EntryMode.CANDLE_CLOSE,
            entry_price=entry, exit_plan=plan, timestamp_ms=ctx.candle.timestamp_ms,
        )]


class _NoTrade(CFDStrategy):
    strategy_id = "_stub_no_trade"
    timeframe = Timeframe.M5
    instruments = ("XAUUSD",)
    min_history = 1

    def evaluate(self, ctx):
        return []


@pytest.fixture()
def store():
    tmp = tempfile.mktemp(suffix=".db")
    s = ResearchStore(db_path=Path(tmp))
    s.start()
    yield s
    s.stop()
    if os.path.exists(tmp):
        os.remove(tmp)


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_candle_close_signal_opens_position(store, monkeypatch):
    # Never in a flatten window for this test.
    monkeypatch.setattr(forex_hours, "should_flatten_before_weekend", lambda *a, **k: False)
    monkeypatch.setattr(forex_hours, "should_flatten_before_daily_reset", lambda *a, **k: False)

    app = _make_app(store, [_AlwaysLong()])
    c = _candle("XAUUSD", 300_000, 2400, 2401, 2399, 2400.0)
    app._candle_builder.inject_history("XAUUSD", Timeframe.M5, [c])

    app._on_candle(c)   # strategy emits a long at 2400

    positions = app._manager.open_positions("XAUUSD")["cfd_demo"]
    assert len(positions) == 1
    assert positions[0].direction is Direction.LONG
    assert positions[0].entry_price == 2400.0


def test_tick_manages_position_to_tp(store, monkeypatch):
    monkeypatch.setattr(forex_hours, "should_flatten_before_weekend", lambda *a, **k: False)
    monkeypatch.setattr(forex_hours, "should_flatten_before_daily_reset", lambda *a, **k: False)

    app = _make_app(store, [_AlwaysLong()])
    c = _candle("XAUUSD", 300_000, 2400, 2401, 2399, 2400.0)
    app._candle_builder.inject_history("XAUUSD", Timeframe.M5, [c])
    app._on_candle(c)   # long @ 2400, SL 2376, TP = 2400 + 2*(24) = 2448

    # A tick below TP keeps it open.
    app._route_tick(Tick("ICMARKETS", "CFD", "XAUUSD", ltp=2420.0,
                         timestamp_ms=310_000, bid=2420.0, ask=2420.2))
    assert len(app._manager.open_positions("XAUUSD")["cfd_demo"]) == 1

    # A tick at/above TP closes it.
    app._route_tick(Tick("ICMARKETS", "CFD", "XAUUSD", ltp=2448.0,
                         timestamp_ms=320_000, bid=2448.0, ask=2448.2))
    assert len(app._manager.open_positions("XAUUSD")["cfd_demo"]) == 0


def test_arm_aging_before_eval_gives_fresh_arm_full_expiry(store, monkeypatch):
    """An INTRABAR arm created on candle N must NOT be aged by candle N."""
    monkeypatch.setattr(forex_hours, "should_flatten_before_weekend", lambda *a, **k: False)
    monkeypatch.setattr(forex_hours, "should_flatten_before_daily_reset", lambda *a, **k: False)

    class _ArmOnce(CFDStrategy):
        strategy_id = "_stub_arm_once"
        timeframe = Timeframe.M5
        instruments = ("XAUUSD",)
        min_history = 1

        def __init__(self):
            self._armed = False

        def evaluate(self, ctx):
            if self._armed:
                return []
            self._armed = True
            trigger = ctx.close + 5.0
            plan = build_rr_exit_plan(Direction.LONG, trigger, trigger - 10.0, rr_targets=[2.0])
            return [CFDSignal(
                strategy_id=self.strategy_id, variant_id="default", instrument="XAUUSD",
                direction=Direction.LONG, entry_mode=EntryMode.INTRABAR,
                entry_price=trigger, exit_plan=plan, timestamp_ms=ctx.candle.timestamp_ms,
                expiry_candles=1,
            )]

    app = _make_app(store, [_ArmOnce()])
    c0 = _candle("XAUUSD", 300_000, 2400, 2401, 2399, 2400.0)
    app._candle_builder.inject_history("XAUUSD", Timeframe.M5, [c0])
    app._on_candle(c0)   # arms a trigger @ 2405 with expiry_candles=1

    ex = app._manager.executor("cfd_demo")
    # The arm must still be live (not aged to zero by its own candle).
    assert ex._arms, "arm was aged on the same candle it was created"

    # Price reaches the trigger on the next tick -> fills.
    app._route_tick(Tick("ICMARKETS", "CFD", "XAUUSD", ltp=2405.0,
                         timestamp_ms=305_000, bid=2405.0, ask=2405.2))
    assert len(app._manager.open_positions("XAUUSD")["cfd_demo"]) == 1


def test_pre_weekend_flatten_suppresses_entries_and_closes_positions(store, monkeypatch):
    # Start OUT of the flatten window: open a position.
    monkeypatch.setattr(forex_hours, "should_flatten_before_weekend", lambda *a, **k: False)
    monkeypatch.setattr(forex_hours, "should_flatten_before_daily_reset", lambda *a, **k: False)
    monkeypatch.setattr(forex_hours, "is_market_open", lambda *a, **k: True)
    monkeypatch.setattr(forex_hours, "trading_day", lambda *a, **k: "2026-08-06")

    app = _make_app(store, [_AlwaysLong()])
    c = _candle("XAUUSD", 300_000, 2400, 2401, 2399, 2400.0)
    app._candle_builder.inject_history("XAUUSD", Timeframe.M5, [c])
    app._on_candle(c)
    assert len(app._manager.open_positions("XAUUSD")["cfd_demo"]) == 1

    # Now ENTER the pre-weekend flatten window: schedule tick should flatten.
    monkeypatch.setattr(forex_hours, "should_flatten_before_weekend", lambda *a, **k: True)
    app._tick_schedule()
    assert len(app._manager.open_positions("XAUUSD")["cfd_demo"]) == 0
    assert app._weekend_flattened is True

    # New signals are suppressed while in the flatten window.
    assert app._entries_blocked() is True
    c2 = _candle("XAUUSD", 600_000, 2400, 2401, 2399, 2400.0)
    app._candle_builder.inject_history("XAUUSD", Timeframe.M5, [c2])
    app._on_candle(c2)
    assert len(app._manager.open_positions("XAUUSD")["cfd_demo"]) == 0

    # Market closes for the weekend -> the flatten flag re-arms for next week.
    monkeypatch.setattr(forex_hours, "is_market_open", lambda *a, **k: False)
    app._tick_schedule()
    assert app._weekend_flattened is False


def test_no_trade_strategy_opens_nothing(store, monkeypatch):
    monkeypatch.setattr(forex_hours, "should_flatten_before_weekend", lambda *a, **k: False)
    monkeypatch.setattr(forex_hours, "should_flatten_before_daily_reset", lambda *a, **k: False)

    app = _make_app(store, [_NoTrade()])
    c = _candle("XAUUSD", 300_000, 2400, 2401, 2399, 2400.0)
    app._candle_builder.inject_history("XAUUSD", Timeframe.M5, [c])
    app._on_candle(c)
    assert len(app._manager.open_positions("XAUUSD")["cfd_demo"]) == 0


def test_strategy_selection_from_registry(store):
    """CFD_PAPER_STRATEGIES filters which registered strategies run."""
    from app.cfd_strategy.registry import get_registry
    reg = get_registry()
    reg.clear()
    reg.register(_AlwaysLong())
    reg.register(_NoTrade())

    os.environ["CFD_PAPER_ARCHIVE_CANDLES"] = "false"
    os.environ["CFD_PAPER_STRATEGIES"] = "_stub_always_long"
    try:
        from app.main_cfd_paper import CFDPaperTradingApp
        app = CFDPaperTradingApp(store=store, notifier=_DummyNotifier())
        ids = [s.strategy_id for s in app._strategies]
        assert ids == ["_stub_always_long"]
    finally:
        del os.environ["CFD_PAPER_STRATEGIES"]
        reg.clear()


def test_demo_strategy_registers_and_emits_valid_signals():
    """The reference SMA strategy produces RR-valid signals on a real crossover."""
    from app.cfd_strategy.strategies.sma_cross import SmaCrossDemo

    strat = SmaCrossDemo(fast_period=3, slow_period=5, atr_period=3, atr_mult=1.5)
    token = "XAUUSD"

    # Build a downtrend (fast below slow), prime prev values, then an up-spike
    # to force a bullish crossover on the final candle.
    closes = [2400, 2398, 2396, 2394, 2392, 2390, 2388]
    candles = []
    for i, c in enumerate(closes):
        candles.append(_candle(token, i * 300_000, c, c + 1, c - 1, float(c)))

    # Prime: evaluate over the declining series (no cross expected on last bar).
    ctx = StrategyContext(token, Timeframe.M5, candles[-1], candles)
    strat.evaluate(ctx)

    # Now a strong up bar so the fast SMA jumps above the slow SMA.
    up = _candle(token, len(closes) * 300_000, 2388, 2440, 2388, 2435.0)
    candles2 = candles + [up]
    ctx2 = StrategyContext(token, Timeframe.M5, up, candles2)
    signals = strat.evaluate(ctx2)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction is Direction.LONG
    assert sig.entry_mode is EntryMode.CANDLE_CLOSE
    # RR floor holds and SL is below entry for a long.
    assert sig.exit_plan.max_rr >= 2.0 - 1e-9
    assert sig.stop_loss < sig.entry_price
