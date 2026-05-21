"""
Technical indicator library.

Pure functions that compute indicators from Candle data.
Used by all strategies. No side effects, no state.
"""

from __future__ import annotations

from app.core.models import Candle


def sma(values: list[float], period: int) -> float | None:
    """Simple Moving Average over the last `period` values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: list[float], period: int) -> float | None:
    """
    Exponential Moving Average over all provided values.
    Uses the standard smoothing factor: k = 2 / (period + 1).
    Returns the final EMA value.
    """
    if len(values) < period:
        return None

    # Seed with SMA of first `period` values
    k = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period

    for price in values[period:]:
        ema_val = price * k + ema_val * (1 - k)

    return ema_val


def ema_series(values: list[float], period: int) -> list[float]:
    """
    Compute full EMA series. Returns list same length as input
    (first `period-1` entries are 0.0 placeholders).
    """
    if len(values) < period:
        return []

    k = 2.0 / (period + 1)
    result = [0.0] * (period - 1)

    # Seed
    ema_val = sum(values[:period]) / period
    result.append(ema_val)

    for price in values[period:]:
        ema_val = price * k + ema_val * (1 - k)
        result.append(ema_val)

    return result


def rsi(closes: list[float], period: int = 14) -> float | None:
    """
    Relative Strength Index (Wilder's smoothing).
    Needs at least `period + 1` values.
    """
    if len(closes) < period + 1:
        return None

    # Calculate price changes
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Initial average gain/loss from first `period` changes
    gains = [max(c, 0) for c in changes[:period]]
    losses = [abs(min(c, 0)) for c in changes[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Smooth with Wilder's method for remaining changes
    for change in changes[period:]:
        gain = max(change, 0)
        loss = abs(min(change, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: list[Candle], period: int = 14) -> float | None:
    """
    Average True Range (Wilder's smoothing).
    Needs at least `period + 1` candles.
    """
    if len(candles) < period + 1:
        return None

    # Calculate True Range for each candle (starting from index 1)
    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    # Initial ATR = simple average of first `period` TRs
    atr_val = sum(true_ranges[:period]) / period

    # Smooth with Wilder's method
    for tr in true_ranges[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period

    return atr_val


def atr_series(candles: list[Candle], period: int = 14) -> list[float]:
    """
    Compute ATR series. Returns list of ATR values aligned with candles
    (first `period` entries are 0.0).
    """
    if len(candles) < period + 1:
        return []

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    # First `period` candles have no ATR
    result = [0.0] * period

    atr_val = sum(true_ranges[:period]) / period
    result.append(atr_val)

    for tr in true_ranges[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
        result.append(atr_val)

    return result


def vwap(candles: list[Candle]) -> float | None:
    """
    Volume-Weighted Average Price.
    Computed from all provided candles (caller should pass today's candles only).
    Returns None if no volume.
    """
    cumulative_tp_vol = 0.0
    cumulative_vol = 0

    for c in candles:
        typical_price = (c.high + c.low + c.close) / 3.0
        cumulative_tp_vol += typical_price * c.volume
        cumulative_vol += c.volume

    if cumulative_vol == 0:
        return None

    return cumulative_tp_vol / cumulative_vol


def adx(candles: list[Candle], period: int = 14) -> float | None:
    """
    Average Directional Index.
    Needs at least `2 * period + 1` candles for reliable output.
    """
    if len(candles) < 2 * period + 1:
        return None

    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []
    tr_list: list[float] = []

    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_high = candles[i - 1].high
        prev_low = candles[i - 1].low
        prev_close = candles[i - 1].close

        # Directional Movement
        up_move = high - prev_high
        down_move = prev_low - low

        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0

        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

        # True Range
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_list.append(tr)

    # Wilder's smoothing for +DM, -DM, TR
    def wilder_smooth(values: list[float], p: int) -> list[float]:
        if len(values) < p:
            return []
        smoothed = [sum(values[:p])]
        for v in values[p:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / p) + v)
        return smoothed

    smooth_plus_dm = wilder_smooth(plus_dm_list, period)
    smooth_minus_dm = wilder_smooth(minus_dm_list, period)
    smooth_tr = wilder_smooth(tr_list, period)

    if not smooth_tr or not smooth_plus_dm or not smooth_minus_dm:
        return None

    # Calculate DI+ and DI-
    dx_list: list[float] = []
    for i in range(len(smooth_tr)):
        if smooth_tr[i] == 0:
            continue
        plus_di = 100.0 * smooth_plus_dm[i] / smooth_tr[i]
        minus_di = 100.0 * smooth_minus_dm[i] / smooth_tr[i]

        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_list.append(0.0)
        else:
            dx = 100.0 * abs(plus_di - minus_di) / di_sum
            dx_list.append(dx)

    if len(dx_list) < period:
        return None

    # ADX = Wilder's smoothed DX
    adx_val = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period

    return adx_val


def bollinger_bands(
    candles: list[Candle],
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[float, float, float] | None:
    """
    Bollinger Bands.

    Returns:
        (upper_band, middle_band, lower_band) or None if insufficient data.
    """
    if len(candles) < period:
        return None

    closes = [c.close for c in candles[-period:]]
    middle = sum(closes) / period

    # Standard deviation
    variance = sum((c - middle) ** 2 for c in closes) / period
    std = variance ** 0.5

    upper = middle + std_dev * std
    lower = middle - std_dev * std

    return (upper, middle, lower)


def bb_width(candles: list[Candle], period: int = 20, std_dev: float = 2.0) -> float | None:
    """
    Bollinger Band Width = (Upper - Lower) / Middle.
    Measures how compressed the bands are. Lower = tighter squeeze.
    """
    result = bollinger_bands(candles, period, std_dev)
    if result is None:
        return None
    upper, middle, lower = result
    if middle == 0:
        return None
    return (upper - lower) / middle


def bb_width_series(
    candles: list[Candle], period: int = 20, std_dev: float = 2.0, lookback: int = 20
) -> list[float]:
    """
    Compute BB Width for the last `lookback` candles.
    Returns list of width values (most recent last).
    """
    if len(candles) < period + lookback:
        return []

    widths: list[float] = []
    for i in range(lookback):
        end_idx = len(candles) - lookback + i + 1
        subset = candles[:end_idx]
        w = bb_width(subset, period, std_dev)
        widths.append(w if w is not None else 0.0)

    return widths


def keltner_channels(
    candles: list[Candle],
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 1.5,
) -> tuple[float, float, float] | None:
    """
    Keltner Channels (used for TTM Squeeze detection).

    Returns:
        (upper_kc, middle_kc, lower_kc) or None.
    """
    if len(candles) < max(ema_period, atr_period + 1):
        return None

    closes = [c.close for c in candles]
    middle = ema(closes, ema_period)
    if middle is None:
        return None

    atr_val = atr(candles, atr_period)
    if atr_val is None:
        return None

    upper = middle + multiplier * atr_val
    lower = middle - multiplier * atr_val

    return (upper, middle, lower)


def is_squeeze(
    candles: list[Candle],
    bb_period: int = 20,
    bb_std: float = 2.0,
    kc_ema_period: int = 20,
    kc_atr_period: int = 10,
    kc_multiplier: float = 1.5,
) -> bool | None:
    """
    TTM Squeeze detection: Bollinger Bands inside Keltner Channels.
    Returns True if squeeze is active, False if not, None if insufficient data.
    """
    bb = bollinger_bands(candles, bb_period, bb_std)
    kc = keltner_channels(candles, kc_ema_period, kc_atr_period, kc_multiplier)

    if bb is None or kc is None:
        return None

    bb_upper, _, bb_lower = bb
    kc_upper, _, kc_lower = kc

    # Squeeze = BB inside KC
    return bb_upper < kc_upper and bb_lower > kc_lower


def supertrend(
    candles: list[Candle],
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> tuple[float, bool] | None:
    """
    SuperTrend indicator.

    Returns:
        (supertrend_value, is_uptrend) or None if insufficient data.
        is_uptrend=True means price is above SuperTrend (bullish).
    """
    if len(candles) < atr_period + 2:
        return None

    # Compute ATR series
    atr_vals = atr_series(candles, atr_period)
    if not atr_vals or len(atr_vals) != len(candles):
        return None

    # Initialize SuperTrend
    upper_band = [0.0] * len(candles)
    lower_band = [0.0] * len(candles)
    st_values = [0.0] * len(candles)
    direction = [True] * len(candles)  # True = uptrend

    # Start computing from atr_period index
    start = atr_period
    for i in range(start, len(candles)):
        hl2 = (candles[i].high + candles[i].low) / 2.0
        atr_val = atr_vals[i]

        basic_upper = hl2 + multiplier * atr_val
        basic_lower = hl2 - multiplier * atr_val

        # Final upper band: take min with previous (band only moves down)
        if i == start:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
        else:
            upper_band[i] = (
                min(basic_upper, upper_band[i - 1])
                if candles[i - 1].close <= upper_band[i - 1]
                else basic_upper
            )
            lower_band[i] = (
                max(basic_lower, lower_band[i - 1])
                if candles[i - 1].close >= lower_band[i - 1]
                else basic_lower
            )

        # Determine direction
        if i == start:
            direction[i] = candles[i].close > upper_band[i]
        else:
            if direction[i - 1]:  # was uptrend
                direction[i] = candles[i].close >= lower_band[i]
            else:  # was downtrend
                direction[i] = candles[i].close > upper_band[i]

        # SuperTrend value
        st_values[i] = lower_band[i] if direction[i] else upper_band[i]

    return (st_values[-1], direction[-1])


def supertrend_with_prev(
    candles: list[Candle],
    atr_period: int = 10,
    multiplier: float = 3.0,
) -> tuple[float, bool, float, bool] | None:
    """
    SuperTrend with previous bar's values for flip detection.

    Returns:
        (current_st, current_is_uptrend, prev_st, prev_is_uptrend) or None.
    """
    if len(candles) < atr_period + 3:
        return None

    atr_vals = atr_series(candles, atr_period)
    if not atr_vals or len(atr_vals) != len(candles):
        return None

    upper_band = [0.0] * len(candles)
    lower_band = [0.0] * len(candles)
    st_values = [0.0] * len(candles)
    direction = [True] * len(candles)

    start = atr_period
    for i in range(start, len(candles)):
        hl2 = (candles[i].high + candles[i].low) / 2.0
        atr_val = atr_vals[i]

        basic_upper = hl2 + multiplier * atr_val
        basic_lower = hl2 - multiplier * atr_val

        if i == start:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
        else:
            upper_band[i] = (
                min(basic_upper, upper_band[i - 1])
                if candles[i - 1].close <= upper_band[i - 1]
                else basic_upper
            )
            lower_band[i] = (
                max(basic_lower, lower_band[i - 1])
                if candles[i - 1].close >= lower_band[i - 1]
                else basic_lower
            )

        if i == start:
            direction[i] = candles[i].close > upper_band[i]
        else:
            if direction[i - 1]:
                direction[i] = candles[i].close >= lower_band[i]
            else:
                direction[i] = candles[i].close > upper_band[i]

        st_values[i] = lower_band[i] if direction[i] else upper_band[i]

    return (
        st_values[-1],
        direction[-1],
        st_values[-2],
        direction[-2],
    )
