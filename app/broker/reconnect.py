"""
Reconnection manager for broker feeds.
Wraps a BrokerFeed with automatic reconnection on failure.
Uses exponential backoff with jitter.
"""

import random
import threading
import time
from typing import Callable

from app.broker.base import BrokerFeed, Instrument, Tick
from app.core.events import EventBus
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Reconnection parameters
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
BACKOFF_MULTIPLIER = 2.0
JITTER_RANGE = 0.5  # ±50% jitter


class ReconnectingFeed:
    """
    Wraps a BrokerFeed with automatic reconnection logic.

    On disconnection or error:
    1. Emits a 'reconnect' event on the event bus.
    2. Waits with exponential backoff + jitter.
    3. Re-authenticates the broker (token may have expired).
    4. Re-subscribes to all previously subscribed instruments.
    5. Resumes consumption.

    Usage:
        feed = GrowwFeedClient(broker)
        reconnecting = ReconnectingFeed(feed, event_bus, broker=broker)
        reconnecting.subscribe_ltp(instruments, on_tick=callback)
        reconnecting.start()  # runs in a thread, auto-reconnects
    """

    def __init__(
        self,
        feed: BrokerFeed,
        event_bus: EventBus,
        max_retries: int = 0,  # 0 = unlimited
        broker=None,           # GrowwBroker instance for re-auth on reconnect
    ) -> None:
        self._feed = feed
        self._event_bus = event_bus
        self._max_retries = max_retries
        self._broker = broker
        self._running = False
        self._thread: threading.Thread | None = None

        # Track subscriptions for re-subscribe on reconnect
        self._ltp_instruments: list[Instrument] = []
        self._ltp_callback: Callable[[Tick], None] | None = None
        self._retry_count = 0

    def subscribe_ltp(
        self,
        instruments: list[Instrument],
        on_tick: Callable[[Tick], None] | None = None,
    ) -> None:
        """Subscribe to LTP. Stores subscription for reconnection."""
        self._ltp_instruments = instruments
        self._ltp_callback = on_tick
        self._feed.subscribe_ltp(instruments, on_tick=on_tick)

    def start(self) -> None:
        """Start the feed in a background thread with reconnection."""
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="feed-reconnect",
            daemon=True,
        )
        self._thread.start()
        logger.info("ReconnectingFeed started (background thread)")

    def start_blocking(self) -> None:
        """Start the feed in the current thread with reconnection."""
        self._running = True
        self._run_loop()

    def stop(self) -> None:
        """Stop the feed and reconnection loop."""
        self._running = False
        try:
            self._feed.stop()
        except Exception as e:
            logger.warning("Error stopping feed: %s", e)
        logger.info("ReconnectingFeed stopped")

    def _run_loop(self) -> None:
        """Main loop: consume feed, reconnect on failure."""
        while self._running:
            try:
                logger.info("Starting feed consumption...")
                self._feed.consume()

                # If consume() returns normally, the feed ended cleanly
                # Reset retry count since we had a successful session
                self._retry_count = 0

                if not self._running:
                    break
                logger.warning("Feed consumption ended unexpectedly")

            except KeyboardInterrupt:
                # Propagate Ctrl+C so the main shutdown handler fires
                raise
            except SystemExit:
                # Propagate explicit exits
                raise
            except BaseException as e:
                # Catch everything including SDK-level errors that bypass Exception
                if not self._running:
                    break
                logger.error("Feed error: %s", e, exc_info=True)

            # Reconnection logic
            if not self._running:
                break

            self._retry_count += 1
            if self._max_retries > 0 and self._retry_count > self._max_retries:
                logger.error(
                    "Max retries (%d) exceeded. Giving up.", self._max_retries
                )
                self._event_bus.emit("error", "max_retries_exceeded")
                break

            backoff = self._calculate_backoff()
            logger.info(
                "Reconnecting in %.1fs (attempt %d)...",
                backoff,
                self._retry_count,
            )
            self._event_bus.emit(
                "reconnect",
                {
                    "attempt": self._retry_count,
                    "backoff_s": backoff,
                    "timestamp": time.time(),
                },
            )

            time.sleep(backoff)

            # Re-authenticate before reconnecting — session tokens expire
            self._reauthenticate()

            # Re-subscribe after reconnection
            self._resubscribe()

    def _reauthenticate(self) -> None:
        """Re-authenticate with the broker to refresh the session token."""
        if self._broker is None:
            return
        try:
            logger.info("Re-authenticating with broker before reconnect...")
            self._broker.authenticate()
            logger.info("Re-authentication successful")
        except Exception as e:
            logger.error("Re-authentication failed: %s", e)

    def _resubscribe(self) -> None:
        """Re-subscribe to all previously subscribed instruments."""
        try:
            if self._ltp_instruments:
                logger.info(
                    "Re-subscribing to %d instruments...",
                    len(self._ltp_instruments),
                )
                self._feed.subscribe_ltp(
                    self._ltp_instruments, on_tick=self._ltp_callback
                )
        except Exception as e:
            logger.error("Re-subscription failed: %s", e)

    def _calculate_backoff(self) -> float:
        """Exponential backoff with jitter."""
        backoff = min(
            INITIAL_BACKOFF_S * (BACKOFF_MULTIPLIER ** (self._retry_count - 1)),
            MAX_BACKOFF_S,
        )
        # Add jitter: ±50%
        jitter = backoff * random.uniform(-JITTER_RANGE, JITTER_RANGE)
        return max(0.1, backoff + jitter)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def retry_count(self) -> int:
        return self._retry_count
