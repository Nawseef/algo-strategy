"""
Polling-based market data runner.
Uses synchronous polling instead of the blocking feed.consume().
Good for debugging and testing during development.

Usage:
    python -m app.main_polling
"""

import signal
import sys
import time

from app.analytics.engine import AnalyticsEngine
from app.broker.base import Instrument, Tick
from app.broker.groww import GrowwBroker, GrowwFeedClient
from app.core.candle_builder import CandleBuilder
from app.core.events import EventBus
from app.core.models import Timeframe
from app.db.store import TradeStore
from app.paper_trader.engine import PaperTradingEngine
from app.strategy.engine import StrategyEngine
from app.strategy.sma_crossover import SMACrossoverStrategy
from app.telegram.notifier import TelegramNotifier
from app.utils.config import load_config
from app.utils.logger import get_logger

logger = get_logger("main_polling")

POLL_INTERVAL = 3.0
_running = True

TIMEFRAME_MAP: dict[str, Timeframe] = {
    "1m": Timeframe.M1,
    "5m": Timeframe.M5,
    "15m": Timeframe.M15,
    "30m": Timeframe.M30,
    "1h": Timeframe.H1,
    "1d": Timeframe.D1,
}


def build_instruments(exchange_tokens: list[str]) -> list[Instrument]:
    return [
        Instrument(exchange="NSE", segment="CASH", exchange_token=token)
        for token in exchange_tokens
    ]


def main() -> None:
    global _running

    logger.info("=" * 60)
    logger.info("ALGO-STRATEGY PLATFORM - Polling Mode")
    logger.info("=" * 60)

    config = load_config()
    logger.info("Config loaded. Auth method: %s", config.groww.auth_method)

    if not config.instruments.exchange_tokens:
        logger.error("No instruments configured. Set SUBSCRIBE_INSTRUMENTS in .env")
        sys.exit(1)

    # Event bus + pipeline
    event_bus = EventBus()

    # SQLite store
    trade_store = TradeStore(event_bus)
    trade_store.start()

    # Candle builder
    timeframes = [TIMEFRAME_MAP[t] for t in config.strategy.timeframes if t in TIMEFRAME_MAP]
    candle_builder = CandleBuilder(event_bus, timeframes=timeframes)
    event_bus.subscribe("tick", candle_builder.on_tick)

    # Strategy engine
    strategy_engine = StrategyEngine(event_bus, candle_builder)
    strategy_engine.register(
        SMACrossoverStrategy(
            fast_period=config.strategy.sma_fast_period,
            slow_period=config.strategy.sma_slow_period,
            instrument_tokens=config.instruments.exchange_tokens,
        )
    )
    strategy_engine.start()

    # Paper trader
    paper_trader = PaperTradingEngine(
        event_bus=event_bus,
        default_quantity=config.paper_trading.default_quantity,
        max_open_positions=config.paper_trading.max_open_positions,
    )
    paper_trader.start()

    # Telegram (after paper_trader so it can reference positions)
    telegram = TelegramNotifier(
        event_bus=event_bus,
        bot_token=config.telegram.bot_token,
        chat_id=config.telegram.chat_id,
        paper_trader=paper_trader,
        summary_interval_minutes=config.telegram.summary_interval_minutes,
        notify_signals=config.telegram.notify_signals,
        notify_positions=config.telegram.notify_positions,
        notify_reconnects=config.telegram.notify_reconnects,
        notify_errors=config.telegram.notify_errors,
    )
    telegram.start()

    # Analytics
    analytics = AnalyticsEngine()

    # Authenticate and subscribe
    broker = GrowwBroker(config.groww)
    try:
        broker.authenticate()
    except Exception as e:
        logger.error("Authentication failed: %s", e)
        sys.exit(1)

    instruments = build_instruments(config.instruments.exchange_tokens)
    feed = GrowwFeedClient(broker)
    feed.subscribe_ltp(instruments)

    # Graceful shutdown
    def shutdown(signum, frame):
        global _running
        logger.info("Shutdown signal received")
        _running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Polling every %.1fs. Press Ctrl+C to stop.", POLL_INTERVAL)

    while _running:
        try:
            ltp_data = feed.get_ltp()
            if ltp_data:
                _process_ltp(ltp_data, event_bus)
        except Exception as e:
            logger.error("Poll error: %s", e)
        time.sleep(POLL_INTERVAL)

    # Cleanup
    feed.stop()
    strategy_engine.stop()
    paper_trader.stop()

    if paper_trader.closed_positions:
        report = analytics.analyze(paper_trader.all_positions)
        logger.info("\n%s", report.summary())

    telegram.stop()
    trade_store.stop()
    logger.info("Shutdown complete")


def _process_ltp(data: dict, event_bus: EventBus) -> None:
    """Parse polled LTP data and emit as tick events."""
    ltp_section = data.get("ltp", {})
    for exchange, segments in ltp_section.items():
        for segment, tokens in segments.items():
            for token, tick_data in tokens.items():
                tick = Tick(
                    exchange=exchange,
                    segment=segment,
                    exchange_token=token,
                    ltp=tick_data.get("ltp", 0.0),
                    timestamp_ms=tick_data.get("tsInMillis", 0.0),
                )
                event_bus.emit("tick", tick)
                logger.info(
                    "LTP | %s:%s token=%s | %.2f",
                    exchange, segment, token, tick.ltp,
                )


if __name__ == "__main__":
    main()
