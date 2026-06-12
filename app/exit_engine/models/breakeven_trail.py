"""
Breakeven + Trail exit models.

The most common real-world trailing approach:
1. Use initial SL (1 ATR)
2. When trade reaches 1R profit, move SL to breakeven (entry price)
3. Then trail from breakeven with various methods

This tests "lock in breakeven early, then let it run" — very different
from the pure trailing models which start trailing from the initial SL.

Models:
- BE + ATR trail: breakeven at 1R, then ATR trail (2× ATR)
- BE + EMA trail: breakeven at 1R, then exit on EMA9 cross
- BE + tight trail: breakeven at 1R, then tight 1× ATR trail (aggressive)
- BE + RR2 target: breakeven at 1R, then hold for RR2 (or trail)
- BE + RR3 target: breakeven at 1R, then hold for RR3 (or trail)
"""

from __future__ import annotations

from app.exit_engine.models.rr_exit import ExitResult
from app.exit_engine.models.trailing_models import _compute_ema_on_path


def _simulate_be_trail(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    be_trigger_r: float,
    trail_atr_multiplier: float,
) -> ExitResult:
    """
    Core breakeven + ATR trail logic.

    Phase 1: Hold with initial SL (1.5 ATR). Wait for BE trigger.
    Phase 2: Once in profit by be_trigger_r × ATR, move SL to entry.
    Phase 3: Trail with trail_atr_multiplier × ATR from best price.
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    risk = atr_at_entry
    be_trigger_level = risk * be_trigger_r  # profit needed to trigger BE

    # Phase 1: initial SL
    if direction == "LONG":
        initial_sl = entry_price - (1.5 * risk)
        current_sl = initial_sl
    else:
        initial_sl = entry_price + (1.5 * risk)
        current_sl = initial_sl

    be_activated = False
    best_price = entry_price
    trail_distance = trail_atr_multiplier * risk

    for i, candle in enumerate(candle_path):
        if direction == "LONG":
            # Track best price
            if candle["high"] > best_price:
                best_price = candle["high"]

            # Check if BE trigger reached
            if not be_activated and (best_price - entry_price) >= be_trigger_level:
                be_activated = True
                current_sl = entry_price  # Move to breakeven

            # Phase 3: trail from best price
            if be_activated:
                new_sl = best_price - trail_distance
                current_sl = max(current_sl, new_sl)

            # Check SL hit
            if candle["low"] <= current_sl:
                pnl = current_sl - entry_price
                reason = "BE_TRAIL_HIT" if be_activated else "INITIAL_SL_HIT"
                return ExitResult(exit_price=current_sl, pnl_points=pnl, exit_reason=reason, exit_candle_index=i)

        else:  # SHORT
            if candle["low"] < best_price:
                best_price = candle["low"]

            if not be_activated and (entry_price - best_price) >= be_trigger_level:
                be_activated = True
                current_sl = entry_price

            if be_activated:
                new_sl = best_price + trail_distance
                current_sl = min(current_sl, new_sl)

            if candle["high"] >= current_sl:
                pnl = entry_price - current_sl
                reason = "BE_TRAIL_HIT" if be_activated else "INITIAL_SL_HIT"
                return ExitResult(exit_price=current_sl, pnl_points=pnl, exit_reason=reason, exit_candle_index=i)

    # EOD close
    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_be_atr_trail(
    entry_price: float, direction: str, atr_at_entry: float, candle_path: list[dict],
) -> ExitResult:
    """BE at 1R, then trail with 2× ATR."""
    return _simulate_be_trail(entry_price, direction, atr_at_entry, candle_path, 1.0, 2.0)


def simulate_be_tight_trail(
    entry_price: float, direction: str, atr_at_entry: float, candle_path: list[dict],
) -> ExitResult:
    """BE at 1R, then tight trail with 1× ATR (aggressive lock)."""
    return _simulate_be_trail(entry_price, direction, atr_at_entry, candle_path, 1.0, 1.0)


def simulate_be_wide_trail(
    entry_price: float, direction: str, atr_at_entry: float, candle_path: list[dict],
) -> ExitResult:
    """BE at 1R, then wide trail with 3× ATR (give room to run)."""
    return _simulate_be_trail(entry_price, direction, atr_at_entry, candle_path, 1.0, 3.0)


def simulate_be_ema_trail(
    entry_price: float, direction: str, atr_at_entry: float, candle_path: list[dict],
) -> ExitResult:
    """BE at 1R, then exit on EMA9 cross (close below/above EMA)."""
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    risk = atr_at_entry

    if direction == "LONG":
        initial_sl = entry_price - (1.5 * risk)
    else:
        initial_sl = entry_price + (1.5 * risk)

    be_activated = False
    best_price = entry_price
    ema_values = _compute_ema_on_path(candle_path, 9)

    for i, candle in enumerate(candle_path):
        close = candle["close"]

        if direction == "LONG":
            if candle["high"] > best_price:
                best_price = candle["high"]

            # Activate BE
            if not be_activated and (best_price - entry_price) >= risk:
                be_activated = True

            # Check initial SL (always active before BE)
            if not be_activated and candle["low"] <= initial_sl:
                return ExitResult(exit_price=initial_sl, pnl_points=initial_sl - entry_price, exit_reason="INITIAL_SL_HIT", exit_candle_index=i)

            # BE + EMA exit
            if be_activated:
                # Breakeven check
                if candle["low"] <= entry_price:
                    return ExitResult(exit_price=entry_price, pnl_points=0.0, exit_reason="BE_HIT", exit_candle_index=i)
                # EMA cross exit
                if i < len(ema_values) and ema_values[i] > 0 and close < ema_values[i]:
                    pnl = close - entry_price
                    return ExitResult(exit_price=close, pnl_points=pnl, exit_reason="BE_EMA_EXIT", exit_candle_index=i)

        else:  # SHORT
            if candle["low"] < best_price:
                best_price = candle["low"]

            if not be_activated and (entry_price - best_price) >= risk:
                be_activated = True

            if not be_activated and candle["high"] >= initial_sl:
                return ExitResult(exit_price=initial_sl, pnl_points=entry_price - initial_sl, exit_reason="INITIAL_SL_HIT", exit_candle_index=i)

            if be_activated:
                if candle["high"] >= entry_price:
                    return ExitResult(exit_price=entry_price, pnl_points=0.0, exit_reason="BE_HIT", exit_candle_index=i)
                if i < len(ema_values) and ema_values[i] > 0 and close > ema_values[i]:
                    pnl = entry_price - close
                    return ExitResult(exit_price=close, pnl_points=pnl, exit_reason="BE_EMA_EXIT", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_be_rr_target(
    entry_price: float, direction: str, atr_at_entry: float, candle_path: list[dict],
    target_rr: float = 2.0,
) -> ExitResult:
    """
    BE at 1R, hold for target RR. If target not hit, trail with 2× ATR from best.
    Combines target with trailing — "aim for 2R but protect via trail."
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    risk = atr_at_entry

    if direction == "LONG":
        initial_sl = entry_price - (1.5 * risk)
        target = entry_price + (target_rr * risk)
    else:
        initial_sl = entry_price + (1.5 * risk)
        target = entry_price - (target_rr * risk)

    current_sl = initial_sl
    be_activated = False
    best_price = entry_price
    trail_distance = 2.0 * risk

    for i, candle in enumerate(candle_path):
        if direction == "LONG":
            if candle["high"] > best_price:
                best_price = candle["high"]

            # Check target hit
            if candle["high"] >= target:
                pnl = target - entry_price
                return ExitResult(exit_price=target, pnl_points=pnl, exit_reason=f"BE_RR{target_rr}_TP", exit_candle_index=i)

            # BE activation
            if not be_activated and (best_price - entry_price) >= risk:
                be_activated = True
                current_sl = entry_price

            # Trail after BE
            if be_activated:
                new_sl = best_price - trail_distance
                current_sl = max(current_sl, new_sl)

            # SL check
            if candle["low"] <= current_sl:
                pnl = current_sl - entry_price
                return ExitResult(exit_price=current_sl, pnl_points=pnl, exit_reason="BE_RR_TRAIL_HIT", exit_candle_index=i)

        else:
            if candle["low"] < best_price:
                best_price = candle["low"]

            if candle["low"] <= target:
                pnl = entry_price - target
                return ExitResult(exit_price=target, pnl_points=pnl, exit_reason=f"BE_RR{target_rr}_TP", exit_candle_index=i)

            if not be_activated and (entry_price - best_price) >= risk:
                be_activated = True
                current_sl = entry_price

            if be_activated:
                new_sl = best_price + trail_distance
                current_sl = min(current_sl, new_sl)

            if candle["high"] >= current_sl:
                pnl = entry_price - current_sl
                return ExitResult(exit_price=current_sl, pnl_points=pnl, exit_reason="BE_RR_TRAIL_HIT", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_all_breakeven_trails(
    entry_price: float, direction: str, atr_at_entry: float, candle_path: list[dict],
) -> dict[str, float]:
    """
    Run all breakeven + trail combinations.

    Returns 7 results:
        be_atr_trail, be_tight_trail, be_wide_trail, be_ema_trail,
        be_rr2_target, be_rr3_target, be_rr5_target
    """
    results: dict[str, float] = {}

    results["be_atr_trail"] = simulate_be_atr_trail(entry_price, direction, atr_at_entry, candle_path).pnl_points
    results["be_tight_trail"] = simulate_be_tight_trail(entry_price, direction, atr_at_entry, candle_path).pnl_points
    results["be_wide_trail"] = simulate_be_wide_trail(entry_price, direction, atr_at_entry, candle_path).pnl_points
    results["be_ema_trail"] = simulate_be_ema_trail(entry_price, direction, atr_at_entry, candle_path).pnl_points
    results["be_rr2_target"] = simulate_be_rr_target(entry_price, direction, atr_at_entry, candle_path, 2.0).pnl_points
    results["be_rr3_target"] = simulate_be_rr_target(entry_price, direction, atr_at_entry, candle_path, 3.0).pnl_points
    results["be_rr5_target"] = simulate_be_rr_target(entry_price, direction, atr_at_entry, candle_path, 5.0).pnl_points

    return results
