"""
Indicator-based exit models.

These test exits based on indicator state changes (not just price level):

1. VWAP Cross — exit when price crosses VWAP from the profitable side
2. EMA Cross exits — exit when price crosses EMA20 or EMA50
3. RSI Extreme — exit when RSI reaches overbought/oversold
4. Bollinger Band — exit when price touches opposite band
5. ATR Expansion — exit when ATR expands beyond threshold (volatility spike)
6. Multi-EMA — exit when short EMA crosses below long EMA (bearish crossover)

Note: These compute indicators from the candle path itself (not from snapshot),
since we need to track the indicator throughout the trade's life.
"""

from __future__ import annotations

from app.exit_engine.models.rr_exit import ExitResult


def _ema_on_closes(closes: list[float], period: int) -> list[float]:
    """Compute EMA series from close prices."""
    if len(closes) < period:
        return [0.0] * len(closes)

    k = 2.0 / (period + 1)
    result = [0.0] * (period - 1)
    ema_val = sum(closes[:period]) / period
    result.append(ema_val)

    for close in closes[period:]:
        ema_val = close * k + ema_val * (1 - k)
        result.append(ema_val)

    return result


def _rsi_on_closes(closes: list[float], period: int = 14) -> list[float]:
    """Compute RSI series from close prices."""
    if len(closes) < period + 1:
        return [50.0] * len(closes)

    result = [50.0] * period  # pad first N with neutral

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes[:period]]
    losses = [abs(min(c, 0)) for c in changes[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for change in changes[period:]:
        gain = max(change, 0)
        loss = abs(min(change, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100.0 - (100.0 / (1.0 + rs)))

    return result


def simulate_vwap_cross_exit(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
) -> ExitResult:
    """
    Exit when price crosses VWAP from profitable side.
    LONG: exit when close drops below VWAP.
    SHORT: exit when close rises above VWAP.
    Uses cumulative VWAP computed from the candle path.
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    # Initial SL for safety
    if direction == "LONG":
        initial_sl = entry_price - (1.5 * atr_at_entry)
    else:
        initial_sl = entry_price + (1.5 * atr_at_entry)

    # Compute running VWAP
    cum_tp_vol = 0.0
    cum_vol = 0

    for i, candle in enumerate(candle_path):
        tp = (candle["high"] + candle["low"] + candle["close"]) / 3.0
        vol = candle.get("volume", 1000)
        cum_tp_vol += tp * vol
        cum_vol += vol
        vwap_val = cum_tp_vol / cum_vol if cum_vol > 0 else entry_price

        close = candle["close"]

        # Check initial SL
        if direction == "LONG" and candle["low"] <= initial_sl:
            return ExitResult(exit_price=initial_sl, pnl_points=initial_sl - entry_price, exit_reason="VWAP_SL_HIT", exit_candle_index=i)
        elif direction == "SHORT" and candle["high"] >= initial_sl:
            return ExitResult(exit_price=initial_sl, pnl_points=entry_price - initial_sl, exit_reason="VWAP_SL_HIT", exit_candle_index=i)

        # VWAP cross (only after first few candles for VWAP to stabilize)
        if i >= 3:
            if direction == "LONG" and close < vwap_val:
                pnl = close - entry_price
                return ExitResult(exit_price=close, pnl_points=pnl, exit_reason="VWAP_CROSS_EXIT", exit_candle_index=i)
            elif direction == "SHORT" and close > vwap_val:
                pnl = entry_price - close
                return ExitResult(exit_price=close, pnl_points=pnl, exit_reason="VWAP_CROSS_EXIT", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_ema_cross_exit(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    ema_period: int = 20,
) -> ExitResult:
    """Exit when close crosses EMA(N) against the trade direction."""
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    if direction == "LONG":
        initial_sl = entry_price - (1.5 * atr_at_entry)
    else:
        initial_sl = entry_price + (1.5 * atr_at_entry)

    closes = [c["close"] for c in candle_path]
    ema_vals = _ema_on_closes(closes, ema_period)

    for i, candle in enumerate(candle_path):
        close = candle["close"]

        # Initial SL
        if direction == "LONG" and candle["low"] <= initial_sl:
            return ExitResult(exit_price=initial_sl, pnl_points=initial_sl - entry_price, exit_reason="EMA_CROSS_SL", exit_candle_index=i)
        elif direction == "SHORT" and candle["high"] >= initial_sl:
            return ExitResult(exit_price=initial_sl, pnl_points=entry_price - initial_sl, exit_reason="EMA_CROSS_SL", exit_candle_index=i)

        # EMA cross
        if i < len(ema_vals) and ema_vals[i] > 0:
            if direction == "LONG" and close < ema_vals[i]:
                pnl = close - entry_price
                return ExitResult(exit_price=close, pnl_points=pnl, exit_reason=f"EMA{ema_period}_CROSS", exit_candle_index=i)
            elif direction == "SHORT" and close > ema_vals[i]:
                pnl = entry_price - close
                return ExitResult(exit_price=close, pnl_points=pnl, exit_reason=f"EMA{ema_period}_CROSS", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_rsi_extreme_exit(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    rsi_exit_level: float = 70.0,
) -> ExitResult:
    """
    Exit when RSI reaches an extreme on the profitable side.
    LONG: exit when RSI > rsi_exit_level (overbought = take profit).
    SHORT: exit when RSI < (100 - rsi_exit_level) (oversold = take profit).
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    if direction == "LONG":
        initial_sl = entry_price - (1.5 * atr_at_entry)
    else:
        initial_sl = entry_price + (1.5 * atr_at_entry)

    closes = [c["close"] for c in candle_path]
    rsi_vals = _rsi_on_closes(closes, 14)

    short_exit_level = 100.0 - rsi_exit_level  # e.g. 30 for 70

    for i, candle in enumerate(candle_path):
        # Initial SL
        if direction == "LONG" and candle["low"] <= initial_sl:
            return ExitResult(exit_price=initial_sl, pnl_points=initial_sl - entry_price, exit_reason="RSI_SL_HIT", exit_candle_index=i)
        elif direction == "SHORT" and candle["high"] >= initial_sl:
            return ExitResult(exit_price=initial_sl, pnl_points=entry_price - initial_sl, exit_reason="RSI_SL_HIT", exit_candle_index=i)

        # RSI extreme exit
        if i < len(rsi_vals):
            close = candle["close"]
            if direction == "LONG" and rsi_vals[i] >= rsi_exit_level:
                pnl = close - entry_price
                return ExitResult(exit_price=close, pnl_points=pnl, exit_reason=f"RSI_{rsi_exit_level}_EXIT", exit_candle_index=i)
            elif direction == "SHORT" and rsi_vals[i] <= short_exit_level:
                pnl = entry_price - close
                return ExitResult(exit_price=close, pnl_points=pnl, exit_reason=f"RSI_{short_exit_level}_EXIT", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_ema_crossover_exit(
    entry_price: float,
    direction: str,
    atr_at_entry: float,
    candle_path: list[dict],
    fast_period: int = 9,
    slow_period: int = 21,
) -> ExitResult:
    """
    Exit when fast EMA crosses slow EMA against trade direction.
    LONG: exit when EMA9 < EMA21 (bearish crossover).
    SHORT: exit when EMA9 > EMA21 (bullish crossover).
    """
    if atr_at_entry <= 0:
        atr_at_entry = entry_price * 0.005

    if direction == "LONG":
        initial_sl = entry_price - (1.5 * atr_at_entry)
    else:
        initial_sl = entry_price + (1.5 * atr_at_entry)

    closes = [c["close"] for c in candle_path]
    fast_ema = _ema_on_closes(closes, fast_period)
    slow_ema = _ema_on_closes(closes, slow_period)

    for i, candle in enumerate(candle_path):
        # Initial SL
        if direction == "LONG" and candle["low"] <= initial_sl:
            return ExitResult(exit_price=initial_sl, pnl_points=initial_sl - entry_price, exit_reason="XOVER_SL_HIT", exit_candle_index=i)
        elif direction == "SHORT" and candle["high"] >= initial_sl:
            return ExitResult(exit_price=initial_sl, pnl_points=entry_price - initial_sl, exit_reason="XOVER_SL_HIT", exit_candle_index=i)

        # EMA crossover
        if i < len(fast_ema) and i < len(slow_ema) and fast_ema[i] > 0 and slow_ema[i] > 0:
            close = candle["close"]
            if direction == "LONG" and fast_ema[i] < slow_ema[i]:
                pnl = close - entry_price
                return ExitResult(exit_price=close, pnl_points=pnl, exit_reason="BEARISH_XOVER_EXIT", exit_candle_index=i)
            elif direction == "SHORT" and fast_ema[i] > slow_ema[i]:
                pnl = entry_price - close
                return ExitResult(exit_price=close, pnl_points=pnl, exit_reason="BULLISH_XOVER_EXIT", exit_candle_index=i)

    last_close = candle_path[-1]["close"] if candle_path else entry_price
    pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
    return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_all_indicator_exits(
    entry_price: float, direction: str, atr_at_entry: float, candle_path: list[dict],
) -> dict[str, float]:
    """
    Run all indicator-based exit models.

    Returns 10 results:
        vwap_cross, ema20_cross, ema50_cross,
        rsi_70_exit, rsi_75_exit, rsi_80_exit,
        ema_9_21_xover, ema_9_50_xover,
        ema9_cross, ema13_cross
    """
    results: dict[str, float] = {}

    # VWAP cross
    results["vwap_cross"] = simulate_vwap_cross_exit(entry_price, direction, atr_at_entry, candle_path).pnl_points

    # EMA cross exits (different periods)
    results["ema9_cross"] = simulate_ema_cross_exit(entry_price, direction, atr_at_entry, candle_path, 9).pnl_points
    results["ema13_cross"] = simulate_ema_cross_exit(entry_price, direction, atr_at_entry, candle_path, 13).pnl_points
    results["ema20_cross"] = simulate_ema_cross_exit(entry_price, direction, atr_at_entry, candle_path, 20).pnl_points
    results["ema50_cross"] = simulate_ema_cross_exit(entry_price, direction, atr_at_entry, candle_path, 50).pnl_points

    # RSI extreme exits
    results["rsi_70_exit"] = simulate_rsi_extreme_exit(entry_price, direction, atr_at_entry, candle_path, 70.0).pnl_points
    results["rsi_75_exit"] = simulate_rsi_extreme_exit(entry_price, direction, atr_at_entry, candle_path, 75.0).pnl_points
    results["rsi_80_exit"] = simulate_rsi_extreme_exit(entry_price, direction, atr_at_entry, candle_path, 80.0).pnl_points

    # EMA crossover exits
    results["ema_9_21_xover"] = simulate_ema_crossover_exit(entry_price, direction, atr_at_entry, candle_path, 9, 21).pnl_points
    results["ema_9_50_xover"] = simulate_ema_crossover_exit(entry_price, direction, atr_at_entry, candle_path, 9, 50).pnl_points

    return results
