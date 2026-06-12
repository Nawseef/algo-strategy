"""
Stop loss exit models.

Three stop loss placement strategies:
1. ATR Stop — SL at 1.5 × ATR from entry (adaptive to volatility)
2. Swing Stop — SL at the last swing low/high before entry
3. Fixed Stop — SL at a fixed percentage from entry (0.5%)

Each model walks the candle path and exits when SL is hit,
or closes at EOD if SL is never triggered.
The "profit" comes from the trade running to EOD without stopping out.
"""

from __future__ import annotations

from app.exit_engine.models.rr_exit import ExitResult


def _find_swing_level(candle_path_before_entry: list[dict], direction: str) -> float | None:
    """
    Find the most recent swing point before entry.
    For LONG: swing low (lowest low in last 5 candles before entry)
    For SHORT: swing high (highest high in last 5 candles before entry)
    """
    if not candle_path_before_entry:
        return None

    lookback = candle_path_before_entry[-5:]  # last 5 candles before entry

    if direction == "LONG":
        return min(c["low"] for c in lookback)
    else:
        return max(c["high"] for c in lookback)


def simulate_atr_stop(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    multiplier: float = 1.5,
) -> ExitResult:
    """
    ATR-based stop loss. SL = entry ± (multiplier × ATR).
    No take profit — exits at SL or EOD close.
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    if direction == "LONG":
        stop_loss = entry_price - (multiplier * atr_at_entry)
    else:
        stop_loss = entry_price + (multiplier * atr_at_entry)

    for i, candle in enumerate(candle_path):
        if direction == "LONG" and candle["low"] <= stop_loss:
            return ExitResult(
                exit_price=stop_loss,
                pnl_points=stop_loss - entry_price,
                exit_reason="ATR_SL_HIT",
                exit_candle_index=i,
            )
        elif direction == "SHORT" and candle["high"] >= stop_loss:
            return ExitResult(
                exit_price=stop_loss,
                pnl_points=entry_price - stop_loss,
                exit_reason="ATR_SL_HIT",
                exit_candle_index=i,
            )

    # EOD close
    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_swing_stop(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
    candles_before_entry: list[dict] | None = None,
) -> ExitResult:
    """
    Swing-based stop loss. SL placed at last swing low/high.
    If no swing found, falls back to 1% from entry.
    """
    swing_level = None
    if candles_before_entry:
        swing_level = _find_swing_level(candles_before_entry, direction)

    if swing_level is None:
        # Fallback: 1% stop
        if direction == "LONG":
            swing_level = entry_price * 0.99
        else:
            swing_level = entry_price * 1.01

    for i, candle in enumerate(candle_path):
        if direction == "LONG" and candle["low"] <= swing_level:
            return ExitResult(
                exit_price=swing_level,
                pnl_points=swing_level - entry_price,
                exit_reason="SWING_SL_HIT",
                exit_candle_index=i,
            )
        elif direction == "SHORT" and candle["high"] >= swing_level:
            return ExitResult(
                exit_price=swing_level,
                pnl_points=entry_price - swing_level,
                exit_reason="SWING_SL_HIT",
                exit_candle_index=i,
            )

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_fixed_stop(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
    stop_pct: float = 0.005,
) -> ExitResult:
    """
    Fixed percentage stop loss. SL = entry ± (stop_pct × entry).
    Default: 0.5% from entry.
    """
    if direction == "LONG":
        stop_loss = entry_price * (1.0 - stop_pct)
    else:
        stop_loss = entry_price * (1.0 + stop_pct)

    for i, candle in enumerate(candle_path):
        if direction == "LONG" and candle["low"] <= stop_loss:
            return ExitResult(
                exit_price=stop_loss,
                pnl_points=stop_loss - entry_price,
                exit_reason="FIXED_SL_HIT",
                exit_candle_index=i,
            )
        elif direction == "SHORT" and candle["high"] >= stop_loss:
            return ExitResult(
                exit_price=stop_loss,
                pnl_points=entry_price - stop_loss,
                exit_reason="FIXED_SL_HIT",
                exit_candle_index=i,
            )

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_all_stops(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    candles_before_entry: list[dict] | None = None,
) -> dict[str, float]:
    """
    Run all stop loss models.

    Returns dict:
        {"atr_stop": -22.5, "swing_stop": -15.0, "fixed_stop": -12.5}
    """
    atr_result = simulate_atr_stop(entry_price, direction, atr_at_entry, candle_path)
    swing_result = simulate_swing_stop(entry_price, direction, candle_path, candles_before_entry)
    fixed_result = simulate_fixed_stop(entry_price, direction, candle_path)

    return {
        "atr_stop": atr_result.pnl_points,
        "swing_stop": swing_result.pnl_points,
        "fixed_stop": fixed_result.pnl_points,
    }
