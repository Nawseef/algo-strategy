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
        # symbol_name -> detailed Symbol object (cached lazily by get_symbol_spec).
        # The executor uses its lots_to_volume() / quantize_price() helpers.
        self._symbol_details: dict[str, Any] = {}
        # The ONE asyncio loop the client lives on for its whole lifetime.
        # The ctrader-api-client transport binds to the loop that created it,
        # so auth AND the feed consume loop MUST share this single loop — a
        # fresh loop per call cannot drive a client opened on a different loop.
        self._loop: asyncio.AbstractEventLoop | None = None

    def authenticate(self) -> str:
        """Connect to cTrader, authenticate app + account, resolve symbols.

        Creates the persistent event loop and runs the async connection on it.
        The loop is intentionally left OPEN so the feed's consume() can keep
        driving the same client afterwards (see class docstring / _loop note).
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_authenticate())
        return "OK"

    async def _async_authenticate(self) -> None:
        """Async authentication flow."""
        from ctrader_api_client import (
            AccountCredentials,
            ClientConfig,
            CTraderClient,
        )

        from app.broker.ctrader_tokens import load_persisted_tokens, make_token_store

        # Prefer persisted (rotated) tokens over the .env seed, but only if they
        # are at least as fresh — so pasting a new token pair into .env wins.
        access = self._config.access_token
        refresh = self._config.refresh_token
        expires = self._config.token_expires_at
        persisted = load_persisted_tokens()
        if persisted and int(persisted.get("expires_at") or 0) >= int(expires or 0):
            access = persisted.get("access_token") or access
            refresh = persisted.get("refresh_token") or refresh
            expires = int(persisted.get("expires_at") or expires)
            logger.info("Using persisted cTrader tokens (expires_at=%s)", expires)

        client_config = ClientConfig(
            client_id=self._config.client_id,
            client_secret=self._config.client_secret,
            host=self._config.host,
            port=self._config.port,
        )

        # Persist rotated tokens so a restart after the library refreshes them
        # doesn't fall back to an invalidated .env refresh token.
        token_store = make_token_store()
        if token_store is not None:
            self._client = CTraderClient(client_config, token_store=token_store)
        else:
            self._client = CTraderClient(client_config)
        await self._client.__aenter__()

        # Resolve account ID from trader login
        self._account_id = await self._client.accounts.resolve_account_id(
            access,
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
                access_token=access,
                refresh_token=refresh,
                expires_at=expires,
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
            # SymbolInfo exposes the numeric id as `symbol_id` (NOT `id`);
            # confirmed live against the demo API.
            name_lookup[sym.name] = sym.symbol_id
            # Also index without common suffixes for flexibility
            base = sym.name.replace(".cash", "").replace(".mini", "")
            if base not in name_lookup:
                name_lookup[base] = sym.symbol_id

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

    def get_symbol_spec(self, symbol: str) -> dict[str, Any] | None:
        """Read the AUTHORITATIVE contract spec for a symbol from cTrader.

        Mirrors ``MT5Broker.get_symbol_spec`` so the runner can correct the
        static instrument table on connect. cTrader's detailed ``Symbol`` (from
        ``symbols.get_by_id``) exposes ``lot_size`` (volume units for 1 lot),
        ``digits`` and ``pip_position`` — but NOT a currency-converted tick
        value like MT5 does. So we return ``contract_size`` (units per lot) and
        ``tick_size``, and the runner corrects sizing via
        ``instruments.set_contract_size`` (which rescales the USD-per-move values
        linearly — correct for every quote currency). We deliberately do NOT
        synthesise a ``tick_value`` here: for a non-USD-quote instrument
        (USDJPY, EUR-quoted DE40) that would need a live FX conversion cTrader
        doesn't provide, and a wrong value would corrupt the (IC-correct) table.

        cTrader volume convention: volume is in 1/100 of a unit, so the real
        contract size in the instrument's own units is ``lot_size / 100``
        (verified live: XAGUSD 100000->1000 oz, EURUSD 10000000->100000,
        US30 100->1). Read-only; safe to call between authenticate() and
        consume() (runs on the broker's idle loop).
        """
        sym_id = self._symbol_map.get(symbol)
        if sym_id is None or self._loop is None or self._loop.is_closed():
            return None
        if self._loop.is_running():
            # Can't drive run_until_complete on an already-running loop; specs
            # are synced before consume() starts, so this shouldn't happen.
            logger.warning("get_symbol_spec(%s) skipped: loop already running", symbol)
            return None
        try:
            info = self._loop.run_until_complete(
                self._client.symbols.get_by_id(self._account_id, sym_id)
            )
        except Exception as e:  # noqa: BLE001 - never let a spec query break the app
            logger.warning("cTrader get_symbol_spec(%s) failed: %s", symbol, e)
            return None
        if info is None:
            return None
        self._symbol_details[symbol] = info

        lot_size = getattr(info, "lot_size", None)
        digits = getattr(info, "digits", None)
        if not lot_size or digits is None:
            return None
        tick_size = 10.0 ** (-int(digits))
        contract_size = float(lot_size) / 100.0
        return {
            "symbol": symbol,
            "contract_size": contract_size,
            "tick_size": tick_size,
            "digits": int(digits),
            "pip_position": getattr(info, "pip_position", None),
            "lot_size": lot_size,
            "min_volume": getattr(info, "min_volume", None),
            "step_volume": getattr(info, "step_volume", None),
            "max_volume": getattr(info, "max_volume", None),
            "measurement_units": getattr(info, "measurement_units", None),
        }

    def symbol_details(self, symbol: str) -> Any | None:
        """Return the cached detailed Symbol object (populated by get_symbol_spec).

        The executor uses its ``lots_to_volume(lots)`` and ``quantize_price(p)``
        helpers to build orders in cTrader's native volume/price units.
        """
        return self._symbol_details.get(symbol)

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

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The persistent event loop the client lives on (feed consume() reuses it)."""
        if self._loop is None:
            raise RuntimeError("Not connected. Call authenticate() first.")
        return self._loop

    def close(self) -> None:
        """Best-effort: close the persistent event loop after consume() returns.

        We deliberately do NOT call the client's ``__aexit__`` here: the library
        opens its context in the authenticate() task and enforces that the
        matching exit happens in the *same* task, which a separate run_until_
        complete cannot satisfy. In a long-running feed the process is simply
        SIGTERM'd and the OS tears the socket down, so closing the loop is
        sufficient and avoids a spurious cancel-scope warning.
        """
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.close()


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
        alert_cb: Callable[[str], None] | None = None,
    ) -> None:
        self._broker = broker
        self._config = config
        self._on_tick: Callable[[Tick], None] | None = None
        self._symbols: list[str] = []
        self._running = False

        # Optional market-schedule hooks (same as MT5FeedClient).
        self._is_market_open = is_market_open
        self._seconds_until_open = seconds_until_open

        # Optional alert sink (e.g. notifier.send) for operational events the
        # library surfaces: a dead refresh token, a lost market-data
        # subscription, a reconnect. Kept as a plain callable so the feed stays
        # decoupled from the notifier.
        self._alert_cb = alert_cb

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
        """Blocking: drives the broker's event loop to receive push events.

        This is the cTrader equivalent of MT5FeedClient.consume(). It blocks
        the calling thread and processes spot events until stop() is called.

        Critically, it reuses ``broker.loop`` (the loop the client was opened
        on in authenticate()) rather than creating a new one — the client's
        transport is bound to that loop and cannot be driven by another.
        """
        self._running = True
        loop = self._broker.loop
        loop.run_until_complete(self._async_consume())

    def _alert(self, msg: str) -> None:
        """Route an operational alert to the sink (if any) and always log it."""
        logger.warning("cTrader ALERT: %s", msg)
        if self._alert_cb is not None:
            try:
                self._alert_cb(msg)
            except Exception as e:  # noqa: BLE001 - an alert failure must not crash the feed
                logger.error("alert callback failed: %s", e)

    def _register_ops_handlers(self) -> None:
        """Register handlers for the library's operational events so a dead
        refresh token or a lost subscription is visible (logged + alerted),
        instead of failing silently. Best-effort: any event the installed
        library version doesn't expose is simply skipped.
        """
        client = self._broker.client
        try:
            from ctrader_api_client import (
                ReconnectedEvent,
                SubscriptionRestoreFailedEvent,
                TokenRefreshFailedEvent,
            )
        except Exception as e:  # noqa: BLE001 - older lib without these events
            logger.warning("cTrader ops-event types unavailable (%s) — skipping alert handlers", e)
            return

        @client.on(TokenRefreshFailedEvent)
        async def _on_token_fail(event) -> None:
            # The library retries; a REPEATING event means the refresh token is
            # dead and the account must be re-authorized (re-mint tokens).
            err = getattr(event, "error", None) or getattr(event, "error_code", "")
            self._alert(
                "\u26a0\ufe0f cTrader token refresh FAILED "
                f"(acct {getattr(event, 'account_id', '?')}): {err}. "
                "If this repeats, the refresh token is dead — re-authorize "
                "(mint new tokens) and update .env / data/ctrader_tokens.json."
            )

        @client.on(SubscriptionRestoreFailedEvent)
        async def _on_sub_fail(event) -> None:
            self._alert(
                "\u26a0\ufe0f cTrader market-data subscription not restored after "
                f"reconnect (acct {getattr(event, 'account_id', '?')}): "
                f"{getattr(event, 'error', '')}. Prices may be stale until the "
                "next reconnect."
            )

        @client.on(ReconnectedEvent)
        async def _on_reconnect(event) -> None:
            restored = getattr(event, "restored_accounts", None)
            self._alert(f"\U0001f501 cTrader reconnected (restored: {restored})")

    async def _async_consume(self) -> None:
        """Subscribe to spots and wait for events (runs inside the asyncio loop)."""
        from ctrader_api_client import SpotEvent

        self._register_ops_handlers()

        client = self._broker.client
        account_id = self._broker.account_id
        symbol_map = self._broker.symbol_map
        id_to_name = self._broker.id_to_name
        exchange = self._config.exchange
        segment = self._config.segment

        # The spot event handler. Registered once PER symbol_id below (the
        # per-symbol filter is the delivery pattern validated against the live
        # API; a no-filter catch-all was not confirmed to deliver events).
        async def on_spot(event: SpotEvent) -> None:
            if not self._running:
                return

            symbol_name = id_to_name.get(event.symbol_id)
            if symbol_name is None:
                return  # not one of our subscribed symbols

            # The library delivers bid/ask as Decimal (already scaled to real
            # prices). Cast to float to match the MT5 feed convention — the
            # candle builder and DB layer work in floats, and mixing Decimal
            # with float downstream raises TypeError.
            bid = float(event.bid) if event.bid else 0.0
            ask = float(event.ask) if event.ask else 0.0

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

        # Register the handler for each symbol_id (one dispatch per event).
        for sym_id in symbol_ids:
            client.on(SpotEvent, symbol_id=sym_id)(on_spot)

        logger.info(
            "Subscribing to %d symbol spots (IDs: %s)...",
            len(symbol_ids), symbol_ids[:5],
        )
        await client.market_data.subscribe_spots(account_id, symbol_ids)
        logger.info("cTrader spot subscription active — receiving live prices")

        # Block until stop() is called (the loop keeps running here, so the
        # library's heartbeat / token-refresh / reconnect tasks stay alive).
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    def stop(self) -> None:
        """Stop the feed gracefully.

        Called from a signal handler or the monitor thread — i.e. NOT from
        inside the asyncio loop — so the Event must be set via the loop's
        thread-safe scheduler, otherwise the waiting coroutine never wakes.
        """
        self._running = False
        ev = getattr(self, "_stop_event", None)
        if ev is not None:
            try:
                self._broker.loop.call_soon_threadsafe(ev.set)
            except Exception as e:  # noqa: BLE001
                logger.warning("cTrader stop() could not signal loop: %s", e)
        logger.info("cTrader feed stopped (ticks received: %d)", self._tick_count)

    @property
    def tick_count(self) -> int:
        return self._tick_count
