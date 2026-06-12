"""
Chandelier Exit and Parabolic-style trailing models.

These are advanced trailing stops used by professional traders:

1. Chandelier Exit — trailing stop from highest high minus N × ATR
   Tests multiple multipliers (2, 3, 4) for different tightness.

2. Percentage Trail — trail from best price by a fixed percentage (0.5%, 1%, 1.5%, 2%)
   Simpler than ATR-based, works across price ranges.

3. Step Trail — moves SL up in fixed increments (every 1R of profit, move SL up by 0.5R)
   Tests "staircase" style trailing.

4. Hybrid Chandelier — combines chandelier with minimum holding period
   (don't trail for first 6 candles, then start chandelier).
"""

from __future__ import annotations

from app.exit_engine.models.rr_exit import ExitResult


def simulate_chandelier_exit(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    multiplier: float = 3.0,
) -> ExitResult:
    """
    Chandelier Exit: trailing stop = highest high - (multiplier × ATR).
    For SHORT: lowest low + (multiplier × ATR).
    
    Classic trend-following exit.
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    chandelier_distance = multiplier * atr_at_entry

    if direction == "LONG":
        highest_high = entry_price
        trailing_stop = entry_price - chandelier_distance

        for i, candle in enumerate(candle_path):
            if candle["high"] > highest_high:
                highest_high = candle["high"]
                trailing_stop = max(trailing_stop, highest_high - chandelier_distance)

            if candle["low"] <= trailing_stop:
                pnl = trailing_stop - entry_price
                return ExitResult(exit_price=trailing_stop, pnl_points=pnl, exit_reason=f"CHANDELIER_{multiplier}X", exit_candle_index=i)

    else:  # SHORT
        lowest_low = entry_price
        trailing_stop = entry_price + chandelier_distance

        for i, candle in enumerate(candle_path):
            if candle["low"] < lowest_low:
                lowest_low = candle["low"]
                trailing_stop = min(trailing_stop, lowest_low + chandelier_distance)

            if candle["high"] >= trailing_stop:
                pnl = entry_price - trailing_stop
                return ExitResult(exit_price=trailing_stop, pnl_points=pnl, exit_reason=f"CHANDELIER_{multiplier}X", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_pct_trail(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
    trail_pct: float = 0.01,
) -> ExitResult:
    """
    Percentage-based trailing stop.
    Trail = best_price × (1 - trail_pct) for LONG.
    """
    if not candle_path:
        return ExitResult(exit_price=entry_price, pnl_points=0.0, exit_reason="NO_PATH", exit_candle_index=-1)

    if direction == "LONG":
        best_price = entry_price
        trailing_stop = entry_price * (1.0 - trail_pct)

        for i, candle in enumerate(candle_path):
            if candle["high"] > best_price:
                best_price = candle["high"]
                trailing_stop = max(trailing_stop, best_price * (1.0 - trail_pct))

            if candle["low"] <= trailing_stop:
                pnl = trailing_stop - entry_price
                return ExitResult(exit_price=trailing_stop, pnl_points=pnl, exit_reason=f"PCT_TRAIL_{trail_pct}", exit_candle_index=i)

    else:
        best_price = entry_price
        trailing_stop = entry_price * (1.0 + trail_pct)

        for i, candle in enumerate(candle_path):
            if candle["low"] < best_price:
                best_price = candle["low"]
                trailing_stop = min(trailing_stop, best_price * (1.0 + trail_pct))

            if candle["high"] >= trailing_stop:
                pnl = entry_price - trailing_stop
                return ExitResult(exit_price=trailing_stop, pnl_points=pnl, exit_reason=f"PCT_TRAIL_{trail_pct}", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_step_trail(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    step_r: float = 1.0,
    lock_r: float = 0.5,
) -> ExitResult:
    """
    Step (staircase) trailing stop.
    
    Every time price moves step_r × ATR in your favor,
    move SL up by lock_r × ATR.
    
    Example (step=1R, lock=0.5R):
    - Price hits +1R → SL moves to -0.5R (from entry)
    - Price hits +2R → SL moves to entry (breakeven)
    - Price hits +3R → SL moves to +0.5R
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    risk = atr_at_entry

    if direction == "LONG":
        initial_sl = entry_price - (1.5 * risk)
        current_sl = initial_sl
        steps_triggered = 0

        for i, candle in enumerate(candle_path):
            # Check how many R we've moved
            max_profit_r = (candle["high"] - entry_price) / risk
            new_steps = int(max_profit_r / step_r)

            if new_steps > steps_triggered:
                steps_triggered = new_steps
                # Move SL: starts at -1.5R, moves up by lock_r per step
                current_sl = entry_price - (1.5 * risk) + (steps_triggered * lock_r * risk)

            if candle["low"] <= current_sl:
                pnl = current_sl - entry_price
                return ExitResult(exit_price=current_sl, pnl_points=pnl, exit_reason="STEP_TRAIL_HIT", exit_candle_index=i)

    else:
        initial_sl = entry_price + (1.5 * risk)
        current_sl = initial_sl
        steps_triggered = 0

        for i, candle in enumerate(candle_path):
            max_profit_r = (entry_price - candle["low"]) / risk
            new_steps = int(max_profit_r / step_r)

            if new_steps > steps_triggered:
                steps_triggered = new_steps
                current_sl = entry_price + (1.5 * risk) - (steps_triggered * lock_r * risk)

            if candle["high"] >= current_sl:
                pnl = entry_price - current_sl
                return ExitResult(exit_price=current_sl, pnl_points=pnl, exit_reason="STEP_TRAIL_HIT", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_delayed_chandelier(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    delay_candles: int = 6,
    multiplier: float = 3.0,
) -> ExitResult:
    """
    Delayed Chandelier: don't trail for first N candles, then apply chandelier.
    Uses initial SL during delay period.
    Tests: "give the trade room at the start, then tighten."
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    chandelier_distance = multiplier * atr_at_entry

    if direction == "LONG":
        initial_sl = entry_price - (1.5 * atr_at_entry)
        highest_high = entry_price

        for i, candle in enumerate(candle_path):
            if candle["high"] > highest_high:
                highest_high = candle["high"]

            if i < delay_candles:
                # Delay phase — only check initial SL
                if candle["low"] <= initial_sl:
                    return ExitResult(exit_price=initial_sl, pnl_points=initial_sl - entry_price, exit_reason="DELAY_SL_HIT", exit_candle_index=i)
            else:
                # Active chandelier phase
                trailing_stop = highest_high - chandelier_distance
                trailing_stop = max(trailing_stop, initial_sl)  # Never worse than initial

                if candle["low"] <= trailing_stop:
                    pnl = trailing_stop - entry_price
                    return ExitResult(exit_price=trailing_stop, pnl_points=pnl, exit_reason="DELAYED_CHANDELIER", exit_candle_index=i)

    else:
        initial_sl = entry_price + (1.5 * atr_at_entry)
        lowest_low = entry_price

        for i, candle in enumerate(candle_path):
            if candle["low"] < lowest_low:
                lowest_low = candle["low"]

            if i < delay_candles:
                if candle["high"] >= initial_sl:
                    return ExitResult(exit_price=initial_sl, pnl_points=entry_price - initial_sl, exit_reason="DELAY_SL_HIT", exit_candle_index=i)
            else:
                trailing_stop = lowest_low + chandelier_distance
                trailing_stop = min(trailing_stop, initial_sl)

                if candle["high"] >= trailing_stop:
                    pnl = entry_price - trailing_stop
                    return ExitResult(exit_price=trailing_stop, pnl_points=pnl, exit_reason="DELAYED_CHANDELIER", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_all_chandelier_exits(
    entry_price: float, direction: str, atr_at_entry: float, candle_path: list[dict],
) -> dict[str, float]:
    """
    Run all chandelier / advanced trailing variants.

    Returns 12 results:
        chandelier_2x, chandelier_3x, chandelier_4x,
        pct_trail_05, pct_trail_1, pct_trail_15, pct_trail_2,
        step_trail_1r, step_trail_05r,
        delayed_chand_3x, delayed_chand_4x, delayed_chand_2x
    """
    results: dict[str, float] = {}

    # Chandelier with different multipliers
    results["chandelier_2x"] = simulate_chandelier_exit(entry_price, direction, atr_at_entry, candle_path, 2.0).pnl_points
    results["chandelier_3x"] = simulate_chandelier_exit(entry_price, direction, atr_at_entry, candle_path, 3.0).pnl_points
    results["chandelier_4x"] = simulate_chandelier_exit(entry_price, direction, atr_at_entry, candle_path, 4.0).pnl_points

    # Percentage trails
    results["pct_trail_05"] = simulate_pct_trail(entry_price, direction, candle_path, 0.005).pnl_points
    results["pct_trail_1"] = simulate_pct_trail(entry_price, direction, candle_path, 0.01).pnl_points
    results["pct_trail_15"] = simulate_pct_trail(entry_price, direction, candle_path, 0.015).pnl_points
    results["pct_trail_2"] = simulate_pct_trail(entry_price, direction, candle_path, 0.02).pnl_points

    # Step trails
    results["step_trail_1r"] = simulate_step_trail(entry_price, direction, atr_at_entry, candle_path, 1.0, 0.5).pnl_points
    results["step_trail_05r"] = simulate_step_trail(entry_price, direction, atr_at_entry, candle_path, 0.5, 0.25).pnl_points

    # Delayed chandeliers
    results["delayed_chand_2x"] = simulate_delayed_chandelier(entry_price, direction, atr_at_entry, candle_path, 6, 2.0).pnl_points
    results["delayed_chand_3x"] = simulate_delayed_chandelier(entry_price, direction, atr_at_entry, candle_path, 6, 3.0).pnl_points
    results["delayed_chand_4x"] = simulate_delayed_chandelier(entry_price, direction, atr_at_entry, candle_path, 6, 4.0).pnl_points

    return results
