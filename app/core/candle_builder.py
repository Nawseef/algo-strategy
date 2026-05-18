"""
Candle builder — aggregates raw ticks into OHLCV candles.

Supports multiple timeframes simultaneously.
Emits 'candle' events on the EventBus when a candle completes.
"""

from dataclasses import dataclass

from app.broker.base import Tick
from app.core.events import EventBus
from app.core.models import Candle, Timeframe
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Timeframe durations in milliseconds
TIMEFRAME_MS: dict[Timeframe, int] = {
    Timeframe.M1: 60_000,
    Timeframe.M5: 300_000,
    Timeframe.M15: 900_000,
    Timeframe.M30: 1_800_000,
    Timeframe.H1: 3_600_000,
    Timeframe.D1: 86_400_000,
}


@dataclass
class _CandleState:
    """Internal mutable state for a candle being built."""

    exchange: str = ""
    segment: str = ""
    exchange_token: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    open_time_ms: float = 0.0
    tick_count: int = 0

    def update(self, tick: Tick) -> None:
        """Update candle with a new tick."""
        self.exchange = tick.exchange
        self.segment = tick.segment
        self.exchange_token = tick.exchange_token

        price = tick.ltp
        if self.tick_count == 0:
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price
        self.tick_count += 1
        self.volume += 1

    def to_candle(self, timeframe: Timeframe) -> Candle:
        """Convert state to an immutable Candle."""
        return Candle(
            exchange=self.exchange,
            segment=self.segment,
            exchange_token=self.exchange_token,
            timeframe=timeframe,
            timestamp_ms=self.open_time_ms,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )

    def reset(self, open_time_ms: float) -> None:
        """Reset state for a new candle period (preserves instrument info)."""
        self.open = 0.0
        self.high = 0.0
        self.low = 0.0
        self.close = 0.0
        self.volume = 0
        self.open_time_ms = open_time_ms
        self.tick_count = 0


class CandleBuilder:
    """
    Aggregates ticks into candles for configured timeframes.

    Subscribe this to the 'tick' event on the EventBus.
    It emits 'candle' events when candles complete.

    Usage:
        builder = CandleBuilder(event_bus, timeframes=[Timeframe.M1, Timeframe.M5])
        event_bus.subscribe("tick", builder.on_tick)
    """

    def __init__(
        self,
        event_bus: EventBus,
        timeframes: list[Timeframe] | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._timeframes = timeframes or [Timeframe.M1]

        # State: {(exchange_token, timeframe): _CandleState}
        self._states: dict[tuple[str, Timeframe], _CandleState] = {}

        # Track completed candles for each instrument (ring buffer, last N)
        self._history: dict[tuple[str, Timeframe], list[Candle]] = {}
        self._max_history = 500  # keep last 500 candles per instrument/timeframe

        logger.info(
            "CandleBuilder initialized. Timeframes: %s",
            [tf.value for tf in self._timeframes],
        )

    def on_tick(self, tick: Tick) -> None:
        """
        Process an incoming tick.
        Called by the EventBus when a 'tick' event is emitted.
        """
        for timeframe in self._timeframes:
            self._process_tick(tick, timeframe)

    def _process_tick(self, tick: Tick, timeframe: Timeframe) -> None:
        """Process a tick for a specific timeframe."""
        key = (tick.exchange_token, timeframe)
        interval_ms = TIMEFRAME_MS[timeframe]

        # Determine which candle period this tick belongs to
        candle_open_time = self._floor_timestamp(tick.timestamp_ms, interval_ms)

        state = self._states.get(key)

        if state is None:
            # First tick for this instrument/timeframe
            state = _CandleState(open_time_ms=candle_open_time)
            self._states[key] = state
            state.update(tick)
            return

        if candle_open_time > state.open_time_ms and state.tick_count > 0:
            # New candle period — emit the completed candle
            completed = state.to_candle(timeframe=timeframe)
            self._emit_candle(completed, key)

            # Reset for new period
            state.reset(candle_open_time)

        state.update(tick)

    def _emit_candle(self, candle: Candle, key: tuple[str, Timeframe]) -> None:
        """Emit completed candle and store in history."""
        logger.debug("Candle complete: %s", candle)

        # Store in history
        if key not in self._history:
            self._history[key] = []
        history = self._history[key]
        history.append(candle)
        if len(history) > self._max_history:
            history.pop(0)

        # Emit event
        self._event_bus.emit("candle", candle)

    def get_history(
        self,
        exchange_token: str,
        timeframe: Timeframe,
        count: int | None = None,
    ) -> list[Candle]:
        """
        Get candle history for an instrument/timeframe.
        Returns most recent candles (up to count).
        """
        key = (exchange_token, timeframe)
        history = self._history.get(key, [])
        if count:
            return history[-count:]
        return list(history)

    def get_current_candle(
        self,
        exchange_token: str,
        timeframe: Timeframe,
    ) -> Candle | None:
        """Get the currently forming (incomplete) candle."""
        key = (exchange_token, timeframe)
        state = self._states.get(key)
        if state and state.tick_count > 0:
            return state.to_candle(timeframe=timeframe)
        return None

    @staticmethod
    def _floor_timestamp(timestamp_ms: float, interval_ms: int) -> float:
        """Floor a timestamp to the nearest interval boundary."""
        return (int(timestamp_ms) // interval_ms) * interval_ms
