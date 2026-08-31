"""
Trend Pullback (EMA bounce) — the fourth research entry hypothesis.

Hypothesis (the single most-discussed retail intraday setup across gold / indices
/ FX — trend continuation): in an established trend, price periodically pulls
back to a fast moving average, then RESUMES in the trend direction. You buy the
dip in an uptrend (or sell the rally in a downtrend) as price bounces off the
fast EMA, with the trend confirmed by a slow EMA + ADX.

WHY THIS COMPLETES THE STRATEGY SET (owner's §2 goal — a set that survives regime
change):
    * ORB = breakout. MR = fade an extension (needs a RANGE, ADX low). Sweep =
      stop-hunt reversal. **This one is TREND CONTINUATION — it needs a TREND
      (ADX high), the exact regime where MR/sweep must sit out.** So the set
      covers both regimes: when the market ranges, MR/sweep fire; when it trends,
      this fires. That's the "one strategy fails when the regime changes" fix.

WHY IT FITS THE MONEY LESSON (§18.3 — big R beats cost):
    * **Tight stop, trend-sized target.** The stop sits just below the pullback
      low (a fast-EMA bounce is shallow), while the target rides the trend — the
      trailing / runner exit models can capture multi-R continuation. So R is big
      relative to the stop, and cost is a small fraction of it.
    * **Trades with the trend, not against it** — the highest-expectancy side of
      a trending market, and the retail-favourite "buy the dip" that people claim
      "always works" in gold / NAS100 uptrends.

Logic (fire-anytime, regime-filtered to TREND):
    1. TREND FILTER (the differentiator): fast EMA on the correct side of slow
       EMA AND ADX >= adx_min (a real trend, not chop). Long only in an uptrend,
       short only in a downtrend.
    2. PULLBACK + RESUME on the just-closed bar:
         * uptrend: bar's LOW dips to/below the fast EMA (the pullback) but the
           bar CLOSES back above it and closes bullish (the bounce) -> LONG
         * downtrend: bar's HIGH pokes to/above the fast EMA but CLOSES back below
           it and closes bearish (the bounce) -> SHORT
    3. MOMENTUM (optional): RSI on the trend side (long: RSI >= 50; short: <= 50)
       — confirms momentum has turned back with the trend, filters dead bounces.
    4. ENTRY at the bar's close. STOP just beyond the pullback extreme
       (+ sl_buffer_atr × ATR). This defines 1R. No exit baked in — the exit
       sweep applies each model (trailing / runner capture the trend beyond 2R).

Parameters (lean):
    ema_fast        — pullback / bounce reference EMA (default 20)
    ema_slow        — trend-direction EMA (default 50)
    adx_period      — ADX period (default 14)
    adx_min         — require ADX >= this (a real trend; default 20)
    atr_period      — ATR for the stop buffer (default 14)
    sl_buffer_atr   — stop buffer beyond the pullback extreme, in ATR (default 0.2)
    rsi_period      — RSI period for the momentum filter (default 14)
    require_momentum — require the RSI momentum confirmation (default True)
    cooldown_bars   — bars to skip after an entry (default 4)
    allow_long / allow_short — enable each side (default both)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.cfd_research.entry_strategy import EntryContext, EntryStrategy
from app.cfd_research.exit_models import EntryIntent
from app.cfd_strategy.base import Direction
from app.core.models import Candle, Timeframe
from app.strategy.indicators import adx, atr, ema, rsi
from app.utils import forex_hours


@dataclass
class _PullbackState:
    cooldown_remaining: int = 0
    last_trading_day: str = ""


class TrendPullback(EntryStrategy):
    """Trend-continuation EMA-pullback bounce — fire-anytime, trend-regime-filtered.

    Builds one variant per timeframe (session/regime/volatility are free tags).
    """

    name = "Trend Pullback (EMA bounce)"

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        adx_period: int = 14,
        adx_min: float = 20.0,
        atr_period: int = 14,
        sl_buffer_atr: float = 0.2,
        rsi_period: int = 14,
        require_momentum: bool = True,
        cooldown_bars: int = 4,
        allow_long: bool = True,
        allow_short: bool = True,
        instruments: tuple[str, ...] = (),
        timeframe: Timeframe = Timeframe.M5,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.adx_period = adx_period
        self.adx_min = adx_min
        self.atr_period = atr_period
        self.sl_buffer_atr = sl_buffer_atr
        self.rsi_period = rsi_period
        self.require_momentum = require_momentum
        self.cooldown_bars = cooldown_bars
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.instruments = instruments
        self.timeframe = timeframe

        # Need enough for the slow EMA, ADX (2*period+1), ATR, RSI.
        self.min_history = max(ema_slow + 1, 2 * adx_period + 2, atr_period + 5, rsi_period + 1)

        sid = f"pullback_ema{ema_fast}_{ema_slow}_adx{adx_min:g}"
        if not require_momentum:
            sid += "_nomom"
        if sl_buffer_atr != 0.2:
            sid += f"_buf{sl_buffer_atr:g}"
        self.strategy_id = sid

        self._state: dict[str, _PullbackState] = {}

    def _st(self, instrument: str) -> _PullbackState:
        st = self._state.get(instrument)
        if st is None:
            st = _PullbackState()
            self._state[instrument] = st
        return st

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

        # --- Cooldown ---
        if st.cooldown_remaining > 0:
            st.cooldown_remaining -= 1
            return []

        if len(history) < self.min_history:
            return []

        closes = [c.close for c in history]
        ef = ema(closes, self.ema_fast)
        es = ema(closes, self.ema_slow)
        if ef is None or es is None:
            return []

        # --- Trend filter: ADX must confirm a real trend (the differentiator) ---
        adx_val = adx(history, self.adx_period)
        if adx_val is None or adx_val < self.adx_min:
            return []

        atr_val = atr(history, self.atr_period)
        if atr_val is None or atr_val <= 0:
            return []
        buffer = self.sl_buffer_atr * atr_val

        rsi_val = rsi(closes, self.rsi_period) if self.require_momentum else None
        if self.require_momentum and rsi_val is None:
            return []

        close = candle.close
        results: list[EntryIntent] = []

        uptrend = ef > es and close > es
        downtrend = ef < es and close < es

        # --- LONG: uptrend, pullback tagged the fast EMA, bar bounced back above ---
        if self.allow_long and uptrend:
            pulled_back = candle.low <= ef
            bounced = close > ef and close > candle.open
            momentum_ok = (not self.require_momentum) or (rsi_val is not None and rsi_val >= 50.0)
            if pulled_back and bounced and momentum_ok:
                stop = candle.low - buffer
                if stop < close:
                    st.cooldown_remaining = self.cooldown_bars
                    return [EntryIntent(
                        instrument=instrument, direction=Direction.LONG,
                        entry_price=close, stop_loss=stop,
                        entry_time_ms=candle.timestamp_ms,
                        reason=f"Trend pullback long: uptrend (ADX={adx_val:.1f}), "
                               f"bounce off EMA{self.ema_fast}",
                    )]

        # --- SHORT: downtrend, pullback tagged the fast EMA, bar bounced back below ---
        if self.allow_short and downtrend:
            pulled_back = candle.high >= ef
            bounced = close < ef and close < candle.open
            momentum_ok = (not self.require_momentum) or (rsi_val is not None and rsi_val <= 50.0)
            if pulled_back and bounced and momentum_ok:
                stop = candle.high + buffer
                if stop > close:
                    st.cooldown_remaining = self.cooldown_bars
                    return [EntryIntent(
                        instrument=instrument, direction=Direction.SHORT,
                        entry_price=close, stop_loss=stop,
                        entry_time_ms=candle.timestamp_ms,
                        reason=f"Trend pullback short: downtrend (ADX={adx_val:.1f}), "
                               f"bounce off EMA{self.ema_fast}",
                    )]

        return []

    def on_day_reset(self) -> None:
        self._state.clear()
