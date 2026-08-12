"""
Backtest exit simulator — resolve a position's mandatory SL/TP over future bars.

Given an entry (price, direction, exit plan) and the sequence of 5m candles that
follow it, determine when and at what price the position closes. Uses the SAME
``evaluate_exit`` logic as the live paper executor, so backtest and live resolve
exits identically.

Intrabar path assumption (standard for OHLC backtesting without tick data):
    * Each candle is expanded into a synthetic tick path: Open -> extreme1 ->
      extreme2 -> Close.
    * For a bullish candle (close >= open): O -> Low -> High -> Close.
    * For a bearish candle (close <  open): O -> High -> Low -> Close.
    This orders the within-bar extremes plausibly.

Money-safe ambiguity rule (inherited from evaluate_exit): if a single candle's
range contains BOTH the stop and a take-profit, the STOP is assumed to hit
first. This never flatters results. For the user's 1:2+ RR strategies a bar
would have to travel 3R within 5 minutes to be ambiguous — rare — and when it
does we count the loss, not the win.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.cfd_execution.base import (
    ExitReason,
    ManagedPosition,
    PartialClose,
    PositionStatus,
    evaluate_exit,
)
from app.cfd_risk.costs import COST_MODEL_INTRADAY, CFDCostModel, calculate_trade_cost
from app.cfd_risk.instruments import get_instrument
from app.cfd_strategy.base import Direction, ExitPlan
from app.core.models import Candle


@dataclass
class SimulatedTrade:
    """Outcome of simulating a single trade's exit through future candles."""

    instrument: str
    direction: Direction
    entry_price: float
    entry_time_ms: float
    exit_price: float
    exit_time_ms: float
    exit_reason: ExitReason
    lots: float
    planned_rr: float
    realized_rr: float
    pnl_price: float          # fraction-weighted price PnL per unit
    pnl_usd: float            # gross USD PnL (before costs)
    cost_usd: float
    net_pnl_usd: float
    mfe_price: float
    mae_price: float
    bars_held: int
    partials: list[PartialClose] = field(default_factory=list)
    closed: bool = True       # False if never exited within the provided window
    # ── Research tags (filled by the replay layer; see CFDBacktestReplay) ──
    strategy_id: str = ""     # which strategy/variant produced it (e.g. orb_london_6b) — clean attribution
    session: str = ""         # FX session at entry (london / new_york / overlap / ...)
    regime: str = ""          # market condition at entry (trend_up / trend_down / range)
    volatility: str = ""      # volatility bucket at entry (loVol / normalVol / hiVol)
    exit_model: str = ""      # which exit rule produced this trade
    timeframe: str = ""       # timeframe the signal came from


def synthetic_tick_path(candle: Candle) -> list[float]:
    """Expand a candle into an ordered synthetic price path (O, ext, ext, C)."""
    if candle.close >= candle.open:
        return [candle.open, candle.low, candle.high, candle.close]
    return [candle.open, candle.high, candle.low, candle.close]


def simulate_exit(
    instrument: str,
    direction: Direction,
    entry_price: float,
    entry_time_ms: float,
    lots: float,
    exit_plan: ExitPlan,
    future_candles: list[Candle],
    cost_model: CFDCostModel | None = None,
) -> SimulatedTrade:
    """
    Simulate SL/TP resolution for one position across ``future_candles``.

    ``future_candles`` are the bars AFTER entry (chronological). For each bar we
    first test the full [low, high] range for a stop hit (conservative), then
    walk the synthetic tick path to resolve take-profits in touch order. Partial
    TPs are supported; the position closes when fully exited or a stop hits.

    If the window ends with the position still open, it is marked ``closed=False``
    and valued at the last candle's close (so the caller can decide how to treat
    unresolved trades — typically force-close at window end).
    """
    cost_model = cost_model or COST_MODEL_INTRADAY
    inst = get_instrument(instrument)

    pos = ManagedPosition(
        position_id="SIM",
        strategy_id="", variant_id="",
        instrument=instrument,
        direction=direction,
        entry_price=entry_price,
        entry_time_ms=entry_time_ms,
        lots=lots,
        exit_plan=exit_plan,
    )

    exit_price = entry_price
    exit_time_ms = entry_time_ms
    exit_reason = ExitReason.EOD_FLATTEN
    bars_held = 0
    closed = False

    for candle in future_candles:
        bars_held += 1
        # Update excursion across the bar's extremes.
        pos.update_excursion(candle.high)
        pos.update_excursion(candle.low)

        # 1) Stop check on the whole bar range (conservative priority).
        #    evaluate_exit already returns STOP first if the range hits it.
        decisions = evaluate_exit(pos, high=candle.high, low=candle.low)

        # If the first decision is a stop, take it and stop.
        if decisions and decisions[0].reason is ExitReason.STOP_LOSS:
            d = decisions[0]
            _apply(pos, d.price, d.fraction, d.reason, candle.timestamp_ms, d.tp_index)
            exit_price, exit_time_ms, exit_reason = d.price, candle.timestamp_ms, d.reason
            closed = True
            break

        # 2) No stop this bar — resolve TPs along the synthetic path in order,
        #    re-evaluating at each step so partials fire at the right price.
        bar_closed = False
        for price in synthetic_tick_path(candle):
            step_decisions = evaluate_exit(pos, high=price, low=price)
            for d in step_decisions:
                _apply(pos, d.price, d.fraction, d.reason, candle.timestamp_ms, d.tp_index)
                exit_price, exit_time_ms, exit_reason = d.price, candle.timestamp_ms, d.reason
                if d.fully_closed:
                    bar_closed = True
                    break
            if bar_closed:
                break
        if bar_closed:
            closed = True
            break

    # If never closed, value the remainder at the last candle's close.
    if not closed and future_candles:
        last = future_candles[-1]
        # Close remaining fraction at last close as a forced flatten.
        if pos.remaining_fraction > 1e-9:
            rr = pos.rr_at(last.close)
            pos.partial_closes.append(PartialClose(
                price=last.close, fraction=pos.remaining_fraction,
                reason=ExitReason.EOD_FLATTEN, timestamp_ms=last.timestamp_ms,
                rr=rr, pnl_price=pos.price_pnl_per_unit(last.close),
            ))
            pos.remaining_fraction = 0.0
        exit_price, exit_time_ms, exit_reason = last.close, last.timestamp_ms, ExitReason.EOD_FLATTEN

    # Aggregate PnL (fraction-weighted).
    pnl_price = sum(pc.pnl_price * pc.fraction for pc in pos.partial_closes)
    realized_rr = sum(pc.rr * pc.fraction for pc in pos.partial_closes)
    pnl_usd = pnl_price * inst.point_value_per_lot * lots

    cost = calculate_trade_cost(
        symbol=instrument, lot_size=lots, cost_model=cost_model, instrument=inst,
    )
    net_pnl_usd = pnl_usd - cost.total_usd

    return SimulatedTrade(
        instrument=instrument,
        direction=direction,
        entry_price=entry_price,
        entry_time_ms=entry_time_ms,
        exit_price=exit_price,
        exit_time_ms=exit_time_ms,
        exit_reason=exit_reason,
        lots=lots,
        planned_rr=exit_plan.max_rr,
        realized_rr=realized_rr,
        pnl_price=pnl_price,
        pnl_usd=pnl_usd,
        cost_usd=cost.total_usd,
        net_pnl_usd=net_pnl_usd,
        mfe_price=pos.mfe_price,
        mae_price=pos.mae_price,
        bars_held=bars_held,
        partials=list(pos.partial_closes),
        closed=closed,
    )


def _apply(
    pos: ManagedPosition, price: float, fraction: float,
    reason: ExitReason, timestamp_ms: float, tp_index: int,
) -> None:
    """Record a (partial) close on the simulated position."""
    if fraction <= 0:
        return
    pos.partial_closes.append(PartialClose(
        price=price, fraction=fraction, reason=reason,
        timestamp_ms=timestamp_ms, rr=pos.rr_at(price),
        pnl_price=pos.price_pnl_per_unit(price),
    ))
    if tp_index >= 0:
        pos._tp_taken.add(tp_index)
    pos.remaining_fraction -= fraction
    if pos.remaining_fraction <= 1e-9 or reason is ExitReason.STOP_LOSS:
        pos.status = PositionStatus.CLOSED
