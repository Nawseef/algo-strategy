"""
CPR (Central Pivot Range) Filter.

Calculates daily bias from previous day's High/Low/Close.
Used as a directional filter for all strategies — not standalone.

CPR Formula:
    PP (Pivot Point) = (Previous High + Previous Low + Previous Close) / 3
    BC (Bottom Central) = (Previous High + Previous Low) / 2
    TC (Top Central) = (2 × PP) - BC

Bias Rules:
    - Price above TC → Bullish day → only LONG signals allowed
    - Price below BC → Bearish day → only SHORT signals allowed
    - Price between BC and TC → Neutral → both directions allowed

Width Rules:
    - Narrow CPR (TC - BC < 0.3% of price) → Trending day expected
    - Wide CPR (TC - BC > 0.7% of price) → Ranging day expected

Over 70% success rate reported in Indian markets (Scribd research).
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from enum import Enum

from app.core.models import Candle, SignalType, Timeframe
from app.utils.logger import get_logger

logger = get_logger(__name__)


class CPRBias(Enum):
    """Daily market bias from CPR."""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class MarketType(Enum):
    """Expected market type from CPR width."""
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    NORMAL = "NORMAL"


class CPRFilter:
    """
    Central Pivot Range filter.

    Calculates CPR levels once per day from previous day's data.
    Provides bias and market type for other strategies to use.

    Usage:
        cpr = CPRFilter()
        cpr.calculate_from_candles(yesterday_daily_candle)

        # In strategy:
        if not cpr.allows_signal(SignalType.BUY, current_price):
            return None  # CPR says don't go long today
    """

    def __init__(self) -> None:
        # CPR levels
        self._pp: float = 0.0   # Pivot Point
        self._tc: float = 0.0   # Top Central
        self._bc: float = 0.0   # Bottom Central
        self._r1: float = 0.0   # Resistance 1
        self._s1: float = 0.0   # Support 1

        # State
        self._bias: CPRBias = CPRBias.NEUTRAL
        self._market_type: MarketType = MarketType.NORMAL
        self._is_virgin: bool = False
        self._calculated: bool = False
        self._last_calc_date: str = ""

    def calculate(self, prev_high: float, prev_low: float, prev_close: float) -> None:
        """
        Calculate CPR from previous day's H/L/C.
        Call this once at start of day (before 9:15 AM).
        """
        # Core CPR
        self._pp = (prev_high + prev_low + prev_close) / 3.0
        self._bc = (prev_high + prev_low) / 2.0
        self._tc = (2 * self._pp) - self._bc

        # Support/Resistance levels
        self._r1 = (2 * self._pp) - prev_low
        self._s1 = (2 * self._pp) - prev_high

        # Determine market type from CPR width
        cpr_width = abs(self._tc - self._bc)
        mid_price = self._pp  # Use pivot point as reference
        width_pct = (cpr_width / mid_price) * 100 if mid_price > 0 else 0

        # If TC == BC (degenerate case), use previous day's range as indicator
        if cpr_width == 0:
            day_range = prev_high - prev_low
            range_pct = (day_range / mid_price) * 100 if mid_price > 0 else 0
            # Small previous day range = trending expected
            if range_pct < 1.5:
                self._market_type = MarketType.TRENDING
            elif range_pct > 3.0:
                self._market_type = MarketType.RANGING
            else:
                self._market_type = MarketType.NORMAL
        elif width_pct < 0.3:
            self._market_type = MarketType.TRENDING
        elif width_pct > 0.7:
            self._market_type = MarketType.RANGING
        else:
            self._market_type = MarketType.NORMAL

        self._calculated = True
        self._last_calc_date = datetime.now().strftime("%Y-%m-%d")

        logger.info(
            "CPR calculated: PP=%.2f TC=%.2f BC=%.2f R1=%.2f S1=%.2f | "
            "Width=%.3f%% (%s) | From H=%.2f L=%.2f C=%.2f",
            self._pp, self._tc, self._bc, self._r1, self._s1,
            width_pct, self._market_type.value,
            prev_high, prev_low, prev_close,
        )

    def calculate_from_candles(self, daily_candles: list[Candle]) -> None:
        """
        Calculate CPR from the most recent daily candle in the list.
        Typically called with warmup data.
        """
        if not daily_candles:
            logger.warning("CPR: No daily candles provided, using neutral bias")
            return

        # Use the last daily candle as "previous day"
        prev = daily_candles[-1]
        self.calculate(prev.high, prev.low, prev.close)

    def calculate_from_5m_candles(self, candles_5m: list[Candle]) -> None:
        """
        Calculate CPR from 5-min candles of the previous day.
        Finds the overall high/low/close from yesterday's 5m candles.
        """
        if not candles_5m:
            return

        # Get yesterday's date
        today = datetime.now().date()
        yesterday_candles = [
            c for c in candles_5m
            if datetime.fromtimestamp(c.timestamp_ms / 1000).date() < today
        ]

        if not yesterday_candles:
            # Use all available candles as fallback
            yesterday_candles = candles_5m

        prev_high = max(c.high for c in yesterday_candles)
        prev_low = min(c.low for c in yesterday_candles)
        prev_close = yesterday_candles[-1].close

        self.calculate(prev_high, prev_low, prev_close)

    def update_bias(self, current_price: float) -> None:
        """
        Update the daily bias based on where price is relative to CPR.
        Call this after market opens (after first few ticks).
        """
        if not self._calculated:
            return

        if current_price > self._tc:
            self._bias = CPRBias.BULLISH
        elif current_price < self._bc:
            self._bias = CPRBias.BEARISH
        else:
            self._bias = CPRBias.NEUTRAL

    def allows_signal(self, signal_type: SignalType, current_price: float) -> bool:
        """
        Check if CPR allows this signal direction.

        Returns True if the signal is allowed, False if blocked.
        Always returns True if CPR hasn't been calculated yet (fail-open).
        """
        if not self._calculated:
            return True  # Fail-open: don't block if no CPR data

        # Update bias with current price
        self.update_bias(current_price)

        if self._bias == CPRBias.NEUTRAL:
            return True  # Both directions allowed

        if self._bias == CPRBias.BULLISH and signal_type == SignalType.SELL:
            return False  # Don't short on bullish day

        if self._bias == CPRBias.BEARISH and signal_type == SignalType.BUY:
            return False  # Don't buy on bearish day

        return True

    def maybe_recalculate(self, candles_5m: list[Candle]) -> None:
        """Recalculate if it's a new day and we haven't calculated yet."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_calc_date:
            self.calculate_from_5m_candles(candles_5m)

    # ─── Public Properties ───────────────────────────────────────

    @property
    def bias(self) -> CPRBias:
        return self._bias

    @property
    def market_type(self) -> MarketType:
        return self._market_type

    @property
    def is_trending_day(self) -> bool:
        return self._market_type == MarketType.TRENDING

    @property
    def is_ranging_day(self) -> bool:
        return self._market_type == MarketType.RANGING

    @property
    def pivot_point(self) -> float:
        return self._pp

    @property
    def top_central(self) -> float:
        return self._tc

    @property
    def bottom_central(self) -> float:
        return self._bc

    @property
    def resistance_1(self) -> float:
        return self._r1

    @property
    def support_1(self) -> float:
        return self._s1

    @property
    def is_calculated(self) -> bool:
        return self._calculated
