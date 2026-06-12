"""
Trailing stop exit models.

Three trailing strategies:
1. ATR Trail — Trailing stop at 2 × ATR below/above highest/lowest price
2. EMA Trail — Exit when price crosses the 9-EMA (computed on candle path)
3. Swing Trail — Trailing stop moves to each new swing low/high

Each model has an initial SL (1.5 ATR), then trails as the trade moves favorably.
"""

from __future__ import annotations

from app.exit_engine.models.rr_exit import ExitResult


def simulate_atr_trail(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    trail_multiplier: float = 2.0,
    initial_sl_multiplier: float = 1.5,
) -> ExitResult:
    """
    ATR trailing stop.

    Initial SL: entry ± (initial_sl_multiplier × ATR).
    Trail: highest/lowest close ± (trail_multiplier × ATR).
    Trail only moves in favorable direction (never widens).
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    trail_distance = trail_multiplier * atr_at_entry

    if direction == "LONG":
        trailing_stop = entry_price - (initial_sl_multiplier * atr_at_entry)
        best_price = entry_price
    else:
        trailing_stop = entry_price + (initial_sl_multiplier * atr_at_entry)
        best_price = entry_price

    for i, candle in enumerate(candle_path):
        if direction == "LONG":
            # Update best price and trail
            if candle["high"] > best_price:
                best_price = candle["high"]
                new_stop = best_price - trail_distance
                trailing_stop = max(trailing_stop, new_stop)  # Only moves up

            # Check stop hit
            if candle["low"] <= trailing_stop:
                pnl = trailing_stop - entry_price
                return ExitResult(
                    exit_price=trailing_stop,
                    pnl_points=pnl,
                    exit_reason="ATR_TRAIL_HIT",
                    exit_candle_index=i,
                )
        else:  # SHORT
            if candle["low"] < best_price:
                best_price = candle["low"]
                new_stop = best_price + trail_distance
                trailing_stop = min(trailing_stop, new_stop)  # Only moves down

            if candle["high"] >= trailing_stop:
                pnl = entry_price - trailing_stop
                return ExitResult(
                    exit_price=trailing_stop,
                    pnl_points=pnl,
                    exit_reason="ATR_TRAIL_HIT",
                    exit_candle_index=i,
                )

    # EOD close
    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def _compute_ema_on_path(candle_path: list[dict], period: int = 9) -> list[float]:
    """Compute EMA series on the candle path closes."""
    if len(candle_path) < period:
        return []

    closes = [c["close"] for c in candle_path]
    k = 2.0 / (period + 1)

    ema_series = []
    ema_val = sum(closes[:period]) / period

    # Fill first period-1 with 0 (not enough data)
    for _ in range(period - 1):
        ema_series.append(0.0)
    ema_series.append(ema_val)

    for close in closes[period:]:
        ema_val = close * k + ema_val * (1 - k)
        ema_series.append(ema_val)

    return ema_series


def simulate_ema_trail(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    ema_period: int = 9,
) -> ExitResult:
    """
    EMA trailing exit.

    Initial SL: 1.5 ATR from entry.
    After EMA stabilizes: exit when candle close crosses EMA.
    LONG exits when close < EMA. SHORT exits when close > EMA.
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    # Initial stop (before EMA kicks in)
    if direction == "LONG":
        initial_stop = entry_price - (1.5 * atr_at_entry)
    else:
        initial_stop = entry_price + (1.5 * atr_at_entry)

    ema_values = _compute_ema_on_path(candle_path, ema_period)

    for i, candle in enumerate(candle_path):
        close = candle["close"]

        # Check initial stop always
        if direction == "LONG" and candle["low"] <= initial_stop:
            return ExitResult(
                exit_price=initial_stop,
                pnl_points=initial_stop - entry_price,
                exit_reason="EMA_INITIAL_SL",
                exit_candle_index=i,
            )
        elif direction == "SHORT" and candle["high"] >= initial_stop:
            return ExitResult(
                exit_price=initial_stop,
                pnl_points=entry_price - initial_stop,
                exit_reason="EMA_INITIAL_SL",
                exit_candle_index=i,
            )

        # EMA trail (only after period stabilizes)
        if i < len(ema_values) and ema_values[i] > 0:
            ema_val = ema_values[i]
            if direction == "LONG" and close < ema_val:
                pnl = close - entry_price
                return ExitResult(
                    exit_price=close,
                    pnl_points=pnl,
                    exit_reason="EMA_TRAIL_EXIT",
                    exit_candle_index=i,
                )
            elif direction == "SHORT" and close > ema_val:
                pnl = entry_price - close
                return ExitResult(
                    exit_price=close,
                    pnl_points=pnl,
                    exit_reason="EMA_TRAIL_EXIT",
                    exit_candle_index=i,
                )

    # EOD close
    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_swing_trail(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    swing_lookback: int = 3,
) -> ExitResult:
    """
    Swing-based trailing stop.

    Trailing stop moves to each new swing low (LONG) or swing high (SHORT).
    A swing low = lowest low of last N candles.
    Initial SL: 1.5 ATR from entry.
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    if direction == "LONG":
        trailing_stop = entry_price - (1.5 * atr_at_entry)
    else:
        trailing_stop = entry_price + (1.5 * atr_at_entry)

    for i, candle in enumerate(candle_path):
        # Update swing trail after we have enough bars
        if i >= swing_lookback:
            lookback_candles = candle_path[i - swing_lookback:i]
            if direction == "LONG":
                swing_low = min(c["low"] for c in lookback_candles)
                trailing_stop = max(trailing_stop, swing_low)
            else:
                swing_high = max(c["high"] for c in lookback_candles)
                trailing_stop = min(trailing_stop, swing_high)

        # Check stop
        if direction == "LONG" and candle["low"] <= trailing_stop:
            return ExitResult(
                exit_price=trailing_stop,
                pnl_points=trailing_stop - entry_price,
                exit_reason="SWING_TRAIL_HIT",
                exit_candle_index=i,
            )
        elif direction == "SHORT" and candle["high"] >= trailing_stop:
            return ExitResult(
                exit_price=trailing_stop,
                pnl_points=entry_price - trailing_stop,
                exit_reason="SWING_TRAIL_HIT",
                exit_candle_index=i,
            )

    # EOD close
    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_all_trails(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
) -> dict[str, float]:
    """
    Run all trailing models.

    Returns:
        {"atr_trail": 45.0, "ema_trail": 30.0, "swing_trail": 25.0}
    """
    atr_result = simulate_atr_trail(entry_price, direction, atr_at_entry, candle_path)
    ema_result = simulate_ema_trail(entry_price, direction, atr_at_entry, candle_path)
    swing_result = simulate_swing_trail(entry_price, direction, atr_at_entry, candle_path)

    return {
        "atr_trail": atr_result.pnl_points,
        "ema_trail": ema_result.pnl_points,
        "swing_trail": swing_result.pnl_points,
    }
