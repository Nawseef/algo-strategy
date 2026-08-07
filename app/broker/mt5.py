"""
MetaTrader 5 (IC Markets CFD) broker + feed implementation.

Consumes the mt5linux RPyC server running on the x86 feed VM (see
MT5_FEED_SETUP.md). This module runs on the ARM consumer VM and reaches the
feed over an SSH tunnel (default localhost:8001).

Design notes (why it looks different from the Groww feed):
  * The Groww feed is push/callback based (WebSocket). MT5/mt5linux has no push
    API, so this feed is POLL based: one batched ``copy_ticks_range`` call per
    symbol per ``poll_interval_s`` (~1s). Per-tick ``symbol_info_tick`` polling
    would overload the 1 GB feed box — batched range pulls are cheap
    (5k ticks in ~80ms, per the load test in MT5_FEED_SETUP.md).
  * CFDs have no last-traded price and no real volume. We use ``bid`` as the
    price (``Tick.ltp = bid``) and also carry ``ask`` for spread/fill modelling.
    Candle volume stays "tick count" (CandleBuilder does ``volume += 1``).
  * Gapless + de-duplicated: each poll queries an overlapping UTC window
    ``[now - (poll_interval + lookback), now]`` and only emits ticks whose
    ``time_msc`` is newer than the last one emitted for that symbol. This is
    robust against poll jitter, reconnect gaps, and the broker's GMT+3 clock
    (we never feed a broker-time value back into a UTC query bound).
  * Timestamps / server-time offset (VERIFIED 30 Jul 2026): tick ``time_msc``
    is in BROKER SERVER time (GMT+3), and ``copy_ticks_range`` interprets its
    ``date_from``/``date_to`` bounds in server time too. Querying with UTC
    bounds silently returns ticks from ``offset`` hours ago — a stale price
    (e.g. XAUUSD 4057 vs live 4077). So we must (a) shift the query window into
    server time (add the offset) to get CURRENT ticks, and (b) subtract the
    offset from ``time_msc`` to store real UTC (matches Dukascopy backtests).
    The offset is AUTO-DETECTED from the live tick on connect (IC Markets
    servers shift GMT+2/GMT+3 with DST, so a hardcoded value would be wrong
    half the year); ``MT5Config.server_utc_offset_hours`` can force a fixed
    value instead.

mt5linux / MetaTrader5 are imported lazily so this module stays importable on
machines without them (e.g. a dev laptop running unit tests).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.broker.base import (
    BaseBroker,
    BrokerFeed,
    Instrument,
    MarketDepth,
    Tick,
)
from app.utils.config import MT5Config
from app.utils.logger import get_logger

logger = get_logger(__name__)


class MT5Broker(BaseBroker):
    """
    Manages the mt5linux connection: initialize() and symbol selection.

    "Authentication" for MT5 is just establishing the RPyC connection and
    calling ``initialize()`` against the already-logged-in terminal (the feed
    VM auto-logs into IC Markets — see MT5_FEED_SETUP.md). We must NOT drive a
    login here.
    """

    # Round detected offset to the nearest 15 min (timezone offsets are always
    # multiples of 15 min; broker servers are whole hours) to shed jitter.
    _OFFSET_ROUND_MS = 15 * 60 * 1000
    # Fallback offset if auto-detection is implausible (e.g. no fresh tick over
    # a weekend): IC Markets summer time. Winter would be +2.
    _FALLBACK_OFFSET_MS = 3 * 60 * 60 * 1000

    def __init__(self, config: MT5Config) -> None:
        self._config = config
        self._mt5: Any = None
        self._copy_ticks_all: int | None = None
        self._server_offset_ms: int = 0
        # True only when the offset came from a live tick (or explicit config) —
        # NOT when the weekend/holiday fallback was used. Backfill must not run
        # on an unmeasured offset (it would misdate replayed candles by ~1h).
        self._offset_measured: bool = False

    def authenticate(self) -> str:
        """Connect to the mt5linux server, initialize, and select symbols."""
        self.connect()
        return "OK"

    def connect(self) -> None:
        """(Re)create the RPyC client, initialize MT5, and select all symbols."""
        # Lazy import so the module is importable without mt5linux installed.
        from mt5linux import MetaTrader5

        logger.info(
            "Connecting to mt5linux server at %s:%d ...",
            self._config.host,
            self._config.port,
        )
        self._mt5 = MetaTrader5(host=self._config.host, port=self._config.port)

        if not self._mt5.initialize():
            raise RuntimeError(
                f"mt5.initialize() failed (last_error={self._safe_last_error()})"
            )

        self._copy_ticks_all = self._mt5.COPY_TICKS_ALL

        info = self._mt5.account_info()
        if info is not None:
            logger.info(
                "MT5 connected: login=%s balance=%s server=%s",
                info.login, info.balance, info.server,
            )
        else:
            logger.warning("MT5 connected but account_info() is None (logged out?)")

        self.select_symbols()
        self._detect_server_offset()

    def _measure_offset_ms(self) -> int | None:
        """Measure server offset from the freshest live tick.

        ``time_msc`` is server time, so ``offset = freshest_time_msc - now``.
        Returns rounded ms, or ``None`` if there is no fresh/plausible tick
        (e.g. market closed) — callers decide whether to fall back or keep the
        current value. Uses epoch ``time.time()`` so it is correct regardless of
        the host machine's local timezone (both VMs run IST, not UTC).
        """
        real_now_ms = time.time() * 1000
        freshest = 0
        for sym in self._config.symbols:
            try:
                tk = self._mt5.symbol_info_tick(sym)
            except Exception:  # noqa: BLE001
                continue
            if tk is not None and int(tk.time_msc) > freshest:
                freshest = int(tk.time_msc)

        if freshest == 0:
            return None

        raw = freshest - real_now_ms
        rounded = round(raw / self._OFFSET_ROUND_MS) * self._OFFSET_ROUND_MS
        # Plausible timezone offsets are within ±14h. Larger => the "freshest"
        # tick is stale (market closed on a weekend) — treat as unmeasurable.
        if abs(rounded) > 14 * 3600 * 1000:
            return None
        return int(rounded)

    def _detect_server_offset(self) -> None:
        """Set the offset on connect: configured value, else measured, else fallback."""
        cfg = self._config.server_utc_offset_hours
        if cfg is not None:
            self._server_offset_ms = int(cfg * 3600 * 1000)
            self._offset_measured = True  # explicit config is authoritative
            logger.info("MT5 server offset: %+.2fh (configured, fixed)", cfg)
            return

        measured = self._measure_offset_ms()
        if measured is None:
            self._server_offset_ms = self._FALLBACK_OFFSET_MS
            self._offset_measured = False
            logger.warning(
                "MT5 server offset: no fresh tick to measure (market closed?) — "
                "using fallback %+.2fh; will re-detect once ticks flow",
                self._FALLBACK_OFFSET_MS / 3600000,
            )
            return

        self._server_offset_ms = measured
        self._offset_measured = True
        logger.info("MT5 server offset: %+.2fh (auto-detected)", measured / 3600000)

    def refresh_offset(self) -> None:
        """Re-measure the offset while running (handles DST flips and the
        weekend->market-open transition without a restart).

        No-op if the offset is fixed by config. If the market is quiet (no fresh
        tick) the current offset is kept. Only updates + logs on an actual change.
        """
        if self._config.server_utc_offset_hours is not None:
            return
        measured = self._measure_offset_ms()
        if measured is None:
            return  # market quiet — keep current offset
        if measured != self._server_offset_ms:
            logger.warning(
                "MT5 server offset changed: %+.2fh -> %+.2fh (DST or server clock "
                "shift) — updating",
                self._server_offset_ms / 3600000, measured / 3600000,
            )
            self._server_offset_ms = measured
        self._offset_measured = True  # we now have a live-tick measurement

    def select_symbols(self) -> None:
        """Select all configured symbols in Market Watch (required on connect)."""
        for sym in self._config.symbols:
            try:
                ok = self._mt5.symbol_select(sym, True)
                if not ok:
                    logger.warning("symbol_select(%s) returned False", sym)
            except Exception as e:  # noqa: BLE001 - log and continue selecting others
                logger.error("symbol_select(%s) failed: %s", sym, e)

    def get_instruments(self) -> list[dict[str, Any]]:
        """Return the configured CFD instruments as descriptor dicts."""
        return [
            {
                "exchange": self._config.exchange,
                "segment": self._config.segment,
                "exchange_token": sym,
            }
            for sym in self._config.symbols
        ]

    def get_symbol_spec(self, symbol: str) -> dict[str, Any] | None:
        """Read the AUTHORITATIVE contract spec for a symbol from the broker.

        MT5's ``symbol_info`` reports the exact contract size and tick economics
        the broker uses for P&L. The dollar value of a 1.0 price move per lot is
        ``trade_tick_value / trade_tick_size`` (already in the account currency,
        so cross-currency conversion is baked in) — this is more reliable than
        computing it from ounces/units, and it differs by broker (e.g. IC
        Markets silver is 1000 oz, others 5000). Read-only; safe to call anytime.

        Returns a dict of the relevant fields, or ``None`` if unavailable.
        """
        if self._mt5 is None:
            return None
        try:
            self._mt5.symbol_select(symbol, True)
            si = self._mt5.symbol_info(symbol)
            if si is None:
                return None
            d = si._asdict()
        except Exception as e:  # noqa: BLE001 - never let a spec query break the app
            logger.warning("symbol_info(%s) failed: %s", symbol, e)
            return None
        return {
            "symbol": symbol,
            "contract_size": d.get("trade_contract_size"),
            "tick_value": d.get("trade_tick_value"),
            "tick_size": d.get("trade_tick_size"),
            "point": d.get("point"),
            "digits": d.get("digits"),
            "volume_min": d.get("volume_min"),
            "volume_step": d.get("volume_step"),
            "volume_max": d.get("volume_max"),
            "currency_profit": d.get("currency_profit"),
        }

    def _safe_last_error(self) -> Any:
        try:
            return self._mt5.last_error()
        except Exception:  # noqa: BLE001
            return "unavailable"

    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            raise RuntimeError("Not connected. Call connect()/authenticate() first.")
        return self._mt5

    @property
    def copy_ticks_all(self) -> int:
        if self._copy_ticks_all is None:
            raise RuntimeError("Not connected. Call connect()/authenticate() first.")
        return self._copy_ticks_all

    @property
    def server_offset_ms(self) -> int:
        """Broker server-time offset from UTC in ms (set on connect)."""
        return self._server_offset_ms

    @property
    def offset_is_measured(self) -> bool:
        """True if the offset is from a live tick or explicit config (reliable
        for backfill), False if the weekend/holiday fallback is in use."""
        return self._offset_measured


class MT5FeedClient(BrokerFeed):
    """
    Poll-based live feed for MT5 CFDs.

    Usage:
        broker = MT5Broker(config.mt5)
        broker.authenticate()
        feed = MT5FeedClient(broker, config.mt5)
        feed.subscribe_ltp(instruments, on_tick=callback)
        feed.consume()   # blocking
    """

    def __init__(
        self,
        broker: MT5Broker,
        config: MT5Config,
        is_market_open: Callable[[], bool] | None = None,
        seconds_until_open: Callable[[], float] | None = None,
        on_resume: Callable[[], None] | None = None,
    ) -> None:
        self._broker = broker
        self._config = config
        self._on_tick: Callable[[Tick], None] | None = None
        self._symbols: list[str] = []
        self._running = False
        # Set on stop()/shutdown so a long backfill can bail out promptly.
        self._stopping = False
        # Called at the market closed->open transition (after offset refresh) so
        # a backfill deferred at cold start (offset not yet measurable) can run.
        self._on_resume = on_resume

        # Optional market-schedule hooks (injected so the feed stays decoupled
        # from forex_hours). When the market is closed the loop idles instead of
        # polling a dead feed — nothing breaks, and it resumes cleanly at open.
        self._is_market_open = is_market_open
        self._seconds_until_open = seconds_until_open
        self._market_was_open: bool | None = None

        # Per-symbol cursor: highest time_msc emitted so far (broker-time ms).
        self._last_msc: dict[str, int] = {}
        # Latest LTP snapshot: symbol -> {"bid","ask","ltp","timestamp_ms"}
        self._ltp_snapshot: dict[str, dict[str, float]] = {}
        # Monotonic time of the last server-offset re-check.
        self._last_offset_check: float = 0.0

    # ─── Subscription ────────────────────────────────────────────

    def subscribe_ltp(
        self,
        instruments: list[Instrument],
        on_tick: Callable[[Tick], None] | None = None,
    ) -> None:
        """Register the symbols to poll and the tick callback."""
        self._on_tick = on_tick
        self._symbols = [inst.exchange_token for inst in instruments]
        logger.info(
            "MT5 feed subscribed to %d symbols: %s",
            len(self._symbols), ", ".join(self._symbols),
        )

    def subscribe_market_depth(
        self,
        instruments: list[Instrument],
        on_depth: Callable[[MarketDepth], None] | None = None,
    ) -> None:
        """Market depth is not consumed in milestone 1 (bid/ask ticks only)."""
        logger.warning("MT5FeedClient.subscribe_market_depth is not implemented (no-op)")

    def unsubscribe_ltp(self, instruments: list[Instrument]) -> None:
        tokens = {inst.exchange_token for inst in instruments}
        self._symbols = [s for s in self._symbols if s not in tokens]

    def unsubscribe_market_depth(self, instruments: list[Instrument]) -> None:
        return None

    def get_ltp(self) -> dict[str, Any]:
        """Return the latest per-symbol LTP snapshot."""
        return dict(self._ltp_snapshot)

    # ─── Per-symbol liveness (tick freshness) ────────────────────
    # The MT5 Python API exposes no per-symbol trading schedule, so we infer
    # "is this instrument trading right now" from how recently it last ticked.
    # This is data-driven (no hardcoded/assumed hours) and covers the daily
    # breaks that indices/metals/oil have.

    def last_tick_age_s(self, symbol: str) -> float | None:
        """Seconds since the symbol's last tick, or None if never seen."""
        d = self._ltp_snapshot.get(symbol)
        if not d:
            return None
        return (time.time() * 1000 - d["timestamp_ms"]) / 1000.0

    def is_symbol_trading(self, symbol: str, max_idle_s: float = 120.0) -> bool:
        """True if the symbol ticked within ``max_idle_s`` (i.e. currently active)."""
        age = self.last_tick_age_s(symbol)
        return age is not None and age <= max_idle_s

    def quiet_symbols(self, max_idle_s: float = 120.0) -> list[str]:
        """Subscribed symbols with no recent tick (in a break / not trading)."""
        now_ms = time.time() * 1000
        out = []
        for s in self._symbols:
            d = self._ltp_snapshot.get(s)
            if d is None or (now_ms - d["timestamp_ms"]) > max_idle_s * 1000:
                out.append(s)
        return out

    # ─── Consumption loop ────────────────────────────────────────

    def consume(self) -> None:
        """Blocking poll loop. Pulls ticks per symbol and emits Tick events."""
        self._running = True
        backoff = self._config.reconnect_backoff_s
        # We just detected the offset on connect; don't re-check immediately.
        self._last_offset_check = time.time()
        self._last_alive_log = time.time()
        logger.info(
            "MT5 feed consuming: poll=%.1fs lookback=%.1fs offset=%+.2fh redetect=%.0fs",
            self._config.poll_interval_s,
            self._config.lookback_s,
            self._broker.server_offset_ms / 3600000,
            self._config.offset_redetect_s,
        )

        while self._running:
            cycle_start = time.time()

            # ── Market-closed idle: don't poll a dead feed (weekends/holidays).
            if self._is_market_open is not None and not self._is_market_open():
                if self._market_was_open is not False:
                    secs = self._seconds_until_open() if self._seconds_until_open else 0
                    logger.info(
                        "Market CLOSED — feed idling (next open in ~%.1fh)",
                        secs / 3600,
                    )
                    self._market_was_open = False
                # Sleep in bounded chunks so stop()/reopen are responsive.
                time.sleep(min(60.0, max(5.0, self._config.poll_interval_s)))
                continue
            if self._market_was_open is False:
                logger.info("Market OPEN — resuming feed")
                self._market_was_open = True
                # Re-detect offset immediately on reopen (may have crossed DST).
                self._broker.refresh_offset()
                self._last_offset_check = cycle_start
                # Run any backfill that was deferred at cold start (offset now measured).
                if self._on_resume is not None:
                    try:
                        self._on_resume()
                    except Exception as e:  # noqa: BLE001
                        logger.error("on_resume hook failed: %s", e)

            # ── Liveness heartbeat log — independent of tick flow, so the
            # watchdog doesn't false-restart a healthy-but-quiet consumer.
            if cycle_start - self._last_alive_log >= 60.0:
                logger.info(
                    "feed alive: market open, %d/%d symbols quiet",
                    len(self.quiet_symbols(120.0)), len(self._symbols),
                )
                self._last_alive_log = cycle_start

            try:
                self._poll_all_symbols()
                # Healthy cycle — reset backoff.
                backoff = self._config.reconnect_backoff_s
                # Periodically re-measure the server offset (DST / weekend->open).
                if cycle_start - self._last_offset_check >= self._config.offset_redetect_s:
                    self._broker.refresh_offset()
                    self._last_offset_check = cycle_start
            except Exception as e:  # noqa: BLE001 - any RPyC/connection error
                if not self._running:
                    break
                logger.error("MT5 feed poll error: %s. Reconnecting in %.1fs ...", e, backoff)
                time.sleep(backoff)
                self._reconnect()
                backoff = min(backoff * 2, self._config.reconnect_backoff_max_s)
                continue

            # Sleep the remainder of the poll interval (account for poll duration).
            elapsed = time.time() - cycle_start
            remaining = self._config.poll_interval_s - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def _poll_all_symbols(self) -> None:
        """One poll cycle across all subscribed symbols."""
        mt5 = self._broker.mt5
        flags = self._broker.copy_ticks_all
        offset_ms = self._broker.server_offset_ms

        # Bounds must be in SERVER time to get current ticks (see module docstring),
        # so shift the UTC window forward by the offset.
        server_now = datetime.now(timezone.utc) + timedelta(milliseconds=offset_ms)
        to_time = server_now
        from_time = server_now - timedelta(
            seconds=self._config.poll_interval_s + self._config.lookback_s
        )

        for symbol in self._symbols:
            ticks = mt5.copy_ticks_range(symbol, from_time, to_time, flags)
            if ticks is None:
                continue
            self._process_ticks(symbol, ticks, offset_ms)

    def _process_ticks(self, symbol: str, ticks: Any, offset_ms: int) -> int:
        """Emit new ticks (time_msc > cursor) for a symbol, de-duplicated.

        Returns the number of new ticks emitted (used by backfill accounting).
        """
        last_seen = self._last_msc.get(symbol, 0)
        max_msc = last_seen
        emitted = 0

        for row in ticks:
            msc = int(row["time_msc"])
            if msc <= last_seen:
                continue  # already processed in a previous overlapping window

            bid = float(row["bid"])
            ask = float(row["ask"])
            # Convert broker server time -> real UTC ms.
            timestamp_ms = float(msc - offset_ms)

            tick = Tick(
                exchange=self._config.exchange,
                segment=self._config.segment,
                exchange_token=symbol,
                ltp=bid,  # CFD price basis = bid (see MT5_FEED_SETUP.md)
                timestamp_ms=timestamp_ms,
                bid=bid,
                ask=ask,
            )

            self._ltp_snapshot[symbol] = {
                "bid": bid,
                "ask": ask,
                "ltp": bid,
                "timestamp_ms": timestamp_ms,
            }

            if self._on_tick is not None:
                self._on_tick(tick)
            emitted += 1

            if msc > max_msc:
                max_msc = msc

        if max_msc > last_seen:
            self._last_msc[symbol] = max_msc
        return emitted

    # ─── Backfill ────────────────────────────────────────────────

    def backfill(self, from_msc: dict[str, int]) -> None:
        """Replay ticks the consumer missed, from the feed's history.

        ``from_msc`` maps symbol -> broker-server epoch ms to resume from.
        Ticks are pulled in chunks and pushed through the normal tick path, so
        they build + store completed candles and advance the live cursor. The
        current (in-progress) candle is left for live ticks to finish.
        """
        self._catch_up(from_msc, label="backfill")

    def _catch_up(self, from_msc: dict[str, int], label: str) -> None:
        """Chunked pager: replay [from_msc .. now] per symbol through the feed."""
        if not self._config.backfill_enabled:
            return
        offset_ms = self._broker.server_offset_ms
        mt5 = self._broker.mt5
        flags = self._broker.copy_ticks_all
        now_server_ms = int(time.time() * 1000) + offset_ms
        chunk_ms = int(self._config.backfill_chunk_s * 1000)
        max_span_ms = int(self._config.backfill_max_days * 86400 * 1000)

        pause_s = self._config.backfill_chunk_pause_s
        grand_total = 0
        for symbol in self._symbols:
            if self._stopping:
                logger.info("%s: interrupted by shutdown", label)
                return
            start = int(from_msc.get(symbol, 0) or 0)
            if start <= 0:
                continue
            if now_server_ms - start > max_span_ms:
                logger.warning(
                    "%s: %s gap exceeds %.1fd — capping (older gap left for history job)",
                    label, symbol, self._config.backfill_max_days,
                )
                start = now_server_ms - max_span_ms
            # Skip trivially small gaps (the rolling live window covers those).
            if now_server_ms - start < 2000:
                continue

            cur = start
            sym_total = 0
            while cur < now_server_ms - 1000:
                if self._stopping:
                    logger.info("%s: interrupted by shutdown", label)
                    return
                to = min(cur + chunk_ms, now_server_ms)
                frm_dt = datetime.fromtimestamp(cur / 1000, timezone.utc)
                to_dt = datetime.fromtimestamp(to / 1000, timezone.utc)
                try:
                    ticks = mt5.copy_ticks_range(symbol, frm_dt, to_dt, flags)
                except Exception as e:  # noqa: BLE001
                    logger.error("%s pull failed for %s: %s", label, symbol, e)
                    break
                rows = list(ticks) if ticks is not None else []
                sym_total += self._process_ticks(symbol, rows, offset_ms)
                cur = to
                # Throttle: don't machine-gun the 1 GB feed VM during a big replay.
                if pause_s > 0 and cur < now_server_ms - 1000:
                    time.sleep(pause_s)
            if sym_total:
                grand_total += sym_total
                logger.info("%s: %s replayed %d ticks", label, symbol, sym_total)
        if grand_total:
            logger.info("%s complete: %d ticks replayed", label, grand_total)
        else:
            logger.info("%s: nothing to replay (no gap)", label)

    def _reconnect(self) -> None:
        """Re-establish the mt5linux connection, re-select symbols, fill the gap."""
        try:
            self._broker.connect()
            logger.info("MT5 feed reconnected")
            # Fill any ticks missed during the outage (feed retains history).
            if self._last_msc:
                self._catch_up(dict(self._last_msc), label="reconnect catch-up")
        except Exception as e:  # noqa: BLE001 - stay in loop, retry next cycle
            logger.error("MT5 reconnect failed: %s", e)

    def stop(self) -> None:
        """Stop the consume loop (and interrupt any in-progress backfill)."""
        self._running = False
        self._stopping = True
        logger.info("MT5 feed stopped")
