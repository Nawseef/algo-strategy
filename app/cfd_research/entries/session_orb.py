"""
Session Opening-Range Breakout (ORB) — the first candidate entry.

Hypothesis (a real, documented intraday edge): when a major session opens
(London / New York), volatility expands and price often breaks out of the range
established in the session's first minutes, then continues. We measure whether
that holds on CFDs, per instrument/session, and let the exit sweep decide how to
manage it.

Logic (edge-triggered, one entry per session per instrument):
    1. When the target session becomes active, start an "opening range" from that
       bar and extend it over the first ``range_bars`` bars (high/low).
    2. After the range completes, on the first bar that CLOSES beyond the range
       (+ an optional buffer), enter in the breakout direction:
           * close above range_high + buffer -> LONG, stop = range_low
           * close below range_low  - buffer -> SHORT, stop = range_high
    3. Only one entry per session; state resets when the session ends / a new one
       opens.

The stop is the opposite side of the opening range (so 1R = the range size at
the breakout). No exit is baked in — the exit sweep applies each exit model.

Session detection is INJECTABLE (``session_fn(dt) -> set[str]``) so it's testable
and DST-correctness comes from ``forex_hours`` in production.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.cfd_research.entry_strategy import EntryContext, EntryStrategy
from app.cfd_research.exit_models import EntryIntent
from app.core.models import Timeframe
from app.cfd_strategy.base import Direction
from app.utils import forex_hours


def _default_session_fn(dt: datetime) -> set[str]:
    """Active FX sessions at ``dt`` (DST-aware), via forex_hours.session_tag."""
    return set(forex_hours.session_tag(dt).split("+"))


@dataclass
class _ORBState:
    in_session: bool = False
    range_high: float = 0.0
    range_low: float = 0.0
    bars: int = 0
    complete: bool = False
    entered: bool = False


class SessionORB(EntryStrategy):
    """Opening-range breakout for one session, both directions."""

    name = "Session Opening-Range Breakout"
    timeframe = Timeframe.M5

    def __init__(
        self,
        session: str = "london",
        range_bars: int = 6,          # 6 x 5m = first 30 min
        buffer_frac: float = 0.0,     # breakout buffer as a fraction of the range
        allow_long: bool = True,
        allow_short: bool = True,
        instruments: tuple[str, ...] = (),
        session_fn=None,
    ) -> None:
        self.session = session
        self.range_bars = range_bars
        self.buffer_frac = buffer_frac
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.instruments = instruments
        self.min_history = range_bars + 1
        self.strategy_id = f"orb_{session}_{range_bars}b"
        self._session_fn = session_fn or _default_session_fn
        self._state: dict[str, _ORBState] = {}

    def _st(self, instrument: str) -> _ORBState:
        st = self._state.get(instrument)
        if st is None:
            st = _ORBState()
            self._state[instrument] = st
        return st

    def entries(self, ctx: EntryContext) -> list[EntryIntent]:
        candle = ctx.candle
        dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, timezone.utc)
        active = self._session_fn(dt)
        st = self._st(ctx.instrument)

        # Session not active -> reset and wait.
        if self.session not in active:
            st.in_session = False
            return []

        # Session just opened -> begin the opening range.
        if not st.in_session:
            st.in_session = True
            st.range_high = candle.high
            st.range_low = candle.low
            st.bars = 1
            st.complete = False
            st.entered = False
            return []

        # Building the opening range.
        if st.bars < self.range_bars:
            st.range_high = max(st.range_high, candle.high)
            st.range_low = min(st.range_low, candle.low)
            st.bars += 1
            if st.bars >= self.range_bars:
                st.complete = True
            return []

        # Range complete -> look for the breakout (once).
        if not st.complete or st.entered:
            return []

        rng = st.range_high - st.range_low
        if rng <= 0:
            return []
        buf = self.buffer_frac * rng
        close = ctx.close

        if self.allow_long and close > st.range_high + buf:
            st.entered = True
            return [EntryIntent(
                instrument=ctx.instrument, direction=Direction.LONG,
                entry_price=close, stop_loss=st.range_low,
                entry_time_ms=candle.timestamp_ms,
                reason=f"ORB {self.session} long breakout",
            )]
        if self.allow_short and close < st.range_low - buf:
            st.entered = True
            return [EntryIntent(
                instrument=ctx.instrument, direction=Direction.SHORT,
                entry_price=close, stop_loss=st.range_high,
                entry_time_ms=candle.timestamp_ms,
                reason=f"ORB {self.session} short breakout",
            )]
        return []

    def on_day_reset(self) -> None:
        self._state.clear()
