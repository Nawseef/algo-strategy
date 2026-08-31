"""
Larry Williams Volatility Breakout — a famous-trader entry hypothesis.

Larry Williams (the trader who turned ~$10k into >$1.1M in the 1987 Robbins World
Cup — a ~11,000% year) popularised the "volatility breakout": project a fraction
of YESTERDAY's range off TODAY's open, and enter when price expands beyond that
level. The thesis is that once price travels a meaningful fraction of the prior
day's range from the open, the day has "picked a side" and tends to keep going.

WHY THIS IS DIFFERENT FROM THE (REJECTED) ORB — and fits the money lesson:
    * The ORB broke out of the first-30-min OPENING RANGE — a tiny R (a few pips),
      so cost ate the edge (§18.3). This breaks out of a fraction of the PRIOR
      DAY'S range projected from the open — a MUCH larger, day-scale move, so 1R
      is big and cost is a small fraction of it. Different anchor, different R.
    * It's the canonical famous-trader momentum/expansion play, complementing the
      set: MR/sweep fade extremes (range), pullback rides trends, and this catches
      volatility-EXPANSION breakout days.

Logic (once per FX trading day, both directions):
    1. Reference = the PREVIOUS FX trading day's range (prevHigh − prevLow).
    2. Trigger levels off TODAY's open:
         long_trigger  = today_open + k × prev_range
         short_trigger = today_open − k × prev_range      (k default 0.5)
    3. On the first bar that CLOSES beyond a trigger, enter that direction:
         close ≥ long_trigger  → LONG
         close ≤ short_trigger → SHORT
    4. STOP = k × prev_range from entry (so 1R = the breakout increment — a
       defined, day-scale fraction of the prior range). No exit baked in — the
       exit sweep applies each model (trailing / runner ride the expansion day).
    5. Optional volatility-expansion filter (Williams' own refinement): only take
       the breakout when short-term ATR is expanding vs its longer average.

Intraday by construction: the replay flattens at the FX day boundary (17:00 NY),
so this is a day-trade — enter on the expansion, out by the close. No overnight.

Parameters (lean):
    k                    — fraction of the prior-day range for the trigger (0.5)
    atr_period           — ATR period (for the optional vol filter + tags) (14)
    require_vol_expansion — only trade when ATR(short) > ATR(long) (default False)
    allow_long / allow_short — enable each side (default both)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.cfd_research.entry_strategy import EntryContext, EntryStrategy
from app.cfd_research.exit_models import EntryIntent
from app.cfd_strategy.base import Direction
from app.core.models import Candle, Timeframe
from app.strategy.indicators import atr
from app.utils import forex_hours


def _tf_minutes(tf: Timeframe) -> int:
    return {"5m": 5, "15m": 15, "30m": 30, "1h": 60}.get(tf.value, 5)


@dataclass
class _VBState:
    entered_day: str = ""   # the FX trading day we already took a trade in


class VolatilityBreakout(EntryStrategy):
    """Larry Williams volatility breakout — one entry per FX trading day.

    Builds one variant per timeframe (session/regime/volatility are free tags).
    """

    name = "Larry Williams Volatility Breakout"

    def __init__(
        self,
        k: float = 0.5,
        atr_period: int = 14,
        require_vol_expansion: bool = False,
        allow_long: bool = True,
        allow_short: bool = True,
        instruments: tuple[str, ...] = (),
        timeframe: Timeframe = Timeframe.M5,
    ) -> None:
        self.k = k
        self.atr_period = atr_period
        self.require_vol_expansion = require_vol_expansion
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.instruments = instruments
        self.timeframe = timeframe

        # We need the FULL previous trading day in the trailing window to measure
        # its range. The replay sets window = max(history_window, min_history), so
        # we request ~2.5 trading days of bars on this timeframe (comfortably
        # covers today + a complete previous day even late in today's session).
        bars_per_day = int(24 * 60 / _tf_minutes(timeframe))
        self.min_history = max(int(bars_per_day * 2.5), atr_period * 4 + 5)

        sid = f"lwvb_k{k:g}"
        if require_vol_expansion:
            sid += "_volexp"
        self.strategy_id = sid

        self._state: dict[str, _VBState] = {}

    def _st(self, instrument: str) -> _VBState:
        st = self._state.get(instrument)
        if st is None:
            st = _VBState()
            self._state[instrument] = st
        return st

    def entries(self, ctx: EntryContext) -> list[EntryIntent]:
        candle = ctx.candle
        history = ctx.history
        instrument = ctx.instrument
        st = self._st(instrument)

        dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, timezone.utc)
        today = forex_hours.trading_day(dt)

        # One entry per trading day.
        if st.entered_day == today:
            return []

        # --- Group the trailing window into FX trading days: per day track the
        # open (first bar) + high/low (the range). history is oldest->newest. ---
        days: dict[str, dict] = {}
        order: list[str] = []
        for c in history:
            d = forex_hours.trading_day(datetime.fromtimestamp(c.timestamp_ms / 1000, timezone.utc))
            e = days.get(d)
            if e is None:
                days[d] = {"open": c.open, "high": c.high, "low": c.low}
                order.append(d)
            else:
                if c.high > e["high"]:
                    e["high"] = c.high
                if c.low < e["low"]:
                    e["low"] = c.low

        if today not in days:
            return []
        idx = order.index(today)
        if idx == 0:
            return []   # no complete previous day in the window yet
        prev = days[order[idx - 1]]
        prev_range = prev["high"] - prev["low"]
        if prev_range <= 0:
            return []

        today_open = days[today]["open"]
        increment = self.k * prev_range          # 1R = the breakout increment
        long_trigger = today_open + increment
        short_trigger = today_open - increment
        close = candle.close

        # --- Optional volatility-expansion filter (short ATR > long ATR) ---
        if self.require_vol_expansion:
            atr_short = atr(history, self.atr_period)
            atr_long = atr(history, self.atr_period * 4)
            if atr_short is None or atr_long is None or atr_short <= atr_long:
                return []

        if self.allow_long and close >= long_trigger:
            st.entered_day = today
            return [EntryIntent(
                instrument=instrument, direction=Direction.LONG,
                entry_price=close, stop_loss=close - increment,
                entry_time_ms=candle.timestamp_ms,
                reason=f"LW vol breakout long: close={close:.5f} >= open+{self.k:g}×prevRange "
                       f"({long_trigger:.5f}); 1R={increment:.5f}",
            )]
        if self.allow_short and close <= short_trigger:
            st.entered_day = today
            return [EntryIntent(
                instrument=instrument, direction=Direction.SHORT,
                entry_price=close, stop_loss=close + increment,
                entry_time_ms=candle.timestamp_ms,
                reason=f"LW vol breakout short: close={close:.5f} <= open−{self.k:g}×prevRange "
                       f"({short_trigger:.5f}); 1R={increment:.5f}",
            )]
        return []

    def on_day_reset(self) -> None:
        self._state.clear()
