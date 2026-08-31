"""
CFD paper-trading runner — feed -> 5m candles -> strategies -> paper executor.

This is the glue that turns the built-but-disconnected pieces into a running
paper-trading loop. It reuses the EXISTING MT5 feed (the x86 feed VM over the
SSH tunnel), so it works TODAY — it does not need the cTrader Open API app to be
approved. When cTrader is live you can swap the feed for ``CTraderFeedClient``
without touching the strategy/executor layers.

Pipeline:

    MT5 feed (bid/ask ticks)
        -> EventBus 'tick'
            -> CandleBuilder            (aggregates ticks into 5m candles)
            -> MultiAccountManager.on_tick   (fills armed entries, manages SL/TP)
        -> EventBus 'candle' (on each completed 5m candle)
            -> LiveCandleStore          (archive to research_db.live_candles)
            -> strategy evaluation      (registered CFDStrategies)
                -> MultiAccountManager.on_signal   (open / arm positions)

No real broker orders are placed — the ``PaperExecutor`` simulates fills and
persists closed trades to ``cfd_paper_trades`` (mode=PAPER), alerting on Telegram.
Because it uses the same exit logic as the backtest and the future live path,
paper results are directly comparable to backtests.

Money-safety notes:
  * Every strategy signal already carries a mandatory SL + a >= 1:2 RR TP
    (enforced at signal construction), so nothing here can risk more than it
    targets.
  * The account's RiskGuard (daily/max drawdown) gates every entry.
  * Weekend / daily-reset flattening is handled by the schedule monitor below,
    driven by the account's prop-firm rules.

IMPORTANT — feed exclusivity:
  This runner ALSO archives 5m candles to ``live_candles`` (a superset of
  ``app.main_mt5``). Run it INSTEAD of ``app.main_mt5`` — do not run both against
  the same feed, or you double the poll load on the 1 GB feed VM. Set
  ``CFD_PAPER_ARCHIVE_CANDLES=false`` to disable archiving here (e.g. if you keep
  the plain consumer running separately).

Configuration (env, all optional):
    CFD_PAPER_STRATEGIES        comma list of strategy ids to run (default: all
                                registered). e.g. "gold_london_orb,eu_reversion"
    CFD_PAPER_ACCOUNT_ID        account label (default "cfd_demo")
    CFD_PAPER_BALANCE           starting balance USD (default paper_trading.starting_balance)
    CFD_PAPER_RISK_PCT          risk per trade % (default 1.0)
    CFD_PAPER_ARCHIVE_CANDLES   archive candles to live_candles (default true)
    CFD_PAPER_SUMMARY_MIN       periodic portfolio-summary cadence (min; 0 = off)
    (plus the FX_* flatten/session vars read by app.utils.forex_hours)

Trading streams (paper / demo / live) are configured via app/cfd_execution/streams.py:
    * A JSON file at $CFD_STREAMS_CONFIG (default data/cfd_streams.json if present)
      lists many streams, each with its own kind, balance, risk, cost model,
      enable toggle, and optional dedicated Telegram channel (per prop firm).
    * Otherwise the legacy flat env vars are used:
        CFD_PAPER_EXECUTION_MODE  paper | live | both (default paper)
        CFD_PAPER_ACCOUNT_ID / CFD_DEMO_ACCOUNT_ID   stream labels
        CFD_PAPER_COST_MODEL   (paper)  /  CFD_DEMO_COST_MODEL  (demo)
    demo + live place REAL cTrader orders; their commission/swap are read from
    cTrader's close deal (not modeled). Paper fills are simulated.

Usage:
    python -m app.main_cfd_paper
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from app.broker.base import Instrument, Tick
from app.broker.ctrader import CTraderBroker, CTraderFeedClient
from app.broker.ctrader_backfill import backfill_candles
from app.broker.mt5 import MT5Broker, MT5FeedClient
from app.cfd_execution.account import AccountConfig, PropFirmRules
from app.cfd_execution.base import ExitReason
from app.cfd_execution.ctrader_executor import CTraderExecutor
from app.cfd_execution.multi_account import MultiAccountManager
from app.cfd_execution.paper_executor import PaperExecutor
from app.cfd_execution.streams import StreamConfig, load_streams
from app.cfd_risk.costs import (
    COST_MODEL_CONSERVATIVE,
    COST_MODEL_INTRADAY,
    COST_MODEL_RAW,
    COST_MODEL_ZERO,
)
from app.cfd_strategy.base import CFDStrategy, StrategyContext
from app.cfd_strategy.registry import get_registry
from app.core.candle_builder import TIMEFRAME_MS, CandleBuilder
from app.core.events import EventBus
from app.core.models import Candle, Timeframe
from app.db.live_candle_store import LiveCandleStore
from app.db.research_store import ResearchStore
try:
    from app.telegram.bot_commands import (
        load_persisted_pause,
        start_command_bot,
        stop_command_bot,
    )
    _HAS_CMD_BOT = True
except ImportError:
    _HAS_CMD_BOT = False
from app.telegram.cfd_notifier import ENGINE_NAME, CFDTradeNotifier
from app.telegram.mt5_notifier import MT5Notifier
from app.utils import forex_hours, memory_probe
from app.utils.config import load_config
from app.utils.logger import get_logger

# Importing the strategies package registers every strategy via @register_strategy.
import app.cfd_strategy.strategies  # noqa: F401

logger = get_logger("main_cfd_paper")

_SESSION_LABEL = {
    "sydney": "Sydney",
    "tokyo": "Tokyo",
    "london": "London",
    "new_york": "New York",
}

_COST_MODELS = {
    "intraday": COST_MODEL_INTRADAY,
    "conservative": COST_MODEL_CONSERVATIVE,
    "zero": COST_MODEL_ZERO,      # spread/slippage/swap off, commission STILL charged
    "raw": COST_MODEL_RAW,        # truly no cost: spread, commission, slippage, swap all 0
}

# How many bars of history to seed each instrument with on startup, on top of
# the largest strategy's min_history requirement.
_WARMUP_BUFFER = 10


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class CFDPaperTradingApp:
    """Wires the CFD feed -> candles -> strategies -> paper executor."""

    def __init__(
        self,
        store: ResearchStore | None = None,
        notifier: MT5Notifier | None = None,
        *,
        feed: str = "mt5",
    ) -> None:
        # ``store``/``notifier`` are injectable so tests can drive the runner
        # with an in-memory store and no Telegram. Production passes neither.
        # ``feed`` selects the data source: "mt5" (the interim feed VM) or
        # "ctrader" (the push Open API). Everything downstream is feed-agnostic.
        self._config = load_config()
        self._feed_kind = (feed or "mt5").lower().strip()
        self._event_bus = EventBus()
        self._candle_builder = CandleBuilder(self._event_bus, timeframes=[Timeframe.M5])

        # ── Feed backend. The strategy / executor / notifier / risk layers
        # below never touch the feed directly, so swapping MT5 <-> cTrader here
        # is the ONLY difference between the two runners. ──
        if self._feed_kind == "ctrader":
            self._feed_cfg = self._config.ctrader
            self._broker = CTraderBroker(self._config.ctrader)
            self._feed = CTraderFeedClient(
                self._broker,
                self._config.ctrader,
                is_market_open=forex_hours.is_market_open,
                seconds_until_open=forex_hours.seconds_until_market_open,
                # Surface dead-token / lost-subscription / reconnect events to
                # Telegram (notifier is built just below; resolved at call time).
                alert_cb=lambda m: self._notifier.send(m),
                # On reconnect, self-heal the candle archive for the gap the
                # push feed missed while disconnected (awaited on the loop).
                reconnect_backfill=lambda: self._backfill_ctrader("reconnect"),
            )
        else:
            self._feed_cfg = self._config.mt5
            self._broker = MT5Broker(self._config.mt5)
            self._feed = MT5FeedClient(
                self._broker,
                self._config.mt5,
                is_market_open=forex_hours.is_market_open,
                seconds_until_open=forex_hours.seconds_until_market_open,
                on_resume=self._maybe_backfill,
            )
        self._startup_backfill_done = False

        # ── Storage + alerts (dedicated CFD channel; never the NSE bot). ──
        self._store = store if store is not None else ResearchStore()
        self._archive_candles = _env_bool("CFD_PAPER_ARCHIVE_CANDLES", True)
        # Max span (days) the cTrader candle backfill will fill in one pass;
        # a longer outage's older gap is left for a dedicated history job.
        self._backfill_max_days = _env_float("CFD_BACKFILL_MAX_DAYS", 3.0)
        # Pause between historical requests during backfill (cTrader rate-limits
        # get_trendbars; a rapid 10-symbol burst gets throttled).
        self._backfill_pause_s = _env_float("CFD_BACKFILL_PAUSE_S", 3.0)
        # Minimum window each backfill scans (fills recent interior holes, not
        # just a trailing gap), and a debounce so a reconnect flap can't hammer
        # the historical API.
        #
        # The 90s default caused a SELF-SUSTAINING DISCONNECT LOOP (Aug 27–31):
        # reconnect -> backfill burst (~10 trendbar requests) -> the demo server
        # went silent ~50-70s later -> our 30s heartbeat timeout dropped the
        # link -> reconnect -> 90s debounce expired -> another burst -> forever
        # (~800-980 losses/day for 5 days, market open OR closed). With a 30min
        # min-interval a post-drop burst is isolated enough that the connection
        # stays up (a single burst never killed a calm connection — see the
        # stable Aug 24-26 stretch). 5m-candle gaps linger <=30min, which is
        # acceptable for the research archive.
        self._backfill_lookback_h = _env_float("CFD_BACKFILL_LOOKBACK_H", 6.0)
        self._backfill_min_interval_s = _env_float("CFD_BACKFILL_MIN_INTERVAL_S", 1800.0)
        self._last_backfill_monotonic = 0.0
        self._use_staging = _env_bool("CFD_CTRADER_STAGING", self._feed_kind == "ctrader")
        self._candle_store = LiveCandleStore(
            self._store, use_staging=self._use_staging
        ) if self._archive_candles else None
        # A rich, multi-account notifier (entry/exit + periodic + EOD + session).
        # Injected notifier is used as-is (tests pass a dummy); otherwise wrap the
        # dedicated CFD Telegram transport. cTrader reuses the same CFD bot, so
        # fall back to the MT5_TELEGRAM_* creds when CTRADER_TELEGRAM_* are unset.
        if notifier is not None:
            self._notifier = notifier
        else:
            bot = self._feed_cfg.telegram_bot_token or self._config.mt5.telegram_bot_token
            chats = self._feed_cfg.telegram_chat_ids or self._config.mt5.telegram_chat_ids
            transport = MT5Notifier(bot, chats)
            self._notifier = CFDTradeNotifier(transport)
        # Periodic portfolio summary cadence (minutes); 0 disables. Default hourly.
        # Fired on the ROUND wall-clock boundary (top of the hour for 60m), not
        # "N minutes since boot", by tracking the interval bucket on the epoch.
        self._summary_interval_s = max(0.0, _env_float("CFD_PAPER_SUMMARY_MIN", 60.0)) * 60.0
        self._last_summary_bucket = (
            int(time.time() // self._summary_interval_s)
            if self._summary_interval_s > 0 else 0
        )

        # ── Trading streams (paper / demo / live), config-driven. ──
        # Each stream is an independent account fed the SAME signals, with its own
        # executor, balance, RiskGuard, alert channel, and PAPER/DEMO/LIVE label.
        # Toggle any on/off in the streams config (see app/cfd_execution/streams.py).
        self._streams = load_streams()
        self._manager = MultiAccountManager(store=self._store)

        order_placing = [s for s in self._streams if s.places_orders]
        if order_placing and self._feed_kind != "ctrader":
            raise RuntimeError(
                f"stream(s) {[s.stream_id for s in order_placing]} place real orders "
                "and require feed='ctrader' (MT5 has no order-placement path). "
                "Use app.main_ctrader, or disable those streams."
            )

        # Per-stream notifier: a stream with its own Telegram creds gets its own
        # channel; otherwise it shares the default CFD channel. Cached so several
        # streams with the same creds reuse one transport.
        self._default_notifier = self._notifier
        self._stream_notifiers: dict[str, object] = {}
        self._accounts: dict[str, AccountConfig] = {}
        self._own_channel_cache: dict[tuple[str, str], object] = {}
        for s in self._streams:
            self._stream_notifiers[s.stream_id] = self._notifier_for(s)
            self._accounts[s.stream_id] = self._build_account(
                s.stream_id, balance=s.balance, risk_pct=s.risk_pct,
                ctrader_account_id=s.ctrader_account_id,
            )

        # PAPER streams get their PaperExecutor now. DEMO/LIVE streams place real
        # orders and need the AUTHENTICATED broker, so they are built in start().
        for s in self._streams:
            if s.is_paper:
                ex = PaperExecutor(
                    self._accounts[s.stream_id], store=self._store,
                    notifier=self._stream_notifiers[s.stream_id],
                    cost_model=_COST_MODELS.get(s.cost_model.lower(), COST_MODEL_INTRADAY),
                    kind=s.kind,
                )
                self._manager.add_executor(ex)
        self._order_streams = order_placing  # built in start()
        if order_placing:
            logger.warning(
                "REAL-ORDER streams enabled: %s (env=%s). These place actual "
                "cTrader orders — not a simulation.",
                [s.stream_id for s in order_placing], self._config.ctrader.env,
            )

        # Representative account for logging + flatten policy (rules are shared
        # across streams). Falls back to a throwaway if somehow no streams.
        self._account = (self._accounts[self._streams[0].stream_id]
                         if self._streams else self._build_account("cfd_demo"))
        # Cache the flatten policy for the schedule monitor + entry gating.
        self._flatten_weekend = self._account.rules.flatten_before_weekend
        self._flatten_reset = self._account.rules.flatten_before_daily_reset

        # ── Strategies. ──
        self._strategies = self._load_strategies()

        # ── Runtime state. ──
        self._tick_count = 0
        self._candle_count = 0
        self._signal_count = 0
        self._last_stats_time = time.time()
        self._stats_interval_s = 30.0
        self._bot_data: dict | None = None  # Set in start() when command bot is enabled

        # Schedule / session monitor state.
        self._monitor_running = False
        self._monitor_thread: threading.Thread | None = None
        self._last_market_open: bool | None = None
        self._last_sessions: set[str] = set()
        self._last_trading_day: str | None = None
        self._weekend_flattened = False
        self._daily_flattened = False

    # ─── Setup helpers ───────────────────────────────────────────

    def _build_account(
        self,
        account_id: str,
        balance: float | None = None,
        risk_pct: float | None = None,
        ctrader_account_id: int = 0,
    ) -> AccountConfig:
        if balance is None:
            balance = _env_float("CFD_PAPER_BALANCE", self._config.paper_trading.starting_balance)
        if risk_pct is None:
            risk_pct = _env_float("CFD_PAPER_RISK_PCT", 1.0)
        # Generic prop-firm rules: 5% daily / 10% max drawdown, flatten before
        # the weekend gap. flatten_before_daily_reset defaults ON so live matches
        # the INTRADAY backtest — the research force-flattens each trade at the FX
        # trading-day boundary (17:00 NY), so live must too (otherwise an ORB
        # trade that never hits SL/TP would ride overnight, diverging from the
        # backtest). Disable with CFD_INTRADAY_FLATTEN=false for a swing strategy.
        intraday_flatten = _env_bool("CFD_INTRADAY_FLATTEN", True)
        rules = PropFirmRules(
            firm_name="paper_demo",
            max_risk_per_trade_pct=risk_pct,
            flatten_before_daily_reset=intraday_flatten,
        )
        return AccountConfig(
            account_id=account_id,
            initial_balance=balance,
            rules=rules,
            risk_per_trade_pct=risk_pct,
            ctrader_account_id=ctrader_account_id,
        )

    def _notifier_for(self, stream: StreamConfig):
        """Return the notifier a stream should use: its own dedicated Telegram
        channel if configured, otherwise the shared default CFD channel. Streams
        sharing identical creds reuse one transport (cached)."""
        if not stream.has_own_channel:
            return self._default_notifier
        key = (stream.telegram_bot_token, stream.telegram_chat_id)
        cached = self._own_channel_cache.get(key)
        if cached is None:
            transport = MT5Notifier(stream.telegram_bot_token, [stream.telegram_chat_id])
            cached = CFDTradeNotifier(transport)
            self._own_channel_cache[key] = cached
        return cached

    def _notifier_groups(self) -> list[tuple[object, list[str]]]:
        """Distinct notifiers paired with the account ids that report to them —
        so portfolio/EOD/session messages go to each channel with only its own
        accounts."""
        groups: dict[int, tuple[object, list[str]]] = {}
        for s in self._streams:
            n = self._stream_notifiers[s.stream_id]
            groups.setdefault(id(n), (n, []))[1].append(s.stream_id)
        return list(groups.values())

    def _summaries_for(self, account_ids: list[str]) -> list[dict]:
        ids = set(account_ids)
        return [s for s in self._manager.summaries() if s.get("account_id") in ids]

    def _load_strategies(self) -> list[CFDStrategy]:
        """Select which registered strategies to run (all, or a filtered set)."""
        registry = get_registry()
        wanted = [s.strip() for s in _env("CFD_PAPER_STRATEGIES", "").split(",") if s.strip()]
        # Explicit candle-archiver mode: CFD_PAPER_STRATEGIES=none (or "off").
        # Runs feed -> 5m candles -> live_candles with NO paper trading.
        if [w.lower() for w in wanted] in (["none"], ["off"]):
            logger.info("CFD_PAPER_STRATEGIES=%s -> candle-archiver mode (no strategies)",
                        ",".join(wanted))
            return []
        if wanted:
            selected: list[CFDStrategy] = []
            for sid in wanted:
                try:
                    selected.append(registry.get(sid))
                except KeyError:
                    logger.error(
                        "CFD_PAPER_STRATEGIES lists unknown strategy id '%s' "
                        "(registered: %s) — skipping", sid, registry.ids(),
                    )
            return selected
        return registry.all()

    # ─── Startup ─────────────────────────────────────────────────

    def start(self) -> None:
        self._store.start()

        if not self._strategies:
            logger.warning(
                "No strategies selected — running as a candle archiver only "
                "(set CFD_PAPER_STRATEGIES or add a strategy to "
                "app/cfd_strategy/strategies/).",
            )
        else:
            for s in self._strategies:
                logger.info(
                    "Active strategy: %s (%s) tf=%s instruments=%s min_history=%d",
                    s.strategy_id, s.name, s.timeframe.value,
                    s.instruments or "ALL", s.min_history,
                )
                try:
                    s.on_start()
                except Exception as e:  # noqa: BLE001
                    logger.error("strategy %s on_start failed: %s", s.strategy_id, e)

        # Warm up the candle builder so strategies have history immediately.
        self._warmup()

        # Wire the tick/candle pipeline (no network — safe to unit-test).
        self._wire_pipeline()

        # Connect to the feed (retry while the SSH tunnel comes up at boot).
        self._connect_with_retry(attempts=6, delay_s=10.0)

        # Pull authoritative contract specs from the broker and correct our
        # static table (catches e.g. silver 5000 vs the broker's real 1000).
        self._sync_instrument_specs()

        # DEMO / LIVE streams: build their CTraderExecutor now that the broker is
        # authenticated (they need a live client to place orders), and register
        # each one's execution-event handler on the broker's loop. These run
        # ALONGSIDE any paper streams on the same signals. Real commission/swap
        # are read from cTrader's close deal — the cost_model is only a fallback.
        for s in self._order_streams:
            ex = CTraderExecutor(
                self._accounts[s.stream_id], broker=self._broker, store=self._store,
                notifier=self._stream_notifiers[s.stream_id],
                cost_model=_COST_MODELS.get(s.cost_model.lower(), COST_MODEL_ZERO),
                kind=s.kind,
            )
            ex.start()
            self._manager.add_executor(ex)
            logger.warning(
                "%s executor armed for account '%s' — signals will place REAL "
                "cTrader orders (server-side SL, managed exits).",
                s.kind.upper(), s.stream_id,
            )

        symbols = list(self._feed_cfg.symbols)
        if self._feed_kind == "ctrader":
            # Only trade symbols that resolved to a cTrader id on connect.
            resolved = getattr(self._broker, "symbol_map", {})
            symbols = [s for s in symbols if s in resolved]
        instruments = [
            Instrument(
                exchange=self._feed_cfg.exchange,
                segment=self._feed_cfg.segment,
                exchange_token=sym,
            )
            for sym in symbols
        ]
        if not instruments:
            raise RuntimeError(
                "No instruments to trade (no symbols resolved). Check the feed "
                "symbol config."
            )

        def emit_tick(tick: Tick) -> None:
            self._event_bus.emit("tick", tick)

        self._feed.subscribe_ltp(instruments, on_tick=emit_tick)

        st = forex_hours.status()
        logger.info("=" * 64)
        logger.info("%s — feed -> 5m candles -> strategies -> executors", ENGINE_NAME)
        for s in self._streams:
            logger.info("  stream '%s' [%s] balance $%.0f risk %.2f%% -> %s",
                        s.stream_id, s.kind.upper(), s.balance, s.risk_pct,
                        "own channel" if s.has_own_channel else "shared channel")
        logger.info("Strategies: %s",
                    ", ".join(x.strategy_id for x in self._strategies) or "(none)")
        logger.info("Feed: %s | Symbols: %s", self._feed_kind, ", ".join(symbols))
        logger.info("Archive candles: %s | store: %s",
                    self._archive_candles,
                    "postgres" if self._store.is_postgres else "sqlite")
        logger.info("Flatten: weekend=%s daily_reset=%s",
                    self._flatten_weekend, self._flatten_reset)
        logger.info("Forex clock: market_open=%s active_sessions=%s",
                    st["market_open"], st["active_sessions"] or "-")
        logger.info("=" * 64)

        # Baseline for the monitor + startup alert.
        self._last_market_open = bool(st["market_open"])
        self._last_sessions = set(st["active_sessions"])
        self._last_trading_day = forex_hours.trading_day()
        sess = ", ".join(_SESSION_LABEL.get(s, s) for s in st["active_sessions"]) or "none"
        strat_ids = [x.strategy_id for x in self._strategies]
        # Each channel gets a session-start banner listing ONLY its own streams,
        # with a Mode line composed from those streams' kinds (paper/demo/live).
        for notifier, acct_ids in self._notifier_groups():
            kinds = [s.kind for s in self._streams if s.stream_id in acct_ids]
            notifier.session_start(
                self._summaries_for(acct_ids), strat_ids,
                bool(st["market_open"]), sess, kinds=kinds,
            )

        # ── Telegram command bot (receive commands from owner). ──
        # Uses the same bot token as the notifier (send + receive coexist). Runs
        # on a background daemon thread with its own asyncio loop — non-blocking.
        user_ids = [
            int(x) for x in os.getenv("CFD_TELEGRAM_USER_ID", "").split(",")
            if x.strip()
        ]
        bot_token = (
            self._feed_cfg.telegram_bot_token
            or self._config.mt5.telegram_bot_token
        )
        if user_ids and bot_token and _HAS_CMD_BOT:
            # Restore the paused state persisted by a previous /pause, so a
            # restart does NOT silently resume trading.
            paused = load_persisted_pause()
            if paused:
                logger.warning(
                    "Starting PAUSED (persisted /pause flag present) — no new "
                    "signals will be taken until /resume.",
                )
            self._bot_data = {
                "manager": self._manager,
                "store": self._store,
                "app_ref": self,
                "paused": paused,
                "boot_time": time.time(),
            }
            start_command_bot(
                token=bot_token,
                user_ids=user_ids,
                bot_data=self._bot_data,
            )
        else:
            self._bot_data = None

        self._start_monitor()

    def _wire_pipeline(self) -> None:
        """Subscribe the tick/candle handlers. ORDER MATTERS:

        'tick': candle_builder FIRST (so a boundary tick completes the candle and
                runs strategy eval + entries), then stats, then the executor tick
                router LAST (so it manages any entry the just-completed candle
                opened, on this same tick).
        'candle': archive first (if enabled), then log + strategy evaluation.
        """
        self._event_bus.subscribe("tick", self._candle_builder.on_tick)
        self._event_bus.subscribe("tick", self._on_tick_stats)
        self._event_bus.subscribe("tick", self._route_tick)
        if self._candle_store is not None:
            self._event_bus.subscribe("candle", self._candle_store.on_candle)
        self._event_bus.subscribe("candle", self._on_candle)

    def _warmup(self) -> None:
        """Seed the candle builder with recent history so strategies can act
        from the first live candle instead of waiting ~hours to accumulate bars.

        Prefers the live archive (``live_candles``); falls back to the Dukascopy
        ``cfd_historical_candles`` when the live archive is too short.
        """
        needed = max((s.min_history for s in self._strategies), default=0) + _WARMUP_BUFFER
        if needed <= 0:
            return

        interval_ms = TIMEFRAME_MS[Timeframe.M5]
        now_ms = time.time() * 1000
        # Look back generously (x4) to cover weekend/holiday gaps in bar coverage.
        start_ms = now_ms - needed * interval_ms * 4

        for sym in self._feed_cfg.symbols:
            rows = self._store.get_live_candles(sym, Timeframe.M5.value, start_ms, now_ms)
            source = "live_candles"
            if len(rows) < needed:
                hist = self._store.get_cfd_historical_candles(
                    sym, Timeframe.M5.value, 0, now_ms
                )
                if len(hist) > len(rows):
                    rows = hist
                    source = "cfd_historical_candles"
            if not rows:
                continue
            candles = [self._row_to_candle(sym, r) for r in rows][-needed:]
            self._candle_builder.inject_history(sym, Timeframe.M5, candles)
            logger.info("warmup %s: seeded %d candles from %s",
                        sym, len(candles), source)

    def _row_to_candle(self, symbol: str, row: dict) -> Candle:
        return Candle(
            exchange=self._feed_cfg.exchange,
            segment=self._feed_cfg.segment,
            exchange_token=symbol,
            timeframe=Timeframe.M5,
            timestamp_ms=row["timestamp_ms"],
            open=row["open"], high=row["high"], low=row["low"], close=row["close"],
            volume=row.get("volume", 0) or 0,
        )

    # ─── Tick path ───────────────────────────────────────────────

    def _route_tick(self, tick: Tick) -> None:
        """Forward every tick to the executor (fills armed entries, manages SL/TP)."""
        bid = tick.bid or tick.ltp
        ask = tick.ask or tick.ltp
        if bid <= 0:
            return
        try:
            self._manager.on_tick(tick.exchange_token, bid, ask, tick.timestamp_ms)
        except Exception as e:  # noqa: BLE001 - a management error must not kill the feed
            logger.error("executor on_tick error for %s: %s", tick.exchange_token, e)

    def _on_tick_stats(self, tick: Tick) -> None:
        self._tick_count += 1
        now = time.time()
        if now - self._last_stats_time >= self._stats_interval_s:
            self._log_stats()
            self._last_stats_time = now

    # ─── Candle path (strategy evaluation) ───────────────────────

    def _on_candle(self, candle: Candle) -> None:
        self._candle_count += 1
        self._log_candle(candle)

        instrument = candle.exchange_token
        ts = candle.timestamp_ms

        # 1) Age pre-existing intrabar arms for this instrument FIRST, so a
        #    signal armed on THIS candle gets its full expiry window.
        self._manager.on_candle_close(instrument, ts)

        if not self._strategies:
            return

        # 2) Evaluate strategies on the just-closed candle. get_history includes
        #    this candle (the builder appends before emitting).
        history = self._candle_builder.get_history(instrument, Timeframe.M5)
        entries_blocked = self._entries_blocked()

        for strat in self._strategies:
            if not strat.applies_to(instrument):
                continue
            if len(history) < strat.min_history:
                continue

            # All strategies receive the 5m history. Strategies on higher
            # timeframes (M15, M30, H1) aggregate internally and only act when
            # their HTF bar closes — see e.g. usdjpy_orb._evaluate_htf().
            ctx = StrategyContext(
                instrument=instrument,
                timeframe=strat.timeframe,
                candle=candle,
                history=history,
            )
            try:
                signals = strat.evaluate(ctx)
            except Exception as e:  # noqa: BLE001 - a bad strategy must not kill the loop
                logger.error("strategy %s.evaluate error on %s: %s",
                             strat.strategy_id, instrument, e)
                continue

            for sig in signals:
                if entries_blocked:
                    logger.info(
                        "Entry suppressed (pre-weekend flatten window): %s", sig
                    )
                    continue
                if self._bot_data and self._bot_data.get("paused"):
                    logger.info("Signal skipped (paused via /pause): %s", sig)
                    continue
                self._signal_count += 1
                logger.info("SIGNAL %s", sig)
                self._manager.on_signal(sig)

    def _entries_blocked(self) -> bool:
        """True when we should NOT open new positions (pre-weekend/reset flatten).

        Managing/closing existing positions still happens on ticks; this only
        gates NEW entries so we don't open a trade seconds before flattening it.
        """
        if self._flatten_weekend and forex_hours.should_flatten_before_weekend():
            return True
        if self._flatten_reset and forex_hours.should_flatten_before_daily_reset():
            return True
        return False

    # ─── Logging ─────────────────────────────────────────────────

    def _log_candle(self, candle: Candle) -> None:
        open_utc = datetime.fromtimestamp(
            candle.timestamp_ms / 1000, timezone.utc
        ).strftime("%H:%M:%S")
        logger.info(
            "CANDLE | %s %s | open(UTC)=%s | O=%s H=%s L=%s C=%s V=%d",
            candle.exchange_token, candle.timeframe.value, open_utc,
            candle.open, candle.high, candle.low, candle.close, candle.volume,
        )

    def _log_stats(self) -> None:
        snapshot = self._feed.get_ltp()
        live = ", ".join(
            f"{sym}={data['bid']:g}" for sym, data in list(snapshot.items())[:5]
        )
        sessions = forex_hours.active_sessions()
        summaries = self._manager.summaries()
        open_positions = sum(len(v) for v in self._manager.open_positions().values())
        bal = summaries[0]["balance"] if summaries else 0.0
        rss = memory_probe.rss_mb()
        rss_txt = f" | rss={rss:.0f}MB" if rss is not None else ""
        logger.info(
            "STATS | ticks=%d candles=%d signals=%d | open_pos=%d bal=$%.2f "
            "| sessions=%s%s | live: %s%s",
            self._tick_count, self._candle_count, self._signal_count,
            open_positions, bal,
            "+".join(sessions) if sessions else "closed",
            rss_txt,
            live, " ..." if len(snapshot) > 5 else "",
        )

    # ─── Schedule / session monitor ──────────────────────────────

    def _start_monitor(self) -> None:
        self._monitor_running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="cfd-schedule-monitor", daemon=True
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        """Runs every ~15s: daily DD reset, day-boundary hooks, flatten guards,
        and market/session transition alerts.
        """
        while self._monitor_running:
            try:
                # Memory probe: SIGUSR2 requests are serviced off the loop
                # thread (see app/utils/memory_probe.py). Cheap no-op normally.
                memory_probe.maybe_dump()
                self._tick_schedule()
                self._tick_transitions()
            except Exception as e:  # noqa: BLE001 - monitor must never crash the app
                logger.error("schedule monitor error: %s", e)
            time.sleep(15.0)

    def _tick_schedule(self) -> None:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        market_open = forex_hours.is_market_open()

        # Daily drawdown reset (idempotent — resets only when the boundary is
        # crossed; the RiskGuard tracks the last reset timestamp internally).
        self._manager.on_day_reset(now_ms)

        # FX trading-day boundary -> send EOD report, reset strategies + tally.
        td = forex_hours.trading_day()
        if self._last_trading_day is not None and td != self._last_trading_day:
            for notifier, acct_ids in self._notifier_groups():
                try:
                    notifier.eod_report(self._summaries_for(acct_ids), self._last_trading_day)
                    notifier.on_day_reset()        # clear that channel's day tally
                except Exception as e:  # noqa: BLE001
                    logger.error("EOD report failed: %s", e)
            for s in self._strategies:
                try:
                    s.on_day_reset()
                except Exception as e:  # noqa: BLE001
                    logger.error("strategy %s on_day_reset failed: %s", s.strategy_id, e)
        self._last_trading_day = td

        # Periodic portfolio summary, aligned to the round wall-clock boundary
        # (e.g. top of each UTC hour), and only while the market is open. The
        # bucket advances every interval even when closed, so we never fire a
        # stale "catch-up" summary mid-hour when the market reopens.
        if self._summary_interval_s > 0:
            bucket = int(time.time() // self._summary_interval_s)
            if bucket != self._last_summary_bucket:
                self._last_summary_bucket = bucket
                if market_open:
                    sessions = "+".join(forex_hours.active_sessions()) or "closed"
                    for notifier, acct_ids in self._notifier_groups():
                        try:
                            notifier.periodic_summary(self._summaries_for(acct_ids), sessions)
                        except Exception as e:  # noqa: BLE001
                            logger.error("periodic summary failed: %s", e)

        # Weekend flatten: while inside the pre-close window, RE-ISSUE the flatten
        # each tick as long as anything is still open (a transient broker close
        # failure on one account — e.g. demo — is retried instead of leaving the
        # position open until Monday). Already-closing positions are guarded, so
        # this never double-closes. The Telegram notice fires once.
        if self._flatten_weekend:
            if forex_hours.should_flatten_before_weekend():
                if any(self._manager.open_positions().values()):
                    logger.info("Pre-weekend flatten window — flattening open positions")
                    self._manager.flatten_all(ExitReason.EOD_FLATTEN)
                if not self._weekend_flattened:
                    self._weekend_flattened = True
                    self._notifier.send("\U0001f3c1 Pre-weekend flatten — all positions closed")
            if not market_open:
                self._weekend_flattened = False  # re-arm for next week

        # Daily-reset flatten (only if the account's rules ask for it). Same
        # retry-while-open behaviour so a failed demo close is re-attempted next
        # tick rather than riding overnight and blocking the next day's entry.
        if self._flatten_reset:
            if forex_hours.should_flatten_before_daily_reset():
                if any(self._manager.open_positions().values()):
                    logger.info("Pre-daily-reset flatten window — flattening open positions")
                    self._manager.flatten_all(ExitReason.EOD_FLATTEN)
                if not self._daily_flattened:
                    self._daily_flattened = True
                    self._notifier.send("\U0001f3c1 Pre-daily-reset flatten — all positions closed")
            else:
                self._daily_flattened = False  # outside the window -> re-arm

    def _tick_transitions(self) -> None:
        open_now = forex_hours.is_market_open()
        sessions_now = set(forex_hours.active_sessions())

        if self._last_market_open is not None and open_now != self._last_market_open:
            if open_now:
                self._notifier.send("\U0001f514 Market OPEN — trading active")
            else:
                secs = forex_hours.seconds_until_market_open()
                self._notifier.send(
                    f"\U0001f515 Market CLOSED — next open in ~{secs / 3600:.1f}h"
                )
        self._last_market_open = open_now

        # Per-session STARTED/ENDED banners (Sydney/Tokyo/London/New York) are
        # intentionally NOT sent — they were pure noise now that the hourly
        # portfolio summary shows the active session. The weekly Market
        # OPEN/CLOSED banner above is kept (rare + meaningful).
        self._last_sessions = sessions_now

    # ─── Feed connect / backfill ─────────────────────────────────

    def _sync_instrument_specs(self) -> None:
        """Correct instrument specs from the broker's authoritative symbol info.

        Best-effort: any failure leaves the static fallback in place and never
        blocks trading. The per-symbol WARNING log (in apply_broker_spec) flags
        any contract size that differed from our hardcoded default.
        """
        from app.cfd_risk import instruments as cfd_instruments

        get_spec = getattr(self._broker, "get_symbol_spec", None)
        if get_spec is None:
            return

        for sym in self._feed_cfg.symbols:
            try:
                spec = get_spec(sym)
            except Exception as e:  # noqa: BLE001
                logger.warning("instrument spec query failed for %s: %s", sym, e)
                continue
            if not spec or not spec.get("contract_size"):
                continue
            try:
                if spec.get("tick_value") is not None and spec.get("tick_size"):
                    # MT5 exposes a currency-converted tick value -> exact.
                    cfd_instruments.apply_broker_spec(
                        sym, spec["contract_size"], spec["tick_value"], spec["tick_size"],
                    )
                else:
                    # cTrader gives no tick value; correct the contract size and
                    # let the USD-per-move values rescale linearly (correct for
                    # any quote currency). No-op when it already matches.
                    cfd_instruments.set_contract_size(sym, spec["contract_size"])
            except (KeyError, ValueError) as e:
                logger.warning("could not apply broker spec for %s: %s", sym, e)

    def _connect_with_retry(self, attempts: int, delay_s: float) -> None:
        for i in range(1, attempts + 1):
            try:
                self._broker.authenticate()
                return
            except Exception as e:  # noqa: BLE001
                logger.error("connect attempt %d/%d failed: %s", i, attempts, e)
                if i < attempts:
                    time.sleep(delay_s)
        self._notifier.send(
            f"\u26a0\ufe0f {ENGINE_NAME} cannot reach the feed — will keep retrying"
        )
        raise RuntimeError("MT5 feed connect failed after retries")

    def _maybe_backfill(self) -> None:
        """Fill the candle gap since the last stored candle (once), only with a
        reliably-measured server offset. Mirrors app.main_mt5 behaviour so the
        archived candle series (and strategy history) stays gapless across
        consumer downtime.
        """
        # cTrader (push feed) has its own trendbar-based backfill: on startup we
        # drive it synchronously on the (idle) broker loop; on reconnect the feed
        # awaits it. This fills the candle archive for any gap the push feed
        # missed while the process/connection was down.
        if self._feed_kind == "ctrader":
            if self._startup_backfill_done or self._candle_store is None:
                self._startup_backfill_done = True
                return
            try:
                self._broker.loop.run_until_complete(self._backfill_ctrader("startup"))
            except Exception as e:  # noqa: BLE001 - never block startup on backfill
                logger.error("cTrader startup backfill failed: %s", e)
            self._startup_backfill_done = True
            return
        # Backfill below is MT5-specific (replays missed ticks from the feed VM's
        # history). Warmup already seeds strategy history from stored candles.
        if self._feed_kind != "mt5":
            self._startup_backfill_done = True
            return
        if self._startup_backfill_done or not self._config.mt5.backfill_enabled:
            return
        if not self._archive_candles:
            # Nothing persists candles here; the plain consumer owns the archive.
            self._startup_backfill_done = True
            return
        if not self._broker.offset_is_measured:
            logger.info("Backfill deferred: server offset not measured yet — will run on market open")
            return

        interval_ms = TIMEFRAME_MS[Timeframe.M5]
        offset_ms = self._broker.server_offset_ms
        from_msc: dict[str, int] = {}
        for sym in self._config.mt5.symbols:
            last_open = self._store.get_last_live_candle_ms(sym, Timeframe.M5.value)
            if last_open is not None:
                from_msc[sym] = int(last_open) + interval_ms + offset_ms
        if from_msc:
            logger.info("Backfill: filling gap since last stored candle for %d symbols ...",
                        len(from_msc))
            self._feed.backfill(from_msc)
        else:
            logger.info("Backfill: no prior candles (fresh start) — skipping")
        self._startup_backfill_done = True

    async def _backfill_ctrader(self, reason: str) -> None:
        """Fill the cTrader candle-archive gap (startup or on reconnect).

        Archive-only: writes finished trendbars straight to the store (idempotent,
        session-tagged) without emitting on the EventBus, so no strategy eval /
        order management runs on backfilled history. Runs on the broker loop.
        """
        if self._candle_store is None:
            return
        # Debounce: a reconnect flap can fire many ReconnectedEvents in minutes;
        # don't re-scan the historical API more than once per interval.
        mono = time.monotonic()
        if mono - self._last_backfill_monotonic < self._backfill_min_interval_s:
            logger.info("cTrader %s backfill skipped (debounced)", reason)
            return
        self._last_backfill_monotonic = mono

        resolved = getattr(self._broker, "symbol_map", {})
        symbols = [s for s in self._feed_cfg.symbols if s in resolved]
        if not symbols:
            return
        try:
            n = await backfill_candles(
                self._broker, self._store, self._candle_store, symbols,
                exchange=self._feed_cfg.exchange, segment=self._feed_cfg.segment,
                timeframe=Timeframe.M5, use_staging=self._use_staging,
                min_lookback_hours=self._backfill_lookback_h,
                max_days=self._backfill_max_days, request_pause_s=self._backfill_pause_s,
            )
            logger.info("cTrader %s backfill wrote %d candle(s)", reason, n)
        except Exception as e:  # noqa: BLE001 - a backfill failure must not crash the feed
            logger.error("cTrader %s backfill error: %s", reason, e)

    # ─── Run / shutdown ──────────────────────────────────────────

    def run(self) -> None:
        self.start()

        def shutdown(signum, frame):
            logger.info("Shutdown signal received. Flattening + stopping ...")
            self._monitor_running = False
            # Stop the Telegram command bot gracefully (prevent 30s polling hang).
            if _HAS_CMD_BOT:
                try:
                    stop_command_bot()
                except Exception as e:  # noqa: BLE001
                    logger.error("command bot stop failed: %s", e)
            # Flatten open positions on shutdown so nothing is left dangling.
            # MUST block until the broker close is actually sent, and MUST run
            # BEFORE self._feed.stop() (which tears down the cTrader async loop).
            # The old fire-and-forget flatten_all let the process exit before the
            # loop ran the close, so the demo position stayed open on a restart.
            try:
                self._manager.flatten_all_blocking(ExitReason.EOD_FLATTEN, timeout=10.0)
            except Exception as e:  # noqa: BLE001
                logger.error("flatten on shutdown failed: %s", e)
            self._feed.stop()
            self._log_stats()
            for notifier, acct_ids in self._notifier_groups():
                try:
                    notifier.session_end(self._summaries_for(acct_ids))
                except Exception as e:  # noqa: BLE001
                    logger.error("session_end alert failed: %s", e)
            self._store.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # Memory diagnostics (no-op unless CFD_TRACEMALLOC=1; SIGUSR2 dumps a
        # heap report without restarting the service).
        memory_probe.start_probe()

        self._maybe_backfill()

        logger.info("Consuming feed (Ctrl+C to stop). Now(UTC)=%s",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._feed.consume()  # blocking


def main() -> None:
    app = CFDPaperTradingApp()
    app.run()


if __name__ == "__main__":
    main()
