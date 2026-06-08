"""
Groww broker implementation.
Wraps the official growwapi SDK with our abstract broker interface.

Handles:
- Equity/FNO via subscribe_ltp()
- Indices via subscribe_index_value()
"""

from typing import Any, Callable

import pyotp
from growwapi import GrowwAPI, GrowwFeed

from app.broker.base import (
    BaseBroker,
    BrokerFeed,
    Instrument,
    MarketDepth,
    MarketDepthLevel,
    Tick,
)
from app.utils.config import GrowwConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Known index tokens (Groww uses string names, not numeric tokens for indices)
INDEX_TOKENS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}


def is_index_token(token: str) -> bool:
    """Check if a token is an index (non-numeric or in known index list)."""
    return token.upper() in INDEX_TOKENS or not token.isdigit()


class GrowwBroker(BaseBroker):
    """Groww broker authentication and REST API wrapper."""

    def __init__(self, config: GrowwConfig):
        self._config = config
        self._access_token: str | None = None
        self._api: GrowwAPI | None = None

    def authenticate(self) -> str:
        """
        Authenticate with Groww using configured method.
        Returns the access token.
        """
        logger.info("Authenticating with Groww (method=%s)", self._config.auth_method)

        if self._config.auth_method == "totp":
            self._access_token = self._auth_totp()
        else:
            self._access_token = self._auth_api_key()

        self._api = GrowwAPI(self._access_token)
        logger.info("Authentication successful")
        return self._access_token

    def _auth_api_key(self) -> str:
        """Authenticate using API key + secret."""
        if not self._config.api_key or not self._config.api_secret:
            raise ValueError("GROWW_API_KEY and GROWW_API_SECRET must be set in .env")

        token = GrowwAPI.get_access_token(
            api_key=self._config.api_key,
            secret=self._config.api_secret,
        )
        return token

    def _auth_totp(self) -> str:
        """Authenticate using TOTP flow."""
        if not self._config.totp_token or not self._config.totp_secret:
            raise ValueError("GROWW_TOTP_TOKEN and GROWW_TOTP_SECRET must be set in .env")

        totp_gen = pyotp.TOTP(self._config.totp_secret)
        totp = totp_gen.now()

        token = GrowwAPI.get_access_token(
            api_key=self._config.totp_token,
            totp=totp,
        )
        return token

    def get_instruments(self) -> list[dict[str, Any]]:
        """Fetch instruments list (placeholder for future use)."""
        if not self._api:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return []

    @property
    def api(self) -> GrowwAPI:
        """Access the underlying GrowwAPI instance."""
        if not self._api:
            raise RuntimeError("Not authenticated. Call authenticate() first.")
        return self._api


class GrowwFeedClient(BrokerFeed):
    """
    Groww live market data feed client.
    Wraps GrowwFeed with our abstract BrokerFeed interface.

    Handles both equity (subscribe_ltp) and index (subscribe_index_value)
    subscriptions transparently based on the instrument token type.
    """

    def __init__(self, broker: GrowwBroker):
        self._broker = broker
        self._feed: GrowwFeed | None = None
        self._on_tick: Callable[[Tick], None] | None = None
        self._on_depth: Callable[[MarketDepth], None] | None = None
        self._subscribed_ltp: list[Instrument] = []
        self._subscribed_indices: list[Instrument] = []
        self._subscribed_depth: list[Instrument] = []
        self._running = False

    def _ensure_feed(self) -> GrowwFeed:
        """Lazily initialize the GrowwFeed client. Creates a fresh instance if needed."""
        if self._feed is None:
            self._feed = GrowwFeed(self._broker.api)
            logger.info("GrowwFeed client initialized")
        return self._feed

    def _reset_feed(self) -> None:
        """Reset the feed so the next _ensure_feed() creates a fresh connection."""
        self._feed = None

    def _to_sdk_format(self, instruments: list[Instrument]) -> list[dict[str, str]]:
        """Convert our Instrument dataclass to Groww SDK dict format."""
        return [
            {
                "exchange": inst.exchange,
                "segment": inst.segment,
                "exchange_token": inst.exchange_token,
            }
            for inst in instruments
        ]

    def _split_instruments(
        self, instruments: list[Instrument]
    ) -> tuple[list[Instrument], list[Instrument]]:
        """Split instruments into equities and indices."""
        equities = []
        indices = []
        for inst in instruments:
            if is_index_token(inst.exchange_token):
                indices.append(inst)
            else:
                equities.append(inst)
        return equities, indices

    # ─── Callbacks ───────────────────────────────────────────────

    def _handle_ltp_data(self, meta: dict) -> None:
        """Internal callback for LTP data from Groww feed."""
        logger.debug("LTP data received: %s", meta)

        if self._on_tick:
            feed = self._ensure_feed()
            ltp_data = feed.get_ltp()
            ticks = self._parse_ltp_data(ltp_data)
            for tick in ticks:
                logger.info(
                    "TICK | %s:%s %s | LTP=%.2f",
                    tick.exchange, tick.segment, tick.exchange_token, tick.ltp,
                )
                self._on_tick(tick)

    def _handle_index_data(self, meta: dict) -> None:
        """Internal callback for index value data from Groww feed."""
        logger.debug("Index data received: %s", meta)

        if self._on_tick:
            feed = self._ensure_feed()
            index_data = feed.get_index_value()
            ticks = self._parse_index_data(index_data)
            for tick in ticks:
                logger.info(
                    "TICK | %s:%s %s | VALUE=%.2f",
                    tick.exchange, tick.segment, tick.exchange_token, tick.ltp,
                )
                self._on_tick(tick)

    def _handle_depth_data(self, meta: dict) -> None:
        """Internal callback for market depth data from Groww feed."""
        logger.debug("Market depth data received: %s", meta)

        if self._on_depth:
            feed = self._ensure_feed()
            depth_data = feed.get_market_depth()
            depths = self._parse_depth_data(depth_data)
            for depth in depths:
                self._on_depth(depth)

    # ─── Parsers ─────────────────────────────────────────────────

    def _parse_ltp_data(self, raw: dict) -> list[Tick]:
        """Parse raw LTP response into normalized Tick objects."""
        ticks = []

        # Handle both {"ltp": {"NSE": {...}}} and {"NSE": {...}} formats
        if "ltp" in raw:
            data_root = raw["ltp"]
        else:
            data_root = raw

        for exchange, segments in data_root.items():
            if not isinstance(segments, dict):
                continue
            for segment, tokens in segments.items():
                if not isinstance(tokens, dict):
                    continue
                for token, data in tokens.items():
                    if not isinstance(data, dict):
                        continue
                    ticks.append(
                        Tick(
                            exchange=exchange,
                            segment=segment,
                            exchange_token=token,
                            ltp=data.get("ltp", 0.0),
                            timestamp_ms=data.get("tsInMillis", 0.0),
                        )
                    )
        return ticks

    def _parse_index_data(self, raw: dict) -> list[Tick]:
        """Parse raw index value response into normalized Tick objects."""
        ticks = []

        # Index response: {"NSE": {"CASH": {"NIFTY": {"tsInMillis": ..., "value": ...}}}}
        for exchange, segments in raw.items():
            if not isinstance(segments, dict):
                continue
            for segment, tokens in segments.items():
                if not isinstance(tokens, dict):
                    continue
                for token, data in tokens.items():
                    if not isinstance(data, dict):
                        continue
                    ticks.append(
                        Tick(
                            exchange=exchange,
                            segment=segment,
                            exchange_token=token,
                            ltp=data.get("value", 0.0),
                            timestamp_ms=data.get("tsInMillis", 0.0),
                        )
                    )
        return ticks

    def _parse_depth_data(self, raw: dict) -> list[MarketDepth]:
        """Parse raw market depth response into normalized MarketDepth objects."""
        depths = []

        for exchange, segments in raw.items():
            if not isinstance(segments, dict):
                continue
            for segment, tokens in segments.items():
                if not isinstance(tokens, dict):
                    continue
                for token, data in tokens.items():
                    if not isinstance(data, dict):
                        continue
                    buy_levels = []
                    sell_levels = []

                    for _level, level_data in sorted(data.get("buyBook", {}).items()):
                        buy_levels.append(
                            MarketDepthLevel(
                                price=level_data.get("price", 0.0),
                                quantity=level_data.get("qty", 0.0),
                            )
                        )

                    for _level, level_data in sorted(data.get("sellBook", {}).items()):
                        sell_levels.append(
                            MarketDepthLevel(
                                price=level_data.get("price", 0.0),
                                quantity=level_data.get("qty", 0.0),
                            )
                        )

                    depths.append(
                        MarketDepth(
                            exchange=exchange,
                            segment=segment,
                            exchange_token=token,
                            timestamp_ms=data.get("tsInMillis", 0.0),
                            buy_levels=buy_levels,
                            sell_levels=sell_levels,
                        )
                    )
        return depths

    # ─── Subscribe / Unsubscribe ─────────────────────────────────

    def subscribe_ltp(
        self,
        instruments: list[Instrument],
        on_tick: Callable[[Tick], None] | None = None,
    ) -> None:
        """
        Subscribe to LTP updates for given instruments.
        Automatically routes indices to subscribe_index_value().
        """
        feed = self._ensure_feed()
        self._on_tick = on_tick

        equities, indices = self._split_instruments(instruments)
        self._subscribed_ltp = equities
        self._subscribed_indices = indices

        # Subscribe equities via subscribe_ltp
        if equities:
            sdk_equities = self._to_sdk_format(equities)
            logger.info(
                "Subscribing to LTP for %d equities: %s",
                len(equities),
                [i.exchange_token for i in equities],
            )
            if on_tick:
                feed.subscribe_ltp(sdk_equities, on_data_received=self._handle_ltp_data)
            else:
                feed.subscribe_ltp(sdk_equities)

        # Subscribe indices via subscribe_index_value
        if indices:
            sdk_indices = self._to_sdk_format(indices)
            logger.info(
                "Subscribing to index value for %d indices: %s",
                len(indices),
                [i.exchange_token for i in indices],
            )
            if on_tick:
                feed.subscribe_index_value(sdk_indices, on_data_received=self._handle_index_data)
            else:
                feed.subscribe_index_value(sdk_indices)

    def subscribe_market_depth(
        self,
        instruments: list[Instrument],
        on_depth: Callable[[MarketDepth], None] | None = None,
    ) -> None:
        """Subscribe to market depth for given instruments."""
        feed = self._ensure_feed()
        self._on_depth = on_depth
        self._subscribed_depth = instruments

        sdk_instruments = self._to_sdk_format(instruments)
        logger.info("Subscribing to market depth for %d instruments", len(instruments))

        if on_depth:
            feed.subscribe_market_depth(sdk_instruments, on_data_received=self._handle_depth_data)
        else:
            feed.subscribe_market_depth(sdk_instruments)

    def unsubscribe_ltp(self, instruments: list[Instrument]) -> None:
        """Unsubscribe from LTP updates."""
        feed = self._ensure_feed()
        equities, indices = self._split_instruments(instruments)

        if equities:
            feed.unsubscribe_ltp(self._to_sdk_format(equities))
        if indices:
            feed.unsubscribe_index_value(self._to_sdk_format(indices))

        logger.info("Unsubscribed from LTP for %d instruments", len(instruments))

    def unsubscribe_market_depth(self, instruments: list[Instrument]) -> None:
        """Unsubscribe from market depth updates."""
        feed = self._ensure_feed()
        sdk_instruments = self._to_sdk_format(instruments)
        feed.unsubscribe_market_depth(sdk_instruments)
        logger.info("Unsubscribed from market depth for %d instruments", len(instruments))

    def get_ltp(self) -> dict[str, Any]:
        """Get the latest LTP snapshot."""
        feed = self._ensure_feed()
        return feed.get_ltp()

    def consume(self) -> None:
        """
        Start consuming the feed. This is a BLOCKING call.
        The feed will continuously receive data and trigger callbacks.
        """
        feed = self._ensure_feed()
        self._running = True
        logger.info("Starting feed consumption (blocking)...")
        feed.consume()

    def stop(self) -> None:
        """Stop the feed gracefully."""
        self._running = False
        all_instruments = self._subscribed_ltp + self._subscribed_indices
        if all_instruments:
            self.unsubscribe_ltp(all_instruments)
        if self._subscribed_depth:
            self.unsubscribe_market_depth(self._subscribed_depth)
        logger.info("Feed stopped")
