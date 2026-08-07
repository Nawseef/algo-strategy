"""
Execution interface — shared contracts for paper and live executors.

``ManagedPosition`` is the runtime state of an open trade: entry, mandatory
SL/TP legs, partial-fill tracking, and MFE/MAE excursion. Both the paper
executor and the future cTrader live executor operate on the same structure,
so downstream reporting/persistence is identical regardless of mode.

The SL/TP evaluation logic (``evaluate_exit``) lives here because it is
IDENTICAL for paper and backtest and live — a position exits when price
crosses its stop or reaches a take-profit. Keeping it in one place means the
paper trade, the backtest, and the live trade all resolve exits the same way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from app.cfd_strategy.base import CFDSignal, Direction, ExitPlan, TakeProfit
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ExecutionMode(Enum):
    """How signals are executed."""

    PAPER = "PAPER"    # Track only (Postgres + Telegram), no broker orders.
    LIVE = "LIVE"      # Place real orders via cTrader Open API.
    BOTH = "BOTH"      # Paper-track AND live-execute (comparison mode).


class PositionStatus(Enum):
    """Lifecycle of a managed position."""

    PENDING = "PENDING"    # Armed (intrabar), not yet filled.
    OPEN = "OPEN"          # Filled, partially or fully live.
    CLOSED = "CLOSED"      # Fully closed.


class ExitReason(Enum):
    """Why a position (or a portion) was closed."""

    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"    # Trailing stop hit (may be a profit).
    TIME_STOP = "TIME_STOP"            # Closed at market after a max holding time.
    MANUAL = "MANUAL"
    EOD_FLATTEN = "EOD_FLATTEN"        # Forced close (weekend / daily reset guard).
    RISK_HALT = "RISK_HALT"            # Closed because a risk limit was breached.
    EXPIRED = "EXPIRED"                # Intrabar arm expired unfilled (pending only).


@dataclass
class PartialClose:
    """A record of one partial (or full) close event on a position."""

    price: float
    fraction: float          # Portion of the ORIGINAL position closed here.
    reason: ExitReason
    timestamp_ms: float
    rr: float                # Realized R multiple for this leg.
    pnl_price: float         # Price PnL for this leg (per unit, direction-adjusted).


@dataclass
class ManagedPosition:
    """
    Runtime state of a position under management.

    Sizing is expressed in ``lots`` (the broker unit). ``remaining_fraction``
    tracks how much of the original position is still open after partial TPs.
    Price-space PnL is direction-adjusted; USD PnL is computed by the executor
    using the instrument's point value (kept out of here to avoid coupling this
    to the instrument spec).
    """

    position_id: str
    strategy_id: str
    variant_id: str
    instrument: str
    direction: Direction
    entry_price: float
    entry_time_ms: float
    lots: float
    exit_plan: ExitPlan
    account_id: str = "default"
    status: PositionStatus = PositionStatus.OPEN

    # Partial-fill tracking.
    remaining_fraction: float = 1.0
    partial_closes: list[PartialClose] = field(default_factory=list)
    # Which TP indices have already been taken (so we don't re-fire them).
    _tp_taken: set[int] = field(default_factory=set)

    # Excursion tracking (best/worst price seen while open).
    max_favorable_price: float = 0.0
    max_adverse_price: float = 0.0

    # Final close bookkeeping (set when fully closed).
    exit_price: float = 0.0
    exit_time_ms: float = 0.0
    final_reason: ExitReason | None = None

    def __post_init__(self) -> None:
        self.max_favorable_price = self.entry_price
        self.max_adverse_price = self.entry_price

    # ─── Derived ─────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN

    @property
    def risk_distance(self) -> float:
        return self.exit_plan.risk_distance

    def price_pnl_per_unit(self, price: float) -> float:
        """Direction-adjusted price PnL per unit at a given price."""
        return (price - self.entry_price) * self.direction.sign

    def rr_at(self, price: float) -> float:
        """Realized R multiple if closed at ``price``."""
        risk = self.risk_distance
        if risk <= 0:
            return 0.0
        return self.price_pnl_per_unit(price) / risk

    # ─── Excursion update ────────────────────────────────────────

    def update_excursion(self, price: float) -> None:
        """Track best/worst price reached while the position is open."""
        if self.direction is Direction.LONG:
            self.max_favorable_price = max(self.max_favorable_price, price)
            self.max_adverse_price = min(self.max_adverse_price, price)
        else:
            self.max_favorable_price = min(self.max_favorable_price, price)
            self.max_adverse_price = max(self.max_adverse_price, price)

    @property
    def mfe_price(self) -> float:
        """Max favorable excursion in price units (>= 0)."""
        return abs(self.max_favorable_price - self.entry_price)

    @property
    def mae_price(self) -> float:
        """Max adverse excursion in price units (>= 0)."""
        return abs(self.max_adverse_price - self.entry_price)


@dataclass
class ExitDecision:
    """The outcome of evaluating a position against a price."""

    hit: bool
    reason: ExitReason | None = None
    price: float = 0.0
    fraction: float = 0.0     # Fraction of ORIGINAL position to close now.
    tp_index: int = -1        # Which TP fired (-1 for SL / none).
    fully_closed: bool = False


def evaluate_exit(pos: ManagedPosition, high: float, low: float) -> list[ExitDecision]:
    """
    Given a price bar (high/low range the position was exposed to), determine
    which exit(s) triggered, in the conservative order.

    Conservative ordering rule (money-safe): if BOTH the stop and a take-profit
    are inside the same bar's range, we assume the STOP hit first. This never
    flatters the result — a real trade could have gone either way, and counting
    the loss avoids optimistic bias. This matches how prudent backtests resolve
    ambiguous bars.

    For a tick/quote update, pass the same value as both high and low.

    Returns a list of ExitDecisions (may be empty). The caller applies them in
    order and stops if one fully closes the position.
    """
    if not pos.is_open:
        return []

    decisions: list[ExitDecision] = []
    sl = pos.exit_plan.stop_loss

    # Did the stop get touched within this range?
    if pos.direction is Direction.LONG:
        stop_hit = low <= sl
    else:
        stop_hit = high >= sl

    if stop_hit:
        # Stop takes precedence (conservative). Close everything remaining.
        decisions.append(ExitDecision(
            hit=True,
            reason=ExitReason.STOP_LOSS,
            price=sl,
            fraction=pos.remaining_fraction,
            tp_index=-1,
            fully_closed=True,
        ))
        return decisions

    # No stop — check take-profits in order of increasing distance (nearest first).
    sign = pos.direction.sign
    ordered = sorted(
        enumerate(pos.exit_plan.take_profits),
        key=lambda it: (it[1].price - pos.entry_price) * sign,
    )

    running_remaining = pos.remaining_fraction
    for idx, tp in ordered:
        if idx in pos._tp_taken:
            continue
        if pos.direction is Direction.LONG:
            tp_hit = high >= tp.price
        else:
            tp_hit = low <= tp.price
        if not tp_hit:
            continue

        # How much to close at this TP (bounded by what remains).
        frac = min(tp.close_fraction, running_remaining)
        if frac <= 0:
            continue
        running_remaining -= frac
        fully = running_remaining <= 1e-9
        decisions.append(ExitDecision(
            hit=True,
            reason=ExitReason.TAKE_PROFIT,
            price=tp.price,
            fraction=frac,
            tp_index=idx,
            fully_closed=fully,
        ))
        if fully:
            break

    return decisions


class BaseExecutor(ABC):
    """
    Abstract executor. Concrete implementations: PaperExecutor, CTraderExecutor.

    The runner feeds an executor two things:
      * signals — via ``on_signal`` (from strategy evaluation on candle close)
      * ticks   — via ``on_tick`` (to fill armed entries and manage SL/TP exits)
    """

    mode: ExecutionMode = ExecutionMode.PAPER

    @abstractmethod
    def on_signal(self, signal: CFDSignal) -> None:
        """Handle a new strategy signal (open or arm a position)."""
        ...

    @abstractmethod
    def on_tick(self, instrument: str, bid: float, ask: float, timestamp_ms: float) -> None:
        """Handle a price update (fill arms, manage exits)."""
        ...

    @abstractmethod
    def open_positions(self, instrument: str | None = None) -> list[ManagedPosition]:
        """Return currently open positions (optionally for one instrument)."""
        ...

    @abstractmethod
    def flatten_all(self, reason: ExitReason = ExitReason.EOD_FLATTEN) -> None:
        """Force-close all open positions (weekend / risk / shutdown)."""
        ...
