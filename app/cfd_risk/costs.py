"""
CFD Cost Model — calculates realistic trading costs for CFD instruments.

Components of a CFD round-trip cost:
1. Spread:      Bid/ask spread at entry (paid once, at entry)
2. Commission:  Broker commission per lot ($7 for FX/metals on Raw, $0 for indices)
3. Slippage:    Market impact / execution slippage (configurable, default 0.5 pips)
4. Swap:        Overnight financing (0 for intraday, configurable for swing)

All costs are returned in USD for a given lot size.

Usage:
    from app.cfd_risk.costs import calculate_trade_cost

    cost = calculate_trade_cost(
        symbol="XAUUSD",
        lot_size=0.50,
        slippage_pips=0.5,
    )
    print(cost.total_usd)       # Total cost in USD
    print(cost.spread_usd)      # Spread component
    print(cost.commission_usd)  # Commission component
    print(cost.slippage_usd)    # Slippage component

    # Or use the model to adjust raw PnL:
    net_pnl = raw_pnl - cost.total_usd
"""

from __future__ import annotations

from dataclasses import dataclass

from app.cfd_risk.instruments import get_instrument, CFDInstrument


@dataclass(frozen=True)
class TradeCost:
    """
    Breakdown of costs for a single trade (round-trip).

    All values in USD.
    """

    symbol: str
    lot_size: float
    spread_usd: float           # Spread cost (entry only)
    commission_usd: float       # Broker commission (round-trip)
    slippage_usd: float         # Execution slippage estimate
    swap_usd: float             # Overnight swap (0 for intraday)
    total_usd: float            # Sum of all costs

    @property
    def total_pips(self) -> float:
        """Total cost expressed in pips (for the given lot size)."""
        inst = get_instrument(self.symbol)
        if inst.pip_value_per_lot <= 0 or self.lot_size <= 0:
            return 0.0
        return self.total_usd / (inst.pip_value_per_lot * self.lot_size)

    def __repr__(self) -> str:
        return (
            f"TradeCost({self.symbol} {self.lot_size:.2f}lots: "
            f"spread=${self.spread_usd:.2f} + comm=${self.commission_usd:.2f} + "
            f"slip=${self.slippage_usd:.2f} + swap=${self.swap_usd:.2f} = "
            f"${self.total_usd:.2f})"
        )


@dataclass(frozen=True)
class CFDCostModel:
    """
    Configurable cost model parameters.

    Override defaults to model different scenarios (optimistic, conservative, etc.)
    """

    name: str = "default"
    description: str = "Standard IC Markets Raw Spread conditions"

    # Slippage in pips (applied per side, so ×2 for round-trip isn't needed
    # because we model it as a single extra cost at entry)
    slippage_pips: float = 0.5

    # Spread multiplier (1.0 = use typical spread, 1.5 = 50% worse than typical)
    spread_multiplier: float = 1.0

    # Extra spread widening for SESSION-OPEN entries (ORB fires at the open, when
    # spreads blow out 2-5×). Applied on top of spread_multiplier. 1.0 = off.
    open_spread_multiplier: float = 1.0

    # If set, slippage is computed from the instrument's native price slippage
    # (typical_slippage_price × this multiplier), instead of the flat
    # slippage_pips path. This fixes G5 (flat 0.5 pips is meaningless for
    # indices). None = use the legacy slippage_pips path.
    slippage_price_multiplier: float | None = None

    # Swap per lot per night in USD (instrument-agnostic average)
    # For intraday: set to 0. For swing: varies by instrument.
    swap_per_night_usd: float = 0.0

    # Number of nights held (for swap calculation)
    nights_held: int = 0


# ─── Pre-built cost models ───────────────────────────────────────────────────

COST_MODEL_INTRADAY = CFDCostModel(
    name="intraday",
    description="Intraday CFD (no swap, normal spread, 0.5 pip slippage)",
    slippage_pips=0.5,
    spread_multiplier=1.0,
    swap_per_night_usd=0.0,
    nights_held=0,
)

COST_MODEL_CONSERVATIVE = CFDCostModel(
    name="conservative",
    description="Conservative estimate (1.5× spread, 1 pip slippage, no swap)",
    slippage_pips=1.0,
    spread_multiplier=1.5,
    swap_per_night_usd=0.0,
    nights_held=0,
)

# The realistic model for OPEN-DRIVEN strategies (ORB): spread widened 2× for the
# session open, and slippage from each instrument's native price slippage (so
# indices are charged properly). This is the run_research default — an edge that
# only survives cheaper models is NOT real.
COST_MODEL_SESSION_OPEN = CFDCostModel(
    name="session_open",
    description="Session-open realistic (2× spread at open, per-instrument price slippage)",
    spread_multiplier=1.0,
    open_spread_multiplier=2.0,
    slippage_price_multiplier=1.0,
    slippage_pips=0.0,
    swap_per_night_usd=0.0,
    nights_held=0,
)

COST_MODEL_ZERO = CFDCostModel(
    name="zero",
    description="No costs (raw PnL)",
    slippage_pips=0.0,
    spread_multiplier=0.0,
    swap_per_night_usd=0.0,
    nights_held=0,
)


# ─── Cost Calculation ────────────────────────────────────────────────────────

def calculate_trade_cost(
    symbol: str,
    lot_size: float,
    slippage_pips: float | None = None,
    spread_multiplier: float = 1.0,
    swap_per_night_usd: float = 0.0,
    nights_held: int = 0,
    cost_model: CFDCostModel | None = None,
    instrument: CFDInstrument | None = None,
) -> TradeCost:
    """
    Calculate the total cost of a trade (round-trip).

    Args:
        symbol:             Instrument symbol (e.g. "XAUUSD").
        lot_size:           Trade size in lots.
        slippage_pips:      Slippage in pips (overrides cost_model if provided).
        spread_multiplier:  Multiplier on typical spread (overrides cost_model).
        swap_per_night_usd: Swap charge per night per lot.
        nights_held:        Number of nights the trade is held.
        cost_model:         Optional CFDCostModel (overrides individual params).
        instrument:         Optional pre-fetched CFDInstrument.

    Returns:
        TradeCost breakdown.
    """
    # Get instrument specs
    inst = instrument or get_instrument(symbol)

    # Apply cost model if provided (individual params override model)
    open_spread_multiplier = 1.0
    slippage_price_multiplier: float | None = None
    if cost_model is not None:
        if slippage_pips is None:
            slippage_pips = cost_model.slippage_pips
        spread_multiplier = cost_model.spread_multiplier
        open_spread_multiplier = cost_model.open_spread_multiplier
        slippage_price_multiplier = cost_model.slippage_price_multiplier
        swap_per_night_usd = cost_model.swap_per_night_usd
        nights_held = cost_model.nights_held

    # Default slippage
    if slippage_pips is None:
        slippage_pips = 0.5

    # ─── Calculate each component ────────────────────────────────

    # Spread: typical_spread × pip_value × lots × multiplier × open-widening.
    spread_usd = (
        inst.typical_spread_pips * inst.pip_value_per_lot * lot_size
        * spread_multiplier * open_spread_multiplier
    )

    # Commission: per-lot cost × lots (already round-trip in the spec)
    commission_usd = inst.commission_per_lot * lot_size

    # Slippage: prefer the instrument-native PRICE slippage (G5) when the model
    # opts in; otherwise the legacy flat slippage_pips path.
    if slippage_price_multiplier is not None:
        slippage_usd = (
            inst.typical_slippage_price * inst.point_value_per_lot
            * lot_size * slippage_price_multiplier
        )
    else:
        slippage_usd = slippage_pips * inst.pip_value_per_lot * lot_size

    # Swap: per-night × lots × nights
    swap_usd = swap_per_night_usd * lot_size * nights_held

    # Total
    total_usd = spread_usd + commission_usd + slippage_usd + swap_usd

    return TradeCost(
        symbol=inst.symbol,
        lot_size=lot_size,
        spread_usd=round(spread_usd, 2),
        commission_usd=round(commission_usd, 2),
        slippage_usd=round(slippage_usd, 2),
        swap_usd=round(swap_usd, 2),
        total_usd=round(total_usd, 2),
    )


def cost_per_lot_usd(symbol: str, cost_model: CFDCostModel | None = None) -> float:
    """
    Quick helper: total cost per standard lot for an instrument.

    Useful for comparing cost efficiency across instruments.
    """
    model = cost_model or COST_MODEL_INTRADAY
    result = calculate_trade_cost(symbol, lot_size=1.0, cost_model=model)
    return result.total_usd


def cost_in_pips(symbol: str, cost_model: CFDCostModel | None = None) -> float:
    """
    Quick helper: total cost expressed in pips for 1 standard lot.

    This tells you how many pips you need to cover costs before profit.
    """
    model = cost_model or COST_MODEL_INTRADAY
    result = calculate_trade_cost(symbol, lot_size=1.0, cost_model=model)
    return result.total_pips
