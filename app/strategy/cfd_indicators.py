"""
CFD Technical Indicator Library.

Pure functions for intraday CFD trading on forex, metals, indices, and oil.
No side effects, no state. All functions operate on list[Candle] or list[float].

Indicators included:
  1. MACD (Moving Average Convergence Divergence)
  2. Stochastic Oscillator (%K, %D)
  3. Ichimoku Cloud (Tenkan, Kijun, Senkou A/B, Chikou)
  4. Fair Value Gap (FVG) detection
  5. Order Block detection
  6. Market Structure (swing highs/lows, BOS, CHoCH)
  7. Session / Kill Zone awareness (CFD/forex sessions)
  8. Fibonacci retracement levels

All functions return None (or empty list) when there is insufficient data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from app.core.models import Candle


# ═══════════════════════════════════════════════════════════════════════════════
# 1. MACD — Moving Average Convergence Divergence
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MACDResult:
    """MACD computation result."""

    macd_line: float  # Fast EMA - Slow EMA
    signal_line: float  # EMA of MACD line
    histogram: float  # MACD line - Signal line


def macd(
    closes: list[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> MACDResult | None:
    """
    Compute MACD (Moving Average Convergence Divergence).

    Standard parameters: fast=12, slow=26, signal=9.
    Requires at least slow_period + signal_period - 1 data points for a
    meaningful signal line (the signal EMA needs signal_period MACD values).

    Formula:
        MACD Line   = EMA(fast) - EMA(slow)
        Signal Line = EMA(signal) of the MACD Line series
        Histogram   = MACD Line - Signal Line
    """
    min_required = slow_period + signal_period - 1
    if len(closes) < min_required:
        return None

    # Compute full EMA series for fast and slow
    fast_ema = _ema_series(closes, fast_period)
    slow_ema = _ema_series(closes, slow_period)

    if not fast_ema or not slow_ema:
        return None

    # MACD line series (aligned from slow_period - 1 onward where both EMAs exist)
    # slow_ema starts producing values at index slow_period - 1
    # fast_ema starts producing values at index fast_period - 1
    # So MACD values start at index slow_period - 1
    macd_start = slow_period - 1
    macd_series: list[float] = []
    for i in range(macd_start, len(closes)):
        macd_series.append(fast_ema[i] - slow_ema[i])

    if len(macd_series) < signal_period:
        return None

    # Signal line = EMA of MACD series
    signal_ema = _ema_series(macd_series, signal_period)
    if not signal_ema:
        return None

    macd_val = macd_series[-1]
    signal_val = signal_ema[-1]
    histogram = macd_val - signal_val

    return MACDResult(macd_line=macd_val, signal_line=signal_val, histogram=histogram)


def macd_series(
    closes: list[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> list[MACDResult]:
    """
    Compute MACD series for the full history.
    Returns list of MACDResult for each bar where all values are available.
    First valid result appears at index slow_period + signal_period - 2.
    """
    min_required = slow_period + signal_period - 1
    if len(closes) < min_required:
        return []

    fast_ema = _ema_series(closes, fast_period)
    slow_ema = _ema_series(closes, slow_period)

    if not fast_ema or not slow_ema:
        return []

    # MACD line from slow_period - 1 onward
    macd_start = slow_period - 1
    macd_values: list[float] = []
    for i in range(macd_start, len(closes)):
        macd_values.append(fast_ema[i] - slow_ema[i])

    if len(macd_values) < signal_period:
        return []

    signal_ema = _ema_series(macd_values, signal_period)
    if not signal_ema:
        return []

    # Signal starts at signal_period - 1 within macd_values
    signal_start = signal_period - 1
    results: list[MACDResult] = []
    for i in range(signal_start, len(macd_values)):
        m = macd_values[i]
        s = signal_ema[i]
        results.append(MACDResult(macd_line=m, signal_line=s, histogram=m - s))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Stochastic Oscillator
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class StochasticResult:
    """Stochastic Oscillator result."""

    k: float  # %K line (0-100)
    d: float  # %D line (0-100, SMA of %K)


def stochastic(
    candles: list[Candle],
    k_period: int = 14,
    k_smooth: int = 3,
    d_period: int = 3,
) -> StochasticResult | None:
    """
    Compute Stochastic Oscillator (%K and %D).

    The "slow stochastic" variant (most commonly used):
        Raw %K = (Close - Lowest Low over k_period) / (Highest High - Lowest Low) * 100
        %K     = SMA(Raw %K, k_smooth)   (smoothed — this is the "slow %K")
        %D     = SMA(%K, d_period)

    Requires at least k_period + k_smooth + d_period - 2 candles.
    """
    min_required = k_period + k_smooth + d_period - 2
    if len(candles) < min_required:
        return None

    # Compute raw %K for each bar from k_period-1 onward
    raw_k_values: list[float] = []
    for i in range(k_period - 1, len(candles)):
        window = candles[i - k_period + 1: i + 1]
        highest = max(c.high for c in window)
        lowest = min(c.low for c in window)
        denom = highest - lowest
        if denom == 0:
            raw_k_values.append(50.0)  # Flat market — neutral
        else:
            raw_k_values.append((candles[i].close - lowest) / denom * 100.0)

    if len(raw_k_values) < k_smooth:
        return None

    # Smooth raw %K with SMA to get slow %K
    slow_k_values: list[float] = []
    for i in range(k_smooth - 1, len(raw_k_values)):
        avg = sum(raw_k_values[i - k_smooth + 1: i + 1]) / k_smooth
        slow_k_values.append(avg)

    if len(slow_k_values) < d_period:
        return None

    # %D = SMA of slow %K
    d_values: list[float] = []
    for i in range(d_period - 1, len(slow_k_values)):
        avg = sum(slow_k_values[i - d_period + 1: i + 1]) / d_period
        d_values.append(avg)

    if not d_values:
        return None

    return StochasticResult(k=slow_k_values[-1], d=d_values[-1])


def stochastic_series(
    candles: list[Candle],
    k_period: int = 14,
    k_smooth: int = 3,
    d_period: int = 3,
) -> list[StochasticResult]:
    """
    Compute full Stochastic series. Returns list of results
    starting from the first bar where both %K and %D are valid.
    """
    min_required = k_period + k_smooth + d_period - 2
    if len(candles) < min_required:
        return []

    raw_k_values: list[float] = []
    for i in range(k_period - 1, len(candles)):
        window = candles[i - k_period + 1: i + 1]
        highest = max(c.high for c in window)
        lowest = min(c.low for c in window)
        denom = highest - lowest
        if denom == 0:
            raw_k_values.append(50.0)
        else:
            raw_k_values.append((candles[i].close - lowest) / denom * 100.0)

    if len(raw_k_values) < k_smooth:
        return []

    slow_k_values: list[float] = []
    for i in range(k_smooth - 1, len(raw_k_values)):
        avg = sum(raw_k_values[i - k_smooth + 1: i + 1]) / k_smooth
        slow_k_values.append(avg)

    if len(slow_k_values) < d_period:
        return []

    results: list[StochasticResult] = []
    for i in range(d_period - 1, len(slow_k_values)):
        d_val = sum(slow_k_values[i - d_period + 1: i + 1]) / d_period
        results.append(StochasticResult(k=slow_k_values[i], d=d_val))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Ichimoku Cloud
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class IchimokuResult:
    """Ichimoku Cloud computation result for the CURRENT bar."""

    tenkan_sen: float  # Conversion Line (9-period midpoint)
    kijun_sen: float  # Base Line (26-period midpoint)
    senkou_span_a: float  # Leading Span A (avg of Tenkan+Kijun, shifted 26 ahead)
    senkou_span_b: float  # Leading Span B (52-period midpoint, shifted 26 ahead)
    chikou_span: float  # Lagging Span (current close, plotted 26 bars back)

    # Current cloud boundaries at the current bar (from values plotted 26 bars ago)
    cloud_top: float  # max(senkou_a, senkou_b) at current position
    cloud_bottom: float  # min(senkou_a, senkou_b) at current position

    @property
    def price_above_cloud(self) -> bool:
        """True if the chikou (current close) is above the cloud."""
        return self.chikou_span > self.cloud_top

    @property
    def price_below_cloud(self) -> bool:
        """True if the chikou (current close) is below the cloud."""
        return self.chikou_span < self.cloud_bottom

    @property
    def cloud_is_bullish(self) -> bool:
        """True if Senkou A > Senkou B (green cloud)."""
        return self.senkou_span_a > self.senkou_span_b


def ichimoku(
    candles: list[Candle],
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
    displacement: int = 26,
) -> IchimokuResult | None:
    """
    Compute Ichimoku Cloud components.

    Formulas:
        Tenkan-sen   = (Highest High(9)  + Lowest Low(9))  / 2
        Kijun-sen    = (Highest High(26) + Lowest Low(26)) / 2
        Senkou Span A = (Tenkan + Kijun) / 2  [plotted 26 periods ahead]
        Senkou Span B = (Highest High(52) + Lowest Low(52)) / 2  [plotted 26 ahead]
        Chikou Span  = Current Close  [plotted 26 periods back]

    For the "current cloud" (the cloud at the current bar), we use
    the Senkou values that were computed 26 bars ago and displaced forward.

    Requires at least senkou_b_period + displacement candles for the full cloud
    at the current position.
    """
    # Need enough data for the cloud at the current bar
    min_required = max(senkou_b_period, kijun_period + displacement)
    if len(candles) < min_required:
        return None

    # Current bar
    current_idx = len(candles) - 1

    # Tenkan-sen (9-period midpoint of high/low)
    tenkan_window = candles[current_idx - tenkan_period + 1: current_idx + 1]
    tenkan = (max(c.high for c in tenkan_window) + min(c.low for c in tenkan_window)) / 2.0

    # Kijun-sen (26-period midpoint)
    kijun_window = candles[current_idx - kijun_period + 1: current_idx + 1]
    kijun = (max(c.high for c in kijun_window) + min(c.low for c in kijun_window)) / 2.0

    # Senkou Span A at current bar = (Tenkan + Kijun) / 2
    # This will be PLOTTED 26 bars ahead. For the future cloud.
    senkou_a = (tenkan + kijun) / 2.0

    # Senkou Span B at current bar = 52-period midpoint
    senkou_b_window = candles[current_idx - senkou_b_period + 1: current_idx + 1]
    senkou_b = (max(c.high for c in senkou_b_window) + min(c.low for c in senkou_b_window)) / 2.0

    # Chikou Span = current close (plotted 26 bars back, but value is just close)
    chikou = candles[current_idx].close

    # Current cloud at this bar = Senkou values computed `displacement` bars ago
    # (they were displaced forward to arrive at the current position)
    past_idx = current_idx - displacement
    if past_idx < max(tenkan_period, kijun_period, senkou_b_period) - 1:
        # Not enough history to have cloud values at current position
        cloud_top = senkou_a
        cloud_bottom = senkou_b
    else:
        # Tenkan/Kijun at past_idx
        past_tenkan_window = candles[past_idx - tenkan_period + 1: past_idx + 1]
        past_kijun_window = candles[past_idx - kijun_period + 1: past_idx + 1]
        past_tenkan = (max(c.high for c in past_tenkan_window) + min(c.low for c in past_tenkan_window)) / 2.0
        past_kijun = (max(c.high for c in past_kijun_window) + min(c.low for c in past_kijun_window)) / 2.0
        past_senkou_a = (past_tenkan + past_kijun) / 2.0

        past_senkou_b_start = past_idx - senkou_b_period + 1
        if past_senkou_b_start < 0:
            past_senkou_b = senkou_b
        else:
            past_sb_window = candles[past_senkou_b_start: past_idx + 1]
            past_senkou_b = (max(c.high for c in past_sb_window) + min(c.low for c in past_sb_window)) / 2.0

        cloud_top = max(past_senkou_a, past_senkou_b)
        cloud_bottom = min(past_senkou_a, past_senkou_b)

    return IchimokuResult(
        tenkan_sen=tenkan,
        kijun_sen=kijun,
        senkou_span_a=senkou_a,
        senkou_span_b=senkou_b,
        chikou_span=chikou,
        cloud_top=cloud_top,
        cloud_bottom=cloud_bottom,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Fair Value Gap (FVG)
# ═══════════════════════════════════════════════════════════════════════════════


class FVGDirection(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass
class FairValueGap:
    """A detected Fair Value Gap (3-candle imbalance)."""

    direction: FVGDirection
    top: float  # Upper boundary of the gap
    bottom: float  # Lower boundary of the gap
    midpoint: float  # (top + bottom) / 2 — common entry target
    candle_index: int  # Index of the middle candle (the impulsive one)
    timestamp_ms: float  # Timestamp of the middle candle

    @property
    def size(self) -> float:
        """Gap size in price units."""
        return self.top - self.bottom


def detect_fvg(candles: list[Candle], min_gap_atr_ratio: float = 0.0) -> list[FairValueGap]:
    """
    Detect all Fair Value Gaps in the candle series.

    A bullish FVG occurs when candle[i].low > candle[i-2].high
    (the wick of candle i doesn't overlap with candle i-2, leaving a gap).

    A bearish FVG occurs when candle[i].high < candle[i-2].low.

    Args:
        candles: List of candles (at least 3 required).
        min_gap_atr_ratio: Minimum gap size as fraction of recent range.
                           0.0 means detect all gaps (no filter).

    Returns:
        List of FairValueGap objects, ordered chronologically.
    """
    if len(candles) < 3:
        return []

    gaps: list[FairValueGap] = []

    for i in range(2, len(candles)):
        # Bullish FVG: gap between candle[i-2] high and candle[i] low
        if candles[i].low > candles[i - 2].high:
            gap_bottom = candles[i - 2].high
            gap_top = candles[i].low
            gap_size = gap_top - gap_bottom

            if min_gap_atr_ratio > 0:
                # Simple filter: gap must be > ratio of the middle candle's range
                mid_range = candles[i - 1].high - candles[i - 1].low
                if mid_range > 0 and gap_size / mid_range < min_gap_atr_ratio:
                    continue

            gaps.append(FairValueGap(
                direction=FVGDirection.BULLISH,
                top=gap_top,
                bottom=gap_bottom,
                midpoint=(gap_top + gap_bottom) / 2.0,
                candle_index=i - 1,
                timestamp_ms=candles[i - 1].timestamp_ms,
            ))

        # Bearish FVG: gap between candle[i] high and candle[i-2] low
        elif candles[i].high < candles[i - 2].low:
            gap_top = candles[i - 2].low
            gap_bottom = candles[i].high
            gap_size = gap_top - gap_bottom

            if min_gap_atr_ratio > 0:
                mid_range = candles[i - 1].high - candles[i - 1].low
                if mid_range > 0 and gap_size / mid_range < min_gap_atr_ratio:
                    continue

            gaps.append(FairValueGap(
                direction=FVGDirection.BEARISH,
                top=gap_top,
                bottom=gap_bottom,
                midpoint=(gap_top + gap_bottom) / 2.0,
                candle_index=i - 1,
                timestamp_ms=candles[i - 1].timestamp_ms,
            ))

    return gaps


def detect_unfilled_fvg(
    candles: list[Candle],
    min_gap_atr_ratio: float = 0.0,
) -> list[FairValueGap]:
    """
    Detect FVGs that have NOT been filled (price hasn't returned to close the gap).

    A bullish FVG is "filled" when a subsequent candle's low touches the gap bottom.
    A bearish FVG is "filled" when a subsequent candle's high touches the gap top.
    """
    all_gaps = detect_fvg(candles, min_gap_atr_ratio)
    unfilled: list[FairValueGap] = []

    for gap in all_gaps:
        filled = False
        # Check all candles after the FVG's third candle (candle_index + 1 onward)
        check_start = gap.candle_index + 2  # +2 because candle_index is the middle
        for j in range(check_start, len(candles)):
            if gap.direction == FVGDirection.BULLISH:
                # Filled if price dips into the gap (low <= gap top)
                if candles[j].low <= gap.bottom:
                    filled = True
                    break
            else:
                # Filled if price rises into the gap (high >= gap bottom)
                if candles[j].high >= gap.top:
                    filled = True
                    break

        if not filled:
            unfilled.append(gap)

    return unfilled


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Order Blocks
# ═══════════════════════════════════════════════════════════════════════════════


class OBDirection(Enum):
    BULLISH = "bullish"  # Last bearish candle before bullish displacement
    BEARISH = "bearish"  # Last bullish candle before bearish displacement


@dataclass
class OrderBlock:
    """A detected Order Block zone."""

    direction: OBDirection
    high: float  # Top of the OB zone
    low: float  # Bottom of the OB zone
    candle_index: int  # Index of the order block candle
    timestamp_ms: float
    displacement_strength: float  # Size of the displacement move (in ATR multiples)

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0


def detect_order_blocks(
    candles: list[Candle],
    displacement_factor: float = 1.5,
    atr_period: int = 14,
    lookback: int = 50,
) -> list[OrderBlock]:
    """
    Detect Order Blocks — the last opposing candle before a strong displacement.

    Logic:
      1. Identify "displacement" candles: body > displacement_factor * ATR.
      2. For each displacement, look backwards for the last candle with the
         opposite body direction. That candle is the Order Block.

    A bullish OB = last bearish (close < open) candle before a bullish displacement.
    A bearish OB = last bullish (close > open) candle before a bearish displacement.

    Args:
        candles: Price data (needs at least atr_period + 2 candles).
        displacement_factor: How many ATRs the displacement candle's body must exceed.
        atr_period: Period for ATR calculation.
        lookback: How many candles back to scan.

    Returns:
        List of OrderBlock objects.
    """
    if len(candles) < atr_period + 2:
        return []

    # Pre-compute ATR values for displacement detection
    from app.strategy.indicators import atr_series

    atr_vals = atr_series(candles, atr_period)
    if not atr_vals:
        return []

    order_blocks: list[OrderBlock] = []
    start_idx = max(atr_period, len(candles) - lookback)

    for i in range(start_idx, len(candles)):
        atr_val = atr_vals[i] if i < len(atr_vals) else 0.0
        if atr_val <= 0:
            continue

        body = candles[i].close - candles[i].open
        body_size = abs(body)

        # Check if this is a displacement candle
        if body_size < displacement_factor * atr_val:
            continue

        is_bullish_displacement = body > 0

        # Look back for the last opposing candle
        for j in range(i - 1, max(i - 15, start_idx - 1) - 1, -1):
            ob_body = candles[j].close - candles[j].open

            if is_bullish_displacement and ob_body < 0:
                # Found bullish OB (last bearish candle before bullish move)
                order_blocks.append(OrderBlock(
                    direction=OBDirection.BULLISH,
                    high=candles[j].high,
                    low=candles[j].low,
                    candle_index=j,
                    timestamp_ms=candles[j].timestamp_ms,
                    displacement_strength=body_size / atr_val,
                ))
                break

            elif not is_bullish_displacement and ob_body > 0:
                # Found bearish OB (last bullish candle before bearish move)
                order_blocks.append(OrderBlock(
                    direction=OBDirection.BEARISH,
                    high=candles[j].high,
                    low=candles[j].low,
                    candle_index=j,
                    timestamp_ms=candles[j].timestamp_ms,
                    displacement_strength=body_size / atr_val,
                ))
                break

    return order_blocks


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Market Structure — Swing Highs/Lows, BOS, CHoCH
# ═══════════════════════════════════════════════════════════════════════════════


class SwingType(Enum):
    HIGH = "swing_high"
    LOW = "swing_low"


class StructureEvent(Enum):
    BOS = "BOS"  # Break of Structure (trend continuation)
    CHOCH = "CHoCH"  # Change of Character (potential reversal)


class TrendBias(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass
class SwingPoint:
    """A confirmed swing high or swing low."""

    swing_type: SwingType
    price: float
    candle_index: int
    timestamp_ms: float


@dataclass
class StructureBreak:
    """A detected BOS or CHoCH event."""

    event: StructureEvent
    direction: TrendBias  # Direction AFTER the break
    broken_level: float  # The swing level that was broken
    break_price: float  # The close that broke it
    candle_index: int  # Index of the candle that broke structure
    timestamp_ms: float


@dataclass
class MarketStructure:
    """Complete market structure analysis result."""

    swing_points: list[SwingPoint]
    structure_breaks: list[StructureBreak]
    current_bias: TrendBias
    last_swing_high: float | None
    last_swing_low: float | None


def detect_swing_points(
    candles: list[Candle],
    left_bars: int = 3,
    right_bars: int = 3,
) -> list[SwingPoint]:
    """
    Detect swing highs and swing lows using the fractal method.

    A swing high is confirmed when `left_bars` candles to the left AND
    `right_bars` candles to the right all have lower highs.

    A swing low is confirmed when surrounding candles all have higher lows.

    This method does NOT repaint — a swing is only confirmed once
    `right_bars` candles have closed past it.

    Args:
        candles: Price history.
        left_bars: Number of bars to the left that must be lower/higher.
        right_bars: Number of bars to the right that must be lower/higher.
    """
    if len(candles) < left_bars + right_bars + 1:
        return []

    swings: list[SwingPoint] = []

    for i in range(left_bars, len(candles) - right_bars):
        # Check swing high
        is_swing_high = True
        for j in range(1, left_bars + 1):
            if candles[i - j].high >= candles[i].high:
                is_swing_high = False
                break
        if is_swing_high:
            for j in range(1, right_bars + 1):
                if candles[i + j].high >= candles[i].high:
                    is_swing_high = False
                    break

        if is_swing_high:
            swings.append(SwingPoint(
                swing_type=SwingType.HIGH,
                price=candles[i].high,
                candle_index=i,
                timestamp_ms=candles[i].timestamp_ms,
            ))

        # Check swing low
        is_swing_low = True
        for j in range(1, left_bars + 1):
            if candles[i - j].low <= candles[i].low:
                is_swing_low = False
                break
        if is_swing_low:
            for j in range(1, right_bars + 1):
                if candles[i + j].low <= candles[i].low:
                    is_swing_low = False
                    break

        if is_swing_low:
            swings.append(SwingPoint(
                swing_type=SwingType.LOW,
                price=candles[i].low,
                candle_index=i,
                timestamp_ms=candles[i].timestamp_ms,
            ))

    # Sort by candle index
    swings.sort(key=lambda s: s.candle_index)
    return swings


def analyze_market_structure(
    candles: list[Candle],
    left_bars: int = 3,
    right_bars: int = 3,
) -> MarketStructure | None:
    """
    Full market structure analysis: detect swings, then classify BOS and CHoCH.

    Logic:
      - Start with neutral bias.
      - When a swing high is broken (candle closes above it):
          * If bias was BULLISH → BOS (continuation)
          * If bias was BEARISH or NEUTRAL → CHoCH (reversal to bullish)
          * Bias becomes BULLISH
      - When a swing low is broken (candle closes below it):
          * If bias was BEARISH → BOS (continuation)
          * If bias was BULLISH or NEUTRAL → CHoCH (reversal to bearish)
          * Bias becomes BEARISH

    Returns MarketStructure with swing points, structure breaks, and current bias.
    """
    if len(candles) < left_bars + right_bars + 3:
        return None

    swings = detect_swing_points(candles, left_bars, right_bars)
    if len(swings) < 2:
        return MarketStructure(
            swing_points=swings,
            structure_breaks=[],
            current_bias=TrendBias.NEUTRAL,
            last_swing_high=None,
            last_swing_low=None,
        )

    structure_breaks: list[StructureBreak] = []
    bias = TrendBias.NEUTRAL
    last_sh: float | None = None
    last_sl: float | None = None

    # Track the most recent swing high and swing low
    for swing in swings:
        if swing.swing_type == SwingType.HIGH:
            last_sh = swing.price
        else:
            last_sl = swing.price

    # Now scan candles after each swing point to detect breaks
    # We iterate through swings and check if any subsequent candle breaks them
    active_highs: list[SwingPoint] = []
    active_lows: list[SwingPoint] = []

    swing_idx = 0
    bias = TrendBias.NEUTRAL

    for i in range(len(candles)):
        # Add any swing points at or before this candle index
        while swing_idx < len(swings) and swings[swing_idx].candle_index <= i:
            sp = swings[swing_idx]
            if sp.swing_type == SwingType.HIGH:
                active_highs.append(sp)
            else:
                active_lows.append(sp)
            swing_idx += 1

        # Check if current candle breaks any active swing high
        # (close above the swing high = break)
        broken_highs = [sh for sh in active_highs if candles[i].close > sh.price and i > sh.candle_index]
        if broken_highs:
            # Take the most recent swing high that was broken
            broken = max(broken_highs, key=lambda s: s.candle_index)
            if bias == TrendBias.BULLISH:
                event = StructureEvent.BOS
            else:
                event = StructureEvent.CHOCH
            structure_breaks.append(StructureBreak(
                event=event,
                direction=TrendBias.BULLISH,
                broken_level=broken.price,
                break_price=candles[i].close,
                candle_index=i,
                timestamp_ms=candles[i].timestamp_ms,
            ))
            bias = TrendBias.BULLISH
            # Remove broken highs from active list
            active_highs = [sh for sh in active_highs if sh.price > candles[i].close]

        # Check if current candle breaks any active swing low
        broken_lows = [sl for sl in active_lows if candles[i].close < sl.price and i > sl.candle_index]
        if broken_lows:
            broken = min(broken_lows, key=lambda s: s.candle_index)
            if bias == TrendBias.BEARISH:
                event = StructureEvent.BOS
            else:
                event = StructureEvent.CHOCH
            structure_breaks.append(StructureBreak(
                event=event,
                direction=TrendBias.BEARISH,
                broken_level=broken.price,
                break_price=candles[i].close,
                candle_index=i,
                timestamp_ms=candles[i].timestamp_ms,
            ))
            bias = TrendBias.BEARISH
            # Remove broken lows from active list
            active_lows = [sl for sl in active_lows if sl.price < candles[i].close]

    # Determine last swing high/low
    final_sh = None
    final_sl = None
    for sp in reversed(swings):
        if sp.swing_type == SwingType.HIGH and final_sh is None:
            final_sh = sp.price
        elif sp.swing_type == SwingType.LOW and final_sl is None:
            final_sl = sp.price
        if final_sh is not None and final_sl is not None:
            break

    return MarketStructure(
        swing_points=swings,
        structure_breaks=structure_breaks,
        current_bias=bias,
        last_swing_high=final_sh,
        last_swing_low=final_sl,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Session / Kill Zone Awareness
# ═══════════════════════════════════════════════════════════════════════════════


class FXSession(Enum):
    """Major FX trading sessions (all in UTC)."""

    SYDNEY = "sydney"
    TOKYO = "tokyo"
    LONDON = "london"
    NEW_YORK = "new_york"
    OFF_HOURS = "off_hours"


class KillZone(Enum):
    """High-probability trading windows (overlaps and opens)."""

    LONDON_OPEN = "london_open"  # 07:00-09:00 UTC
    NEW_YORK_OPEN = "new_york_open"  # 12:00-14:00 UTC
    LONDON_NY_OVERLAP = "london_ny_overlap"  # 12:00-16:00 UTC
    LONDON_CLOSE = "london_close"  # 15:00-16:00 UTC
    TOKYO_LONDON_OVERLAP = "tokyo_london_overlap"  # 07:00-08:00 UTC
    NONE = "none"


# Session boundaries in UTC hours (start_hour, end_hour)
# Sessions can span midnight (e.g. Sydney 21:00-06:00)
_SESSION_TIMES: dict[FXSession, tuple[int, int]] = {
    FXSession.SYDNEY: (21, 6),  # 21:00 - 06:00 UTC (wraps midnight)
    FXSession.TOKYO: (0, 9),  # 00:00 - 09:00 UTC
    FXSession.LONDON: (7, 16),  # 07:00 - 16:00 UTC
    FXSession.NEW_YORK: (12, 21),  # 12:00 - 21:00 UTC
}


def get_active_sessions(timestamp_ms: float) -> list[FXSession]:
    """
    Get all active trading sessions for a given timestamp.

    Returns a list because sessions overlap (e.g. London + New York).
    """
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    hour = dt.hour
    active: list[FXSession] = []

    for session, (start, end) in _SESSION_TIMES.items():
        if start < end:
            # Normal range (e.g. London 07-16)
            if start <= hour < end:
                active.append(session)
        else:
            # Wraps midnight (e.g. Sydney 21-06)
            if hour >= start or hour < end:
                active.append(session)

    if not active:
        active.append(FXSession.OFF_HOURS)

    return active


def get_kill_zone(timestamp_ms: float) -> KillZone:
    """
    Determine if the current time falls within a high-probability kill zone.

    Kill zones are specific windows where institutional activity concentrates,
    producing the largest moves and best setups:
      - London Open:    07:00 - 09:00 UTC (gold, GBP, EUR)
      - NY Open:        12:00 - 14:00 UTC (indices, USD pairs)
      - London/NY Lap:  12:00 - 16:00 UTC (peak liquidity)
      - London Close:   15:00 - 16:00 UTC (daily fix, reversals)
      - Tokyo/London:   07:00 - 08:00 UTC (JPY, early EUR)
    """
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    hour = dt.hour
    minute = dt.minute
    time_decimal = hour + minute / 60.0

    # Check kill zones (most specific first)
    if 15.0 <= time_decimal < 16.0:
        return KillZone.LONDON_CLOSE
    if 12.0 <= time_decimal < 14.0:
        return KillZone.NEW_YORK_OPEN
    if 12.0 <= time_decimal < 16.0:
        return KillZone.LONDON_NY_OVERLAP
    if 7.0 <= time_decimal < 8.0:
        return KillZone.TOKYO_LONDON_OVERLAP
    if 7.0 <= time_decimal < 9.0:
        return KillZone.LONDON_OPEN

    return KillZone.NONE


def is_kill_zone_for_instrument(timestamp_ms: float, instrument: str) -> bool:
    """
    Check if the current time is a kill zone for the given instrument.

    Kill zone selection per instrument type:
      - Gold (XAUUSD): London Open + London/NY overlap
      - Silver (XAGUSD): Same as gold
      - FX (EURUSD, GBPUSD, USDJPY): London Open + NY Open
      - US Indices (US30, US500, USTEC): NY Open + London/NY overlap
      - European Index (DE40): London session
      - Oil (XTIUSD): NY Open + London/NY overlap
    """
    kz = get_kill_zone(timestamp_ms)
    if kz == KillZone.NONE:
        return False

    instrument = instrument.upper()

    # Metals: London + overlap
    if instrument in ("XAUUSD", "XAGUSD"):
        return kz in (
            KillZone.LONDON_OPEN,
            KillZone.LONDON_NY_OVERLAP,
            KillZone.NEW_YORK_OPEN,
            KillZone.LONDON_CLOSE,
        )

    # FX pairs
    if instrument in ("EURUSD", "GBPUSD", "USDJPY"):
        return kz in (
            KillZone.LONDON_OPEN,
            KillZone.NEW_YORK_OPEN,
            KillZone.LONDON_NY_OVERLAP,
            KillZone.TOKYO_LONDON_OVERLAP,
        )

    # US Indices
    if instrument in ("US30", "US500", "USTEC"):
        return kz in (
            KillZone.NEW_YORK_OPEN,
            KillZone.LONDON_NY_OVERLAP,
        )

    # European Index
    if instrument == "DE40":
        return kz in (
            KillZone.LONDON_OPEN,
            KillZone.TOKYO_LONDON_OVERLAP,
            KillZone.LONDON_NY_OVERLAP,
        )

    # Oil
    if instrument == "XTIUSD":
        return kz in (
            KillZone.NEW_YORK_OPEN,
            KillZone.LONDON_NY_OVERLAP,
        )

    # Default: any kill zone is valid
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Fibonacci Retracement
# ═══════════════════════════════════════════════════════════════════════════════


# Standard Fibonacci levels used in trading
FIBONACCI_LEVELS: tuple[float, ...] = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)

# Extension levels (beyond the move)
FIBONACCI_EXTENSIONS: tuple[float, ...] = (1.272, 1.414, 1.618, 2.0, 2.618)


@dataclass
class FibonacciLevel:
    """A single Fibonacci retracement or extension level."""

    ratio: float  # e.g. 0.618
    price: float  # Computed price at this level
    label: str  # Human-readable label


@dataclass
class FibonacciResult:
    """Complete Fibonacci analysis from a swing high to swing low."""

    swing_high: float
    swing_low: float
    is_uptrend: bool  # True if retracement from an up-move (measuring pullback)
    retracement_levels: list[FibonacciLevel]
    extension_levels: list[FibonacciLevel]


def fibonacci_retracement(
    swing_high: float,
    swing_low: float,
    is_uptrend: bool = True,
    include_extensions: bool = True,
) -> FibonacciResult:
    """
    Compute Fibonacci retracement (and optionally extension) levels.

    In an uptrend (retracing from high):
        Level price = swing_high - ratio * (swing_high - swing_low)
        e.g. 0.618 level = high - 0.618 * range

    In a downtrend (retracing from low):
        Level price = swing_low + ratio * (swing_high - swing_low)
        e.g. 0.618 level = low + 0.618 * range

    Args:
        swing_high: The high point of the move.
        swing_low: The low point of the move.
        is_uptrend: True if price moved up (retracing down), False if moved down.
        include_extensions: Whether to compute extension levels beyond the move.
    """
    price_range = swing_high - swing_low

    retracement_levels: list[FibonacciLevel] = []
    for ratio in FIBONACCI_LEVELS:
        if is_uptrend:
            # Retracing DOWN from the high
            price = swing_high - ratio * price_range
        else:
            # Retracing UP from the low
            price = swing_low + ratio * price_range

        retracement_levels.append(FibonacciLevel(
            ratio=ratio,
            price=price,
            label=f"{ratio:.1%}" if ratio != 0.5 else "50%",
        ))

    extension_levels: list[FibonacciLevel] = []
    if include_extensions:
        for ratio in FIBONACCI_EXTENSIONS:
            if is_uptrend:
                # Extension ABOVE the high
                price = swing_high + (ratio - 1.0) * price_range
            else:
                # Extension BELOW the low
                price = swing_low - (ratio - 1.0) * price_range

            extension_levels.append(FibonacciLevel(
                ratio=ratio,
                price=price,
                label=f"{ratio:.3f}",
            ))

    return FibonacciResult(
        swing_high=swing_high,
        swing_low=swing_low,
        is_uptrend=is_uptrend,
        retracement_levels=retracement_levels,
        extension_levels=extension_levels,
    )


def auto_fibonacci(
    candles: list[Candle],
    left_bars: int = 5,
    right_bars: int = 5,
) -> FibonacciResult | None:
    """
    Automatically compute Fibonacci levels from the most recent confirmed
    swing high and swing low.

    Detects the latest swing high and swing low, determines the trend direction
    based on which occurred more recently, and computes retracement levels.

    Returns None if no valid swing pair is found.
    """
    swings = detect_swing_points(candles, left_bars, right_bars)
    if len(swings) < 2:
        return None

    # Find last swing high and last swing low
    last_high: SwingPoint | None = None
    last_low: SwingPoint | None = None

    for sp in reversed(swings):
        if sp.swing_type == SwingType.HIGH and last_high is None:
            last_high = sp
        elif sp.swing_type == SwingType.LOW and last_low is None:
            last_low = sp
        if last_high is not None and last_low is not None:
            break

    if last_high is None or last_low is None:
        return None

    # Determine trend: if swing high is more recent, price was going up (uptrend)
    is_uptrend = last_high.candle_index > last_low.candle_index

    return fibonacci_retracement(
        swing_high=last_high.price,
        swing_low=last_low.price,
        is_uptrend=is_uptrend,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _ema_series(values: list[float], period: int) -> list[float]:
    """
    Compute EMA series. Same logic as the existing ema_series in indicators.py.
    Returns list same length as input (first period-1 entries are placeholders).

    Seed: SMA of first `period` values.
    Smoothing factor: k = 2 / (period + 1).
    """
    if len(values) < period:
        return []

    k = 2.0 / (period + 1)
    result = [0.0] * (period - 1)

    # Seed with SMA
    ema_val = sum(values[:period]) / period
    result.append(ema_val)

    for price in values[period:]:
        ema_val = price * k + ema_val * (1 - k)
        result.append(ema_val)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 9. RSI Divergence Detection
# ═══════════════════════════════════════════════════════════════════════════════


class DivergenceType(Enum):
    """Type of RSI divergence."""

    REGULAR_BULLISH = "regular_bullish"  # Price lower low, RSI higher low → reversal up
    REGULAR_BEARISH = "regular_bearish"  # Price higher high, RSI lower high → reversal down
    HIDDEN_BULLISH = "hidden_bullish"  # Price higher low, RSI lower low → continuation up
    HIDDEN_BEARISH = "hidden_bearish"  # Price lower high, RSI higher high → continuation down


@dataclass
class Divergence:
    """A detected RSI divergence."""

    divergence_type: DivergenceType
    # First pivot (older)
    price_pivot_1: float
    rsi_pivot_1: float
    index_1: int
    # Second pivot (more recent)
    price_pivot_2: float
    rsi_pivot_2: float
    index_2: int
    # Metadata
    timestamp_ms: float  # Timestamp of the second (confirming) pivot


def detect_rsi_divergence(
    candles: list[Candle],
    rsi_period: int = 14,
    pivot_left: int = 3,
    pivot_right: int = 3,
    max_pivot_distance: int = 30,
) -> list[Divergence]:
    """
    Detect RSI divergences (regular and hidden) using confirmed swing pivots.

    Regular Bullish: Price makes lower low, RSI makes higher low → reversal signal.
    Regular Bearish: Price makes higher high, RSI makes lower high → reversal signal.
    Hidden Bullish:  Price makes higher low, RSI makes lower low → trend continuation.
    Hidden Bearish:  Price makes lower high, RSI makes higher high → trend continuation.

    Only uses CONFIRMED pivots (with right_bars confirmation) — does not repaint.

    Args:
        candles: Price data.
        rsi_period: RSI calculation period.
        pivot_left: Bars to left for pivot confirmation.
        pivot_right: Bars to right for pivot confirmation.
        max_pivot_distance: Max bars between two pivots for a valid divergence.

    Returns:
        List of Divergence objects, chronologically ordered.
    """
    from app.strategy.indicators import rsi as rsi_func

    if len(candles) < rsi_period + pivot_left + pivot_right + 2:
        return []

    # Compute RSI series
    closes = [c.close for c in candles]
    rsi_values: list[float | None] = []
    for i in range(len(candles)):
        if i < rsi_period:
            rsi_values.append(None)
        else:
            val = rsi_func(closes[: i + 1], rsi_period)
            rsi_values.append(val)

    # Find price swing lows and swing highs (for divergence pivot points)
    price_lows: list[tuple[int, float]] = []  # (index, low_price)
    price_highs: list[tuple[int, float]] = []  # (index, high_price)

    for i in range(pivot_left, len(candles) - pivot_right):
        if rsi_values[i] is None:
            continue

        # Check swing low (price)
        is_low = True
        for j in range(1, pivot_left + 1):
            if candles[i - j].low <= candles[i].low:
                is_low = False
                break
        if is_low:
            for j in range(1, pivot_right + 1):
                if candles[i + j].low <= candles[i].low:
                    is_low = False
                    break
        if is_low:
            price_lows.append((i, candles[i].low))

        # Check swing high (price)
        is_high = True
        for j in range(1, pivot_left + 1):
            if candles[i - j].high >= candles[i].high:
                is_high = False
                break
        if is_high:
            for j in range(1, pivot_right + 1):
                if candles[i + j].high >= candles[i].high:
                    is_high = False
                    break
        if is_high:
            price_highs.append((i, candles[i].high))

    divergences: list[Divergence] = []

    # Check consecutive swing lows for bullish divergences
    for i in range(1, len(price_lows)):
        idx1, price1 = price_lows[i - 1]
        idx2, price2 = price_lows[i]

        if idx2 - idx1 > max_pivot_distance:
            continue

        rsi1 = rsi_values[idx1]
        rsi2 = rsi_values[idx2]
        if rsi1 is None or rsi2 is None:
            continue

        # Regular Bullish: price lower low, RSI higher low
        if price2 < price1 and rsi2 > rsi1:
            divergences.append(Divergence(
                divergence_type=DivergenceType.REGULAR_BULLISH,
                price_pivot_1=price1, rsi_pivot_1=rsi1, index_1=idx1,
                price_pivot_2=price2, rsi_pivot_2=rsi2, index_2=idx2,
                timestamp_ms=candles[idx2].timestamp_ms,
            ))

        # Hidden Bullish: price higher low, RSI lower low
        if price2 > price1 and rsi2 < rsi1:
            divergences.append(Divergence(
                divergence_type=DivergenceType.HIDDEN_BULLISH,
                price_pivot_1=price1, rsi_pivot_1=rsi1, index_1=idx1,
                price_pivot_2=price2, rsi_pivot_2=rsi2, index_2=idx2,
                timestamp_ms=candles[idx2].timestamp_ms,
            ))

    # Check consecutive swing highs for bearish divergences
    for i in range(1, len(price_highs)):
        idx1, price1 = price_highs[i - 1]
        idx2, price2 = price_highs[i]

        if idx2 - idx1 > max_pivot_distance:
            continue

        rsi1 = rsi_values[idx1]
        rsi2 = rsi_values[idx2]
        if rsi1 is None or rsi2 is None:
            continue

        # Regular Bearish: price higher high, RSI lower high
        if price2 > price1 and rsi2 < rsi1:
            divergences.append(Divergence(
                divergence_type=DivergenceType.REGULAR_BEARISH,
                price_pivot_1=price1, rsi_pivot_1=rsi1, index_1=idx1,
                price_pivot_2=price2, rsi_pivot_2=rsi2, index_2=idx2,
                timestamp_ms=candles[idx2].timestamp_ms,
            ))

        # Hidden Bearish: price lower high, RSI higher high
        if price2 < price1 and rsi2 > rsi1:
            divergences.append(Divergence(
                divergence_type=DivergenceType.HIDDEN_BEARISH,
                price_pivot_1=price1, rsi_pivot_1=rsi1, index_1=idx1,
                price_pivot_2=price2, rsi_pivot_2=rsi2, index_2=idx2,
                timestamp_ms=candles[idx2].timestamp_ms,
            ))

    # Sort by the second pivot index (chronological)
    divergences.sort(key=lambda d: d.index_2)
    return divergences


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Parabolic SAR
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ParabolicSARResult:
    """Parabolic SAR computation result."""

    sar: float  # Current SAR value (stop level)
    is_uptrend: bool  # True = SAR is below price (bullish)
    af: float  # Current acceleration factor


def parabolic_sar(
    candles: list[Candle],
    af_start: float = 0.02,
    af_increment: float = 0.02,
    af_max: float = 0.20,
) -> ParabolicSARResult | None:
    """
    Compute Parabolic SAR (Stop And Reverse).

    Welles Wilder's trend-following indicator. The SAR trails price:
    - In an uptrend: SAR is below price, rises toward price.
    - In a downtrend: SAR is above price, falls toward price.
    - When price crosses SAR → trend reverses (stop and reverse).

    The Acceleration Factor (AF) starts at af_start and increases by
    af_increment each time a new extreme point is made, up to af_max.

    Args:
        candles: At least 2 candles required.
        af_start: Initial acceleration factor (default 0.02).
        af_increment: AF step increase (default 0.02).
        af_max: Maximum AF (default 0.20).

    Returns:
        ParabolicSARResult with current SAR value and trend direction.
    """
    if len(candles) < 2:
        return None

    # Initialize: assume uptrend if second candle closes higher than first
    is_uptrend = candles[1].close > candles[0].close

    if is_uptrend:
        sar = candles[0].low
        ep = candles[0].high  # Extreme point (highest high in uptrend)
    else:
        sar = candles[0].high
        ep = candles[0].low  # Extreme point (lowest low in downtrend)

    af = af_start

    for i in range(1, len(candles)):
        prev_sar = sar

        # Calculate new SAR
        sar = prev_sar + af * (ep - prev_sar)

        if is_uptrend:
            # SAR must not be above the prior two lows
            if i >= 2:
                sar = min(sar, candles[i - 1].low, candles[i - 2].low)
            else:
                sar = min(sar, candles[i - 1].low)

            # Check for reversal: price falls below SAR
            if candles[i].low < sar:
                # Reverse to downtrend
                is_uptrend = False
                sar = ep  # SAR becomes the previous extreme point
                ep = candles[i].low
                af = af_start
            else:
                # Continue uptrend: update extreme point if new high
                if candles[i].high > ep:
                    ep = candles[i].high
                    af = min(af + af_increment, af_max)
        else:
            # SAR must not be below the prior two highs
            if i >= 2:
                sar = max(sar, candles[i - 1].high, candles[i - 2].high)
            else:
                sar = max(sar, candles[i - 1].high)

            # Check for reversal: price rises above SAR
            if candles[i].high > sar:
                # Reverse to uptrend
                is_uptrend = True
                sar = ep  # SAR becomes the previous extreme point
                ep = candles[i].high
                af = af_start
            else:
                # Continue downtrend: update extreme point if new low
                if candles[i].low < ep:
                    ep = candles[i].low
                    af = min(af + af_increment, af_max)

    return ParabolicSARResult(sar=sar, is_uptrend=is_uptrend, af=af)


def parabolic_sar_series(
    candles: list[Candle],
    af_start: float = 0.02,
    af_increment: float = 0.02,
    af_max: float = 0.20,
) -> list[ParabolicSARResult]:
    """
    Compute Parabolic SAR for the entire candle history.
    Returns a list of results starting from index 1.
    """
    if len(candles) < 2:
        return []

    results: list[ParabolicSARResult] = []

    is_uptrend = candles[1].close > candles[0].close
    if is_uptrend:
        sar = candles[0].low
        ep = candles[0].high
    else:
        sar = candles[0].high
        ep = candles[0].low

    af = af_start

    for i in range(1, len(candles)):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)

        if is_uptrend:
            if i >= 2:
                sar = min(sar, candles[i - 1].low, candles[i - 2].low)
            else:
                sar = min(sar, candles[i - 1].low)

            if candles[i].low < sar:
                is_uptrend = False
                sar = ep
                ep = candles[i].low
                af = af_start
            else:
                if candles[i].high > ep:
                    ep = candles[i].high
                    af = min(af + af_increment, af_max)
        else:
            if i >= 2:
                sar = max(sar, candles[i - 1].high, candles[i - 2].high)
            else:
                sar = max(sar, candles[i - 1].high)

            if candles[i].high > sar:
                is_uptrend = True
                sar = ep
                ep = candles[i].high
                af = af_start
            else:
                if candles[i].low < ep:
                    ep = candles[i].low
                    af = min(af + af_increment, af_max)

        results.append(ParabolicSARResult(sar=sar, is_uptrend=is_uptrend, af=af))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 11. CCI (Commodity Channel Index)
# ═══════════════════════════════════════════════════════════════════════════════


def cci(candles: list[Candle], period: int = 20) -> float | None:
    """
    Compute Commodity Channel Index.

    CCI measures how far price deviates from its statistical mean.
    Readings above +100 = unusually strong (overbought territory).
    Readings below -100 = unusually weak (oversold territory).

    Formula:
        Typical Price = (High + Low + Close) / 3
        CCI = (TP - SMA(TP, period)) / (0.015 * Mean Deviation)

    The constant 0.015 ensures ~75% of values fall between -100 and +100.

    Args:
        candles: At least `period` candles required.
        period: Lookback period (default 20).
    """
    if len(candles) < period:
        return None

    # Compute typical prices for the last `period` candles
    window = candles[-period:]
    typical_prices = [(c.high + c.low + c.close) / 3.0 for c in window]

    # SMA of typical prices
    tp_mean = sum(typical_prices) / period

    # Mean Deviation (average absolute deviation from the mean)
    mean_deviation = sum(abs(tp - tp_mean) for tp in typical_prices) / period

    if mean_deviation == 0:
        return 0.0

    # CCI
    current_tp = typical_prices[-1]
    return (current_tp - tp_mean) / (0.015 * mean_deviation)


def cci_series(candles: list[Candle], period: int = 20) -> list[float]:
    """
    Compute CCI for the entire candle history.
    Returns list of CCI values (first period-1 entries are 0.0 placeholders).
    """
    if len(candles) < period:
        return []

    result = [0.0] * (period - 1)

    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1: i + 1]
        typical_prices = [(c.high + c.low + c.close) / 3.0 for c in window]
        tp_mean = sum(typical_prices) / period
        mean_deviation = sum(abs(tp - tp_mean) for tp in typical_prices) / period

        if mean_deviation == 0:
            result.append(0.0)
        else:
            current_tp = typical_prices[-1]
            result.append((current_tp - tp_mean) / (0.015 * mean_deviation))

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Donchian Channel
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class DonchianResult:
    """Donchian Channel computation result."""

    upper: float  # Highest high over period
    lower: float  # Lowest low over period
    middle: float  # (Upper + Lower) / 2
    width: float  # Upper - Lower (channel width)


def donchian_channel(candles: list[Candle], period: int = 20) -> DonchianResult | None:
    """
    Compute Donchian Channel.

    The simplest breakout indicator: upper band = highest high of last N bars,
    lower band = lowest low. A breakout above upper = new uptrend signal,
    below lower = new downtrend. Used in the Turtle Traders system.

    Args:
        candles: At least `period` candles required.
        period: Lookback period (default 20).
    """
    if len(candles) < period:
        return None

    window = candles[-period:]
    upper = max(c.high for c in window)
    lower = min(c.low for c in window)
    middle = (upper + lower) / 2.0

    return DonchianResult(upper=upper, lower=lower, middle=middle, width=upper - lower)


def donchian_channel_series(
    candles: list[Candle], period: int = 20
) -> list[DonchianResult]:
    """
    Compute Donchian Channel for the entire history.
    Returns list starting from index period-1.
    """
    if len(candles) < period:
        return []

    results: list[DonchianResult] = []
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1: i + 1]
        upper = max(c.high for c in window)
        lower = min(c.low for c in window)
        results.append(DonchianResult(
            upper=upper, lower=lower,
            middle=(upper + lower) / 2.0,
            width=upper - lower,
        ))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Williams %R
# ═══════════════════════════════════════════════════════════════════════════════


def williams_r(candles: list[Candle], period: int = 14) -> float | None:
    """
    Compute Williams %R.

    Measures where the current close sits relative to the high-low range
    of the last N periods. Scale: 0 to -100.
      - Above -20 = overbought (price near top of range)
      - Below -80 = oversold (price near bottom of range)

    Formula:
        %R = (Highest High - Close) / (Highest High - Lowest Low) * -100

    Mathematically equivalent to inverted Stochastic %K:
        %R = -(100 - %K)

    The key difference in usage: traders watch for %R to EXIT the extreme
    zone (crossing back above -80 = buy signal, crossing below -20 = sell).

    Args:
        candles: At least `period` candles required.
        period: Lookback period (default 14).
    """
    if len(candles) < period:
        return None

    window = candles[-period:]
    highest = max(c.high for c in window)
    lowest = min(c.low for c in window)

    denom = highest - lowest
    if denom == 0:
        return -50.0  # Flat market — middle of range

    return (highest - candles[-1].close) / denom * -100.0


def williams_r_series(candles: list[Candle], period: int = 14) -> list[float]:
    """
    Compute Williams %R for the entire history.
    Returns list starting from index period-1.
    """
    if len(candles) < period:
        return []

    results: list[float] = []
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1: i + 1]
        highest = max(c.high for c in window)
        lowest = min(c.low for c in window)
        denom = highest - lowest
        if denom == 0:
            results.append(-50.0)
        else:
            results.append((highest - candles[i].close) / denom * -100.0)

    return results
