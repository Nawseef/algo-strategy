"""
Mean-Reversion / VWAP-Fade — the second research entry hypothesis.

Hypothesis (well-documented intraday edge): when price extends far from the
session's volume-weighted average price, it has a statistical tendency to revert
back. This is especially pronounced on range-bound / low-ADX days, and during
the mid-session (not right at the open). ~70% of trading days on indices are
range-bound — so a mean-reversion edge that works in those conditions fires
frequently and fits the prop-firm requirement of consistent small wins with low
drawdown.

WHY THIS SOLVES THE ORB'S PROBLEM (the money lesson from §18.3):
    * **Big R per trade.** The stop is a tight 1×ATR beyond the candle extreme.
      The target is VWAP (or the Bollinger midline) — typically 2–4× that distance.
      So cost (spread + commission) is a SMALL FRACTION of R, unlike the ORB where
      cost was a large fraction of the tiny opening-range R.
    * **High frequency.** Fires any time conditions align (not once per session
      open). More signals → faster eval pass → solves the ORB's "2–9 years to
      pass" time problem.
    * **Natural fit for prop-firm rules.** High WR (~55–65% with filters), small
      losses (tight ATR stop), consistent small gains → daily DD stays low.

Logic (fire-anytime, regime-filtered):
    1. Compute session VWAP (resets each FX trading day) and Bollinger Bands
       (BB period / std_dev on the lookback).
    2. REGIME FILTER: skip if ADX > threshold (trending market = reversion fails).
    3. EXTENSION CHECK: price must be at or beyond the outer Bollinger Band AND
       at least ``min_vwap_dev × ATR`` away from VWAP (confirms a real stretch,
       not just tight bands).
    4. REJECTION CANDLE (optional): the entry bar shows a wick-rejection off the
       extreme (bullish rejection at lows = long; bearish rejection at highs =
       short). This is the "rubber band has started snapping back" confirmation.
    5. ENTRY at the candle close (toward VWAP). STOP = ATR beyond the extreme
       (tight, risk is small relative to the distance back to VWAP).
    6. No exit baked in — the exit sweep applies each exit model. The natural
       target for the exit models is VWAP / the BB midline (roughly 2–4R away),
       but the fixed-RR / trail / time models all apply and the pipeline scores
       them.

Fire-anytime design:
    * Session / regime / volatility become FREE TAGS (sliced afterward, not
      generation axes). The strategy builds ONE variant per timeframe.
    * Multiple entries per day are expected (range days produce 2–5 fade setups).
    * Cooldown: after an entry, skip the next ``cooldown_bars`` bars to avoid
      stacking into the same move that hasn't reverted yet.

Parameters (lean — each earns its place):
    bb_period       — Bollinger Band lookback (default 20)
    bb_std          — standard deviations for the outer band (default 2.0)
    adx_threshold   — skip entry when ADX > this (default 25; trending)
    adx_period      — ADX computation period (default 14)
    atr_period      — ATR for stop sizing + extension check (default 14)
    atr_stop_mult   — stop = N × ATR beyond the extreme (default 1.0)
    min_vwap_dev    — minimum |price − VWAP| / ATR to confirm extension (default 1.5)
    require_rejection — require a rejection candle pattern (default True)
    cooldown_bars   — bars to skip after an entry (default 6 = 30 min on 5m)
    session_vwap    — use session VWAP (True) or just BB midline (False) for the
                      extension check (default True; False = simpler BB-only mode)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.cfd_research.entry_strategy import EntryContext, EntryStrategy
from app.cfd_research.exit_models import EntryIntent
from app.cfd_strategy.base import Direction
from app.core.models import Candle, Timeframe
from app.strategy.indicators import adx, atr, bollinger_bands
from app.utils import forex_hours


def _session_vwap(candles: list[Candle]) -> float | None:
    """
    Compute VWAP from the provided candles (caller passes today's session candles).

    Uses tick-volume weighting. Returns None if no volume or empty.
    """
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


def _is_bullish_rejection(candle: Candle) -> bool:
    """Lower wick > 2× body, close above open (hammer-like)."""
    body = abs(candle.close - candle.open)
    lower_wick = min(candle.close, candle.open) - candle.low
    return lower_wick > 2.0 * body and candle.close > candle.open


def _is_bearish_rejection(candle: Candle) -> bool:
    """Upper wick > 2× body, close below open (shooting-star-like)."""
    body = abs(candle.close - candle.open)
    upper_wick = candle.high - max(candle.close, candle.open)
    return upper_wick > 2.0 * body and candle.close < candle.open


@dataclass
class _MRState:
    """Per-instrument state for the cooldown logic."""
    cooldown_remaining: int = 0
    last_trading_day: str = ""


class MeanReversion(EntryStrategy):
    """
    VWAP / Bollinger-Band mean-reversion fade — fire-anytime, regime-filtered.

    Builds one variant per timeframe (session/regime/volatility are free tags).
    """

    name = "Mean-Reversion VWAP Fade"

    def __init__(
        self,
        bb_period: int = 20,
        bb_std: float = 2.0,
        adx_threshold: float = 25.0,
        adx_period: int = 14,
        atr_period: int = 14,
        atr_stop_mult: float = 1.0,
        min_vwap_dev: float = 1.5,
        require_rejection: bool = True,
        cooldown_bars: int = 6,
        session_vwap: bool = True,
        instruments: tuple[str, ...] = (),
        timeframe: Timeframe = Timeframe.M5,
    ) -> None:
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.adx_threshold = adx_threshold
        self.adx_period = adx_period
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.min_vwap_dev = min_vwap_dev
        self.require_rejection = require_rejection
        self.cooldown_bars = cooldown_bars
        self.session_vwap = session_vwap
        self.instruments = instruments
        self.timeframe = timeframe

        # min_history: need enough for BB (bb_period) + ADX (2*adx_period+1) + ATR
        self.min_history = max(bb_period + 1, 2 * adx_period + 2, atr_period + 5)

        # Build a descriptive strategy_id
        sid = f"mr_bb{bb_period}s{bb_std:g}_adx{adx_threshold:g}"
        if not require_rejection:
            sid += "_norej"
        if atr_stop_mult != 1.0:
            sid += f"_sl{atr_stop_mult:g}"
        if min_vwap_dev != 1.5:
            sid += f"_dev{min_vwap_dev:g}"
        if not session_vwap:
            sid += "_bbonly"
        self.strategy_id = sid

        self._state: dict[str, _MRState] = {}

    def _st(self, instrument: str) -> _MRState:
        st = self._state.get(instrument)
        if st is None:
            st = _MRState()
            self._state[instrument] = st
        return st

    def entries(self, ctx: EntryContext) -> list[EntryIntent]:
        candle = ctx.candle
        history = ctx.history
        instrument = ctx.instrument

        st = self._st(instrument)

        # --- Trading-day reset (clear cooldown on new day) ---
        dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, timezone.utc)
        today = forex_hours.trading_day(dt)
        if today != st.last_trading_day:
            st.last_trading_day = today
            st.cooldown_remaining = 0

        # --- Cooldown check ---
        if st.cooldown_remaining > 0:
            st.cooldown_remaining -= 1
            return []

        # --- Compute indicators ---
        # ADX regime filter: skip if trending
        adx_val = adx(history, self.adx_period)
        if adx_val is None or adx_val > self.adx_threshold:
            return []

        # ATR for stop sizing and extension check
        atr_val = atr(history, self.atr_period)
        if atr_val is None or atr_val <= 0:
            return []

        # Bollinger Bands
        bb = bollinger_bands(history, self.bb_period, self.bb_std)
        if bb is None:
            return []
        upper_bb, middle_bb, lower_bb = bb

        # Session VWAP (optional — for the extension confirmation)
        vwap_val: float | None = None
        if self.session_vwap:
            # Collect today's candles from history for VWAP computation.
            # Walk backward from the end of history to find same-trading-day bars.
            today_candles: list[Candle] = []
            for c in reversed(history):
                c_dt = datetime.fromtimestamp(c.timestamp_ms / 1000, timezone.utc)
                if forex_hours.trading_day(c_dt) == today:
                    today_candles.append(c)
                else:
                    break
            today_candles.reverse()
            if today_candles:
                vwap_val = _session_vwap(today_candles)

        # --- Entry logic ---
        close = ctx.close
        results: list[EntryIntent] = []

        # SHORT setup: price at or above upper BB (overextended to the upside)
        if close >= upper_bb:
            # Extension confirmation: distance from VWAP (or BB midline) must be
            # at least min_vwap_dev × ATR
            ref = vwap_val if (self.session_vwap and vwap_val is not None) else middle_bb
            deviation = close - ref
            if deviation < self.min_vwap_dev * atr_val:
                pass  # not extended enough
            elif self.require_rejection and not _is_bearish_rejection(candle):
                pass  # no rejection candle
            else:
                # STOP: above the candle high by atr_stop_mult × ATR
                stop = candle.high + self.atr_stop_mult * atr_val
                results.append(EntryIntent(
                    instrument=instrument,
                    direction=Direction.SHORT,
                    entry_price=close,
                    stop_loss=stop,
                    entry_time_ms=candle.timestamp_ms,
                    target_price=ref,   # the mean — natural target for the fade
                    reason=f"MR fade short: close={close:.5f} >= upperBB={upper_bb:.5f}, "
                           f"dev={deviation / atr_val:.1f}×ATR, ADX={adx_val:.1f}",
                ))

        # LONG setup: price at or below lower BB (overextended to the downside)
        elif close <= lower_bb:
            ref = vwap_val if (self.session_vwap and vwap_val is not None) else middle_bb
            deviation = ref - close
            if deviation < self.min_vwap_dev * atr_val:
                pass  # not extended enough
            elif self.require_rejection and not _is_bullish_rejection(candle):
                pass  # no rejection candle
            else:
                # STOP: below the candle low by atr_stop_mult × ATR
                stop = candle.low - self.atr_stop_mult * atr_val
                results.append(EntryIntent(
                    instrument=instrument,
                    direction=Direction.LONG,
                    entry_price=close,
                    stop_loss=stop,
                    entry_time_ms=candle.timestamp_ms,
                    target_price=ref,   # the mean — natural target for the fade
                    reason=f"MR fade long: close={close:.5f} <= lowerBB={lower_bb:.5f}, "
                           f"dev={deviation / atr_val:.1f}×ATR, ADX={adx_val:.1f}",
                ))

        # Set cooldown if an entry was emitted
        if results:
            st.cooldown_remaining = self.cooldown_bars

        return results

    def on_day_reset(self) -> None:
        """Reset per-instrument state at the FX trading-day boundary."""
        self._state.clear()
