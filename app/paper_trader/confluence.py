"""
Confluence Tracker — Detects when multiple strategies agree.

Buffers signals and fires a combined "confluence signal" when 2+ or 3+
strategies signal the same direction on the same instrument within
the same candle window (5 minutes).

This implements the "voting system" approach used by professional
ensemble trading systems.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.events import EventBus
from app.core.models import Signal, SignalType
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Signals within this window (seconds) are considered "agreeing"
CONFLUENCE_WINDOW_SECONDS = 300  # 5 minutes = 1 candle on 5m timeframe


@dataclass
class BufferedSignal:
    """A signal waiting for confluence."""

    signal: Signal
    timestamp: float  # wall-clock time when received


@dataclass
class ConfluenceResult:
    """A confluence signal combining multiple strategy agreements."""

    signal_type: SignalType
    exchange: str
    segment: str
    exchange_token: str
    price: float  # average entry price from agreeing strategies
    stop_loss: float  # tightest (safest) SL
    take_profit: float  # average TP
    timestamp_ms: float
    strategies: list[str]  # names of agreeing strategies
    count: int  # how many strategies agree


class ConfluenceTracker:
    """
    Tracks signals and detects confluence (multiple strategies agreeing).

    Emits events:
        - 'confluence_2' when 2+ strategies agree
        - 'confluence_3' when 3+ strategies agree

    Each instrument+direction combo is tracked independently.
    Signals expire after CONFLUENCE_WINDOW_SECONDS.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        # Buffer: {(exchange_token, direction): [BufferedSignal, ...]}
        self._buffer: dict[tuple[str, str], list[BufferedSignal]] = {}
        # Track what we've already fired to avoid duplicates
        self._fired_2: dict[tuple[str, str], float] = {}  # key -> last fire time
        self._fired_3: dict[tuple[str, str], float] = {}

    def on_signal(self, signal: Signal) -> None:
        """
        Buffer a signal and check for confluence.
        Called for every signal from every strategy.
        """
        now = time.time()
        direction = signal.signal_type.value  # "BUY" or "SELL"
        key = (signal.exchange_token, direction)

        # Add to buffer
        if key not in self._buffer:
            self._buffer[key] = []
        self._buffer[key].append(BufferedSignal(signal=signal, timestamp=now))

        # Clean expired signals
        self._clean_expired(key, now)

        # Check confluence
        active_signals = self._buffer[key]
        unique_strategies = set(bs.signal.strategy_name for bs in active_signals)
        count = len(unique_strategies)

        # Fire confluence_2 if 2+ agree (and not already fired in this window)
        if count >= 2:
            last_fired = self._fired_2.get(key, 0)
            if now - last_fired > CONFLUENCE_WINDOW_SECONDS:
                self._fired_2[key] = now
                result = self._build_confluence(active_signals, unique_strategies)
                logger.info(
                    "CONFLUENCE 2+ | %s %s | %d strategies agree: %s",
                    direction, signal.exchange_token, count,
                    ", ".join(result.strategies),
                )
                self._event_bus.emit("confluence_2", result)

        # Fire confluence_3 if 3+ agree
        if count >= 3:
            last_fired = self._fired_3.get(key, 0)
            if now - last_fired > CONFLUENCE_WINDOW_SECONDS:
                self._fired_3[key] = now
                result = self._build_confluence(active_signals, unique_strategies)
                logger.info(
                    "CONFLUENCE 3+ | %s %s | %d strategies agree: %s",
                    direction, signal.exchange_token, count,
                    ", ".join(result.strategies),
                )
                self._event_bus.emit("confluence_3", result)

    def _clean_expired(self, key: tuple[str, str], now: float) -> None:
        """Remove signals older than the confluence window."""
        if key in self._buffer:
            self._buffer[key] = [
                bs for bs in self._buffer[key]
                if now - bs.timestamp <= CONFLUENCE_WINDOW_SECONDS
            ]

    def _build_confluence(
        self,
        signals: list[BufferedSignal],
        strategies: set[str],
    ) -> ConfluenceResult:
        """Build a confluence result from agreeing signals."""
        # Use the most recent signal's metadata as base
        latest = max(signals, key=lambda bs: bs.timestamp)
        sig = latest.signal

        # Tightest SL (safest): for BUY = highest SL, for SELL = lowest SL
        sl_values = [bs.signal.stop_loss for bs in signals if bs.signal.stop_loss]
        if sl_values:
            if sig.signal_type == SignalType.BUY:
                best_sl = max(sl_values)  # highest SL = tightest for longs
            else:
                best_sl = min(sl_values)  # lowest SL = tightest for shorts
        else:
            best_sl = sig.stop_loss or 0.0

        # Average TP
        tp_values = [bs.signal.take_profit for bs in signals if bs.signal.take_profit]
        best_tp = sum(tp_values) / len(tp_values) if tp_values else (sig.take_profit or 0.0)

        # Average price
        prices = [bs.signal.price for bs in signals]
        avg_price = sum(prices) / len(prices)

        return ConfluenceResult(
            signal_type=sig.signal_type,
            exchange=sig.exchange,
            segment=sig.segment,
            exchange_token=sig.exchange_token,
            price=avg_price,
            stop_loss=best_sl,
            take_profit=best_tp,
            timestamp_ms=sig.timestamp_ms,
            strategies=sorted(strategies),
            count=len(strategies),
        )

    def reset_daily(self) -> None:
        """Clear all buffers at start of new day."""
        self._buffer.clear()
        self._fired_2.clear()
        self._fired_3.clear()
        logger.info("ConfluenceTracker daily reset")
