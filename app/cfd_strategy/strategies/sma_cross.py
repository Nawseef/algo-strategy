"""
SMA crossover — DEMONSTRATION strategy (plumbing only, NOT for real trading).

⚠️  READ THIS FIRST  ⚠️
This strategy exists for ONE purpose: to prove the paper-trading runner wires
the feed -> candle builder -> strategy evaluation -> executor together
correctly, end to end. A simple moving-average crossover has no demonstrated
edge on CFDs. Do NOT treat its paper/backtest results as meaningful, and do NOT
run it on a funded account.

When you write real strategies, add them alongside this file and select them via
the ``CFD_PAPER_STRATEGIES`` env var (comma-separated strategy ids). To stop this
demo from trading, set ``CFD_PAPER_STRATEGIES`` to your real strategy ids only.

Logic (intentionally minimal):
  * Compute a fast and a slow simple moving average of closes.
  * When the fast SMA crosses ABOVE the slow SMA -> go LONG at the candle close.
  * When the fast SMA crosses BELOW the slow SMA -> go SHORT at the candle close.
  * Stop-loss is placed ``atr_mult`` ATRs away from entry; the take-profit is set
    by ``build_rr_exit_plan`` at the enforced minimum 1:2 reward:risk.

Crossover detection keeps the previous bar's SMA values PER INSTRUMENT (one
strategy instance is shared across all symbols by the runner).
"""

from __future__ import annotations

from app.cfd_strategy.base import (
    CFDSignal,
    CFDStrategy,
    Direction,
    EntryMode,
    StrategyContext,
    build_rr_exit_plan,
)
from app.cfd_strategy.registry import register_strategy
from app.core.models import Timeframe
from app.strategy.indicators import atr, sma
from app.utils.logger import get_logger

logger = get_logger(__name__)


@register_strategy
class SmaCrossDemo(CFDStrategy):
    """Fast/slow SMA crossover with an ATR-based stop and a 1:2 RR target.

    DEMONSTRATION ONLY — see module docstring.
    """

    strategy_id = "sma_cross_demo"
    name = "SMA Crossover (DEMO — plumbing only, no edge)"
    timeframe = Timeframe.M5
    instruments = ()  # empty = applies to all configured CFD symbols
    variants = ("default",)

    def __init__(
        self,
        fast_period: int = 10,
        slow_period: int = 30,
        atr_period: int = 14,
        atr_mult: float = 1.5,
        rr: float = 2.0,
    ) -> None:
        self._fast = fast_period
        self._slow = slow_period
        self._atr_period = atr_period
        self._atr_mult = atr_mult
        self._rr = rr
        # Need enough bars for the slow SMA and the ATR, plus one prior bar to
        # detect a crossover.
        self.min_history = max(slow_period, atr_period + 1) + 2

        # Previous bar's SMA values, per instrument (for crossover detection).
        self._prev_fast: dict[str, float] = {}
        self._prev_slow: dict[str, float] = {}

    def evaluate(self, ctx: StrategyContext) -> list[CFDSignal]:
        closes = ctx.closes
        if len(closes) < self._slow + 1:
            return []

        fast_val = sma(closes, self._fast)
        slow_val = sma(closes, self._slow)
        if fast_val is None or slow_val is None:
            return []

        atr_val = atr(ctx.history, self._atr_period)
        if atr_val is None or atr_val <= 0:
            return []

        token = ctx.instrument
        prev_fast = self._prev_fast.get(token)
        prev_slow = self._prev_slow.get(token)

        # Record current values for the next bar's comparison.
        self._prev_fast[token] = fast_val
        self._prev_slow[token] = slow_val

        # Need a previous reading to detect a crossover.
        if prev_fast is None or prev_slow is None:
            return []

        close = ctx.close
        if close <= 0:
            return []

        # Bullish crossover: fast crosses above slow -> LONG.
        if prev_fast <= prev_slow and fast_val > slow_val:
            stop = close - self._atr_mult * atr_val
            return self._build_signal(ctx, Direction.LONG, close, stop,
                                      reason="SMA fast crossed above slow")

        # Bearish crossover: fast crosses below slow -> SHORT.
        if prev_fast >= prev_slow and fast_val < slow_val:
            stop = close + self._atr_mult * atr_val
            return self._build_signal(ctx, Direction.SHORT, close, stop,
                                      reason="SMA fast crossed below slow")

        return []

    def _build_signal(
        self,
        ctx: StrategyContext,
        direction: Direction,
        entry: float,
        stop: float,
        reason: str,
    ) -> list[CFDSignal]:
        """Build a single CANDLE_CLOSE signal, guarding the RR/price invariants.

        ``build_rr_exit_plan`` / ``CFDSignal`` raise ValueError if the resulting
        exit plan is invalid (e.g. a computed take-profit price is <= 0, which
        can happen for a large ATR stop on a low-priced instrument). We treat
        that as "no trade" rather than letting it bubble up.
        """
        try:
            plan = build_rr_exit_plan(
                direction=direction,
                entry_price=entry,
                stop_loss=stop,
                rr_targets=[self._rr],
                exit_model=f"atr{self._atr_mult}_rr{self._rr}",
            )
            signal = CFDSignal(
                strategy_id=self.strategy_id,
                variant_id="default",
                instrument=ctx.instrument,
                direction=direction,
                entry_mode=EntryMode.CANDLE_CLOSE,
                entry_price=entry,
                exit_plan=plan,
                timestamp_ms=ctx.candle.timestamp_ms,
                reason=reason,
            )
        except ValueError as e:
            logger.debug("sma_cross_demo skipped %s %s: %s", direction.value, ctx.instrument, e)
            return []
        return [signal]

    def on_day_reset(self) -> None:
        # Crossover state should persist across FX day boundaries (a cross can
        # straddle the 17:00 NY roll), so we intentionally keep _prev_* here.
        pass
