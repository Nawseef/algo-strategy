"""
Lean, purpose-built exit-model library for CFD research (entry/exit separation).

The mistake to avoid (the NSE 150k graveyard) was NOT "having exit models" — it
was applying dozens of exits blindly to entries with no edge. Here each exit
model earns its place by solving a specific prop-account problem:

    fixed_rr2         — SL + full TP at 2R. The baseline (your 1:2 floor).
    breakeven_1R      — TP 2R, move stop to entry once +1R. Kills half the
                        losers -> directly protects the daily drawdown.
    scale_2R_runner   — bank half at 2R, breakeven the rest and trail it. Locks
                        the 1:2 while letting winners run.
    atr_trailing      — trail by k*ATR; capture trends beyond 2R (no fixed TP).
    time_stop_2R      — TP 2R, but bail at market after N bars. Caps exposure,
                        avoids holding into reversals (intraday discipline).

An entry is expressed as an ``EntryIntent`` (direction, entry price, stop) — the
STOP defines 1R. The backtest runs the SAME entry through each model, so we can
answer "which exit fits this entry / session / regime."

Money-safety (identical philosophy to exit_simulator):
    * Stop is checked against the full bar range BEFORE take-profits (if both are
      in one bar, the stop wins — never flatters the result).
    * Trailing stops ratchet using only PRIOR bars' extremes, so a single bar
      cannot both raise the trail and stop out on the same bar.
    * No favorable slippage; costs applied at close via the cost model.

NOTE: this is a RESEARCH exit engine (isolated from the live/paper exit logic in
``evaluate_exit``). When a winning exit is chosen, port it to the live executor
and validate parity there. Keeping it separate lets us iterate fast without
touching money-critical live code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_execution.base import ExitReason, PartialClose
from app.cfd_risk.costs import COST_MODEL_INTRADAY, CFDCostModel, calculate_trade_cost
from app.cfd_risk.instruments import get_instrument
from app.cfd_risk.position_sizing import calculate_lot_size
from app.cfd_strategy.base import Direction
from app.core.models import Candle


@dataclass
class EntryIntent:
    """A filled entry with a mandatory stop (the stop defines 1R). No exit baked in."""

    instrument: str
    direction: Direction
    entry_price: float
    stop_loss: float
    entry_time_ms: float
    reason: str = ""
    # Optional "mean" / target price the entry wants to revert to (VWAP, BB
    # midline, or the opposing liquidity pool). Used ONLY by the target_mean exit
    # model; None for trend entries (they ride the trend, no fixed mean target).
    target_price: float | None = None

    @property
    def r_distance(self) -> float:
        return abs(self.entry_price - self.stop_loss)


@dataclass
class _Outcome:
    """Raw result of an exit model walking the future bars."""

    legs: list[tuple[float, float, ExitReason]]  # (fraction, price, reason), sums to ~1.0
    exit_time_ms: float
    bars_held: int
    max_fav_price: float
    max_adv_price: float
    closed: bool


# ─── Bar helpers ─────────────────────────────────────────────────────────────


def _stop_hit(direction: Direction, stop: float, low: float, high: float) -> bool:
    return low <= stop if direction is Direction.LONG else high >= stop


def _tp_hit(direction: Direction, tp: float, low: float, high: float) -> bool:
    return high >= tp if direction is Direction.LONG else low <= tp


def _excursion(direction: Direction, best: float, worst: float, high: float, low: float):
    """Update best (favorable) / worst (adverse) prices seen."""
    if direction is Direction.LONG:
        return max(best, high), min(worst, low)
    return min(best, low), max(worst, high)


def _ratchet(direction: Direction, cur_stop: float, best: float, dist: float, floor: float | None):
    """Move a trailing stop toward price; never loosen; optional floor (breakeven)."""
    if direction is Direction.LONG:
        new = best - dist
        if floor is not None:
            new = max(new, floor)
        return max(cur_stop, new)
    new = best + dist
    if floor is not None:
        new = min(new, floor)
    return min(cur_stop, new)


# ─── Exit models ─────────────────────────────────────────────────────────────


class ExitModel:
    """Base: name + target RR (nominal, for reporting) + simulate()."""

    name: str = "base"
    target_rr: float = 2.0

    def simulate(self, entry: float, direction: Direction, stop: float,
                 future: list[Candle], atr_at_entry: float | None,
                 target_price: float | None = None) -> _Outcome:
        raise NotImplementedError

    # Shared: value any unresolved remainder at the last bar's close.
    def _flatten(self, future, entry, best, worst, bars, remaining=1.0):
        last = future[-1]
        return _Outcome([(remaining, last.close, ExitReason.EOD_FLATTEN)],
                        last.timestamp_ms, bars, best, worst, False)


class FixedRR(ExitModel):
    def __init__(self, rr: float = 2.0):
        self.rr = rr
        self.target_rr = rr
        self.name = f"fixed_rr{rr:g}"

    def simulate(self, entry, direction, stop, future, atr_at_entry, target_price=None):
        R = abs(entry - stop)
        sign = direction.sign
        tp = entry + self.rr * R * sign
        best = worst = entry
        bars = 0
        for c in future:
            bars += 1
            best, worst = _excursion(direction, best, worst, c.high, c.low)
            if _stop_hit(direction, stop, c.low, c.high):
                return _Outcome([(1.0, stop, ExitReason.STOP_LOSS)], c.timestamp_ms, bars, best, worst, True)
            if _tp_hit(direction, tp, c.low, c.high):
                return _Outcome([(1.0, tp, ExitReason.TAKE_PROFIT)], c.timestamp_ms, bars, best, worst, True)
        return self._flatten(future, entry, best, worst, bars)


class BreakevenAfter1R(ExitModel):
    def __init__(self, rr: float = 2.0, be_at: float = 1.0):
        self.rr = rr
        self.be_at = be_at
        self.target_rr = rr
        self.name = f"breakeven{be_at:g}R_rr{rr:g}"

    def simulate(self, entry, direction, stop, future, atr_at_entry, target_price=None):
        R = abs(entry - stop)
        sign = direction.sign
        tp = entry + self.rr * R * sign
        be_trigger = entry + self.be_at * R * sign
        cur_stop = stop
        be_active = False
        best = worst = entry
        bars = 0
        for c in future:
            bars += 1
            best, worst = _excursion(direction, best, worst, c.high, c.low)
            if _stop_hit(direction, cur_stop, c.low, c.high):
                return _Outcome([(1.0, cur_stop, ExitReason.STOP_LOSS)], c.timestamp_ms, bars, best, worst, True)
            if _tp_hit(direction, tp, c.low, c.high):
                return _Outcome([(1.0, tp, ExitReason.TAKE_PROFIT)], c.timestamp_ms, bars, best, worst, True)
            if not be_active and _tp_hit(direction, be_trigger, c.low, c.high):
                be_active = True
                cur_stop = entry     # breakeven, effective next bar
        return self._flatten(future, entry, best, worst, bars)


class TimeStop(ExitModel):
    def __init__(self, rr: float = 2.0, max_bars: int = 24):
        self.rr = rr
        self.max_bars = max_bars
        self.target_rr = rr
        self.name = f"time_stop_rr{rr:g}_{max_bars}b"

    def simulate(self, entry, direction, stop, future, atr_at_entry, target_price=None):
        R = abs(entry - stop)
        sign = direction.sign
        tp = entry + self.rr * R * sign
        best = worst = entry
        bars = 0
        for c in future:
            bars += 1
            best, worst = _excursion(direction, best, worst, c.high, c.low)
            if _stop_hit(direction, stop, c.low, c.high):
                return _Outcome([(1.0, stop, ExitReason.STOP_LOSS)], c.timestamp_ms, bars, best, worst, True)
            if _tp_hit(direction, tp, c.low, c.high):
                return _Outcome([(1.0, tp, ExitReason.TAKE_PROFIT)], c.timestamp_ms, bars, best, worst, True)
            if bars >= self.max_bars:
                return _Outcome([(1.0, c.close, ExitReason.TIME_STOP)], c.timestamp_ms, bars, best, worst, True)
        return self._flatten(future, entry, best, worst, bars)


class AtrTrailing(ExitModel):
    def __init__(self, atr_mult: float = 2.0):
        self.atr_mult = atr_mult
        self.target_rr = 2.0     # nominal; realized RR varies
        self.name = f"atr_trail{atr_mult:g}"

    def simulate(self, entry, direction, stop, future, atr_at_entry, target_price=None):
        # Fall back to 1R as the trail distance if ATR is unavailable.
        dist = self.atr_mult * (atr_at_entry if atr_at_entry and atr_at_entry > 0 else abs(entry - stop))
        sign = direction.sign
        best = worst = entry
        cur_stop = stop
        bars = 0
        for c in future:
            bars += 1
            # Exit uses cur_stop from PRIOR bars (no same-bar raise-then-stop).
            if _stop_hit(direction, cur_stop, c.low, c.high):
                best, worst = _excursion(direction, best, worst, c.high, c.low)
                reason = ExitReason.TRAILING_STOP if abs(cur_stop - stop) > 1e-9 else ExitReason.STOP_LOSS
                return _Outcome([(1.0, cur_stop, reason)], c.timestamp_ms, bars, best, worst, True)
            best, worst = _excursion(direction, best, worst, c.high, c.low)
            cur_stop = _ratchet(direction, cur_stop, best, dist, floor=None)
        return self._flatten(future, entry, best, worst, bars)


class ScaleRunner(ExitModel):
    def __init__(self, first_rr: float = 2.0, first_frac: float = 0.5, atr_mult: float = 2.0):
        self.first_rr = first_rr
        self.first_frac = first_frac
        self.atr_mult = atr_mult
        self.target_rr = first_rr
        self.name = f"scale_rr{first_rr:g}_trail{atr_mult:g}"

    def simulate(self, entry, direction, stop, future, atr_at_entry, target_price=None):
        R = abs(entry - stop)
        sign = direction.sign
        first_tp = entry + self.first_rr * R * sign
        dist = self.atr_mult * (atr_at_entry if atr_at_entry and atr_at_entry > 0 else R)
        legs: list[tuple[float, float, ExitReason]] = []
        first_taken = False
        cur_stop = stop
        remaining = 1.0
        best = worst = entry
        bars = 0
        for c in future:
            bars += 1
            if not first_taken:
                best, worst = _excursion(direction, best, worst, c.high, c.low)
                if _stop_hit(direction, cur_stop, c.low, c.high):
                    legs.append((1.0, cur_stop, ExitReason.STOP_LOSS))
                    return _Outcome(legs, c.timestamp_ms, bars, best, worst, True)
                if _tp_hit(direction, first_tp, c.low, c.high):
                    legs.append((self.first_frac, first_tp, ExitReason.TAKE_PROFIT))
                    remaining -= self.first_frac
                    first_taken = True
                    cur_stop = entry     # breakeven floor for the runner
                    cur_stop = _ratchet(direction, cur_stop, best, dist, floor=entry)
            else:
                # Manage the runner with a breakeven-floored trailing stop.
                if _stop_hit(direction, cur_stop, c.low, c.high):
                    reason = ExitReason.TRAILING_STOP if abs(cur_stop - entry) > 1e-9 else ExitReason.STOP_LOSS
                    legs.append((remaining, cur_stop, reason))
                    return _Outcome(legs, c.timestamp_ms, bars, best, worst, True)
                best, worst = _excursion(direction, best, worst, c.high, c.low)
                cur_stop = _ratchet(direction, cur_stop, best, dist, floor=entry)
        # Unresolved: close the runner (or full) at last close.
        last = future[-1]
        legs.append((remaining, last.close, ExitReason.EOD_FLATTEN))
        return _Outcome(legs, last.timestamp_ms, bars, best, worst, False)


class MeanTargetExit(ExitModel):
    """Take profit when price returns to the 'mean' the entry carries as
    ``target_price`` (VWAP / BB midline for a fade; the opposing liquidity pool
    for a sweep). This is the NATURAL exit for a mean-reversion entry: the thesis
    is "snap back to the middle", so bank the trade WHEN THE MIDDLE IS REACHED —
    wherever that is — instead of waiting for a fixed 2R the reversion may never
    reach.

    Falls back to a fixed-RR target when the entry carries no ``target_price``
    (e.g. trend entries), or if the carried level isn't on the profit side of
    entry, so this model is ALWAYS well-defined and never errors. Money-safety is
    identical to FixedRR: the stop is checked against the full bar BEFORE the TP
    (stop wins on an ambiguous bar).
    """

    def __init__(self, fallback_rr: float = 2.0):
        self.fallback_rr = fallback_rr
        self.target_rr = fallback_rr   # nominal; realized RR varies with the mean
        self.name = "target_mean"

    def simulate(self, entry, direction, stop, future, atr_at_entry, target_price=None):
        R = abs(entry - stop)
        sign = direction.sign
        # Use the carried mean if it's on the profit side; else fall back to RR.
        if target_price is not None and (target_price - entry) * sign > 0:
            tp = target_price
        else:
            tp = entry + self.fallback_rr * R * sign
        best = worst = entry
        bars = 0
        for c in future:
            bars += 1
            best, worst = _excursion(direction, best, worst, c.high, c.low)
            if _stop_hit(direction, stop, c.low, c.high):
                return _Outcome([(1.0, stop, ExitReason.STOP_LOSS)], c.timestamp_ms, bars, best, worst, True)
            if _tp_hit(direction, tp, c.low, c.high):
                return _Outcome([(1.0, tp, ExitReason.TAKE_PROFIT)], c.timestamp_ms, bars, best, worst, True)
        return self._flatten(future, entry, best, worst, bars)


def default_exit_models() -> list[ExitModel]:
    """The lean, purpose-built set swept per entry."""
    return [
        FixedRR(2.0),
        BreakevenAfter1R(2.0),
        ScaleRunner(2.0, 0.5, 2.0),
        AtrTrailing(2.0),
        TimeStop(2.0, 24),
        MeanTargetExit(2.0),
    ]


# ─── Entry -> trade under one exit model ─────────────────────────────────────


def simulate_entry(
    intent: EntryIntent,
    future_candles: list[Candle],
    model: ExitModel,
    *,
    risk_pct: float,
    ref_balance: float,
    cost_model: CFDCostModel | None = None,
    atr_at_entry: float | None = None,
) -> SimulatedTrade | None:
    """
    Resolve one entry under one exit model over the following bars.

    Sizes the position off ``ref_balance`` at ``risk_pct`` (constant, so % returns
    are stable and risk-scalable in the challenge sim). Returns a tagged
    SimulatedTrade, or None if the entry can't be sized/resolved.
    """
    cost_model = cost_model or COST_MODEL_INTRADAY
    R = intent.r_distance
    if R <= 0 or not future_candles:
        return None
    inst = get_instrument(intent.instrument)
    sizing = calculate_lot_size(
        symbol=intent.instrument, account_balance=ref_balance,
        risk_pct=risk_pct, sl_distance_price=R,
    )
    if sizing.rejected:
        return None
    lots = sizing.lot_size

    outcome = model.simulate(
        intent.entry_price, intent.direction, intent.stop_loss, future_candles, atr_at_entry,
        target_price=intent.target_price,
    )
    if not outcome.legs:
        return None

    sign = intent.direction.sign
    entry = intent.entry_price
    pnl_price = sum(frac * (price - entry) * sign for frac, price, _ in outcome.legs)
    realized_rr = sum(frac * ((price - entry) * sign / R) for frac, price, _ in outcome.legs)
    pnl_usd = pnl_price * inst.point_value_per_lot * lots
    cost = calculate_trade_cost(
        symbol=intent.instrument, lot_size=lots, cost_model=cost_model, instrument=inst,
    )
    net_pnl_usd = pnl_usd - cost.total_usd

    # MAE cap (money-safe correctness fix): the max adverse excursion cannot
    # exceed the stop distance R while the position is open — once price reaches
    # the stop you are FLAT (realized at the stop), so a single bar that spikes
    # past the stop must not be recorded as a floating loss you "held". Every
    # exit model starts its stop at R and only moves it favourably (breakeven /
    # trail tighter), so adverse-from-entry is bounded by R for all of them; the
    # cap therefore also handles the scale/breakeven models (their worst dip is
    # pre-move, at full size, <= R). Without this, ~30-42% of trades recorded
    # MAE > risk% and inflated the drawdown/blow-up. NOTE: this assumes the stop
    # fills AT its level (no gap slippage) — consistent with how realized PnL is
    # already booked; true gap realism would add adverse stop slippage to BOTH.
    mae_price = min(abs(outcome.max_adv_price - entry), R)

    partials = [
        PartialClose(
            price=price, fraction=frac, reason=reason,
            timestamp_ms=outcome.exit_time_ms,
            rr=((price - entry) * sign / R), pnl_price=(price - entry) * sign,
        )
        for frac, price, reason in outcome.legs
    ]

    return SimulatedTrade(
        instrument=intent.instrument,
        direction=intent.direction,
        entry_price=entry,
        entry_time_ms=intent.entry_time_ms,
        exit_price=outcome.legs[-1][1],
        exit_time_ms=outcome.exit_time_ms,
        exit_reason=outcome.legs[-1][2],
        lots=lots,
        planned_rr=model.target_rr,
        realized_rr=realized_rr,
        pnl_price=pnl_price,
        pnl_usd=pnl_usd,
        cost_usd=cost.total_usd,
        net_pnl_usd=net_pnl_usd,
        mfe_price=abs(outcome.max_fav_price - entry),
        mae_price=mae_price,
        bars_held=outcome.bars_held,
        partials=partials,
        closed=outcome.closed,
        exit_model=model.name,
    )
