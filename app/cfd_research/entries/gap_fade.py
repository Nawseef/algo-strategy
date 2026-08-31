"""
Opening Gap Fade — a well-documented statistical intraday edge (indices esp.).

Markets frequently open away from the prior session's close (an overnight "gap").
For COMMON gaps (no major catalyst) the well-known statistical tendency is that
price drifts back to fill the gap — retrace to the prior close — within the
session. Studies on the S&P cite a ~70% fill rate for ordinary gaps. You FADE the
gap: sell an up-gap / buy a down-gap, targeting the prior close.

WHY IT'S A DISTINCT ADDITION:
    * It is the ONLY entry keyed off the OVERNIGHT GAP (prior-day close vs today's
      open) — a fresh signal none of the others use (ORB/LWVB break intraday
      levels, MR/sweep/rsi2 fade intraday extremes, pullback rides a trend,
      squeeze trades compression).
    * Its natural target is the GAP FILL = the prior close, a defined mean — so it
      pairs exactly with the target_mean exit. High-probability, modest R.
    * Most reliable on indices (US30/US500/USTEC/DE40), but applies to all
      instruments; session/regime become free tags.

Logic (once per FX trading day, on the day's OPEN bar):
    1. Measure the gap = today_open − prev_day_close.
    2. Qualify it by size (in ATR units): only fade if
       ``min_gap_atr`` ≤ |gap|/ATR ≤ ``max_gap_atr`` — skip trivial gaps (noise)
       and huge breakaway/catalyst gaps (which tend NOT to fill).
    3. FADE toward the prior close:
         gap UP   (open > prev close) -> SHORT, target = prev close
         gap DOWN (open < prev close) -> LONG,  target = prev close
    4. ENTRY at the open bar's close. STOP = ``sl_atr_mult`` × ATR beyond entry
       (defines 1R). TARGET = prev close (carried as target_price for target_mean).

Parameters (lean):
    min_gap_atr   — minimum gap size to fade, in ATR units (default 0.25)
    max_gap_atr   — maximum gap size to fade (skip breakaways) (default 4.0)
    atr_period    — ATR period (default 14)
    sl_atr_mult   — stop distance beyond entry, in ATR units (default 1.0)
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
class _GapState:
    entered_day: str = ""


class OpeningGapFade(EntryStrategy):
    """Fade the overnight opening gap toward the prior close — one entry per day.

    Builds one variant per timeframe (session/regime/volatility are free tags).
    """

    name = "Opening Gap Fade"

    def __init__(
        self,
        min_gap_atr: float = 0.25,
        max_gap_atr: float = 4.0,
        atr_period: int = 14,
        sl_atr_mult: float = 1.0,
        allow_long: bool = True,
        allow_short: bool = True,
        instruments: tuple[str, ...] = (),
        timeframe: Timeframe = Timeframe.M5,
    ) -> None:
        self.min_gap_atr = min_gap_atr
        self.max_gap_atr = max_gap_atr
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.instruments = instruments
        self.timeframe = timeframe

        # Need the full previous trading day in the window (for its close). The
        # replay sets window = max(history_window, min_history), so request ~2.5
        # trading days on this timeframe.
        bars_per_day = int(24 * 60 / _tf_minutes(timeframe))
        self.min_history = max(int(bars_per_day * 2.5), atr_period + 5)

        sid = f"gapfade_g{min_gap_atr:g}-{max_gap_atr:g}"
        self.strategy_id = sid

        self._state: dict[str, _GapState] = {}

    def _st(self, instrument: str) -> _GapState:
        st = self._state.get(instrument)
        if st is None:
            st = _GapState()
            self._state[instrument] = st
        return st

    def entries(self, ctx: EntryContext) -> list[EntryIntent]:
        candle = ctx.candle
        history = ctx.history
        instrument = ctx.instrument
        st = self._st(instrument)

        dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, timezone.utc)
        today = forex_hours.trading_day(dt)
        if st.entered_day == today:
            return []

        if len(history) < self.min_history:
            return []

        # --- Group the window into FX trading days: per day track first bar
        # (open + its ts) and the last close seen. history is oldest -> newest. ---
        days: dict[str, dict] = {}
        order: list[str] = []
        for c in history:
            d = forex_hours.trading_day(datetime.fromtimestamp(c.timestamp_ms / 1000, timezone.utc))
            e = days.get(d)
            if e is None:
                days[d] = {"open": c.open, "open_ts": c.timestamp_ms, "close": c.close}
                order.append(d)
            else:
                e["close"] = c.close   # ordered -> ends as the day's last close

        if today not in days:
            return []
        # Only act on the day's OPEN bar (the current bar must be today's first).
        if candle.timestamp_ms != days[today]["open_ts"]:
            return []
        idx = order.index(today)
        if idx == 0:
            return []   # no previous day in the window
        prev_close = days[order[idx - 1]]["close"]

        atr_val = atr(history, self.atr_period)
        if atr_val is None or atr_val <= 0:
            return []

        today_open = days[today]["open"]
        gap = today_open - prev_close
        gap_atr = abs(gap) / atr_val
        if gap_atr < self.min_gap_atr or gap_atr > self.max_gap_atr:
            return []   # too small (noise) or too big (breakaway — may not fill)

        close = candle.close
        stop_dist = self.sl_atr_mult * atr_val

        # Gap UP -> fade SHORT toward the prior close (below).
        if gap > 0 and self.allow_short:
            st.entered_day = today
            return [EntryIntent(
                instrument=instrument, direction=Direction.SHORT,
                entry_price=close, stop_loss=close + stop_dist,
                entry_time_ms=candle.timestamp_ms,
                target_price=prev_close,   # gap fill = prior close
                reason=f"Gap fade short: gap +{gap_atr:.2f}×ATR above prev close; target fill",
            )]
        # Gap DOWN -> fade LONG toward the prior close (above).
        if gap < 0 and self.allow_long:
            st.entered_day = today
            return [EntryIntent(
                instrument=instrument, direction=Direction.LONG,
                entry_price=close, stop_loss=close - stop_dist,
                entry_time_ms=candle.timestamp_ms,
                target_price=prev_close,   # gap fill = prior close
                reason=f"Gap fade long: gap -{gap_atr:.2f}×ATR below prev close; target fill",
            )]
        return []

    def on_day_reset(self) -> None:
        self._state.clear()
