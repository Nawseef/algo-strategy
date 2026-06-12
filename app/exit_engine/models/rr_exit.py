"""
Fixed Risk-Reward exit model.

Simulates exits at fixed R:R ratios.
Uses ATR at entry as the risk unit (1R = ATR).

For each trade:
- Stop loss = 1 ATR from entry
- Target = RR × ATR from entry

Walks the candle path and checks if TP or SL is hit first.
If neither is hit by market close, marks the trade as "open at close"
and returns the PnL at close price.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExitResult:
    """Result of a single exit model simulation."""

    exit_price: float
    pnl_points: float  # raw points profit/loss
    exit_reason: str  # "TP_HIT", "SL_HIT", "CLOSE_AT_EOD"
    exit_candle_index: int  # which candle the exit happened on (-1 if EOD)


# Standard RR multiples to test
RR_MULTIPLES = [1.0, 1.5, 2.0, 2.5, 3.0, 5.0, 10.0]


def simulate_rr_exit(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    rr_multiple: float,
) -> ExitResult:
    """
    Simulate a fixed risk-reward exit.

    Args:
        entry_price: Price at which trade was entered.
        direction: "LONG" or "SHORT".
        atr_at_entry: ATR value at entry time (defines 1R).
        candle_path: List of candle dicts (OHLCV) from entry to market close.
        rr_multiple: Target as multiple of risk (e.g. 2.0 = 2R target).

    Returns:
        ExitResult with PnL and exit reason.
    """
    if atr_at_entry <= 0:
        # Fallback: use 0.5% of entry price as risk unit
        atr_at_entry = entry_price * 0.005

    risk = atr_at_entry  # 1R = 1 ATR

    if direction == "LONG":
        stop_loss = entry_price - risk
        take_profit = entry_price + (risk * rr_multiple)
    else:
        stop_loss = entry_price + risk
        take_profit = entry_price - (risk * rr_multiple)

    # Walk candle path
    for i, candle in enumerate(candle_path):
        high = candle["high"]
        low = candle["low"]

        if direction == "LONG":
            # Check SL first (conservative — assumes worst case within bar)
            if low <= stop_loss:
                pnl = stop_loss - entry_price
                return ExitResult(
                    exit_price=stop_loss,
                    pnl_points=pnl,
                    exit_reason="SL_HIT",
                    exit_candle_index=i,
                )
            # Check TP
            if high >= take_profit:
                pnl = take_profit - entry_price
                return ExitResult(
                    exit_price=take_profit,
                    pnl_points=pnl,
                    exit_reason="TP_HIT",
                    exit_candle_index=i,
                )
        else:  # SHORT
            # Check SL first
            if high >= stop_loss:
                pnl = entry_price - stop_loss
                return ExitResult(
                    exit_price=stop_loss,
                    pnl_points=pnl,
                    exit_reason="SL_HIT",
                    exit_candle_index=i,
                )
            # Check TP
            if low <= take_profit:
                pnl = entry_price - take_profit
                return ExitResult(
                    exit_price=take_profit,
                    pnl_points=pnl,
                    exit_reason="TP_HIT",
                    exit_candle_index=i,
                )

    # Neither hit — close at last candle's close
    last_close = candle_path[-1]["close"] if candle_path else entry_price
    if direction == "LONG":
        pnl = last_close - entry_price
    else:
        pnl = entry_price - last_close

    return ExitResult(
        exit_price=last_close,
        pnl_points=pnl,
        exit_reason="CLOSE_AT_EOD",
        exit_candle_index=-1,
    )


def simulate_all_rr(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
) -> dict[str, float]:
    """
    Run all RR exit models and return PnL for each.

    Returns dict like:
        {"rr1": -15.0, "rr1_5": 22.5, "rr2": 30.0, ...}
    """
    results: dict[str, float] = {}

    # Key mapping to match DB column expectations
    _KEY_MAP = {
        1.0: "rr1",
        1.5: "rr1_5",
        2.0: "rr2",
        2.5: "rr2_5",
        3.0: "rr3",
        5.0: "rr5",
        10.0: "rr10",
    }

    for rr in RR_MULTIPLES:
        result = simulate_rr_exit(entry_price, direction, atr_at_entry, candle_path, rr)
        key = _KEY_MAP[rr]
        results[key] = result.pnl_points

    return results
