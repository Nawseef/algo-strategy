"""
Main entry point for the algo-strategy platform.

Wires together the full pipeline:
    Broker Feed → Reconnect → Ticks → Candle Builder → Strategy Engine
    → Paper Trader → SQLite Store → Telegram Alerts

Usage:
    python -m app.main
"""

import signal
import sys

from app.analytics.engine import AnalyticsEngine
from app.broker.base import Instrument, Tick
from app.broker.groww import GrowwBroker, GrowwFeedClient
from app.broker.reconnect import ReconnectingFeed
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

logger = get_logger("main")

# Map string timeframe codes to Timeframe enum
TIMEFRAME_MAP: dict[str, Timeframe] = {
    "1m": Timeframe.M1,
    "5m": Timeframe.M5,
    "15m": Timeframe.M15,
    "30m": Timeframe.M30,
    "1h": Timeframe.H1,
    "1d": Timeframe.D1,
}


def build_instruments(exchange_tokens: list[str]) -> list[Instrument]:
    """Build instrument list from exchange tokens (NSE CASH by default)."""
    return [
        Instrument(exchange="NSE", segment="CASH", exchange_token=token)
        for token in exchange_tokens
    ]


def main() -> None:
    """Main execution flow — full pipeline."""
    logger.info("=" * 60)
    logger.info("ALGO-STRATEGY PLATFORM")
    logger.info("=" * 60)

    # ─── Configuration ───────────────────────────────────────────
    config = load_config()
    logger.info("Config loaded")
    logger.info("  Auth method: %s", config.groww.auth_method)
    logger.info("  Instruments: %s", config.instruments.exchange_tokens)
    logger.info("  Timeframes: %s", config.strategy.timeframes)
    logger.info("  SMA: fast=%d slow=%d", config.strategy.sma_fast_period, config.strategy.sma_slow_period)
    logger.info("  Paper qty: %d, max positions: %s", config.paper_trading.default_quantity, config.paper_trading.max_open_positions or "unlimited")
    logger.info("  Telegram: %s", "enabled" if config.telegram.bot_token else "disabled")

    if not config.instruments.exchange_tokens:
        logger.error("No instruments configured. Set SUBSCRIBE_INSTRUMENTS in .env")
        sys.exit(1)

    # ─── Event Bus ───────────────────────────────────────────────
    event_bus = EventBus()

    # ─── Broker Authentication ───────────────────────────────────
    broker = GrowwBroker(config.groww)
    try:
        broker.authenticate()
    except Exception as e:
        logger.error("Authentication failed: %s", e)
        sys.exit(1)

    # ─── SQLite Store (Phase 5) ──────────────────────────────────
    trade_store = TradeStore(event_bus)
    trade_store.start()

    # ─── Candle Builder (Phase 2) ────────────────────────────────
    timeframes = []
    for tf_str in config.strategy.timeframes:
        tf = TIMEFRAME_MAP.get(tf_str)
        if tf:
            timeframes.append(tf)
        else:
            logger.warning("Unknown timeframe '%s', skipping", tf_str)

    candle_builder = CandleBuilder(event_bus, timeframes=timeframes)
    event_bus.subscribe("tick", candle_builder.on_tick)

    # ─── Strategy Engine (Phase 3) ───────────────────────────────
    strategy_engine = StrategyEngine(event_bus, candle_builder)

    sma_strategy = SMACrossoverStrategy(
        fast_period=config.strategy.sma_fast_period,
        slow_period=config.strategy.sma_slow_period,
        instrument_tokens=config.instruments.exchange_tokens,
    )
    strategy_engine.register(sma_strategy)
    strategy_engine.start()

    # ─── Paper Trading Engine (Phase 4) ──────────────────────────
    paper_trader = PaperTradingEngine(
        event_bus=event_bus,
        default_quantity=config.paper_trading.default_quantity,
        max_open_positions=config.paper_trading.max_open_positions,
    )
    paper_trader.start()

    # ─── Telegram Notifier (Phase 6) ─────────────────────────────
    telegram = TelegramNotifier(
        event_bus=event_bus,
        bot_token=config.telegram.bot_token,
        chat_id=config.telegram.chat_id,
        paper_trader=paper_trader,
        starting_balance=config.paper_trading.starting_balance,
        summary_interval_minutes=config.telegram.summary_interval_minutes,
        notify_signals=config.telegram.notify_signals,
        notify_positions=config.telegram.notify_positions,
        notify_reconnects=config.telegram.notify_reconnects,
        notify_errors=config.telegram.notify_errors,
    )
    telegram.start()

    # ─── Analytics Engine (Phase 7) ──────────────────────────────
    analytics = AnalyticsEngine()

    # ─── Feed Setup (Phase 1 + 2) ───────────────────────────────
    instruments = build_instruments(config.instruments.exchange_tokens)
    feed = GrowwFeedClient(broker)

    def emit_tick(tick: Tick) -> None:
        event_bus.emit("tick", tick)

    reconnecting_feed = ReconnectingFeed(
        feed=feed,
        event_bus=event_bus,
        max_retries=config.reconnect.max_retries,
    )

    def on_reconnect(info: dict) -> None:
        logger.warning(
            "RECONNECT | attempt=%d backoff=%.1fs",
            info["attempt"],
            info["backoff_s"],
        )

    event_bus.subscribe("reconnect", on_reconnect)
    reconnecting_feed.subscribe_ltp(instruments, on_tick=emit_tick)

    # ─── Graceful Shutdown ───────────────────────────────────────
    def shutdown(signum, frame):
        logger.info("Shutdown signal received. Cleaning up...")
        reconnecting_feed.stop()
        strategy_engine.stop()
        paper_trader.stop()

        # Print analytics report on shutdown
        if paper_trader.closed_positions:
            report = analytics.analyze(paper_trader.all_positions)
            logger.info("\n%s", report.summary())

        telegram.stop()
        trade_store.stop()
        logger.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ─── Start ───────────────────────────────────────────────────
    logger.info("Pipeline ready. Starting feed...")
    logger.info("  Feed → Ticks → Candles → Strategy → Paper Trader → DB → Telegram")
    logger.info("Press Ctrl+C to stop")

    try:
        reconnecting_feed.start_blocking()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
