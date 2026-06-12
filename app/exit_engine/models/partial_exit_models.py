"""
Partial exit models.

Three partial exit strategies:
- Partial A: Exit 50% at RR1, trail remaining with ATR trail
- Partial B: Exit 33% at RR1, 33% at RR2, trail remaining
- Partial C: Scale out — 25% at RR1, 25% at RR2, 25% at RR3, trail 25%

Each returns a blended PnL as if the full position was split into portions.
All models use 1 ATR as the risk unit (stop loss distance).
"""

from __future__ import annotations

from app.exit_engine.models.rr_exit import ExitResult, simulate_rr_exit
from app.exit_engine.models.trailing_models import simulate_atr_trail


def simulate_partial_a(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
) -> ExitResult:
    """
    Partial A: 50% at RR1, 50% trails with ATR trail.

    Logic:
    1. Walk path checking for RR1 target (50% position)
    2. If RR1 hits, move SL to entry (breakeven) on remaining 50%
    3. Trail remaining 50% with ATR trail from the RR1 hit point onward
    4. Blend PnL: 0.5 × RR1_pnl + 0.5 × trail_pnl
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    risk = atr_at_entry

    # Check RR1 hit
    rr1_result = simulate_rr_exit(entry_price, direction, atr_at_entry, candle_path, 1.0)

    if rr1_result.exit_reason == "SL_HIT":
        # Full position stopped out before RR1
        return ExitResult(
            exit_price=rr1_result.exit_price,
            pnl_points=rr1_result.pnl_points,  # Full loss
            exit_reason="PARTIAL_A_FULL_SL",
            exit_candle_index=rr1_result.exit_candle_index,
        )

    if rr1_result.exit_reason == "CLOSE_AT_EOD":
        # RR1 never hit, close everything at EOD
        return ExitResult(
            exit_price=rr1_result.exit_price,
            pnl_points=rr1_result.pnl_points,
            exit_reason="PARTIAL_A_EOD",
            exit_candle_index=-1,
        )

    # RR1 hit — take 50% profit at RR1
    rr1_pnl = rr1_result.pnl_points  # = 1 ATR
    rr1_candle_idx = rr1_result.exit_candle_index

    # Trail remaining 50% from the RR1 hit point forward
    remaining_path = candle_path[rr1_candle_idx + 1:]
    if remaining_path:
        # New entry for trailing is at RR1 price (breakeven stop = entry)
        trail_result = simulate_atr_trail(
            entry_price=entry_price,
            direction=direction,
            atr_at_entry=atr_at_entry,
            candle_path=remaining_path,
        )
        trail_pnl = trail_result.pnl_points
    else:
        # No candles after RR1 — close at RR1
        trail_pnl = rr1_pnl

    # Blended: 50% at RR1 + 50% trail
    blended_pnl = 0.5 * rr1_pnl + 0.5 * trail_pnl

    return ExitResult(
        exit_price=0.0,  # blended — no single exit price
        pnl_points=blended_pnl,
        exit_reason="PARTIAL_A_BLENDED",
        exit_candle_index=-1,
    )


def simulate_partial_b(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
) -> ExitResult:
    """
    Partial B: 33% at RR1, 33% at RR2, 33% trails.
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    # Check RR1
    rr1_result = simulate_rr_exit(entry_price, direction, atr_at_entry, candle_path, 1.0)

    if rr1_result.exit_reason == "SL_HIT":
        return ExitResult(
            exit_price=rr1_result.exit_price,
            pnl_points=rr1_result.pnl_points,
            exit_reason="PARTIAL_B_FULL_SL",
            exit_candle_index=rr1_result.exit_candle_index,
        )

    if rr1_result.exit_reason == "CLOSE_AT_EOD":
        return ExitResult(
            exit_price=rr1_result.exit_price,
            pnl_points=rr1_result.pnl_points,
            exit_reason="PARTIAL_B_EOD",
            exit_candle_index=-1,
        )

    rr1_pnl = rr1_result.pnl_points
    rr1_idx = rr1_result.exit_candle_index

    # Check RR2 from RR1 hit point forward
    remaining_after_rr1 = candle_path[rr1_idx + 1:]
    rr2_result = simulate_rr_exit(entry_price, direction, atr_at_entry, remaining_after_rr1, 2.0)

    if rr2_result.exit_reason in ("SL_HIT", "CLOSE_AT_EOD"):
        # 33% at RR1 + 67% at whatever happened
        rr2_pnl = rr2_result.pnl_points
        blended = (1 / 3) * rr1_pnl + (2 / 3) * rr2_pnl
        return ExitResult(
            exit_price=0.0,
            pnl_points=blended,
            exit_reason="PARTIAL_B_PARTIAL",
            exit_candle_index=-1,
        )

    rr2_pnl = rr2_result.pnl_points
    rr2_idx = rr2_result.exit_candle_index

    # Trail remaining 33%
    remaining_after_rr2 = remaining_after_rr1[rr2_idx + 1:]
    if remaining_after_rr2:
        trail_result = simulate_atr_trail(entry_price, direction, atr_at_entry, remaining_after_rr2)
        trail_pnl = trail_result.pnl_points
    else:
        trail_pnl = rr2_pnl

    blended = (1 / 3) * rr1_pnl + (1 / 3) * rr2_pnl + (1 / 3) * trail_pnl

    return ExitResult(
        exit_price=0.0,
        pnl_points=blended,
        exit_reason="PARTIAL_B_BLENDED",
        exit_candle_index=-1,
    )


def simulate_partial_c(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
) -> ExitResult:
    """
    Partial C: Scale out — 25% at RR1, 25% at RR2, 25% at RR3, 25% trails.
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    portions: list[float] = []
    current_path = candle_path

    # Sequentially check RR1, RR2, RR3
    for rr_target in [1.0, 2.0, 3.0]:
        result = simulate_rr_exit(entry_price, direction, atr_at_entry, current_path, rr_target)

        if result.exit_reason == "SL_HIT":
            # Everything remaining stopped out
            remaining_weight = 1.0 - 0.25 * len(portions)
            full_pnl = sum(0.25 * p for p in portions) + remaining_weight * result.pnl_points
            return ExitResult(
                exit_price=0.0, pnl_points=full_pnl,
                exit_reason="PARTIAL_C_PARTIAL_SL", exit_candle_index=-1,
            )

        if result.exit_reason == "CLOSE_AT_EOD":
            remaining_weight = 1.0 - 0.25 * len(portions)
            full_pnl = sum(0.25 * p for p in portions) + remaining_weight * result.pnl_points
            return ExitResult(
                exit_price=0.0, pnl_points=full_pnl,
                exit_reason="PARTIAL_C_EOD", exit_candle_index=-1,
            )

        # RR target hit
        portions.append(result.pnl_points)
        current_path = current_path[result.exit_candle_index + 1:]

    # Trail last 25%
    if current_path:
        trail_result = simulate_atr_trail(entry_price, direction, atr_at_entry, current_path)
        trail_pnl = trail_result.pnl_points
    else:
        trail_pnl = portions[-1] if portions else 0.0

    blended = sum(0.25 * p for p in portions) + 0.25 * trail_pnl

    return ExitResult(
        exit_price=0.0,
        pnl_points=blended,
        exit_reason="PARTIAL_C_BLENDED",
        exit_candle_index=-1,
    )


def simulate_all_partials(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
) -> dict[str, float]:
    """
    Run all partial exit models.

    Returns:
        {"partial_a": 35.0, "partial_b": 28.0, "partial_c": 22.0}
    """
    a_result = simulate_partial_a(entry_price, direction, atr_at_entry, candle_path)
    b_result = simulate_partial_b(entry_price, direction, atr_at_entry, candle_path)
    c_result = simulate_partial_c(entry_price, direction, atr_at_entry, candle_path)

    return {
        "partial_a": a_result.pnl_points,
        "partial_b": b_result.pnl_points,
        "partial_c": c_result.pnl_points,
    }
