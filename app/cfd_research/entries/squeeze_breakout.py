"""
TTM Squeeze Breakout (John Carter) — a famous-trader entry hypothesis.

John Carter (Simpler Trading, "Mastering the Trade") popularised the "squeeze":
when Bollinger Bands contract INSIDE the Keltner Channels, volatility has
compressed to an unusual low — a coiled spring. When the bands expand back
outside the Keltner Channels the squeeze "fires", and a directional expansion
move tends to follow. You enter on the fire, in the direction of momentum.

WHY IT'S A DISTINCT, ACTIVE INTRADAY ADDITION:
    * Different mechanism from everything else in the set: MR/sweep fade extremes,
      pullback rides an existing trend, ORB/LWVB break a price level. This trades a
      VOLATILITY-REGIME transition (compression -> expansion) — a fresh axis.
    * Fires whenever a squeeze forms and releases (several times a day on 5m/15m),
      so it's genuinely active, not once-per-day.
    * Fits the money lesson: a squeeze release is the START of an expansion move, so
      the trailing/runner exits can ride a large multiple of the (small, compressed)
      initial risk — big R relative to a tight stop.

Logic (fire on the release bar, momentum direction):
    1. SQUEEZE ON when BB(bb_period, bb_std) is fully inside KC(kc_ema, kc_atr,
       kc_mult) — low volatility (``is_squeeze``).
    2. SQUEEZE FIRES on the first bar where the prior bar was in a squeeze and the
       current bar is NOT (bands expanded back outside the Keltner Channel).
    3. DIRECTION from Carter's momentum baseline over ``mom_len`` bars:
       baseline = avg( (highestHigh+lowestLow)/2 , SMA(close) );
       momentum = close − baseline. momentum > 0 -> LONG, < 0 -> SHORT.
    4. ENTRY at the release bar's close. STOP = ``sl_atr_mult`` × ATR from entry
       (tight — the compressed range). This defines 1R. No exit baked in — the exit
       sweep applies each model; the trailing/runner exits ride the expansion.
    5. A cooldown prevents re-firing every bar while volatility stays expanded.

Parameters (lean):
    bb_period, bb_std           — Bollinger Bands (20, 2.0)
    kc_ema, kc_atr, kc_mult     — Keltner Channels (20, 10, 1.5) — Carter defaults
    mom_len                     — momentum baseline lookback (20)
    atr_period, sl_atr_mult     — ATR stop (14, 1.5)
    cooldown_bars               — bars to skip after a fire (default 6)
    allow_long / allow_short    — enable each side (default both)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.cfd_research.entry_strategy import EntryContext, EntryStrategy
from app.cfd_research.exit_models import EntryIntent
from app.cfd_strategy.base import Direction
from app.core.models import Candle, Timeframe
from app.strategy.indicators import atr, is_squeeze, sma
from app.utils import forex_hours


@dataclass
class _SqueezeState:
    cooldown_remaining: int = 0
    last_trading_day: str = ""


class SqueezeBreakout(EntryStrategy):
    """TTM Squeeze release breakout — fire-anytime, volatility-regime entry.

    Builds one variant per timeframe (session/regime/volatility are free tags).
    """

    name = "TTM Squeeze Breakout"

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        kc_ema: int = 20,
        kc_atr: int = 10,
        kc_mult: float = 1.5,
        mom_len: int = 20,
        atr_period: int = 14,
        sl_atr_mult: float = 1.5,
        cooldown_bars: int = 6,
        allow_long: bool = True,
        allow_short: bool = True,
        instruments: tuple[str, ...] = (),
        timeframe: Timeframe = Timeframe.M5,
    ) -> None:
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.kc_ema = kc_ema
        self.kc_atr = kc_atr
        self.kc_mult = kc_mult
        self.mom_len = mom_len
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.cooldown_bars = cooldown_bars
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.instruments = instruments
        self.timeframe = timeframe

        # Need enough for the squeeze (BB/KC), momentum baseline, and ATR — plus
        # one extra bar to compare against the prior bar's squeeze state.
        self.min_history = max(bb_period, kc_ema, kc_atr + 2, mom_len, atr_period + 5) + 2

        sid = f"squeeze_bb{bb_period}_kc{kc_mult:g}"
        if sl_atr_mult != 1.5:
            sid += f"_sl{sl_atr_mult:g}"
        self.strategy_id = sid

        self._state: dict[str, _SqueezeState] = {}

    def _st(self, instrument: str) -> _SqueezeState:
        st = self._state.get(instrument)
        if st is None:
            st = _SqueezeState()
            self._state[instrument] = st
        return st

    def _squeeze(self, candles: list[Candle]):
        return is_squeeze(candles, self.bb_period, self.bb_std,
                          self.kc_ema, self.kc_atr, self.kc_mult)

    def entries(self, ctx: EntryContext) -> list[EntryIntent]:
        candle = ctx.candle
        history = ctx.history
        instrument = ctx.instrument
        st = self._st(instrument)

        # --- Trading-day reset (clear cooldown on new FX day) ---
        dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, timezone.utc)
        today = forex_hours.trading_day(dt)
        if today != st.last_trading_day:
            st.last_trading_day = today
            st.cooldown_remaining = 0

        if st.cooldown_remaining > 0:
            st.cooldown_remaining -= 1
            return []

        if len(history) < self.min_history:
            return []

        # --- Squeeze fire = prior bar in squeeze, current bar out of squeeze ---
        sq_now = self._squeeze(history)
        sq_prev = self._squeeze(history[:-1])
        if sq_now is None or sq_prev is None:
            return []
        if not (sq_prev and not sq_now):
            return []   # not a fresh release

        atr_val = atr(history, self.atr_period)
        if atr_val is None or atr_val <= 0:
            return []

        # --- Momentum direction (Carter baseline) over mom_len bars ---
        window = history[-self.mom_len:]
        hh = max(c.high for c in window)
        ll = min(c.low for c in window)
        sma_c = sma([c.close for c in window], len(window))
        if sma_c is None:
            return []
        baseline = ((hh + ll) / 2.0 + sma_c) / 2.0
        close = candle.close
        momentum = close - baseline

        stop_dist = self.sl_atr_mult * atr_val
        if momentum > 0 and self.allow_long:
            st.cooldown_remaining = self.cooldown_bars
            return [EntryIntent(
                instrument=instrument, direction=Direction.LONG,
                entry_price=close, stop_loss=close - stop_dist,
                entry_time_ms=candle.timestamp_ms,
                reason=f"Squeeze fire long: BB expanded outside KC, mom={momentum:.5f}>0",
            )]
        if momentum < 0 and self.allow_short:
            st.cooldown_remaining = self.cooldown_bars
            return [EntryIntent(
                instrument=instrument, direction=Direction.SHORT,
                entry_price=close, stop_loss=close + stop_dist,
                entry_time_ms=candle.timestamp_ms,
                reason=f"Squeeze fire short: BB expanded outside KC, mom={momentum:.5f}<0",
            )]
        return []

    def on_day_reset(self) -> None:
        self._state.clear()
