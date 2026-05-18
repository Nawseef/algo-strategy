"""
Simple synchronous event bus.
Decouples producers (feed, strategy) from consumers (paper trader, logger).
Keeps the architecture modular without introducing external dependencies.
"""

from collections import defaultdict
from typing import Any, Callable

from app.utils.logger import get_logger

logger = get_logger(__name__)

# Type alias for event handlers
EventHandler = Callable[..., None]


class EventBus:
    """
    Lightweight publish/subscribe event bus.

    Events:
        tick        - raw tick from broker feed
        candle      - completed candle from candle builder
        signal      - trading signal from strategy
        order       - paper order executed
        position_open   - new position opened
        position_close  - position closed
        error       - error event
        reconnect   - feed reconnection event
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event: str, handler: EventHandler) -> None:
        """Register a handler for an event type."""
        self._handlers[event].append(handler)
        logger.debug("Subscribed %s to event '%s'", handler.__qualname__, event)

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        """Remove a handler from an event type."""
        if handler in self._handlers[event]:
            self._handlers[event].remove(handler)

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Emit an event to all registered handlers."""
        handlers = self._handlers.get(event, [])
        for handler in handlers:
            try:
                handler(*args, **kwargs)
            except Exception as e:
                logger.error(
                    "Error in handler %s for event '%s': %s",
                    handler.__qualname__,
                    event,
                    e,
                    exc_info=True,
                )
