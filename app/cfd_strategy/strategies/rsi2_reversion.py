"""
RSI-2 USDJPY Sydney-session mean-reversion — live paper/demo port.

Ported from the research entry ``app/cfd_research/entries/rsi2_reversion.py`` and
the single clean deployable slice found in the 10-year backtest:

    instrument=USDJPY  session=sydney  timeframe=5m  regime=range
    exit_model=atr_trail2  risk=0.50%
    -> 100.0% pass (478 decisive evals), 0% blowup, medDays 56, WR 40.7%.

FAITHFUL PORT — gated EXACTLY to that slice (a bad, un-gated port is worthless —
cf. the London-ORB range_bars mismatch). The live strategy only fires when ALL of
the slice's conditions hold:

  * ENTRY (Larry Connors RSI-2):
      - RSI(2) < 10 while close > SMA(200)  -> LONG  (dip in an uptrend)
      - RSI(2) > 90 while close < SMA(200)  -> SHORT (spike in a downtrend)
  * SESSION gate: the FX session tag must be exactly ``sydney`` (Sydney-only,
    before Tokyo opens) — the slice was session=sydney, NOT the sydney+tokyo
    overlap, so we match the exact tag.
  * REGIME gate: RANGE only, via the SAME ``classify_regime`` the backtest used
    (ADX(14) + EMA(20/50), threshold 22). 'unknown' (insufficient history) => no
    trade, exactly as the slice excluded those bars.
  * STOP: 1.5 x ATR(14) from the entry close (defines 1R).
  * EXIT (atr_trail2): a trailing stop 2 x ATR behind the best price, via
    ``ExitPolicy(trail_distance = 2 x ATR)`` — the shared ``apply_dynamic_stop``
    ratchets it in our favour only, identical for paper and live.
      NOTE ON THE BACKSTOP TP: the live framework mandates at least one TP with
      the furthest >= MIN_RR (2R); atr_trail2 has NO fixed TP. So we attach a FAR
      20R backstop TP purely to satisfy that invariant. The 2xATR trailing stop
      (or the 17:00-NY intraday flatten) always exits first — a 20R (=30xATR)
      monotonic move in a ~2h Sydney window is effectively impossible — so the
      backstop is never the real exit. This reproduces the no-fixed-TP trailing
      behaviour of the deployable slice.
  * COOLDOWN: 3 bars after an entry (matches the research variant).

KNOWN PARITY CAVEATS (same class as the ORB port — forward-test, not proof):
  * The research challenge sim allowed OVERLAPPING concurrent trades (its DD was
    concurrency-aware). The live executor manages one position per instrument at
    a time, so the live trade COUNT will be lower than the backtest; per-trade
    behaviour is faithful.
  * Live trailing ratchets on live TICKS; the backtest ratcheted on completed 5m
    bars — live is marginally more responsive (a known research-vs-live gap).
  * Sized at 0.50% risk (the slice that passed; 1% doubles drawdown through the
    10% cap — see the research notes).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.cfd_research.regime import classify_regime
from app.cfd_strategy.base import (
    CFDSignal,
    CFDStrategy,
    Direction,
    EntryMode,
    ExitPolicy,
    StrategyContext,
    build_rr_exit_plan,
)
from app.cfd_strategy.registry import register_strategy
from app.core.models import Timeframe
from app.strategy.indicators import atr, rsi, sma
from app.utils import forex_hours
from app.utils.logger import get_logger

logger = get_logger(__name__)


@register_strategy
class Rsi2UsdjpySydney5m(CFDStrategy):
    """Larry Connors RSI-2 on USDJPY, Sydney session, 5m, range regime, ATR-trail 2x.

    The live realisation of the deployable research slice (see module docstring).
    """

    strategy_id = "rsi2_usdjpy_sydney_5m"
    name = "RSI-2 USDJPY Sydney 5m (range, ATR-trail 2x)"
    timeframe = Timeframe.M5
    instruments = ("USDJPY",)
    variants = ("default",)

    # Parameters — locked to the deployable slice / research defaults.
    _RSI_PERIOD = 2
    _OVERSOLD = 10.0
    _OVERBOUGHT = 90.0
    _TREND_MA = 200
    _ATR_PERIOD = 14
    _SL_ATR_MULT = 1.5        # 1R
    _TRAIL_ATR_MULT = 2.0     # atr_trail2
    _SESSION = "sydney"
    _COOLDOWN_BARS = 3
    _BACKSTOP_RR = 20.0       # far TP to satisfy the >=2R invariant; never the real exit

    def __init__(self) -> None:
        # SMA(200) dominates; +buffer covers regime (EMA50/ADX ~51) + ATR + RSI +
        # warmup slack so the first Sydney session can classify.
        self.min_history = self._TREND_MA + 30
        # Per-instrument cooldown + trading-day tracking (mirrors the research).
        self._cooldown: dict[str, int] = {}
        self._last_day: dict[str, str] = {}

    def evaluate(self, ctx: StrategyContext) -> list[CFDSignal]:
        candle = ctx.candle
        history = ctx.history
        instrument = ctx.instrument

        if instrument != "USDJPY":
            return []
        if len(history) < self.min_history:
            return []

        dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, timezone.utc)

        # --- Trading-day reset (clear cooldown on a new FX day) ---
        today = forex_hours.trading_day(dt)
        if self._last_day.get(instrument) != today:
            self._last_day[instrument] = today
            self._cooldown[instrument] = 0

        # --- Cooldown ---
        cd = self._cooldown.get(instrument, 0)
        if cd > 0:
            self._cooldown[instrument] = cd - 1
            return []

        # --- SESSION gate: exactly the Sydney-only session tag ---
        if forex_hours.session_tag(dt) != self._SESSION:
            return []

        # --- REGIME gate: range only (same classifier as the backtest) ---
        if classify_regime(history) != "range":
            return []

        # --- Indicators ---
        closes = [c.close for c in history]
        trend = sma(closes, self._TREND_MA)
        r = rsi(closes, self._RSI_PERIOD)
        atr_val = atr(history, self._ATR_PERIOD)
        if trend is None or r is None or atr_val is None or atr_val <= 0:
            return []

        close = ctx.close
        stop_dist = self._SL_ATR_MULT * atr_val
        trail_dist = self._TRAIL_ATR_MULT * atr_val

        # --- Entry trigger (trend-aligned RSI-2 extreme) ---
        direction: Direction | None = None
        stop_loss: float = 0.0
        if close > trend and r < self._OVERSOLD:
            direction = Direction.LONG
            stop_loss = close - stop_dist
        elif close < trend and r > self._OVERBOUGHT:
            direction = Direction.SHORT
            stop_loss = close + stop_dist

        if direction is None:
            return []

        # --- Build the signal: 1.5xATR stop (1R) + 2xATR trailing exit + far
        # backstop TP (to satisfy the mandatory >=2R invariant). ---
        try:
            plan = build_rr_exit_plan(
                direction=direction,
                entry_price=close,
                stop_loss=stop_loss,
                rr_targets=[self._BACKSTOP_RR],
                exit_model="atr_trail2",
                exit_policy=ExitPolicy(trail_distance=trail_dist),
            )
            signal = CFDSignal(
                strategy_id=self.strategy_id,
                variant_id="default",
                instrument=instrument,
                direction=direction,
                entry_mode=EntryMode.CANDLE_CLOSE,
                entry_price=close,
                exit_plan=plan,
                timestamp_ms=candle.timestamp_ms,
                reason=(
                    f"RSI2 {direction.value} {instrument} sydney/range | "
                    f"RSI({self._RSI_PERIOD})={r:.1f} vs SMA{self._TREND_MA} | "
                    f"SL 1.5xATR, trail 2xATR"
                ),
            )
        except ValueError as e:
            logger.debug("%s: signal rejected (invalid plan): %s", self.strategy_id, e)
            return []

        self._cooldown[instrument] = self._COOLDOWN_BARS
        logger.info(
            "%s ENTRY: %s %s @ %.5f | SL=%.5f | trail=%.5f (2xATR) | R=%.5f | "
            "regime=range session=sydney RSI2=%.1f",
            self.strategy_id, direction.value, instrument, close, stop_loss,
            trail_dist, stop_dist, r,
        )
        return [signal]

    def on_day_reset(self) -> None:
        """Reset per-instrument state at the FX trading-day boundary."""
        self._cooldown.clear()
        self._last_day.clear()
