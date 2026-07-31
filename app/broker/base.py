"""
Abstract base classes for broker integration.
All broker implementations must conform to these interfaces.
This allows swapping brokers without changing strategy/trading logic.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Tick:
    """Normalized tick data from any broker.

    ``ltp`` is the single price everything downstream consumes (candle builder,
    indicators, strategies). For exchange-traded instruments (NSE/Groww) it is
    the last traded price. For OTC CFDs (IC Markets/MT5) there is no last traded
    price, so ``ltp`` is populated with the ``bid`` (see MT5_FEED_SETUP.md).

    ``bid``/``ask`` are optional and only populated by feeds that quote a
    two-sided market (CFDs). They let us measure spread and model fills
    (buy@ask, sell@bid) without changing any downstream code that reads ``ltp``.
    """

    exchange: str
    segment: str
    exchange_token: str
    ltp: float
    timestamp_ms: float
    bid: float = 0.0
    ask: float = 0.0


@dataclass
class MarketDepthLevel:
    """Single level in the order book."""

    price: float
    quantity: float


@dataclass
class MarketDepth:
    """Normalized market depth from any broker."""

    exchange: str
    segment: str
    exchange_token: str
    timestamp_ms: float
    buy_levels: list[MarketDepthLevel]
    sell_levels: list[MarketDepthLevel]


@dataclass
class Instrument:
    """Instrument subscription descriptor."""

    exchange: str
    segment: str
    exchange_token: str


class BaseBroker(ABC):
    """Abstract broker interface for authentication and REST operations."""

    @abstractmethod
    def authenticate(self) -> str:
        """Authenticate with the broker and return an access token."""
        ...

    @abstractmethod
    def get_instruments(self) -> list[dict[str, Any]]:
        """Fetch available instruments."""
        ...


class BrokerFeed(ABC):
    """Abstract interface for live market data feeds."""

    @abstractmethod
    def subscribe_ltp(
        self,
        instruments: list[Instrument],
        on_tick: Callable[[Tick], None] | None = None,
    ) -> None:
        """Subscribe to last traded price updates."""
        ...

    @abstractmethod
    def subscribe_market_depth(
        self,
        instruments: list[Instrument],
        on_depth: Callable[[MarketDepth], None] | None = None,
    ) -> None:
        """Subscribe to market depth updates."""
        ...

    @abstractmethod
    def unsubscribe_ltp(self, instruments: list[Instrument]) -> None:
        """Unsubscribe from LTP updates."""
        ...

    @abstractmethod
    def unsubscribe_market_depth(self, instruments: list[Instrument]) -> None:
        """Unsubscribe from market depth updates."""
        ...

    @abstractmethod
    def get_ltp(self) -> dict[str, Any]:
        """Get the latest LTP snapshot."""
        ...

    @abstractmethod
    def consume(self) -> None:
        """Start consuming the feed (blocking call)."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the feed gracefully."""
        ...
