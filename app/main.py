"""
Main entry point for the algo-strategy platform.

Wires together the full pipeline:
    Broker Feed → Reconnect → Ticks → Candle Builder → Strategy Engine
    → Multi-Trader (Isolated + Confluence) → SQLite Store → Telegram Alerts

Architecture:
    - 5 isolated paper traders (one per strategy, no limits)
    - 2 confluence traders (2+ agree, 3+ agree)
    - All receive the same tick feed for SL/TP execution
    - Each strategy is evaluated independently AND in combination

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
from app.paper_trader.multi_trader import MultiTraderManager
from app.strategy.bb_squeeze import BBSqueezeStrategy
from app.strategy.cpr_filter import CPRFilter
from app.strategy.ema_crossover import EMACrossoverStrategy
from app.strategy.engine import StrategyEngine
from app.strategy.orb import ORBStrategy
from app.strategy.sma_crossover import SMACrossoverStrategy
from app.strategy.supertrend_strategy import SuperTrendStrategy
from app.strategy.vwap_rsi import VWAPRSIStrategy
from app.telegram.notifier import TelegramNotifier
from app.utils.config import load_config
from app.utils.instruments import get_instrument_map
from app.utils.logger import get_logger
from app.warmup import DataManager

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


def _send_boot_report(telegram, config, warmup_result, strategy_engine, multi_trader) -> None:
    """Send a comprehensive boot report to Telegram on startup."""
    from datetime import datetime
    from app.utils.instruments import get_instrument_short_name

    now = datetime.now()
    strategies = [s.name for s in strategy_engine.strategies]
    instruments = [get_instrument_short_name(t) for t in config.instruments.exchange_tokens]

    # Warmup section
    if warmup_result:
        if warmup_result.failed == 0:
            warmup_status = f"OK — {warmup_result.candles_loaded} candles loaded in {warmup_result.duration_seconds:.1f}s"
        else:
            warmup_status = (
                f"PARTIAL — {warmup_result.successful}/{warmup_result.total_requests} OK, "
                f"{warmup_result.failed} failed"
            )
            if warmup_result.errors:
                warmup_status += "\n    Errors: " + ", ".join(warmup_result.errors[:3])
    else:
        warmup_status = "Disabled"

    trader_names = list(multi_trader.all_traders.keys())

    msg = (
        f"{'='*30}\n"
        f"BOT READY\n"
        f"{now.strftime('%d %b %Y')} | {now.strftime('%I:%M:%S %p')}\n"
        f"{'='*30}\n\n"
        f"--- Mode ---\n"
        f"MULTI-TRADER (Isolated + Confluence)\n"
        f"Each strategy trades independently\n"
        f"+ Confluence when 2+ or 3+ agree\n\n"
        f"--- Warmup ---\n"
        f"{warmup_status}\n\n"
        f"--- Strategies ({len(strategies)}) ---\n"
        + "\n".join(f"  {s}" for s in strategies)
        + f"\n\n--- Paper Traders ({len(trader_names)}) ---\n"
        + "\n".join(f"  {name}" for name in trader_names)
        + f"\n\n--- Instruments ({len(instruments)}) ---\n"
        + "\n".join(f"  {name}" for name in instruments)
        + f"\n\n--- Config ---\n"
        f"Balance per trader: Rs.{config.paper_trading.starting_balance:,.0f}\n"
        f"Position size: {config.paper_trading.position_size_pct}%\n"
        f"No position limits (isolated evaluation)\n"
        f"Timeframes: {', '.join(config.strategy.timeframes)}\n\n"
        f"{'='*30}\n"
        f"Waiting for market open at 9:15 AM\n"
        f"{'='*30}"
    )

    telegram.send_message(msg)


def _send_warmup_starting(telegram, config) -> None:
    """Send immediate notification that bot is alive and warming up."""
    from datetime import datetime
    now = datetime.now()

    msg = (
        f"{'='*30}\n"
        f"BOT STARTING\n"
        f"{now.strftime('%d %b %Y')} | {now.strftime('%I:%M:%S %p')}\n"
        f"{'='*30}\n\n"
        f"Auth: {config.groww.auth_method.upper()} OK\n"
        f"Instruments: {len(config.instruments.exchange_tokens)}\n"
        f"Mode: Multi-Trader (Isolated + Confluence)\n"
        f"Warming up historical data...\n"
    )

    telegram.send_message(msg)


def main() -> None:
    """Main execution flow — full pipeline."""
    logger.info("=" * 60)
    logger.info("ALGO-STRATEGY PLATFORM — MULTI-TRADER MODE")
    logger.info("=" * 60)

    # ─── Configuration ───────────────────────────────────────────
    config = load_config()
    logger.info("Config loaded")
    logger.info("  Auth method: %s", config.groww.auth_method)
    logger.info("  Instruments: %s", config.instruments.exchange_tokens)
    logger.info("  Timeframes: %s", config.strategy.timeframes)
    logger.info("  Mode: Multi-Trader (Isolated + Confluence)")
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

    # ─── CPR Filter (shared across all strategies) ───────────────
    cpr_filter = CPRFilter()
    # CPR will be calculated from warmup data after warmup completes

    # Strategy 1: ORB (Opening Range Breakout)
    orb_strategy = ORBStrategy(
        instrument_tokens=config.instruments.exchange_tokens,
        rr_ratio=1.5,
        max_range_pct=1.5,
        min_range_pct=0.1,
        use_vwap_filter=True,
        cpr_filter=cpr_filter,
    )
    strategy_engine.register(orb_strategy)

    # Strategy 2: VWAP + RSI Pullback
    vwap_rsi_strategy = VWAPRSIStrategy(
        instrument_tokens=config.instruments.exchange_tokens,
        rsi_period=14,
        rsi_oversold=40.0,
        rsi_overbought=60.0,
        adx_threshold=20.0,
        min_sl_pct=0.3,
        rr_ratio=2.0,
        max_trades_per_day=2,
        cpr_filter=cpr_filter,
    )
    strategy_engine.register(vwap_rsi_strategy)

    # Strategy 3: EMA 9/21 Crossover + ADX
    ema_strategy = EMACrossoverStrategy(
        instrument_tokens=config.instruments.exchange_tokens,
        fast_period=9,
        slow_period=21,
        adx_threshold=25.0,
        atr_period=14,
        sl_atr_multiplier=1.5,
        tp_atr_multiplier=3.0,
        volume_multiplier=1.3,
        use_vwap_filter=True,
        cooldown_candles=5,
        cpr_filter=cpr_filter,
    )
    strategy_engine.register(ema_strategy)

    # Strategy 4: SuperTrend
    supertrend_strategy = SuperTrendStrategy(
        instrument_tokens=config.instruments.exchange_tokens,
        atr_period=10,
        multiplier=3.0,
        ema_period=20,
        rr_ratio=2.0,
        max_flips_in_window=3,
        chop_window=10,
        cpr_filter=cpr_filter,
    )
    strategy_engine.register(supertrend_strategy)

    # Strategy 5: Bollinger Band Squeeze
    bb_squeeze_strategy = BBSqueezeStrategy(
        instrument_tokens=config.instruments.exchange_tokens,
        bb_period=20,
        bb_std=2.0,
        min_squeeze_candles=5,
        rr_ratio=1.5,
        volume_multiplier=1.5,
        use_vwap_filter=True,
        max_trades_per_day=2,
        cpr_filter=cpr_filter,
    )
    strategy_engine.register(bb_squeeze_strategy)

    # Strategy 6: SMA Crossover (baseline comparison)
    sma_strategy = SMACrossoverStrategy(
        fast_period=config.strategy.sma_fast_period,
        slow_period=config.strategy.sma_slow_period,
        instrument_tokens=config.instruments.exchange_tokens,
    )
    strategy_engine.register(sma_strategy)

    # ─── Multi-Trader Manager (Isolated + Confluence) ────────────
    strategy_names = [s.name for s in strategy_engine.strategies]
    multi_trader = MultiTraderManager(
        event_bus=event_bus,
        strategy_names=strategy_names,
        starting_balance=config.paper_trading.starting_balance,
        position_size_pct=config.paper_trading.position_size_pct,
    )
    multi_trader.setup()

    # ─── Telegram Notifier ───────────────────────────────────────
    # Pass the first isolated trader for basic position tracking in telegram
    # The notifier will also subscribe to confluence signals
    telegram = TelegramNotifier(
        event_bus=event_bus,
        bot_token=config.telegram.bot_token,
        chat_id=config.telegram.chat_id,
        paper_trader=None,  # We'll use multi_trader for summaries
        starting_balance=config.paper_trading.starting_balance,
        summary_interval_minutes=config.telegram.summary_interval_minutes,
        notify_signals=config.telegram.notify_signals,
        notify_positions=config.telegram.notify_positions,
        notify_reconnects=config.telegram.notify_reconnects,
        notify_errors=config.telegram.notify_errors,
        multi_trader=multi_trader,
    )

    # Send "bot starting" immediately (before warmup)
    _send_warmup_starting(telegram, config)

    # ─── Historical Data Warmup ──────────────────────────────────
    warmup_result = None
    if config.warmup.enabled:
        logger.info("─── Starting Historical Data Warmup ───")
        instrument_map = get_instrument_map()
        data_manager = DataManager(
            broker=broker,
            candle_builder=candle_builder,
            concurrency=config.warmup.concurrency,
            delay_between_requests_ms=config.warmup.delay_ms,
            max_retries=config.warmup.max_retries,
            retry_backoff_base=config.warmup.retry_backoff_base,
        )
        warmup_result = data_manager.warmup(
            strategies=strategy_engine.strategies,
            exchange_tokens=config.instruments.exchange_tokens,
            instrument_map=instrument_map,
        )
        logger.info("Warmup: %s", warmup_result.summary())
        if warmup_result.errors:
            for err in warmup_result.errors[:5]:
                logger.warning("  Warmup error: %s", err)
    else:
        logger.info("Warmup disabled (WARMUP_ENABLED=false)")

    # Start strategy engine (after warmup so indicators have context)
    strategy_engine.start()

    # ─── Calculate CPR from warmup data ──────────────────────────
    # CPR needs previous day's H/L/C — get from candle builder history
    if config.instruments.exchange_tokens:
        first_token = config.instruments.exchange_tokens[0]
        from app.core.models import Timeframe as TF
        history_5m = candle_builder.get_history(first_token, TF.M5)
        if history_5m:
            cpr_filter.calculate_from_5m_candles(history_5m)
            logger.info("CPR bias: %s | Market type: %s", cpr_filter.bias.value, cpr_filter.market_type.value)
        else:
            logger.info("CPR: No historical data available, using neutral bias")

    # ─── Start Multi-Trader & Telegram ───────────────────────────
    multi_trader.start()
    telegram.start()

    # Send full boot report (after warmup, with results)
    _send_boot_report(telegram, config, warmup_result, strategy_engine, multi_trader)

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
        multi_trader.stop()

        # Print multi-strategy comparison
        logger.info("\n%s", multi_trader.get_summary())

        telegram.stop()
        trade_store.stop()
        logger.info("Shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ─── Start ───────────────────────────────────────────────────
    logger.info("Pipeline ready. Starting feed...")
    logger.info("  Feed → Ticks → Candles → Strategy → Multi-Trader → DB → Telegram")
    logger.info("  %d isolated traders + 2 confluence traders", len(strategy_names))
    logger.info("Press Ctrl+C to stop")

    try:
        reconnecting_feed.start_blocking()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
