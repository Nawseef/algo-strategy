"""
cTrader CFD consumer: connect, receive live spots, build 5m candles, store them.

Push-based replacement for main_mt5.py. Instead of polling an RPyC bridge on a
separate VM every 1s, this connects directly to Spotware's cloud and receives
every bid/ask change the instant it happens.

Pipeline:
    cTrader Open API (SpotEvent push) -> Tick(bid) -> EventBus -> CandleBuilder
        -> log + LiveCandleStore (research DB)

No feed VM needed. No SSH tunnel. No Wine. No Docker. Runs on ARM Linux.

Prerequisites:
    * cTrader Open API app activated (status=Active on openapi.ctrader.com)
    * OAuth tokens in .env (CTRADER_ACCESS_TOKEN, CTRADER_REFRESH_TOKEN)
    * pip install ctrader-api-client

Usage:
    python -m app.main_ctrader
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from datetime import datetime, timezone

from app.broker.base import Instrument, Tick
from app.broker.ctrader import CTraderBroker, CTraderFeedClient
from app.core.candle_builder import CandleBuilder
from app.core.events import EventBus
from app.core.models import Candle, Timeframe
from app.db.live_candle_store import LiveCandleStore
from app.db.research_store import ResearchStore
from app.telegram.mt5_notifier import MT5Notifier
from app.utils import forex_hours
from app.utils.config import load_config
from app.utils.logger import get_logger

logger = get_logger("main_ctrader")

# Pretty labels for FX sessions in alerts.
_SESSION_LABEL = {
    "sydney": "Sydney",
    "tokyo": "Tokyo",
    "london": "London",
    "new_york": "New York",
}


class CTraderConsumerApp:
    """Wires the cTrader feed -> 5m candles -> log + persistent storage."""

    def __init__(self) -> None:
        self._config = load_config()
        self._event_bus = EventBus()
        self._candle_builder = CandleBuilder(
            self._event_bus,
            timeframes=[Timeframe.M5],
        )
        self._broker = CTraderBroker(self._config.ctrader)
        self._feed = CTraderFeedClient(
            self._broker,
            self._config.ctrader,
            is_market_open=forex_hours.is_market_open,
            seconds_until_open=forex_hours.seconds_until_market_open,
        )

        # Persistent candle storage (Postgres on VM / SQLite locally).
        self._store = ResearchStore()
        self._candle_store = LiveCandleStore(self._store)

        # Dedicated CFD Telegram alerts (reuses the same bot as MT5 consumer).
        self._notifier = MT5Notifier(
            self._config.ctrader.telegram_bot_token,
            self._config.ctrader.telegram_chat_ids,
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

        # Connect + authenticate + resolve symbols
        logger.info("Connecting to cTrader Open API (%s)...", self._config.ctrader.host)
        self._broker.authenticate()

        instruments = [
            Instrument(
                exchange=self._config.ctrader.exchange,
                segment=self._config.ctrader.segment,
                exchange_token=sym,
            )
            for sym in self._config.ctrader.symbols
            if sym in self._broker.symbol_map  # only resolved symbols
        ]

        if not instruments:
            raise RuntimeError(
                "No symbols resolved. Check CTRADER_SYMBOLS and cTrader symbol names."
            )

        def emit_tick(tick: Tick) -> None:
            self._event_bus.emit("tick", tick)

        self._feed.subscribe_ltp(instruments, on_tick=emit_tick)

        st = forex_hours.status()
        logger.info("=" * 60)
        logger.info("cTrader CONSUMER — live 5m candles + storage")
        logger.info("Symbols: %s", ", ".join(self._broker.symbol_map.keys()))
        logger.info("Timeframe: 5m | price basis: bid | store: %s",
                    "postgres" if self._store.is_postgres else "sqlite")
        logger.info("Feed: PUSH (cTrader Open API) | no polling | no feed VM")
        logger.info(
            "Forex clock: market_open=%s active_sessions=%s",
            st["market_open"], st["active_sessions"] or "-",
        )
        if not st["market_open"]:
            logger.info(
                "Market CLOSED — next open in ~%.1fh (feed will be quiet until then)",
                float(st["secs_to_open"]) / 3600,
            )
        logger.info("=" * 60)

        # Startup alert
        self._last_market_open = bool(st["market_open"])
        self._last_sessions = set(st["active_sessions"])
        sess = ", ".join(_SESSION_LABEL.get(s, s) for s in st["active_sessions"]) or "none"
        self._notifier.send(
            f"\U0001f7e2 CFD consumer STARTED (cTrader)\n"
            f"Symbols: {len(instruments)} | store: "
            f"{'postgres' if self._store.is_postgres else 'sqlite'}\n"
            f"Market: {'OPEN' if st['market_open'] else 'CLOSED'} | sessions: {sess}\n"
            f"Feed: push (no feed VM)"
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
        quiet = self._feed.quiet_symbols(120.0) if forex_hours.is_market_open() else []
        quiet_note = f" quiet={','.join(quiet)}" if quiet else ""
        logger.info(
            "STATS | ticks=%d candles=%d stored=%d | sessions=%s%s | live: %s%s",
            self._tick_count,
            self._candle_count,
            self._candle_store.stored,
            "+".join(sessions) if sessions else "closed",
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

                if self._last_market_open is not None and open_now != self._last_market_open:
                    if open_now:
                        self._notifier.send("\U0001f7e2 Market OPEN \u2014 feed active")
                    else:
                        secs = forex_hours.seconds_until_market_open()
                        self._notifier.send(
                            f"\U0001f534 Market CLOSED \u2014 next open in ~{secs / 3600:.1f}h"
                        )
                self._last_market_open = open_now

                started = sessions_now - self._last_sessions
                ended = self._last_sessions - sessions_now
                for s in sorted(started):
                    self._notifier.send(f"\U0001f552 {_SESSION_LABEL.get(s, s)} session STARTED")
                for s in sorted(ended):
                    self._notifier.send(f"\U0001f552 {_SESSION_LABEL.get(s, s)} session ENDED")
                self._last_sessions = sessions_now
            except Exception as e:  # noqa: BLE001
                logger.error("session monitor error: %s", e)
            time.sleep(20.0)

    def run(self) -> None:
        self.start()

        def shutdown(signum, frame):
            logger.info("Shutdown signal received. Stopping feed ...")
            self._monitor_running = False
            self._feed.stop()
            self._log_stats()
            self._store.stop()
            self._notifier.send(
                "\U0001f534 CFD consumer STOPPED (shutdown signal)", block=True
            )
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        logger.info(
            "Consuming cTrader feed (Ctrl+C to stop). Now(UTC)=%s",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._feed.consume()  # blocking


def main() -> None:
    app = CTraderConsumerApp()
    app.run()


if __name__ == "__main__":
    main()
