"""
CFD strategy framework — core contracts.

Design principles:
  * Every entry has a MANDATORY stop-loss and take-profit. No naked entries.
  * The exit plan must achieve at least a minimum R:R (default 1:2). This is
    enforced in ``ExitPlan`` construction — a strategy CANNOT emit a signal that
    risks more than it targets. This is a money-safety invariant.
  * A strategy may support two entry modes:
      - CANDLE_CLOSE: enter at the close price of the signalling candle.
      - INTRABAR: arm a trigger price; the entry fires when price reaches it
        during a later bar (matches the NSE armed/trigger model, and the
        synthetic-tick backtest replay).
  * A single strategy can define 1..N variants. A "variant" is just a named
    parameter set the strategy evaluates. Most strategies will have 1 variant.

Nothing here talks to a broker, DB, or the feed. Strategies are pure decision
functions: given market context, return zero or more signals. Execution is
handled separately (paper executor / cTrader executor).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.models import Candle, Timeframe

# The money-safety floor. A strategy's furthest take-profit must be at least
# this many times the stop distance. 2.0 = 1:2 risk:reward minimum.
MIN_RR: float = 2.0

# Floating-point tolerance for RR comparisons (avoids rejecting a valid 2.0
# plan that computes to 1.9999999 due to float rounding).
_RR_EPSILON: float = 1e-6


class Direction(Enum):
    """Trade direction."""

    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        """+1 for LONG, -1 for SHORT (for price-distance math)."""
        return 1 if self is Direction.LONG else -1


class EntryMode(Enum):
    """How a signal enters a trade."""

    CANDLE_CLOSE = "CANDLE_CLOSE"  # Enter at the signalling candle's close.
    INTRABAR = "INTRABAR"          # Arm a trigger price; fill when price reaches it.


@dataclass(frozen=True)
class TakeProfit:
    """
    A single take-profit target.

    ``close_fraction`` is the portion of the position to close at this level
    (1.0 = close the whole position; 0.5 = close half). When a strategy uses
    multiple partial TPs, their fractions should sum to 1.0.
    """

    price: float
    close_fraction: float = 1.0

    def __post_init__(self) -> None:
        if not (0.0 < self.close_fraction <= 1.0):
            raise ValueError(
                f"TakeProfit.close_fraction must be in (0, 1], got {self.close_fraction}"
            )
        if self.price <= 0:
            raise ValueError(f"TakeProfit.price must be positive, got {self.price}")


@dataclass(frozen=True)
class ExitPlan:
    """
    The mandatory exit plan attached to every entry.

    Invariants (enforced in __post_init__):
      * stop_loss is on the correct (losing) side of entry.
      * at least one take-profit exists, on the correct (winning) side.
      * the FURTHEST take-profit achieves at least MIN_RR (1:2 by default).
      * partial close fractions do not exceed 1.0 in total.

    All prices are absolute (not distances). ``entry_price`` is the reference
    used to validate sides and compute R:R.
    """

    direction: Direction
    entry_price: float
    stop_loss: float
    take_profits: tuple[TakeProfit, ...]
    min_rr: float = MIN_RR
    # Optional: a strategy can name/describe the exit model it used (e.g.
    # "atr_2x_sl_rr3", "structure_sl_rr2"). Free-form, for research grouping.
    exit_model: str = ""

    def __post_init__(self) -> None:
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be positive, got {self.entry_price}")
        if self.stop_loss <= 0:
            raise ValueError(f"stop_loss must be positive, got {self.stop_loss}")
        if not self.take_profits:
            raise ValueError("ExitPlan requires at least one take-profit (TP is mandatory)")

        sign = self.direction.sign

        # Stop must be on the losing side: for LONG, SL < entry; for SHORT, SL > entry.
        sl_distance = (self.entry_price - self.stop_loss) * sign
        if sl_distance <= 0:
            raise ValueError(
                f"stop_loss {self.stop_loss} is on the wrong side of entry "
                f"{self.entry_price} for a {self.direction.value} trade "
                f"(risk distance must be positive)"
            )

        # Every TP must be on the winning side.
        for tp in self.take_profits:
            tp_distance = (tp.price - self.entry_price) * sign
            if tp_distance <= 0:
                raise ValueError(
                    f"take-profit {tp.price} is on the wrong side of entry "
                    f"{self.entry_price} for a {self.direction.value} trade"
                )

        # Partial fractions sanity: total should not exceed 1.0 (allow small float slack).
        total_fraction = sum(tp.close_fraction for tp in self.take_profits)
        if total_fraction > 1.0 + 1e-9:
            raise ValueError(
                f"take-profit close fractions sum to {total_fraction:.4f} (> 1.0); "
                f"a position cannot close more than 100%"
            )

        # R:R floor: the FURTHEST take-profit must achieve at least min_rr.
        furthest_rr = self.max_rr
        if furthest_rr + _RR_EPSILON < self.min_rr:
            raise ValueError(
                f"exit plan R:R {furthest_rr:.3f} is below the minimum "
                f"{self.min_rr:.3f} (furthest TP must be >= {self.min_rr}R). "
                f"entry={self.entry_price} SL={self.stop_loss} "
                f"TPs={[tp.price for tp in self.take_profits]}"
            )

    # ─── Derived quantities ──────────────────────────────────────

    @property
    def risk_distance(self) -> float:
        """Absolute price distance from entry to stop (always positive)."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def max_rr(self) -> float:
        """R:R of the furthest take-profit (the best-case outcome)."""
        sign = self.direction.sign
        risk = self.risk_distance
        if risk <= 0:
            return 0.0
        furthest = max(self.take_profits, key=lambda tp: (tp.price - self.entry_price) * sign)
        reward = abs(furthest.price - self.entry_price)
        return reward / risk

    @property
    def blended_rr(self) -> float:
        """
        Fraction-weighted R:R across all take-profits.

        If a position closes partially at each TP, this is the expected R:R
        assuming every TP is reached (best case). Any unclosed remainder is
        assumed to close at the furthest TP.
        """
        risk = self.risk_distance
        if risk <= 0:
            return 0.0

        total_r = 0.0
        allocated = 0.0
        for tp in self.take_profits:
            reward = abs(tp.price - self.entry_price)
            total_r += (reward / risk) * tp.close_fraction
            allocated += tp.close_fraction

        # Remainder rides to the furthest TP.
        remainder = max(0.0, 1.0 - allocated)
        if remainder > 0:
            total_r += self.max_rr * remainder

        return total_r

    @property
    def take_profit_prices(self) -> list[float]:
        return [tp.price for tp in self.take_profits]


@dataclass
class CFDSignal:
    """
    A trading decision emitted by a strategy.

    Carries everything execution needs: what/where to enter, how to exit, and
    how the entry should be reached (at candle close, or armed for an intrabar
    trigger).
    """

    strategy_id: str
    variant_id: str
    instrument: str            # e.g. "XAUUSD"
    direction: Direction
    entry_mode: EntryMode
    entry_price: float         # candle close (CANDLE_CLOSE) or trigger price (INTRABAR)
    exit_plan: ExitPlan
    timestamp_ms: float        # signal time (candle close time)
    # How many candles an INTRABAR arm stays valid before it expires unfilled.
    # Ignored for CANDLE_CLOSE entries.
    expiry_candles: int = 1
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Guard: the exit plan's entry price must match the signal's entry price,
        # otherwise the R:R validation was done against the wrong reference.
        if abs(self.exit_plan.entry_price - self.entry_price) > 1e-6:
            raise ValueError(
                f"CFDSignal.entry_price ({self.entry_price}) does not match "
                f"ExitPlan.entry_price ({self.exit_plan.entry_price}). "
                f"Build the exit plan against the same entry price."
            )
        if self.exit_plan.direction is not self.direction:
            raise ValueError(
                f"CFDSignal.direction ({self.direction}) does not match "
                f"ExitPlan.direction ({self.exit_plan.direction})."
            )
        if self.expiry_candles < 1:
            raise ValueError(f"expiry_candles must be >= 1, got {self.expiry_candles}")

    @property
    def stop_loss(self) -> float:
        return self.exit_plan.stop_loss

    @property
    def take_profits(self) -> tuple[TakeProfit, ...]:
        return self.exit_plan.take_profits

    def __repr__(self) -> str:
        return (
            f"CFDSignal({self.direction.value} {self.instrument} "
            f"@{self.entry_price:.5g} SL={self.stop_loss:.5g} "
            f"TP={self.exit_plan.take_profit_prices} "
            f"RR={self.exit_plan.max_rr:.2f} mode={self.entry_mode.value} "
            f"[{self.strategy_id}/{self.variant_id}])"
        )


@dataclass
class StrategyContext:
    """
    Everything a strategy needs to make a decision on a candle close.

    ``history`` is the completed-candle history for this instrument/timeframe
    (most recent last), INCLUDING the just-closed candle. ``candle`` is that
    just-closed candle (== history[-1]).

    Higher-timeframe or cross-instrument data can be added later via ``extra``.
    """

    instrument: str
    timeframe: Timeframe
    candle: Candle
    history: list[Candle]
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def close(self) -> float:
        return self.candle.close

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.history]


class CFDStrategy(ABC):
    """
    Base class for a hand-written CFD strategy.

    Subclass and implement ``evaluate``. A strategy is a pure decision function:
    given a StrategyContext (candle + history), return zero or more CFDSignals.
    It must NOT place orders, write to a DB, or touch the feed — that is the
    executor's job.

    Attributes to set on the subclass:
        strategy_id     — unique short id (e.g. "gold_london_orb")
        name            — human-readable name
        timeframe       — the candle timeframe it trades (default M5)
        instruments     — which of the 10 CFD symbols it applies to
        min_history     — candles required before it will produce signals
        variants        — optional list of variant ids this strategy runs
    """

    strategy_id: str = "unnamed"
    name: str = "Unnamed CFD Strategy"
    timeframe: Timeframe = Timeframe.M5
    instruments: tuple[str, ...] = ()
    min_history: int = 50
    variants: tuple[str, ...] = ("default",)

    def applies_to(self, instrument: str) -> bool:
        """Whether this strategy trades the given instrument."""
        return not self.instruments or instrument in self.instruments

    def has_enough_history(self, ctx: StrategyContext) -> bool:
        return len(ctx.history) >= self.min_history

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> list[CFDSignal]:
        """
        Decide whether to emit signals for the just-closed candle.

        Return an empty list for "no trade". Return one or more CFDSignals to
        enter (each may be for a different variant). Implementations should call
        ``build_rr_exit_plan`` (or construct an ExitPlan directly) so the 1:2
        R:R floor is enforced.
        """
        ...

    # Optional lifecycle hooks (no-ops by default).
    def on_start(self) -> None:  # noqa: D401
        """Called once when the strategy is registered/started."""

    def on_day_reset(self) -> None:
        """Called at each trading-day boundary (for stateful strategies)."""


# ─── Helpers ─────────────────────────────────────────────────────────────────


def build_rr_exit_plan(
    direction: Direction,
    entry_price: float,
    stop_loss: float,
    rr_targets: list[float] | None = None,
    close_fractions: list[float] | None = None,
    exit_model: str = "",
    min_rr: float = MIN_RR,
) -> ExitPlan:
    """
    Build an ExitPlan from a stop-loss and one or more R:R targets.

    This is the recommended way for a strategy to define exits, because it
    computes TP prices from the stop distance and guarantees the R:R floor.

    Args:
        direction:       LONG or SHORT.
        entry_price:     Entry (or trigger) price.
        stop_loss:       Absolute stop-loss price (on the losing side of entry).
        rr_targets:      R multiples for each TP, e.g. [2.0] or [2.0, 3.0, 5.0].
                         Defaults to [min_rr] (a single 1:2 target).
        close_fractions: Portion to close at each TP (parallel to rr_targets).
                         Defaults to closing the full position at the last TP
                         and splitting evenly if multiple targets are given.
        exit_model:      Free-form label for research grouping.
        min_rr:          The enforced R:R floor (default 2.0 = 1:2).

    Returns:
        A validated ExitPlan.

    Example (gold long, $10 stop, targets at 2R and 3R, half at each):
        build_rr_exit_plan(Direction.LONG, 2400.0, 2390.0,
                           rr_targets=[2.0, 3.0], close_fractions=[0.5, 0.5])
    """
    if rr_targets is None:
        rr_targets = [min_rr]
    if not rr_targets:
        raise ValueError("rr_targets must contain at least one target")

    sign = direction.sign
    risk = abs(entry_price - stop_loss)
    if risk <= 0:
        raise ValueError(
            f"stop_loss {stop_loss} equals entry {entry_price}; risk distance is zero"
        )

    # Default fractions: if one target, close 100%. If many, split evenly.
    if close_fractions is None:
        n = len(rr_targets)
        frac = 1.0 / n
        close_fractions = [frac] * n
        # Fix rounding so they sum to exactly 1.0 on the last leg.
        close_fractions[-1] = 1.0 - frac * (n - 1)

    if len(close_fractions) != len(rr_targets):
        raise ValueError(
            f"close_fractions ({len(close_fractions)}) must match "
            f"rr_targets ({len(rr_targets)}) in length"
        )

    take_profits = tuple(
        TakeProfit(price=entry_price + sign * (rr * risk), close_fraction=frac)
        for rr, frac in zip(rr_targets, close_fractions)
    )

    return ExitPlan(
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profits=take_profits,
        min_rr=min_rr,
        exit_model=exit_model,
    )
