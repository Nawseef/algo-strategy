"""
MT5 CFD consumer: connect, fetch live ticks, build 5m candles, store them.

Pulls live bid/ask ticks from the mt5linux feed (x86 VM, via SSH tunnel to
localhost:8001), builds 5m OHLCV candles for the 10 IC Markets CFD instruments,
logs each completed candle, and persists it to research_db.live_candles.
No strategy / no trading yet.

Pipeline:
    MT5 feed (copy_ticks_range) -> Tick(bid) -> EventBus -> CandleBuilder
        -> log + LiveCandleStore (research DB)

Prerequisites:
    * SSH tunnel to the feed VM is up:
        ssh -i <key> -N -L 8001:localhost:8001 ubuntu@144.24.154.233
    * venv has mt5linux + rpyc==5.2.3.

Usage:
    python -m app.main_mt5
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from datetime import datetime, timezone

from app.broker.base import Instrument, Tick
from app.broker.mt5 import MT5Broker, MT5FeedClient
from app.core.candle_builder import TIMEFRAME_MS, CandleBuilder
from app.core.events import EventBus
from app.core.models import Candle, Timeframe
from app.db.live_candle_store import LiveCandleStore
from app.db.research_store import ResearchStore
from app.telegram.mt5_notifier import MT5Notifier
from app.utils import forex_hours
from app.utils.config import load_config
from app.utils.logger import get_logger

logger = get_logger("main_mt5")

# Pretty labels for FX sessions in alerts.
_SESSION_LABEL = {
    "sydney": "Sydney",
    "tokyo": "Tokyo",
    "london": "London",
    "new_york": "New York",
}


class MT5ConsumerApp:
    """Wires the MT5 feed -> 5m candles -> log + persistent storage."""

    def __init__(self) -> None:
        self._config = load_config()
        self._event_bus = EventBus()
        self._candle_builder = CandleBuilder(
            self._event_bus,
            timeframes=[Timeframe.M5],
        )
        self._broker = MT5Broker(self._config.mt5)
        self._feed = MT5FeedClient(
            self._broker,
            self._config.mt5,
            is_market_open=forex_hours.is_market_open,
            seconds_until_open=forex_hours.seconds_until_market_open,
            on_resume=self._maybe_backfill,  # run deferred backfill at market open
        )
        self._startup_backfill_done = False

        # Persistent candle storage (Postgres on VM / SQLite locally).
        self._store = ResearchStore()
        self._candle_store = LiveCandleStore(self._store)

        # Dedicated CFD Telegram alerts (isolated from the NSE bot).
        self._notifier = MT5Notifier(
            self._config.mt5.telegram_bot_token,
            self._config.mt5.telegram_chat_ids,
        )

        # Lightweight tick stats for a periodic heartbeat.
        self._tick_count = 0
        self._candle_count = 0
        self._last_stats_time = time.time()
        self._stats_interval_s = 30.0

        # Session/market-schedule monitor (runs in a background thread).
        self._monitor_running = False
        self._monitor_thread: threading.Thread | None = None
        self._last_market_open: bool | None = None
        self._last_sessions: set[str] = set()

    def start(self) -> None:
        # Storage first, so no completed candle is missed.
        self._store.start()

        # tick -> candle builder; candle -> store + logger + stats
        self._event_bus.subscribe("tick", self._candle_builder.on_tick)
        self._event_bus.subscribe("tick", self._on_tick)
        self._event_bus.subscribe("candle", self._candle_store.on_candle)
        self._event_bus.subscribe("candle", self._on_candle)

        # Connect with retries — the tunnel may not be bound yet at boot. Alert
        # (once) if it stays down, so a broken tunnel isn't a silent restart loop.
        self._connect_with_retry(attempts=6, delay_s=10.0)

        instruments = [
            Instrument(
                exchange=self._config.mt5.exchange,
                segment=self._config.mt5.segment,
                exchange_token=sym,
            )
            for sym in self._config.mt5.symbols
        ]

        def emit_tick(tick: Tick) -> None:
            self._event_bus.emit("tick", tick)

        self._feed.subscribe_ltp(instruments, on_tick=emit_tick)

        st = forex_hours.status()
        logger.info("=" * 60)
        logger.info("MT5 CONSUMER — live 5m candles + storage")
        logger.info("Symbols: %s", ", ".join(self._config.mt5.symbols))
        logger.info("Timeframe: 5m | price basis: bid | store: %s",
                    "postgres" if self._store.is_postgres else "sqlite")
        logger.info(
            "Forex clock: market_open=%s active_sessions=%s trading_sessions=%s",
            st["market_open"], st["active_sessions"] or "-", st["trading_sessions"],
        )
        if not st["market_open"]:
            logger.info(
                "Market CLOSED — next open in ~%.1fh (feed will be quiet until then)",
                float(st["secs_to_open"]) / 3600,
            )
        logger.info("=" * 60)

        # Startup alert + baseline for the session monitor.
        self._last_market_open = bool(st["market_open"])
        self._last_sessions = set(st["active_sessions"])
        sess = ", ".join(_SESSION_LABEL.get(s, s) for s in st["active_sessions"]) or "none"
        self._notifier.send(
            f"🟢 CFD consumer STARTED\n"
            f"Symbols: {len(self._config.mt5.symbols)} | store: "
            f"{'postgres' if self._store.is_postgres else 'sqlite'}\n"
            f"Market: {'OPEN' if st['market_open'] else 'CLOSED'} | sessions: {sess}"
        )
        self._start_session_monitor()

    def _on_tick(self, tick: Tick) -> None:
        self._tick_count += 1
        now = time.time()
        if now - self._last_stats_time >= self._stats_interval_s:
            self._log_stats()
            self._last_stats_time = now

    def _on_candle(self, candle: Candle) -> None:
        self._candle_count += 1
        snap = self._feed.get_ltp().get(candle.exchange_token, {})
        spread = ""
        if snap.get("ask") and snap.get("bid"):
            spread = f" spread~{snap['ask'] - snap['bid']:.5g}"
        open_utc = datetime.fromtimestamp(
            candle.timestamp_ms / 1000, timezone.utc
        ).strftime("%H:%M:%S")
        logger.info(
            "CANDLE | %s %s | open(UTC)=%s | O=%s H=%s L=%s C=%s V=%d%s",
            candle.exchange_token,
            candle.timeframe.value,
            open_utc,
            candle.open, candle.high, candle.low, candle.close,
            candle.volume, spread,
        )

    def _log_stats(self) -> None:
        snapshot = self._feed.get_ltp()
        live = ", ".join(
            f"{sym}={data['bid']:g}" for sym, data in list(snapshot.items())[:5]
        )
        sessions = forex_hours.active_sessions()
        # Quiet symbols only meaningful while the market is open (else all quiet).
        quiet = self._feed.quiet_symbols(120.0) if forex_hours.is_market_open() else []
        quiet_note = f" quiet={','.join(quiet)}" if quiet else ""
        logger.info(
            "STATS | ticks=%d candles=%d stored=%d | sessions=%s can_open=%s%s | live: %s%s",
            self._tick_count,
            self._candle_count,
            self._candle_store.stored,
            "+".join(sessions) if sessions else "closed",
            forex_hours.can_open_new_position(),
            quiet_note,
            live,
            " ..." if len(snapshot) > 5 else "",
        )

    # ─── Session / market-schedule monitor ──────────────────────

    def _start_session_monitor(self) -> None:
        self._monitor_running = True
        self._monitor_thread = threading.Thread(
            target=self._session_monitor_loop, name="session-monitor", daemon=True
        )
        self._monitor_thread.start()

    def _session_monitor_loop(self) -> None:
        """Alert on market open/close and FX session start/end transitions."""
        while self._monitor_running:
            try:
                open_now = forex_hours.is_market_open()
                sessions_now = set(forex_hours.active_sessions())

                # Market open/close transition.
                if self._last_market_open is not None and open_now != self._last_market_open:
                    if open_now:
                        self._notifier.send("🟢 Market OPEN — feed resuming")
                    else:
                        secs = forex_hours.seconds_until_market_open()
                        self._notifier.send(
                            f"🔴 Market CLOSED — next open in ~{secs / 3600:.1f}h"
                        )
                self._last_market_open = open_now

                # Session start/end transitions (only meaningful while open).
                started = sessions_now - self._last_sessions
                ended = self._last_sessions - sessions_now
                for s in sorted(started):
                    self._notifier.send(f"🕒 {_SESSION_LABEL.get(s, s)} session STARTED")
                for s in sorted(ended):
                    self._notifier.send(f"🕒 {_SESSION_LABEL.get(s, s)} session ENDED")
                self._last_sessions = sessions_now
            except Exception as e:  # noqa: BLE001 - monitor must never crash the app
                logger.error("session monitor error: %s", e)
            time.sleep(20.0)

    def _connect_with_retry(self, attempts: int, delay_s: float) -> None:
        """Authenticate to the feed, retrying while the tunnel comes up."""
        for i in range(1, attempts + 1):
            try:
                self._broker.authenticate()
                return
            except Exception as e:  # noqa: BLE001
                logger.error("connect attempt %d/%d failed: %s", i, attempts, e)
                if i < attempts:
                    time.sleep(delay_s)
        # Still down — alert (so it's not a silent restart loop) and give up;
        # systemd will restart the service and we try again.
        self._notifier.send(
            "⚠️ CFD consumer cannot reach the feed (tunnel/RPyC down) — will keep retrying"
        )
        raise RuntimeError("MT5 feed connect failed after retries")

    def _maybe_backfill(self) -> None:
        """Fill the gap since the last stored candle — once, and only with a
        reliably-measured offset (never the weekend fallback, which would misdate
        replayed candles). Deferred to market open if the offset isn't measured yet.
        """
        if self._startup_backfill_done or not self._config.mt5.backfill_enabled:
            return
        if not self._broker.offset_is_measured:
            logger.info(
                "Backfill deferred: server offset not measured yet (market closed?) "
                "— will run on market open"
            )
            return

        interval_ms = TIMEFRAME_MS[Timeframe.M5]
        offset_ms = self._broker.server_offset_ms
        from_msc: dict[str, int] = {}
        for sym in self._config.mt5.symbols:
            last_open = self._store.get_last_live_candle_ms(sym, Timeframe.M5.value)
            if last_open is not None:
                # Resume at the candle after the last stored one, in server ms.
                from_msc[sym] = int(last_open) + interval_ms + offset_ms
        if from_msc:
            logger.info(
                "Backfill: filling gap since last stored candle for %d symbols ...",
                len(from_msc),
            )
            self._feed.backfill(from_msc)
        else:
            logger.info("Backfill: no prior candles (fresh start) — skipping")
        self._startup_backfill_done = True

    def run(self) -> None:
        self.start()

        def shutdown(signum, frame):
            logger.info("Shutdown signal received. Stopping feed ...")
            self._monitor_running = False
            self._feed.stop()
            self._log_stats()
            self._store.stop()
            self._notifier.send("🔴 CFD consumer STOPPED (shutdown signal)", block=True)
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # Backfill any consumer-downtime gap before going live (defers to market
        # open if the offset couldn't be measured yet).
        self._maybe_backfill()

        logger.info("Consuming feed (Ctrl+C to stop). Now(UTC)=%s",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._feed.consume()  # blocking


def main() -> None:
    app = MT5ConsumerApp()
    app.run()


if __name__ == "__main__":
    main()
