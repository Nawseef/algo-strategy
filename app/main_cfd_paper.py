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
    CFD_PAPER_COST_MODEL        intraday | conservative | zero (default intraday)
    (plus the FX_* flatten/session vars read by app.utils.forex_hours)

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
from app.broker.mt5 import MT5Broker, MT5FeedClient
from app.cfd_execution.account import AccountConfig, PropFirmRules
from app.cfd_execution.base import ExitReason
from app.cfd_execution.multi_account import MultiAccountManager
from app.cfd_risk.costs import (
    COST_MODEL_CONSERVATIVE,
    COST_MODEL_INTRADAY,
    COST_MODEL_ZERO,
)
from app.cfd_strategy.base import CFDStrategy, StrategyContext
from app.cfd_strategy.registry import get_registry
from app.core.candle_builder import TIMEFRAME_MS, CandleBuilder
from app.core.events import EventBus
from app.core.models import Candle, Timeframe
from app.db.live_candle_store import LiveCandleStore
from app.db.research_store import ResearchStore
from app.telegram.cfd_notifier import CFDTradeNotifier
from app.telegram.mt5_notifier import MT5Notifier
from app.utils import forex_hours
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
    "zero": COST_MODEL_ZERO,
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
    ) -> None:
        # ``store``/``notifier`` are injectable so tests can drive the runner
        # with an in-memory store and no Telegram. Production passes neither.
        self._config = load_config()
        self._event_bus = EventBus()
        self._candle_builder = CandleBuilder(self._event_bus, timeframes=[Timeframe.M5])

        # ── Feed (reuse the MT5 bridge; swap to cTrader later without touching
        # the strategy/executor layers). ──
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
        self._candle_store = LiveCandleStore(self._store) if self._archive_candles else None
        # A rich, multi-account notifier (entry/exit + periodic + EOD + session).
        # Injected notifier is used as-is (tests pass a dummy); otherwise wrap the
        # dedicated CFD Telegram transport.
        if notifier is not None:
            self._notifier = notifier
        else:
            transport = MT5Notifier(
                self._config.mt5.telegram_bot_token,
                self._config.mt5.telegram_chat_ids,
            )
            self._notifier = CFDTradeNotifier(transport)
        # Periodic portfolio summary cadence (minutes); 0 disables.
        self._summary_interval_s = max(0.0, _env_float("CFD_PAPER_SUMMARY_MIN", 30.0)) * 60.0
        self._last_summary_ts = 0.0

        # ── Trading account(s) + executor. ──
        cost_model = _COST_MODELS.get(
            _env("CFD_PAPER_COST_MODEL", "intraday").lower(), COST_MODEL_INTRADAY
        )
        self._manager = MultiAccountManager(
            store=self._store, notifier=self._notifier, cost_model=cost_model,
        )
        self._account = self._build_account()
        self._manager.add_account(self._account)
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

        # Schedule / session monitor state.
        self._monitor_running = False
        self._monitor_thread: threading.Thread | None = None
        self._last_market_open: bool | None = None
        self._last_sessions: set[str] = set()
        self._last_trading_day: str | None = None
        self._weekend_flattened = False
        self._daily_flattened = False

    # ─── Setup helpers ───────────────────────────────────────────

    def _build_account(self) -> AccountConfig:
        account_id = _env("CFD_PAPER_ACCOUNT_ID", "cfd_demo")
        balance = _env_float("CFD_PAPER_BALANCE", self._config.paper_trading.starting_balance)
        risk_pct = _env_float("CFD_PAPER_RISK_PCT", 1.0)
        # Generic prop-firm rules are a safe default for a demo paper account:
        # 5% daily / 10% max drawdown, flatten before the weekend gap.
        rules = PropFirmRules(
            firm_name="paper_demo",
            max_risk_per_trade_pct=risk_pct,
        )
        return AccountConfig(
            account_id=account_id,
            initial_balance=balance,
            rules=rules,
            risk_per_trade_pct=risk_pct,
        )

    def _load_strategies(self) -> list[CFDStrategy]:
        """Select which registered strategies to run (all, or a filtered set)."""
        registry = get_registry()
        wanted = [s.strip() for s in _env("CFD_PAPER_STRATEGIES", "").split(",") if s.strip()]
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
        logger.info("=" * 64)
        logger.info("CFD PAPER TRADER — feed -> 5m candles -> strategies -> paper executor")
        logger.info("Account: %s | balance $%.2f | risk/trade %.2f%%",
                    self._account.account_id, self._account.initial_balance,
                    self._account.effective_risk_per_trade_pct())
        logger.info("Strategies: %s",
                    ", ".join(s.strategy_id for s in self._strategies) or "(none)")
        logger.info("Symbols: %s", ", ".join(self._config.mt5.symbols))
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
        self._notifier.session_start(
            self._manager.summaries(),
            [s.strategy_id for s in self._strategies],
            bool(st["market_open"]),
            sess,
        )
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

        for sym in self._config.mt5.symbols:
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
            exchange=self._config.mt5.exchange,
            segment=self._config.mt5.segment,
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
            if strat.timeframe is not Timeframe.M5:
                continue
            if not strat.applies_to(instrument):
                continue
            if len(history) < strat.min_history:
                continue

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
        logger.info(
            "STATS | ticks=%d candles=%d signals=%d | open_pos=%d bal=$%.2f "
            "| sessions=%s | live: %s%s",
            self._tick_count, self._candle_count, self._signal_count,
            open_positions, bal,
            "+".join(sessions) if sessions else "closed",
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
            try:
                self._notifier.eod_report(self._manager.summaries(), self._last_trading_day)
            except Exception as e:  # noqa: BLE001
                logger.error("EOD report failed: %s", e)
            self._notifier.on_day_reset()          # clear the notifier's day tally
            for s in self._strategies:
                try:
                    s.on_day_reset()
                except Exception as e:  # noqa: BLE001
                    logger.error("strategy %s on_day_reset failed: %s", s.strategy_id, e)
        self._last_trading_day = td

        # Periodic portfolio summary (market hours only).
        if self._summary_interval_s > 0 and market_open:
            now_s = time.time()
            if now_s - self._last_summary_ts >= self._summary_interval_s:
                self._last_summary_ts = now_s
                try:
                    sessions = "+".join(forex_hours.active_sessions()) or "closed"
                    self._notifier.periodic_summary(self._manager.summaries(), sessions)
                except Exception as e:  # noqa: BLE001
                    logger.error("periodic summary failed: %s", e)

        # Weekend flatten: fire once when we enter the pre-close window; re-arm
        # after the market closes for the weekend.
        if self._flatten_weekend:
            if forex_hours.should_flatten_before_weekend():
                if not self._weekend_flattened:
                    logger.info("Pre-weekend flatten window — flattening all positions")
                    self._manager.flatten_all(ExitReason.EOD_FLATTEN)
                    self._weekend_flattened = True
                    self._notifier.send("\U0001f3c1 Pre-weekend flatten — all positions closed")
            if not market_open:
                self._weekend_flattened = False  # re-arm for next week

        # Daily-reset flatten (only if the account's rules ask for it).
        if self._flatten_reset:
            if forex_hours.should_flatten_before_daily_reset():
                if not self._daily_flattened:
                    logger.info("Pre-daily-reset flatten window — flattening all positions")
                    self._manager.flatten_all(ExitReason.EOD_FLATTEN)
                    self._daily_flattened = True
                    self._notifier.send("\U0001f3c1 Pre-daily-reset flatten — all positions closed")
            else:
                self._daily_flattened = False  # outside the window -> re-arm

    def _tick_transitions(self) -> None:
        open_now = forex_hours.is_market_open()
        sessions_now = set(forex_hours.active_sessions())

        if self._last_market_open is not None and open_now != self._last_market_open:
            if open_now:
                self._notifier.send("\U0001f7e2 Market OPEN — trading active")
            else:
                secs = forex_hours.seconds_until_market_open()
                self._notifier.send(
                    f"\U0001f534 Market CLOSED — next open in ~{secs / 3600:.1f}h"
                )
        self._last_market_open = open_now

        for s in sorted(sessions_now - self._last_sessions):
            self._notifier.send(f"\U0001f552 {_SESSION_LABEL.get(s, s)} session STARTED")
        for s in sorted(self._last_sessions - sessions_now):
            self._notifier.send(f"\U0001f552 {_SESSION_LABEL.get(s, s)} session ENDED")
        self._last_sessions = sessions_now

    # ─── Feed connect / backfill ─────────────────────────────────

    def _sync_instrument_specs(self) -> None:
        """Correct instrument specs from the broker's authoritative symbol info.

        Best-effort: any failure leaves the static fallback in place and never
        blocks trading. The per-symbol WARNING log (in apply_broker_spec) flags
        any contract size that differed from our hardcoded default.
        """
        from app.cfd_risk import instruments as cfd_instruments

        for sym in self._config.mt5.symbols:
            try:
                spec = self._broker.get_symbol_spec(sym)
            except Exception as e:  # noqa: BLE001
                logger.warning("instrument spec query failed for %s: %s", sym, e)
                continue
            if not spec or spec.get("tick_value") is None:
                continue
            if not spec.get("contract_size") or not spec.get("tick_size"):
                continue
            try:
                cfd_instruments.apply_broker_spec(
                    sym, spec["contract_size"], spec["tick_value"], spec["tick_size"],
                )
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
            "\u26a0\ufe0f CFD paper trader cannot reach the feed (tunnel/RPyC down) — will keep retrying"
        )
        raise RuntimeError("MT5 feed connect failed after retries")

    def _maybe_backfill(self) -> None:
        """Fill the candle gap since the last stored candle (once), only with a
        reliably-measured server offset. Mirrors app.main_mt5 behaviour so the
        archived candle series (and strategy history) stays gapless across
        consumer downtime.
        """
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

    # ─── Run / shutdown ──────────────────────────────────────────

    def run(self) -> None:
        self.start()

        def shutdown(signum, frame):
            logger.info("Shutdown signal received. Flattening + stopping ...")
            self._monitor_running = False
            # Flatten open paper positions on shutdown so nothing is left dangling.
            try:
                self._manager.flatten_all(ExitReason.EOD_FLATTEN)
            except Exception as e:  # noqa: BLE001
                logger.error("flatten on shutdown failed: %s", e)
            self._feed.stop()
            self._log_stats()
            try:
                self._notifier.session_end(self._manager.summaries())
            except Exception as e:  # noqa: BLE001
                logger.error("session_end alert failed: %s", e)
            self._store.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        self._maybe_backfill()

        logger.info("Consuming feed (Ctrl+C to stop). Now(UTC)=%s",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._feed.consume()  # blocking


def main() -> None:
    app = CFDPaperTradingApp()
    app.run()


if __name__ == "__main__":
    main()
