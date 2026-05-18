"""
Abstract base class for trading strategies.
All strategies must implement this interface.
"""

from abc import ABC, abstractmethod

from app.broker.base import Tick
from app.core.models import Candle, Signal


class BaseStrategy(ABC):
    """
    Abstract strategy interface.

    Strategies receive market data (ticks and/or candles) and produce
    trading signals. They should be stateless with respect to position
    management — that's the paper trader's job.

    Lifecycle:
        1. on_tick() is called for every incoming tick.
        2. on_candle() is called when a candle completes.
        3. Either method may return a Signal or None.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier."""
        ...

    @abstractmethod
    def on_candle(self, candle: Candle, history: list[Candle]) -> Signal | None:
        """
        Called when a new candle completes.

        Args:
            candle: The just-completed candle.
            history: Recent candle history for this instrument/timeframe.

        Returns:
            A Signal if the strategy wants to trade, None otherwise.
        """
        ...

    def on_tick(self, tick: Tick) -> Signal | None:
        """
        Called for every incoming tick.
        Override if the strategy needs tick-level granularity.
        Default: no-op.
        """
        return None

    def on_start(self) -> None:
        """Called when the strategy engine starts. Override for initialization."""
        pass

    def on_stop(self) -> None:
        """Called when the strategy engine stops. Override for cleanup."""
        pass
