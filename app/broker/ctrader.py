"""
cTrader Open API broker + feed implementation.

Connects directly to Spotware's cloud (IC Markets cTrader) via TCP/WebSocket.
Push-based: the server sends every bid/ask change the moment it happens.
Runs natively on any OS/arch (no Wine, Docker, RPyC, or x86 required).

This replaces the entire MT5 feed VM + SSH tunnel + RPyC bridge with a single
TCP connection from the consumer VM to demo.ctraderapi.com:5035.

Design notes:
  * Uses the ``ctrader-api-client`` asyncio library (pip install ctrader-api-client).
  * The BrokerFeed.consume() method is blocking (matches the interface contract).
    Internally it runs the asyncio event loop. All cTrader async operations happen
    inside that loop.
  * Symbol resolution: cTrader uses numeric symbol IDs. On connect, we resolve
    the configured string names (XAUUSD, US500, etc.) to their IDs and cache
    the mapping.
  * Price basis: ``ltp = bid`` (same convention as the MT5 feed — CFDs have no
    last-traded-price; bid is used for candle building, ask carried for spread).
  * Timestamps are real UTC milliseconds (no server-offset gymnastics like MT5).
  * Reconnection + token refresh are handled by the library automatically.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from app.broker.base import (
    BaseBroker,
    BrokerFeed,
    Instrument,
    MarketDepth,
    Tick,
)
from app.utils.config import CTraderConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


class CTraderBroker(BaseBroker):
    """
    Manages cTrader Open API connection: app auth, account auth, symbol resolution.

    Unlike MT5 (which needs a running terminal), cTrader auth is pure OAuth over
    TCP — no GUI, no Wine, no RPyC.
    """

    def __init__(self, config: CTraderConfig) -> None:
        self._config = config
        self._client: Any = None
        self._account_id: int | None = None
        # symbol_name -> symbol_id mapping (resolved on connect)
        self._symbol_map: dict[str, int] = {}
        # symbol_id -> symbol_name (reverse)
        self._id_to_name: dict[int, str] = {}

    def authenticate(self) -> str:
        """Connect to cTrader, authenticate app + account, resolve symbols.

        This runs the async connection synchronously (called from the main
        thread before the feed loop starts).
        """
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._async_authenticate())
        finally:
            loop.close()
        return "OK"

    async def _async_authenticate(self) -> None:
        """Async authentication flow."""
        from ctrader_api_client import (
            AccountCredentials,
            ClientConfig,
            CTraderClient,
        )

        client_config = ClientConfig(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            host=self._config.host,
            port=self._config.port,
        )

        self._client = CTraderClient(client_config)
        await self._client.__aenter__()

        # Resolve account ID from trader login
        self._account_id = await self._client.accounts.resolve_account_id(
            self._config.access_token,
            trader_login=self._config.account_login,
        )
        logger.info(
            "cTrader account resolved: login=%d -> account_id=%d",
            self._config.account_login, self._account_id,
        )

        # Authenticate the trading account
        await self._client.auth.authenticate_trader(
            AccountCredentials(
                account_id=self._account_id,
                access_token=self._config.access_token,
                refresh_token=self._config.refresh_token,
                expires_at=self._config.token_expires_at,
            )
        )
        logger.info("cTrader account authenticated")

        # Resolve symbol names -> IDs
        await self._resolve_symbols()

    async def _resolve_symbols(self) -> None:
        """Map configured symbol names (XAUUSD, US500, ...) to numeric IDs."""
        all_symbols = await self._client.symbols.list_all(self._account_id)

        # Build a name -> id lookup from the full symbol list
        name_lookup: dict[str, int] = {}
        for sym in all_symbols:
            # cTrader symbols may have suffixes like ".cash" — try exact match first
            name_lookup[sym.name] = sym.id
            # Also index without common suffixes for flexibility
            base = sym.name.replace(".cash", "").replace(".mini", "")
            if base not in name_lookup:
                name_lookup[base] = sym.id

        # Resolve our 10 configured symbols
        for name in self._config.symbols:
            sym_id = name_lookup.get(name)
            if sym_id is None:
                # Try case-insensitive
                for k, v in name_lookup.items():
                    if k.upper() == name.upper():
                        sym_id = v
                        break
            if sym_id is not None:
                self._symbol_map[name] = sym_id
                self._id_to_name[sym_id] = name
                logger.info("  Symbol resolved: %s -> id=%d", name, sym_id)
            else:
                logger.warning("  Symbol NOT FOUND: %s (check cTrader symbol name)", name)

        logger.info(
            "Symbol resolution: %d/%d resolved",
            len(self._symbol_map), len(self._config.symbols),
        )

    def get_instruments(self) -> list[dict[str, Any]]:
        """Return the configured CFD instruments as descriptor dicts."""
        return [
            {
                "exchange": self._config.exchange,
                "segment": self._config.segment,
                "exchange_token": sym,
                "symbol_id": self._symbol_map.get(sym),
            }
            for sym in self._config.symbols
        ]

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("Not connected. Call authenticate() first.")
        return self._client

    @property
    def account_id(self) -> int:
        if self._account_id is None:
            raise RuntimeError("Not connected. Call authenticate() first.")
        return self._account_id

    @property
    def symbol_map(self) -> dict[str, int]:
        return self._symbol_map

    @property
    def id_to_name(self) -> dict[int, str]:
        return self._id_to_name


class CTraderFeedClient(BrokerFeed):
    """
    Push-based live feed for IC Markets CFDs via cTrader Open API.

    Unlike MT5FeedClient (poll every 1s), this receives push events from the
    server on every price change. No polling, no missed ticks, no server-offset
    gymnastics.

    Usage:
        broker = CTraderBroker(config.ctrader)
        broker.authenticate()
        feed = CTraderFeedClient(broker, config.ctrader)
        feed.subscribe_ltp(instruments, on_tick=callback)
        feed.consume()   # blocking
    """

    def __init__(
        self,
        broker: CTraderBroker,
        config: CTraderConfig,
        is_market_open: Callable[[], bool] | None = None,
        seconds_until_open: Callable[[], float] | None = None,
    ) -> None:
        self._broker = broker
        self._config = config
        self._on_tick: Callable[[Tick], None] | None = None
        self._symbols: list[str] = []
        self._running = False

        # Optional market-schedule hooks (same as MT5FeedClient).
        self._is_market_open = is_market_open
        self._seconds_until_open = seconds_until_open

        # Latest LTP snapshot: symbol_name -> {"bid","ask","ltp","timestamp_ms"}
        self._ltp_snapshot: dict[str, dict[str, float]] = {}

        # Stats
        self._tick_count = 0

    # ─── Subscription ────────────────────────────────────────────

    def subscribe_ltp(
        self,
        instruments: list[Instrument],
        on_tick: Callable[[Tick], None] | None = None,
    ) -> None:
        """Register the symbols to stream and the tick callback."""
        self._on_tick = on_tick
        self._symbols = [inst.exchange_token for inst in instruments]
        logger.info(
            "cTrader feed subscribed to %d symbols: %s",
            len(self._symbols), ", ".join(self._symbols),
        )

    def subscribe_market_depth(
        self,
        instruments: list[Instrument],
        on_depth: Callable[[MarketDepth], None] | None = None,
    ) -> None:
        """Market depth not used for CFD candle building (no-op)."""
        logger.warning("CTraderFeedClient.subscribe_market_depth is not implemented (no-op)")

    def unsubscribe_ltp(self, instruments: list[Instrument]) -> None:
        tokens = {inst.exchange_token for inst in instruments}
        self._symbols = [s for s in self._symbols if s not in tokens]

    def unsubscribe_market_depth(self, instruments: list[Instrument]) -> None:
        return None

    def get_ltp(self) -> dict[str, Any]:
        """Return the latest per-symbol LTP snapshot."""
        return dict(self._ltp_snapshot)

    # ─── Per-symbol liveness ─────────────────────────────────────

    def last_tick_age_s(self, symbol: str) -> float | None:
        """Seconds since the symbol's last tick, or None if never seen."""
        d = self._ltp_snapshot.get(symbol)
        if not d:
            return None
        return (time.time() * 1000 - d["timestamp_ms"]) / 1000.0

    def quiet_symbols(self, max_idle_s: float = 120.0) -> list[str]:
        """Subscribed symbols with no recent tick."""
        now_ms = time.time() * 1000
        out = []
        for s in self._symbols:
            d = self._ltp_snapshot.get(s)
            if d is None or (now_ms - d["timestamp_ms"]) > max_idle_s * 1000:
                out.append(s)
        return out

    # ─── Consumption (blocking) ──────────────────────────────────

    def consume(self) -> None:
        """Blocking: runs the asyncio event loop that receives push events.

        This is the cTrader equivalent of MT5FeedClient.consume(). It blocks
        the calling thread and processes spot events until stop() is called.
        """
        self._running = True
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._async_consume())
        finally:
            loop.close()

    async def _async_consume(self) -> None:
        """Subscribe to spots and wait for events (runs inside the asyncio loop)."""
        from ctrader_api_client import SpotEvent

        client = self._broker.client
        account_id = self._broker.account_id
        symbol_map = self._broker.symbol_map
        id_to_name = self._broker.id_to_name
        exchange = self._config.exchange
        segment = self._config.segment

        # Register the spot event handler
        @client.on(SpotEvent)
        async def on_spot(event: SpotEvent) -> None:
            if not self._running:
                return

            symbol_name = id_to_name.get(event.symbol_id)
            if symbol_name is None:
                return  # not one of our subscribed symbols

            # cTrader sends bid/ask as integers with a scale factor.
            # The ctrader-api-client library handles the conversion to float.
            bid = event.bid if event.bid else 0.0
            ask = event.ask if event.ask else 0.0

            # Use bid as ltp (same convention as MT5 — CFDs have no last-traded)
            if bid <= 0:
                return  # Skip if no valid bid

            # Timestamp: use current UTC time in ms (cTrader spot events are
            # real-time push — the moment we receive it IS the tick time).
            timestamp_ms = time.time() * 1000

            tick = Tick(
                exchange=exchange,
                segment=segment,
                exchange_token=symbol_name,
                ltp=bid,
                timestamp_ms=timestamp_ms,
                bid=bid,
                ask=ask,
            )

            self._ltp_snapshot[symbol_name] = {
                "bid": bid,
                "ask": ask,
                "ltp": bid,
                "timestamp_ms": timestamp_ms,
            }

            self._tick_count += 1

            if self._on_tick is not None:
                self._on_tick(tick)

        # Subscribe to spots for all resolved symbols
        symbol_ids = [
            symbol_map[name]
            for name in self._symbols
            if name in symbol_map
        ]

        if not symbol_ids:
            raise RuntimeError(
                "No symbols resolved — cannot subscribe. Check CTRADER_SYMBOLS "
                "match IC Markets cTrader symbol names."
            )

        logger.info(
            "Subscribing to %d symbol spots (IDs: %s)...",
            len(symbol_ids), symbol_ids[:5],
        )
        await client.market_data.subscribe_spots(account_id, symbol_ids)
        logger.info("cTrader spot subscription active — receiving live prices")

        # Block until stop() is called
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    def stop(self) -> None:
        """Stop the feed gracefully."""
        self._running = False
        if hasattr(self, "_stop_event"):
            # Set the event from any thread (the asyncio loop may be in another thread)
            self._stop_event.set()
        logger.info("cTrader feed stopped (ticks received: %d)", self._tick_count)

    @property
    def tick_count(self) -> int:
        return self._tick_count
