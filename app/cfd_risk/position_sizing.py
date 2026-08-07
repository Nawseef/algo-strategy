"""
Position Sizing Engine — Risk-based lot calculation for CFD trading.

Core formula:
    lot_size = (account_balance × risk_per_trade_pct) / (sl_distance_price × point_value_per_lot)

Example (Gold, $100K account, 1% risk, $10 SL):
    risk_amount = $100,000 × 0.01 = $1,000
    sl_cost_per_lot = $10 × 100 (point_value) = $1,000
    lot_size = $1,000 / $1,000 = 1.00 lots

Example (EURUSD, $10K account, 1% risk, 20-pip SL):
    risk_amount = $10,000 × 0.01 = $100
    sl_distance_price = 0.0020 (20 pips)
    sl_cost_per_lot = 0.0020 × 100,000 (contract) = $200
    lot_size = $100 / $200 = 0.50 lots

The engine:
    1. Calculates raw lot size from the formula
    2. Rounds DOWN to the nearest lot_step (never round up = never over-risk)
    3. Clamps to min_lot / max_lot bounds
    4. Returns 0.0 if the calculated size is below min_lot (trade rejected)

Usage:
    from app.cfd_risk.position_sizing import calculate_lot_size

    result = calculate_lot_size(
        symbol="XAUUSD",
        account_balance=100_000.0,
        risk_pct=1.0,
        sl_distance_price=10.0,  # $10 SL distance
    )
    print(result.lot_size)    # 1.00
    print(result.risk_usd)    # 1000.0
    print(result.rejected)    # False
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.cfd_risk.instruments import get_instrument, CFDInstrument


@dataclass(frozen=True)
class PositionSizeResult:
    """
    Result of a position size calculation.

    Attributes:
        lot_size:       Final lot size (0.0 if rejected)
        risk_usd:       Actual USD risk at this lot size
        risk_pct:       Actual risk % of balance at this lot size
        raw_lots:       Unrounded calculated lots (before step/min/max)
        rejected:       True if trade cannot be taken (below min lot)
        reject_reason:  Why the trade was rejected (empty if accepted)
        sl_pips:        SL distance in pips
        sl_cost_per_lot: USD risk per standard lot for this SL
    """

    lot_size: float
    risk_usd: float
    risk_pct: float
    raw_lots: float
    rejected: bool
    reject_reason: str
    sl_pips: float
    sl_cost_per_lot: float

    @property
    def risk_reward_info(self) -> str:
        """Human-readable summary."""
        if self.rejected:
            return f"REJECTED: {self.reject_reason}"
        return (
            f"{self.lot_size:.2f} lots | "
            f"Risk: ${self.risk_usd:.2f} ({self.risk_pct:.2f}%) | "
            f"SL: {self.sl_pips:.1f} pips"
        )


def calculate_lot_size(
    symbol: str,
    account_balance: float,
    risk_pct: float,
    sl_distance_price: float,
    instrument: CFDInstrument | None = None,
) -> PositionSizeResult:
    """
    Calculate position size based on risk percentage and stop-loss distance.

    Args:
        symbol:             Instrument symbol (e.g. "XAUUSD"). Ignored if instrument provided.
        account_balance:    Current account balance in USD.
        risk_pct:           Risk per trade as percentage (e.g. 1.0 = 1%).
        sl_distance_price:  Stop-loss distance in PRICE units (not pips).
                            For XAUUSD: if entry=2400, SL=2390, then sl_distance=10.0
                            For EURUSD: if entry=1.0850, SL=1.0830, then sl_distance=0.0020
        instrument:         Optional pre-fetched CFDInstrument (skips lookup).

    Returns:
        PositionSizeResult with the calculated lot size and metadata.
    """
    # Get instrument specs
    inst = instrument or get_instrument(symbol)
    sl_distance = abs(sl_distance_price)

    # Reject if SL is zero or negative
    if sl_distance <= 0:
        return PositionSizeResult(
            lot_size=0.0,
            risk_usd=0.0,
            risk_pct=0.0,
            raw_lots=0.0,
            rejected=True,
            reject_reason="SL distance is zero or negative",
            sl_pips=0.0,
            sl_cost_per_lot=0.0,
        )

    # Reject if balance is zero or negative
    if account_balance <= 0:
        return PositionSizeResult(
            lot_size=0.0,
            risk_usd=0.0,
            risk_pct=0.0,
            raw_lots=0.0,
            rejected=True,
            reject_reason="Account balance is zero or negative",
            sl_pips=0.0,
            sl_cost_per_lot=0.0,
        )

    # Calculate
    risk_amount_usd = account_balance * (risk_pct / 100.0)
    sl_cost_per_lot = inst.sl_cost_per_lot(sl_distance)
    sl_pips = inst.price_to_pips(sl_distance)

    # Avoid division by zero
    if sl_cost_per_lot <= 0:
        return PositionSizeResult(
            lot_size=0.0,
            risk_usd=0.0,
            risk_pct=0.0,
            raw_lots=0.0,
            rejected=True,
            reject_reason="SL cost per lot calculated as zero",
            sl_pips=sl_pips,
            sl_cost_per_lot=0.0,
        )

    # Raw lot size
    raw_lots = risk_amount_usd / sl_cost_per_lot

    # Round DOWN to nearest lot_step (never over-risk)
    lot_size = math.floor(raw_lots / inst.lot_step) * inst.lot_step

    # Clamp to max
    lot_size = min(lot_size, inst.max_lot)

    # Check min lot
    if lot_size < inst.min_lot:
        return PositionSizeResult(
            lot_size=0.0,
            risk_usd=0.0,
            risk_pct=0.0,
            raw_lots=raw_lots,
            rejected=True,
            reject_reason=(
                f"Calculated {raw_lots:.4f} lots, below minimum {inst.min_lot} "
                f"(need larger account or tighter SL)"
            ),
            sl_pips=sl_pips,
            sl_cost_per_lot=sl_cost_per_lot,
        )

    # Actual risk at the rounded lot size
    actual_risk_usd = lot_size * sl_cost_per_lot
    actual_risk_pct = (actual_risk_usd / account_balance) * 100.0

    return PositionSizeResult(
        lot_size=round(lot_size, 2),  # Clean up floating point
        risk_usd=round(actual_risk_usd, 2),
        risk_pct=round(actual_risk_pct, 4),
        raw_lots=round(raw_lots, 6),
        rejected=False,
        reject_reason="",
        sl_pips=round(sl_pips, 1),
        sl_cost_per_lot=round(sl_cost_per_lot, 2),
    )


def max_lot_for_risk(
    symbol: str,
    account_balance: float,
    risk_pct: float,
    sl_distance_price: float,
) -> float:
    """
    Quick helper: return just the lot size (0.0 if rejected).
    For use in strategy code where you just need the number.
    """
    result = calculate_lot_size(symbol, account_balance, risk_pct, sl_distance_price)
    return result.lot_size
