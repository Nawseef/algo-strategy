"""
Liquidity Sweep Reversal — the third research entry hypothesis.

Hypothesis (the single most trader-reported "reliable, repeatable" intraday
pattern on gold/silver/indices right now — SMC / institutional order-flow):
price is driven THROUGH a cluster of resting stop orders just beyond a recent
swing high / low (a "liquidity sweep" / stop-hunt), triggers those stops, then
REVERSES sharply back into the range. You fade the sweep — enter as price closes
back inside the range, stop just beyond the sweep wick, target the opposing
liquidity pool.

WHY THIS FITS THE MONEY LESSON (§18.3 — big R beats cost):
    * **Tight stop, far target.** The stop sits just past the sweep wick (a few
      ticks); the target is the opposite side of the range (the opposing swing).
      So the natural R:R is large (often 2–4:1) and cost is a SMALL fraction of R
      — the exact property the ORB lacked (its R was tiny, so cost ate the edge).
    * **Objective + mechanical.** A sweep is one condition: a wick pierces the
      lookback swing extreme AND the bar closes back inside. No discretion.
    * **Fire-anytime.** Fires whenever a sweep + confirmation aligns (not once per
      session). Session / regime / volatility become FREE TAGS the scorer slices.

Reference (documented rule set, "Balanced" mode — the recommended XAUUSD M5 setup):
    1. LIQUIDITY ZONES: rolling highest-high / lowest-low over ``lookback`` bars
       (excluding the current bar) — where retail stops accumulate.
    2. SWEEP: a bar whose wick breaks a zone but whose CLOSE is back inside:
         * high > prior swing-high AND close < swing-high  -> bearish sweep -> SHORT
         * low  < prior swing-low  AND close > swing-low    -> bullish sweep -> LONG
    3. CONFIRMATION (both, toggleable):
         * EMA reclaim: long requires close > EMA(ema_len); short close < EMA.
         * VWAP bias:   long requires close > session VWAP; short close < VWAP.
    4. ENTRY at the sweep bar's close (back inside the range). STOP just beyond
       the sweep wick (+ ``sl_buffer_atr`` × ATR). The stop is technically
       invalidating (if price reclaims the wick, the setup is wrong). This defines
       1R. No exit baked in — the exit sweep applies each exit model (their fixed
       / trailing targets approximate "the opposing liquidity pool").

Parameters (lean):
    lookback        — bars for the swing high/low liquidity zone (default 20)
    ema_len         — EMA for the reclaim confirmation (default 9)
    sl_buffer_atr   — stop buffer beyond the wick, in ATR units (default 0.1)
    atr_period      — ATR period for the buffer (default 14)
    require_ema     — require the EMA reclaim confirmation (default True)
    require_vwap    — require the session-VWAP bias confirmation (default True)
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
from app.strategy.indicators import atr, ema
from app.utils import forex_hours


def _session_vwap(candles: list[Candle]) -> float | None:
    """Session VWAP (tick-volume weighted) from the provided same-day candles."""
    if not candles:
        return None
    cum_tpv = 0.0
    cum_vol = 0
    for c in candles:
        tp = (c.high + c.low + c.close) / 3.0
        cum_tpv += tp * c.volume
        cum_vol += c.volume
    if cum_vol == 0:
        return None
    return cum_tpv / cum_vol


@dataclass
class _SweepState:
    cooldown_remaining: int = 0
    last_trading_day: str = ""


class LiquiditySweep(EntryStrategy):
    """Liquidity-sweep (stop-hunt) reversal — fire-anytime, confirmation-filtered.

    Builds one variant per timeframe (session/regime/volatility are free tags).
    """

    name = "Liquidity Sweep Reversal"

    def __init__(
        self,
        lookback: int = 20,
        ema_len: int = 9,
        sl_buffer_atr: float = 0.1,
        atr_period: int = 14,
        require_ema: bool = True,
        require_vwap: bool = True,
        cooldown_bars: int = 3,
        allow_long: bool = True,
        allow_short: bool = True,
        instruments: tuple[str, ...] = (),
        timeframe: Timeframe = Timeframe.M5,
    ) -> None:
        self.lookback = lookback
        self.ema_len = ema_len
        self.sl_buffer_atr = sl_buffer_atr
        self.atr_period = atr_period
        self.require_ema = require_ema
        self.require_vwap = require_vwap
        self.cooldown_bars = cooldown_bars
        self.allow_long = allow_long
        self.allow_short = allow_short
        self.instruments = instruments
        self.timeframe = timeframe

        # Need enough for the lookback swing + EMA + ATR.
        self.min_history = max(lookback + 1, ema_len + 1, atr_period + 5)

        sid = f"sweep_lb{lookback}"
        if require_ema:
            sid += f"_ema{ema_len}"
        else:
            sid += "_noema"
        if not require_vwap:
            sid += "_novwap"
        if sl_buffer_atr != 0.1:
            sid += f"_buf{sl_buffer_atr:g}"
        self.strategy_id = sid

        self._state: dict[str, _SweepState] = {}

    def _st(self, instrument: str) -> _SweepState:
        st = self._state.get(instrument)
        if st is None:
            st = _SweepState()
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

        # --- Liquidity zones: swing high/low over the lookback, EXCLUDING the
        # current bar (the sweep bar must break a level formed by PRIOR bars). ---
        prior = history[-(self.lookback + 1):-1]
        if len(prior) < self.lookback:
            return []
        swing_high = max(c.high for c in prior)
        swing_low = min(c.low for c in prior)

        atr_val = atr(history, self.atr_period)
        if atr_val is None or atr_val <= 0:
            return []
        buffer = self.sl_buffer_atr * atr_val

        # --- EMA (reclaim confirmation) ---
        ema_val = ema([c.close for c in history], self.ema_len) if self.require_ema else None
        if self.require_ema and ema_val is None:
            return []

        # --- Session VWAP (bias confirmation) ---
        vwap_val = None
        if self.require_vwap:
            today_candles: list[Candle] = []
            for c in reversed(history):
                c_dt = datetime.fromtimestamp(c.timestamp_ms / 1000, timezone.utc)
                if forex_hours.trading_day(c_dt) == today:
                    today_candles.append(c)
                else:
                    break
            today_candles.reverse()
            vwap_val = _session_vwap(today_candles)
            if vwap_val is None:
                return []

        close = candle.close
        results: list[EntryIntent] = []

        # --- Bearish sweep -> SHORT: wick pierced swing HIGH, closed back inside ---
        if self.allow_short and candle.high > swing_high and close < swing_high:
            ok = True
            if self.require_ema and not (close < ema_val):
                ok = False
            if self.require_vwap and not (close < vwap_val):
                ok = False
            if ok:
                stop = candle.high + buffer
                if stop > close:  # valid short stop (above entry)
                    results.append(EntryIntent(
                        instrument=instrument, direction=Direction.SHORT,
                        entry_price=close, stop_loss=stop,
                        entry_time_ms=candle.timestamp_ms,
                        target_price=swing_low,   # opposing liquidity pool
                        reason=f"Sweep short: high={candle.high:.5f} > swingHi={swing_high:.5f}, "
                               f"close back inside; SL beyond wick",
                    ))

        # --- Bullish sweep -> LONG: wick pierced swing LOW, closed back inside ---
        elif self.allow_long and candle.low < swing_low and close > swing_low:
            ok = True
            if self.require_ema and not (close > ema_val):
                ok = False
            if self.require_vwap and not (close > vwap_val):
                ok = False
            if ok:
                stop = candle.low - buffer
                if stop < close:  # valid long stop (below entry)
                    results.append(EntryIntent(
                        instrument=instrument, direction=Direction.LONG,
                        entry_price=close, stop_loss=stop,
                        entry_time_ms=candle.timestamp_ms,
                        target_price=swing_high,   # opposing liquidity pool
                        reason=f"Sweep long: low={candle.low:.5f} < swingLo={swing_low:.5f}, "
                               f"close back inside; SL beyond wick",
                    ))

        if results:
            st.cooldown_remaining = self.cooldown_bars
        return results

    def on_day_reset(self) -> None:
        self._state.clear()
