"""
Simple Moving Average Crossover Strategy.

A basic demonstration strategy:
- BUY when fast SMA crosses above slow SMA.
- SELL when fast SMA crosses below slow SMA.

This is intentionally simple — meant as a starting template
for building more sophisticated strategies.
"""

from app.core.models import Candle, Signal, SignalType
from app.strategy.base import BaseStrategy
from app.utils.logger import get_logger

logger = get_logger(__name__)


class SMACrossoverStrategy(BaseStrategy):
    """
    SMA Crossover strategy.

    Parameters:
        fast_period: Number of candles for the fast moving average.
        slow_period: Number of candles for the slow moving average.
        instrument_tokens: Only generate signals for these tokens.
                          Empty list = all instruments.
    """

    def __init__(
        self,
        fast_period: int = 5,
        slow_period: int = 20,
        instrument_tokens: list[str] | None = None,
    ) -> None:
        self._fast_period = fast_period
        self._slow_period = slow_period
        self._instrument_tokens = instrument_tokens or []

        # Track previous SMA state per instrument for crossover detection
        self._prev_fast: dict[str, float] = {}
        self._prev_slow: dict[str, float] = {}

    @property
    def name(self) -> str:
        return f"SMA_Crossover({self._fast_period}/{self._slow_period})"

    def on_candle(self, candle: Candle, history: list[Candle]) -> Signal | None:
        """Evaluate SMA crossover on each completed candle."""
        # Filter instruments if configured
        if self._instrument_tokens and candle.exchange_token not in self._instrument_tokens:
            return None

        # Need enough history for slow SMA
        if len(history) < self._slow_period:
            return None

        # Calculate SMAs from closing prices
        closes = [c.close for c in history[-self._slow_period:]]
        fast_sma = self._sma(closes, self._fast_period)
        slow_sma = self._sma(closes, self._slow_period)

        if fast_sma is None or slow_sma is None:
            return None

        token = candle.exchange_token
        prev_fast = self._prev_fast.get(token)
        prev_slow = self._prev_slow.get(token)

        # Store current values for next comparison
        self._prev_fast[token] = fast_sma
        self._prev_slow[token] = slow_sma

        # Need previous values to detect crossover
        if prev_fast is None or prev_slow is None:
            return None

        # Detect crossover
        signal = self._detect_crossover(
            prev_fast, prev_slow, fast_sma, slow_sma, candle
        )
        return signal

    def _detect_crossover(
        self,
        prev_fast: float,
        prev_slow: float,
        curr_fast: float,
        curr_slow: float,
        candle: Candle,
    ) -> Signal | None:
        """Detect bullish or bearish crossover."""
        # Bullish crossover: fast crosses above slow
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            logger.info(
                "Bullish crossover on %s: fast=%.2f > slow=%.2f",
                candle.exchange_token,
                curr_fast,
                curr_slow,
            )
            return Signal(
                signal_type=SignalType.BUY,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=candle.exchange_token,
                price=candle.close,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"Bullish SMA crossover (fast={curr_fast:.2f}, slow={curr_slow:.2f})",
                metadata={
                    "fast_sma": curr_fast,
                    "slow_sma": curr_slow,
                    "timeframe": candle.timeframe.value,
                },
            )

        # Bearish crossover: fast crosses below slow
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            logger.info(
                "Bearish crossover on %s: fast=%.2f < slow=%.2f",
                candle.exchange_token,
                curr_fast,
                curr_slow,
            )
            return Signal(
                signal_type=SignalType.SELL,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=candle.exchange_token,
                price=candle.close,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"Bearish SMA crossover (fast={curr_fast:.2f}, slow={curr_slow:.2f})",
                metadata={
                    "fast_sma": curr_fast,
                    "slow_sma": curr_slow,
                    "timeframe": candle.timeframe.value,
                },
            )

        return None

    @staticmethod
    def _sma(values: list[float], period: int) -> float | None:
        """Calculate Simple Moving Average over the last N values."""
        if len(values) < period:
            return None
        return sum(values[-period:]) / period
