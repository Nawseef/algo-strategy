"""
USDJPY Session Opening-Range Breakout — live paper-trading variants.

Ported from the research-side ``app/cfd_research/entries/session_orb.py`` and the
10-year backtest results. Two specific slices are implemented here:

  1. **orb_usdjpy_tokyo_5m** — USDJPY, Tokyo session open, 5m timeframe, range
     regime only, fixed 2R exit. (Research: 436/436 decisive pass at raw cost.)
  2. **orb_usdjpy_london_15m** — USDJPY, London session open, 15m timeframe,
     range regime only, fixed 2R exit. (Research raw/no-vol: 100% decisive pass
     (d418), 0% blowup, deployable — range_bars=6, i.e. a 90-min opening range.)

Both use the SAME ORB logic as the research engine:
  - Build the opening range over the first ``range_bars`` bars of the session.
  - On the first bar that CLOSES beyond the range, enter in the breakout
    direction. Stop = opposite side of the range (1R = range size).
  - Only fire in RANGE regime (ADX(14) < 22).
  - One entry per session per instrument. State resets on session end / day roll.

Exit plan: fixed 2R (``build_rr_exit_plan`` with ``rr_targets=[2.0]``).

These are registered as separate strategy instances (not one strategy with N
variants) because they trade on DIFFERENT timeframes (5m vs 15m), which requires
different evaluation cadence in the runner.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.cfd_research.regime import classify_regime
from app.cfd_research.timeframe import aggregate_htf
from app.cfd_strategy.base import (
    CFDSignal,
    CFDStrategy,
    Direction,
    EntryMode,
    StrategyContext,
    build_rr_exit_plan,
)
from app.cfd_strategy.registry import register_strategy
from app.core.models import Candle, Timeframe
from app.utils import forex_hours
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ─── ORB state machine (per instrument) ─────────────────────────

class _ORBState:
    """Tracks the opening range + breakout status for one instrument/session."""

    __slots__ = ("in_session", "range_high", "range_low", "bars", "complete", "entered")

    def __init__(self) -> None:
        self.in_session: bool = False
        self.range_high: float = 0.0
        self.range_low: float = 0.0
        self.bars: int = 0
        self.complete: bool = False
        self.entered: bool = False

    def reset(self) -> None:
        self.in_session = False
        self.range_high = 0.0
        self.range_low = 0.0
        self.bars = 0
        self.complete = False
        self.entered = False


# ─── Regime filter ───────────────────────────────────────────────

def _is_range_regime(candles: list[Candle], adx_period: int = 14, threshold: float = 22.0) -> bool:
    """True only if the CURRENT regime is exactly 'range' — using the SAME
    ``classify_regime`` the backtest used (ADX(14) + EMA(20/50), threshold 22),
    so the live filter matches the deployable slice bit-for-bit.

    Note: classify_regime returns 'unknown' until it has >= 51 bars (EMA50+1);
    'unknown' is NOT 'range', so we correctly take no trade until there is
    enough history — exactly as the research slice excluded those bars.
    """
    return classify_regime(
        candles, adx_period=adx_period, adx_trend_threshold=threshold
    ) == "range"


# ─── Base ORB strategy ───────────────────────────────────────────

class _LiveSessionORB(CFDStrategy):
    """Base class for both the 5m and 15m ORB variants.

    NOT registered directly — the two concrete subclasses below are the ones
    that get registered with the paper runner.
    """

    def __init__(
        self,
        *,
        strategy_id: str,
        name: str,
        session: str,
        timeframe: Timeframe,
        range_bars: int = 6,
        adx_period: int = 14,
        adx_threshold: float = 22.0,
        rr_target: float = 2.0,
    ) -> None:
        self.strategy_id = strategy_id
        self.name = name
        self.timeframe = timeframe
        self.instruments = ("USDJPY",)
        self.variants = ("default",)

        self._session = session
        self._range_bars = range_bars
        self._adx_period = adx_period
        self._adx_threshold = adx_threshold
        self._rr_target = rr_target

        # min_history (in HTF bars) must cover the REGIME lookback so warmup
        # seeds enough to classify from the first session — classify_regime needs
        # EMA50+1 = 51 bars, plus the opening-range bars and a small buffer.
        # Below that it returns 'unknown' (no trade), same as the backtest.
        self._htf_min = max(range_bars + 1, 51) + 6
        # min_history is expressed in BASE (5m) bars, which is what the runner
        # feeds us; for a higher TF we need (TF/5m) as many 5m bars.
        tf_mult = {
            Timeframe.M5: 1, Timeframe.M15: 3, Timeframe.M30: 6, Timeframe.H1: 12,
        }.get(timeframe, 1)
        self.min_history = self._htf_min * tf_mult + 10

        # Per-instrument state.
        self._state: dict[str, _ORBState] = {}
        # Track the last HTF bar timestamp we acted on (15m only) to avoid
        # re-evaluating within the same HTF bar.
        self._last_htf_ts: dict[str, int] = {}

    def _get_state(self, instrument: str) -> _ORBState:
        st = self._state.get(instrument)
        if st is None:
            st = _ORBState()
            self._state[instrument] = st
        return st

    def evaluate(self, ctx: StrategyContext) -> list[CFDSignal]:
        """Main evaluation — called on every 5m candle close by the runner."""
        # For the 5m variant, we evaluate directly on the 5m history.
        # For the 15m variant, we aggregate and only act when a new 15m bar
        # has completed (i.e. the HTF bar's timestamp changed).
        if self.timeframe == Timeframe.M5:
            return self._evaluate_on_bars(ctx.instrument, ctx.history, ctx.candle.timestamp_ms)
        else:
            return self._evaluate_htf(ctx)

    def _evaluate_htf(self, ctx: StrategyContext) -> list[CFDSignal]:
        """Aggregate 5m history to HTF, act only on newly completed HTF bars."""
        htf_bars = aggregate_htf(ctx.history, self.timeframe)
        if len(htf_bars) < self._htf_min:
            return []

        # Only act when the LAST completed HTF bar is new (its timestamp changed).
        # The last HTF bar in aggregate_htf is the one containing the current 5m
        # candle. If the 15m bar isn't fully closed yet (we're mid-bar), we should
        # NOT act on it. A 15m bar closes when 3 consecutive 5m bars fill it.
        # We detect "bar just closed" by checking if the most recent COMPLETE
        # 15m bar has a new timestamp vs last time we acted.
        #
        # Strategy: use the SECOND-TO-LAST HTF bar (the last COMPLETED one)
        # unless the current 5m candle is the LAST constituent of the current
        # HTF bucket (i.e. the HTF bar just closed on this tick).
        from app.cfd_research.timeframe import INTERVAL_MS, _bucket_open
        interval_ms = INTERVAL_MS[self.timeframe]
        current_bucket = _bucket_open(ctx.candle.timestamp_ms, interval_ms)
        # The current 5m bar belongs to `current_bucket`. If the NEXT 5m bar
        # would belong to a DIFFERENT bucket, then this bar is the last
        # constituent -> the HTF bar just closed.
        next_5m_ts = ctx.candle.timestamp_ms + INTERVAL_MS[Timeframe.M5]
        next_bucket = _bucket_open(next_5m_ts, interval_ms)

        if next_bucket == current_bucket:
            # We're mid-HTF-bar. Don't evaluate yet.
            return []

        # The HTF bar that just closed is the one at `current_bucket`.
        # Check if we already evaluated this bar.
        last_ts = self._last_htf_ts.get(ctx.instrument, -1)
        if current_bucket <= last_ts:
            return []
        self._last_htf_ts[ctx.instrument] = current_bucket

        # Use all HTF bars up to and including the just-closed one.
        return self._evaluate_on_bars(ctx.instrument, htf_bars, ctx.candle.timestamp_ms)

    def _evaluate_on_bars(
        self, instrument: str, bars: list[Candle], signal_ts_ms: float
    ) -> list[CFDSignal]:
        """Core ORB logic — operates on the strategy's native timeframe bars."""
        candle = bars[-1]
        dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, timezone.utc)
        active = set(forex_hours.session_tag(dt).split("+"))
        st = self._get_state(instrument)

        # Session not active -> reset and wait.
        if self._session not in active:
            if st.in_session:
                # Session just ended; reset for the next occurrence.
                st.reset()
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
        if st.bars < self._range_bars:
            st.range_high = max(st.range_high, candle.high)
            st.range_low = min(st.range_low, candle.low)
            st.bars += 1
            if st.bars >= self._range_bars:
                st.complete = True
            return []

        # Range complete -> look for the breakout (once per session).
        if not st.complete or st.entered:
            return []

        rng = st.range_high - st.range_low
        if rng <= 0:
            return []

        close = candle.close

        # REGIME FILTER: only trade in range regime (ADX < threshold).
        if not _is_range_regime(bars, self._adx_period, self._adx_threshold):
            return []

        # Detect breakout.
        direction: Direction | None = None
        stop_loss: float = 0.0

        if close > st.range_high:
            direction = Direction.LONG
            stop_loss = st.range_low
        elif close < st.range_low:
            direction = Direction.SHORT
            stop_loss = st.range_high

        if direction is None:
            return []

        # Mark entered so we don't fire again this session.
        st.entered = True

        # Build the signal with a fixed 2R exit plan.
        entry_price = close
        try:
            plan = build_rr_exit_plan(
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                rr_targets=[self._rr_target],
                exit_model=f"fixed_rr{self._rr_target:g}",
            )
            signal = CFDSignal(
                strategy_id=self.strategy_id,
                variant_id="default",
                instrument=instrument,
                direction=direction,
                entry_mode=EntryMode.CANDLE_CLOSE,
                entry_price=entry_price,
                exit_plan=plan,
                timestamp_ms=signal_ts_ms,
                reason=(
                    f"ORB {self._session} {direction.value} breakout | "
                    f"range [{st.range_low:.5g}, {st.range_high:.5g}] "
                    f"R={rng:.5g}"
                ),
            )
        except ValueError as e:
            # Can happen on very tight ranges where TP price computes badly.
            logger.debug(
                "%s: signal rejected (invalid plan): %s", self.strategy_id, e
            )
            return []

        logger.info(
            "%s ENTRY: %s %s @ %.5f | SL=%.5f TP=%.5f | R=%.5f | regime=range",
            self.strategy_id, direction.value, instrument,
            entry_price, stop_loss, plan.take_profit_prices[0], rng,
        )
        return [signal]

    def on_day_reset(self) -> None:
        """Clear per-instrument state on FX day rollover."""
        self._state.clear()
        self._last_htf_ts.clear()


# ─── Concrete variants (registered with the runner) ──────────────

@register_strategy
class OrbUsdjpyTokyo5m(_LiveSessionORB):
    """USDJPY Tokyo 5m ORB — range regime, fixed 2R.

    Research slice: instrument=USDJPY session=tokyo timeframe=5m regime=range
    exit_model=fixed_rr2, risk=0.5%. 436/436 decisive pass at raw cost.
    """

    def __init__(self) -> None:
        super().__init__(
            strategy_id="orb_usdjpy_tokyo_5m",
            name="USDJPY Tokyo ORB 5m (range, fixed 2R)",
            session="tokyo",
            timeframe=Timeframe.M5,
            range_bars=6,           # 6 x 5m = first 30 min of Tokyo
            adx_period=14,
            adx_threshold=22.0,
            rr_target=2.0,
        )


@register_strategy
class OrbUsdjpyLondon15m(_LiveSessionORB):
    """USDJPY London 15m ORB — range regime, fixed 2R.

    Research slice: instrument=USDJPY session=london timeframe=15m regime=range
    exit_model=fixed_rr2, risk=0.5%. Raw/no-volatility: n=1285, 100% decisive
    pass (d418), 0% blowup, deployable. Opening range = 6 x 15m bars (90 min).
    """

    def __init__(self) -> None:
        super().__init__(
            strategy_id="orb_usdjpy_london_15m",
            name="USDJPY London ORB 15m (range, fixed 2R)",
            session="london",
            timeframe=Timeframe.M15,
            range_bars=6,           # 6 x 15m = first 90 min — matches the deployable
                                    # research slice orb_london_6b (range_bars counts in
                                    # THIS timeframe's bars, not 5m bars). Was 2 (30 min),
                                    # which made the live variant a different, unvalidated
                                    # strategy from the backtested 6-bar slice.
            adx_period=14,
            adx_threshold=22.0,
            rr_target=2.0,
        )
