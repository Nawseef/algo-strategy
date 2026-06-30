"""
Reconnection manager for broker feeds.
Wraps a BrokerFeed with automatic reconnection on failure.
Uses exponential backoff with jitter.

Key feature: consume() runs in a daemon thread with a liveness heartbeat.
If consume() deadlocks (common with Groww NATS SDK after disconnect),
the heartbeat detects silence and force-restarts the feed.
"""

import os
import random
import threading
import time
from typing import Callable

from app.broker.base import BrokerFeed, Instrument, Tick
from app.core.events import EventBus
from app.utils.logger import get_logger
from app.utils.market_hours import is_within_active_window

logger = get_logger(__name__)

# Reconnection parameters
INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
BACKOFF_MULTIPLIER = 2.0
JITTER_RANGE = 0.5  # ±50% jitter

# Liveness detection: if no tick arrives for this long, assume feed is dead
LIVENESS_TIMEOUT_S = 300  # 5 minutes — enough for even illiquid instruments

# After market close, how long to wait before forcing exit
POST_MARKET_GRACE_S = 120  # 2 minutes after market close, disconnect and exit


class ReconnectingFeed:
    """
    Wraps a BrokerFeed with automatic reconnection logic.

    On disconnection or error:
    1. Emits a 'reconnect' event on the event bus.
    2. Waits with exponential backoff + jitter.
    3. Re-authenticates the broker (token may have expired).
    4. Re-subscribes to all previously subscribed instruments.
    5. Resumes consumption.

    Liveness protection:
    - consume() runs in a daemon thread.
    - Main thread monitors last_tick_time.
    - If no tick for LIVENESS_TIMEOUT_S during market hours → force restart.
    - If market is closed → clean exit (let systemd restart next day).

    Usage:
        feed = GrowwFeedClient(broker)
        reconnecting = ReconnectingFeed(feed, event_bus, broker=broker)
        reconnecting.subscribe_ltp(instruments, on_tick=callback)
        reconnecting.start_blocking()  # runs with auto-reconnect + liveness
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

        # Liveness tracking
        self._last_tick_time: float = 0.0
        self._consume_thread: threading.Thread | None = None
        self._consume_finished = threading.Event()

    def subscribe_ltp(
        self,
        instruments: list[Instrument],
        on_tick: Callable[[Tick], None] | None = None,
    ) -> None:
        """Store subscription params. Actual subscription happens in _resubscribe()
        when start/start_blocking is called — this avoids double-subscribing."""
        self._ltp_instruments = instruments
        self._ltp_callback = on_tick

        # Wrap the callback to track liveness
        if on_tick:
            original_callback = on_tick

            def _tracked_callback(tick: Tick) -> None:
                self._last_tick_time = time.time()
                original_callback(tick)

            self._tracked_ltp_callback = _tracked_callback
        else:
            self._tracked_ltp_callback = None

        # NOTE: Do NOT subscribe here. _run_loop() → _resubscribe() handles it.
        # Subscribing here AND in _resubscribe() causes duplicate callbacks.

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
        """Main loop: consume feed with liveness monitoring, reconnect on failure."""
        while self._running:
            try:
                logger.info("Starting feed consumption...")
                # Ensure we have a working subscription before consuming
                self._resubscribe()

                # Run consume() in a daemon thread so we can monitor liveness
                self._last_tick_time = time.time()
                self._consume_finished.clear()
                self._consume_thread = threading.Thread(
                    target=self._consume_with_signal,
                    name="feed-consume",
                    daemon=True,
                )
                self._consume_thread.start()

                # Monitor liveness from this thread
                dead_reason = self._monitor_liveness()

                if dead_reason == "market_closed":
                    logger.info(
                        "Market closed — disconnecting feed and exiting process. "
                        "Systemd will restart for next trading day."
                    )
                    self._running = False
                    self._force_stop_feed()
                    # Exit the process — systemd Restart=always will bring us back
                    os._exit(0)

                elif dead_reason == "no_ticks":
                    logger.warning(
                        "Feed appears dead (no ticks for %ds). Force-restarting...",
                        LIVENESS_TIMEOUT_S,
                    )
                    self._force_stop_feed()
                    # Fall through to reconnection logic below

                elif dead_reason == "consume_ended":
                    # consume() returned or threw — normal reconnect path
                    self._retry_count = 0
                    if not self._running:
                        break
                    logger.warning("Feed consumption ended unexpectedly")

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                if not self._running:
                    break
                logger.error("Feed error: %s", e, exc_info=True)

            # Reconnection logic
            if not self._running:
                break

            # If market is no longer active, just exit cleanly
            if not is_within_active_window():
                logger.info(
                    "Market closed during reconnect — exiting. "
                    "Systemd will restart for next trading day."
                )
                os._exit(0)

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

    def _consume_with_signal(self) -> None:
        """Run feed.consume() and signal when it exits (normally or by error)."""
        try:
            self._feed.consume()
        except Exception as e:
            logger.error("consume() raised: %s", e)
        finally:
            self._consume_finished.set()

    def _monitor_liveness(self) -> str:
        """
        Monitor the feed for liveness. Returns reason for exiting:
        - "market_closed": market window ended, should exit process
        - "no_ticks": feed is dead (deadlocked), should reconnect
        - "consume_ended": consume() returned/crashed, normal reconnect
        """
        while self._running:
            # Check if consume() thread exited on its own
            if self._consume_finished.wait(timeout=30.0):
                return "consume_ended"

            # Check if market has closed
            if not is_within_active_window():
                # Give a grace period after market close for final ticks
                logger.info(
                    "Market window ended. Waiting %ds grace period...",
                    POST_MARKET_GRACE_S,
                )
                time.sleep(POST_MARKET_GRACE_S)
                return "market_closed"

            # Check liveness — have we received any tick recently?
            silence = time.time() - self._last_tick_time
            if silence > LIVENESS_TIMEOUT_S:
                return "no_ticks"

        return "consume_ended"

    def _force_stop_feed(self) -> None:
        """Force-stop the feed. The consume thread is a daemon so it dies with the process."""
        try:
            self._feed.stop()
        except Exception:
            pass
        # Reset for fresh connection on next attempt
        if hasattr(self._feed, '_reset_feed'):
            self._feed._reset_feed()

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
        """Re-subscribe to all previously subscribed instruments.
        Resets the feed client first to force a fresh WebSocket connection."""
        try:
            if self._ltp_instruments:
                # Reset the feed so a fresh NATS connection is created
                if hasattr(self._feed, '_reset_feed'):
                    self._feed._reset_feed()
                logger.info(
                    "Re-subscribing to %d instruments...",
                    len(self._ltp_instruments),
                )
                self._feed.subscribe_ltp(
                    self._ltp_instruments, on_tick=self._tracked_ltp_callback
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
