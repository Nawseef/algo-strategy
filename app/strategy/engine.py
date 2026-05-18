"""
Strategy engine — orchestrates strategy execution.

Listens to candle and tick events, routes them to registered strategies,
and emits signals on the event bus.
"""

from app.broker.base import Tick
from app.core.candle_builder import CandleBuilder
from app.core.events import EventBus
from app.core.models import Candle, Signal
from app.strategy.base import BaseStrategy
from app.utils.logger import get_logger

logger = get_logger(__name__)


class StrategyEngine:
    """
    Manages and executes trading strategies.

    Subscribes to 'tick' and 'candle' events.
    Routes data to all registered strategies.
    Emits 'signal' events when strategies produce signals.

    Usage:
        engine = StrategyEngine(event_bus, candle_builder)
        engine.register(MyStrategy())
        engine.start()
    """

    def __init__(
        self,
        event_bus: EventBus,
        candle_builder: CandleBuilder,
    ) -> None:
        self._event_bus = event_bus
        self._candle_builder = candle_builder
        self._strategies: list[BaseStrategy] = []
        self._running = False

    def register(self, strategy: BaseStrategy) -> None:
        """Register a strategy for execution."""
        self._strategies.append(strategy)
        logger.info("Strategy registered: %s", strategy.name)

    def start(self) -> None:
        """Start the strategy engine. Subscribe to events."""
        self._running = True

        # Subscribe to events
        self._event_bus.subscribe("tick", self._on_tick)
        self._event_bus.subscribe("candle", self._on_candle)

        # Notify strategies
        for strategy in self._strategies:
            try:
                strategy.on_start()
            except Exception as e:
                logger.error("Error starting strategy %s: %s", strategy.name, e)

        logger.info(
            "StrategyEngine started with %d strategies: %s",
            len(self._strategies),
            [s.name for s in self._strategies],
        )

    def stop(self) -> None:
        """Stop the strategy engine."""
        self._running = False

        self._event_bus.unsubscribe("tick", self._on_tick)
        self._event_bus.unsubscribe("candle", self._on_candle)

        for strategy in self._strategies:
            try:
                strategy.on_stop()
            except Exception as e:
                logger.error("Error stopping strategy %s: %s", strategy.name, e)

        logger.info("StrategyEngine stopped")

    def _on_tick(self, tick: Tick) -> None:
        """Route tick to all strategies."""
        if not self._running:
            return

        for strategy in self._strategies:
            try:
                signal = strategy.on_tick(tick)
                if signal:
                    self._emit_signal(signal)
            except Exception as e:
                logger.error(
                    "Strategy %s error on tick: %s",
                    strategy.name,
                    e,
                    exc_info=True,
                )

    def _on_candle(self, candle: Candle) -> None:
        """Route candle to all strategies with history."""
        if not self._running:
            return

        # Get history for this instrument/timeframe
        history = self._candle_builder.get_history(
            exchange_token=candle.exchange_token,
            timeframe=candle.timeframe,
        )

        for strategy in self._strategies:
            try:
                signal = strategy.on_candle(candle, history)
                if signal:
                    self._emit_signal(signal)
            except Exception as e:
                logger.error(
                    "Strategy %s error on candle: %s",
                    strategy.name,
                    e,
                    exc_info=True,
                )

    def _emit_signal(self, signal: Signal) -> None:
        """Emit a signal on the event bus."""
        logger.info("SIGNAL | %s", signal)
        self._event_bus.emit("signal", signal)

    @property
    def strategies(self) -> list[BaseStrategy]:
        """List of registered strategies."""
        return list(self._strategies)
