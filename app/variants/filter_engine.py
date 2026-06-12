"""
Filter evaluation engine — pure function, no state.

Takes a FilterSet + IndicatorSnapshot and returns True/False.
Short-circuits on first failure for performance.

This is called ~150,000 times per instrument per candle close,
so it must be FAST. No allocations, no logging, no side effects.
"""

from __future__ import annotations

from app.variants.models import (
    ADXFilter,
    ATRFilter,
    FilterSet,
    IndicatorSnapshot,
    RSIFilter,
    VIXFilter,
    VolumeFilter,
    VWAPFilter,
)


def evaluate_filters(filters: FilterSet, snapshot: IndicatorSnapshot) -> bool:
    """
    Evaluate whether all filter conditions are met.

    Returns True if ALL active filters pass.
    Returns True immediately if all filters are NONE (no conditions).
    Short-circuits on first failure.

    This is the hottest function in the system — called 150K times per candle.
    """

    # ATR filter: ATR must be greater than threshold
    if filters.atr is not ATRFilter.NONE:
        if snapshot.atr < filters.atr.value:
            return False

    # ADX filter: ADX must be greater than threshold (trending market)
    if filters.adx is not ADXFilter.NONE:
        if snapshot.adx < filters.adx.value:
            return False

    # VIX filter: India VIX must be greater than threshold
    if filters.vix is not VIXFilter.NONE:
        if snapshot.vix < filters.vix.value:
            return False

    # Volume filter: volume ratio must exceed multiplier
    if filters.volume is not VolumeFilter.NONE:
        if snapshot.volume_ratio < filters.volume.value:
            return False

    # RSI filter: conditional on direction
    if filters.rsi is not RSIFilter.NONE:
        if not _check_rsi(filters.rsi, snapshot.rsi):
            return False

    # VWAP filter: price position relative to VWAP
    if filters.vwap is not VWAPFilter.NONE:
        if not _check_vwap(filters.vwap, snapshot):
            return False

    return True


def _check_rsi(rsi_filter: RSIFilter, rsi_value: float) -> bool:
    """Check RSI filter condition."""
    if rsi_filter == RSIFilter.LT_30:
        return rsi_value < 30.0
    elif rsi_filter == RSIFilter.LT_35:
        return rsi_value < 35.0
    elif rsi_filter == RSIFilter.GT_65:
        return rsi_value > 65.0
    elif rsi_filter == RSIFilter.GT_70:
        return rsi_value > 70.0
    return True


def _check_vwap(vwap_filter: VWAPFilter, snapshot: IndicatorSnapshot) -> bool:
    """Check VWAP position filter."""
    if vwap_filter == VWAPFilter.ABOVE_VWAP:
        # price_vs_vwap > 0 means price is above VWAP
        return snapshot.price_vs_vwap > 0

    elif vwap_filter == VWAPFilter.BELOW_VWAP:
        return snapshot.price_vs_vwap < 0

    elif vwap_filter == VWAPFilter.DISTANCE_GT_0_5_ATR:
        # Absolute distance from VWAP must be > 0.5 ATR
        return abs(snapshot.price_vs_vwap) > 0.5

    elif vwap_filter == VWAPFilter.DISTANCE_GT_1_ATR:
        return abs(snapshot.price_vs_vwap) > 1.0

    return True
