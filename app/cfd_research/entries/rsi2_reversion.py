"""
Larry Connors RSI-2 — a famous-trader entry hypothesis.

Larry Connors (with Cesar Alvarez, "Short Term Trading Strategies That Work",
2008) popularised the 2-period RSI: use an absurdly short RSI to catch a
short-term panic dip WITHIN a larger uptrend, buy it, and exit when price snaps
back to its short moving average. It is one of the most-cited, most-backtested
systematic mean-reversion strategies in existence.

WHY IT'S A DISTINCT ADDITION (not a duplicate of mr/sweep/pullback):
    * mr fades a stretch from VWAP in a RANGE; pullback buys an EMA touch in a
      trend; sweep fades a stop-hunt. RSI-2 is different again: a **trend-ALIGNED
      oscillator mean-reversion** — it only buys dips while price is ABOVE a long
      trend MA (200), using a 2-period RSI extreme as the trigger, not a band
      touch or an EMA tag. High win rate, small per-trade gain.
    * It pairs EXACTLY with the target_mean exit (Connors exits at the short MA —
      the mean), which is why that exit model was added. The other exits still
      apply so the scorer can compare.

Logic (fire-anytime, trend-filtered by the 200-MA):
    1. TREND FILTER: long only when close > SMA(trend_ma); short only when
       close < SMA(trend_ma). (Connors classically trades only the long side in
       an uptrend; we allow both symmetrically and let the scorer decide.)
    2. TRIGGER: RSI(rsi_period=2) < oversold  -> LONG  (short-term panic dip);
                RSI(rsi_period=2) > overbought -> SHORT (short-term euphoria spike).
    3. ENTRY at the bar close. TARGET = SMA(exit_ma) — the short mean Connors
       exits into (carried as target_price for the target_mean exit).
    4. STOP: sl_atr_mult × ATR from entry (the engine requires a hard stop to
       define 1R; Connors' original exits on the MA cross, which target_mean
       reproduces — the ATR stop is the money-safety floor).

Parameters (lean):
    rsi_period      — the (very short) RSI lookback (default 2)
    oversold        — long trigger, RSI below this (default 10)
    overbought      — short trigger, RSI above this (default 90)
    trend_ma        — long trend filter SMA (default 200)
    exit_ma         — the short mean target SMA (default 5)
    atr_period, sl_atr_mult — ATR stop (14, 1.5)
    cooldown_bars   — bars to skip after an entry (default 3)
    allow_long / allow_short — enable each side (default both)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.cfd_research.entry_strategy import EntryContext, EntryStrategy
from app.cfd_research.exit_models import EntryIntent
from app.cfd_strategy.base import Direction
from app.core.models import Candle, Timeframe
from app.strategy.indicators import atr, rsi, sma
from app.utils import forex_hours


@dataclass
class _Rsi2State:
    cooldown_remaining: int = 0
    last_trading_day: str = ""


class Rsi2Reversion(EntryStrategy):
    """Larry Connors RSI-2 trend-aligned mean reversion — fire-anytime.

    Builds one variant per timeframe (session/regime/volatility are free tags).
    """

    name = "Larry Connors RSI-2"

    def __init__(
        self,
        rsi_period: int = 2,
        oversold: float = 10.0,
        overbought: float = 90.0,
        trend_ma: int = 200,
        exit_ma: int = 5,
        atr_period: int = 14,
        sl_atr_mult: float = 1.5,
        cooldown_bars: int = 3,
        allow_long: bool = True,
        allow_short: bool = True,
        instruments: tuple[str, ...] = (),
        timeframe: Timeframe = Timeframe.M5,
    ) -> None:
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.trend_ma = trend_ma
        self.exit_ma = exit_ma
        self.atr_period = atr_period
        self.sl_atr_mult = sl_atr_mult
        self.cooldown_bars = cooldown_bars
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.instruments = instruments
        self.timeframe = timeframe

        # Need the full trend MA (200) plus RSI/ATR — the trend MA dominates.
        self.min_history = max(trend_ma + 1, rsi_period + 2, atr_period + 5, exit_ma + 1)

        sid = f"rsi2_{rsi_period}_os{oversold:g}_ma{trend_ma}"
        self.strategy_id = sid

        self._state: dict[str, _Rsi2State] = {}

    def _st(self, instrument: str) -> _Rsi2State:
        st = self._state.get(instrument)
        if st is None:
            st = _Rsi2State()
            self._state[instrument] = st
        return st

    def entries(self, ctx: EntryContext) -> list[EntryIntent]:
        candle = ctx.candle
        history = ctx.history
        instrument = ctx.instrument
        st = self._st(instrument)

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

        closes = [c.close for c in history]
        trend = sma(closes, self.trend_ma)
        r = rsi(closes, self.rsi_period)
        mean_target = sma(closes, self.exit_ma)
        atr_val = atr(history, self.atr_period)
        if trend is None or r is None or mean_target is None or atr_val is None or atr_val <= 0:
            return []

        close = candle.close
        stop_dist = self.sl_atr_mult * atr_val

        # LONG: uptrend (above the trend MA) + short-term oversold panic
        if self.allow_long and close > trend and r < self.oversold:
            st.cooldown_remaining = self.cooldown_bars
            return [EntryIntent(
                instrument=instrument, direction=Direction.LONG,
                entry_price=close, stop_loss=close - stop_dist,
                entry_time_ms=candle.timestamp_ms,
                target_price=mean_target,   # exit at the short mean (Connors)
                reason=f"RSI2 long: uptrend (close>SMA{self.trend_ma}), RSI({self.rsi_period})={r:.1f}<{self.oversold:g}",
            )]

        # SHORT: downtrend (below the trend MA) + short-term overbought spike
        if self.allow_short and close < trend and r > self.overbought:
            st.cooldown_remaining = self.cooldown_bars
            return [EntryIntent(
                instrument=instrument, direction=Direction.SHORT,
                entry_price=close, stop_loss=close + stop_dist,
                entry_time_ms=candle.timestamp_ms,
                target_price=mean_target,   # exit at the short mean (Connors)
                reason=f"RSI2 short: downtrend (close<SMA{self.trend_ma}), RSI({self.rsi_period})={r:.1f}>{self.overbought:g}",
            )]

        return []

    def on_day_reset(self) -> None:
        self._state.clear()
