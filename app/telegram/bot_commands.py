"""
Telegram command interface for the CFD trading engine.

Receives commands from the owner (whitelist-only), queries the live engine
state, and replies. Runs on a BACKGROUND THREAD with its own asyncio loop
(the main app is threaded/blocking, not async).

Uses the SAME bot token as the send-only MT5Notifier. Telegram supports one
bot both sending and receiving simultaneously — no conflict. But only ONE
process should poll for updates at a time (no duplicate getUpdates callers).

Commands:
  /status     — portfolio snapshot (per-account balance, day PnL, DD, open count)
  /positions  — each open position detail (instrument, direction, entry, floating)
  /today      — today's closed trades + day totals
  /last N     — last N closed trades from the DB (default 5)
  /risk       — risk guard state per account (DD used/remaining, status, trades)
  /ping       — uptime, ticks, candles, feed status, last tick age
  /config     — running strategies, risk %, sizing, mode, instruments
  /closeall   — flatten all positions (requires YES confirmation within 30s)
  /close SYM  — close positions on a specific instrument (requires YES confirm)
  /pause      — stop taking new signals (keep managing open positions)
  /resume     — resume taking signals
  /help       — list all available commands
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from datetime import datetime, timezone
from functools import wraps

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ─── Security ─────────────────────────────────────────────────────────────────
AUTHORIZED_USER_IDS: set[int] = set()


def _authorized(func):
    """Decorator: silently ignore commands from non-whitelisted users."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user is None:
            return
        if update.effective_user.id not in AUTHORIZED_USER_IDS:
            return  # silent ignore
        return await func(update, context)

    return wrapper


# ─── Formatting helpers (mirror cfd_notifier.py) ──────────────────────────────


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _price(price: float, instrument: str | None = None) -> str:
    """Format a price with instrument-aware decimal precision."""
    if instrument:
        try:
            from app.cfd_risk.instruments import get_instrument

            inst = get_instrument(instrument)
            decimals = max(1, -int(math.floor(math.log10(inst.pip_size))) + 1)
            return f"{price:.{decimals}f}"
        except Exception:
            pass
    return f"{price:.6f}".rstrip("0").rstrip(".")


def _b(text: str) -> str:
    return f"<b>{text}</b>"


def _i(text: str) -> str:
    return f"<i>{text}</i>"


def _code(text: str) -> str:
    return f"<code>{text}</code>"


def _duration(seconds: float) -> str:
    """Human-friendly duration string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days = hours // 24
    hours = hours % 24
    return f"{days}d {hours}h"


def _hold_time(entry_ms: float) -> str:
    """Time since entry in human-friendly format."""
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return _duration((now_ms - entry_ms) / 1000)


# ─── Kind icon (matches cfd_notifier) ────────────────────────────────────────
_KIND_ICON = {
    "paper": "\U0001f4c4",  # 📄
    "demo": "\U0001f4e5",   # 📥
    "live": "\u26a1",       # ⚡
}


# ─── Command Handlers ─────────────────────────────────────────────────────────


@_authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Portfolio snapshot — per-account balance, day PnL, DD, open positions."""
    mgr = context.bot_data["manager"]
    summaries = mgr.summaries()
    app_ref = context.bot_data["app_ref"]
    paused = context.bot_data.get("paused", False)

    now = datetime.now(timezone.utc)
    lines = [
        f"\U0001f4ca {_b('SENTINEL')} \u2014 {now.strftime('%H:%M')} UTC",
        f"Paused: {'yes \u23f8\ufe0f' if paused else 'no'}",
        "",
    ]

    if not summaries:
        lines.append("No accounts configured.")
    else:
        for s in summaries:
            kind = s.get("kind", "paper")
            icon = _KIND_ICON.get(kind, "\U0001f4c4")
            status = s.get("status", "ACTIVE")
            status_icon = "\U0001f7e2" if status == "ACTIVE" else "\U0001f534"
            balance = s.get("balance", 0)
            daily_pnl = s.get("daily_pnl", 0)
            initial = s.get("initial_balance", balance)
            daily_pct = (daily_pnl / initial * 100) if initial else 0
            dd_pct = s.get("daily_dd_used_pct", 0)
            open_pos = s.get("open_positions", 0)
            trades_today = s.get("trades_today", 0)

            lines.append(
                f"{icon} {_b(s.get('account_id', '?'))}  {status_icon}"
            )
            lines.append(
                f"  Bal {_money(balance)}  |  Day {_signed(daily_pnl)} ({daily_pct:+.1f}%)"
            )
            lines.append(
                f"  DD {dd_pct:.1f}%  |  {open_pos} open  |  {trades_today}t today"
            )
            # Floating PnL if there are open positions
            unrealized = s.get("unrealized_pnl", 0)
            if open_pos > 0:
                lines.append(f"  Floating: {_signed(unrealized)}")
            lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_authorized
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open positions detail."""
    mgr = context.bot_data["manager"]
    all_positions = mgr.open_positions()  # dict[account_id, list[ManagedPosition]]

    lines = []
    total_count = 0
    for account_id, positions in all_positions.items():
        if not positions:
            continue
        for pos in positions:
            total_count += 1
            dir_icon = "\U0001f431" if pos.direction.value == "LONG" else "\U0001f43b"
            plan = pos.exit_plan
            sym = pos.instrument

            # Current floating (use MFE/MAE tracking as proxy — the last tick
            # price isn't stored on the position, but we can derive direction).
            # Best available: use the executor's last_prices if accessible.
            entry = pos.entry_price
            sl = plan.stop_loss
            tps = ", ".join(_price(p, sym) for p in plan.take_profit_prices)
            hold = _hold_time(pos.entry_time_ms)

            lines.append(f"{dir_icon} {pos.direction.value} {_b(sym)}")
            lines.append(f"  @ {_code(_price(entry, sym))}  \u23f1\ufe0f{hold}")
            lines.append(f"  SL {_code(_price(sl, sym))} | TP {_code(tps)}")
            lines.append(f"  {_i(pos.strategy_id)} ({account_id})")
            lines.append("")

    if total_count == 0:
        lines.append("No open positions.")

    await update.message.reply_text("\n".join(lines) or "No open positions.", parse_mode="HTML")


@_authorized
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Today's closed trades from the DB."""
    store = context.bot_data["store"]
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    trades = store.get_recent_cfd_paper_trades(limit=50, session_date=today_str)

    if not trades:
        await update.message.reply_text("No closed trades today.")
        return

    lines = [f"\U0001f4c5 {_b('Today')} ({today_str})", ""]
    total_pnl = 0.0
    wins = 0
    losses = 0

    for t in trades:
        net = t.get("net_pnl_usd") or 0.0
        total_pnl += net
        if net >= 0:
            wins += 1
        else:
            losses += 1
        icon = "\u2705" if net >= 0 else "\u274c"
        sym = t.get("instrument", "?")
        direction = t.get("direction", "?")
        rr = t.get("realized_rr") or 0.0
        reason = t.get("exit_reason", "?")
        lines.append(f"{icon} {sym} {direction} {rr:+.2f}R {_signed(net)} [{reason}]")

    lines.append("")
    lines.append(
        f"{_b('Total')}: {_signed(total_pnl)}  |  "
        f"W{wins}/L{losses}  WR {wins / (wins + losses) * 100:.0f}%"
        if (wins + losses) > 0
        else f"{_b('Total')}: {_signed(total_pnl)}"
    )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_authorized
async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Last N trades. Usage: /last 5"""
    store = context.bot_data["store"]

    # Parse N from args
    n = 5
    if context.args:
        try:
            n = int(context.args[0])
            n = max(1, min(n, 50))
        except (ValueError, IndexError):
            pass

    trades = store.get_recent_cfd_paper_trades(limit=n)

    if not trades:
        await update.message.reply_text("No closed trades yet.")
        return

    lines = [f"\U0001f4dc {_b(f'Last {len(trades)} trades')}", ""]
    for t in trades:
        net = t.get("net_pnl_usd") or 0.0
        icon = "\u2705" if net >= 0 else "\u274c"
        sym = t.get("instrument", "?")
        direction = t.get("direction", "?")
        rr = t.get("realized_rr") or 0.0
        reason = t.get("exit_reason", "?")
        session_date = t.get("session_date", "")
        lines.append(
            f"{icon} {session_date} {sym} {direction} {rr:+.2f}R {_signed(net)} [{reason}]"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_authorized
async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Risk guard state per account."""
    mgr = context.bot_data["manager"]
    summaries = mgr.summaries()

    lines = ["\U0001f6e1\ufe0f " + _b("Risk Guard"), ""]

    for s in summaries:
        kind = s.get("kind", "paper")
        icon = _KIND_ICON.get(kind, "\U0001f4c4")
        status = s.get("status", "ACTIVE")
        status_icon = "\U0001f7e2" if status == "ACTIVE" else "\U0001f534"

        lines.append(f"{icon} {_b(s.get('account_id', '?'))} {status_icon} {status}")
        lines.append(
            f"  Daily DD: {s.get('daily_dd_used_pct', 0):.2f}% used  |  "
            f"${s.get('daily_dd_remaining_usd', 0):,.0f} remaining"
        )
        lines.append(
            f"  Max DD: {s.get('max_dd_used_pct', 0):.2f}% used  |  "
            f"${s.get('max_dd_remaining_usd', 0):,.0f} remaining"
        )
        lines.append(
            f"  Trades today: {s.get('trades_today', 0)}  |  "
            f"Halts: {s.get('daily_halts', 0)}"
        )
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_authorized
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Health check — uptime, ticks, candles, feed status."""
    app_ref = context.bot_data["app_ref"]
    paused = context.bot_data.get("paused", False)

    # Uptime
    boot_time = context.bot_data.get("boot_time", time.time())
    uptime = _duration(time.time() - boot_time)

    # Stats from the app
    tick_count = getattr(app_ref, "_tick_count", 0)
    candle_count = getattr(app_ref, "_candle_count", 0)
    signal_count = getattr(app_ref, "_signal_count", 0)
    strategies = getattr(app_ref, "_strategies", [])

    # Feed status — newest tick across all symbols. Both MT5FeedClient and
    # CTraderFeedClient expose get_ltp() -> {symbol: {"timestamp_ms": ...}}.
    feed = getattr(app_ref, "_feed", None)
    last_tick_age = "?"
    quiet_note = ""
    if feed is not None and hasattr(feed, "get_ltp"):
        try:
            snapshot = feed.get_ltp()
            timestamps = [
                d.get("timestamp_ms", 0)
                for d in snapshot.values()
                if isinstance(d, dict)
            ]
            newest = max(timestamps) if timestamps else 0
            if newest > 0:
                age_s = (time.time() * 1000 - newest) / 1000
                last_tick_age = f"{age_s:.1f}s ago"
            # How many subscribed symbols are quiet (>120s) — a health signal.
            if hasattr(feed, "quiet_symbols"):
                quiet = feed.quiet_symbols(120.0)
                if quiet:
                    quiet_note = f"  ({len(quiet)} quiet)"
        except Exception as e:  # noqa: BLE001 - /ping must never fail
            logger.warning("ping feed status error: %s", e)

    feed_kind = getattr(app_ref, "_feed_kind", "?")

    lines = [
        "\U0001f3d3 " + _b("SENTINEL alive"),
        f"Uptime: {uptime}",
        f"Ticks: {tick_count:,}  |  Candles: {candle_count:,}  |  Signals: {signal_count:,}",
        f"Feed: {feed_kind}  |  Last tick: {last_tick_age}{quiet_note}",
        f"Strategies: {len(strategies)} active  |  Paused: {'yes' if paused else 'no'}",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_authorized
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show running configuration."""
    app_ref = context.bot_data["app_ref"]
    mgr = context.bot_data["manager"]
    paused = context.bot_data.get("paused", False)

    strategies = getattr(app_ref, "_strategies", [])
    streams = getattr(app_ref, "_streams", [])
    feed_kind = getattr(app_ref, "_feed_kind", "?")

    lines = ["\u2699\ufe0f " + _b("Configuration"), ""]

    # Feed
    lines.append(f"Feed: {_b(feed_kind)}")
    lines.append(f"Paused: {'yes' if paused else 'no'}")
    lines.append("")

    # Strategies
    lines.append(_b("Strategies:"))
    if strategies:
        for s in strategies:
            instruments = ", ".join(s.instruments) if s.instruments else "ALL"
            lines.append(f"  \u2022 {s.strategy_id} ({s.timeframe.value}) \u2192 {instruments}")
    else:
        lines.append("  (none / candle-archiver mode)")
    lines.append("")

    # Streams
    lines.append(_b("Streams:"))
    for s in streams:
        lines.append(
            f"  \u2022 {s.stream_id} ({s.kind}) bal=${s.balance:,.0f} risk={s.risk_pct}%"
        )
    lines.append("")

    # Accounts
    lines.append(_b("Accounts:"))
    for summary in mgr.summaries():
        lines.append(
            f"  \u2022 {summary.get('account_id')} ({summary.get('kind')}) "
            f"bal={_money(summary.get('balance', 0))}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_authorized
async def cmd_closeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Flatten everything — requires YES confirmation within 30s."""
    context.user_data["pending_closeall"] = True
    context.user_data["pending_close_instrument"] = None
    context.user_data["closeall_time"] = time.time()
    await update.message.reply_text(
        "\u26a0\ufe0f Close ALL positions on all accounts? Reply YES within 30s."
    )


@_authorized
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close positions on a specific instrument. Usage: /close USDJPY"""
    if not context.args:
        await update.message.reply_text("Usage: /close USDJPY")
        return

    instrument = context.args[0].upper()
    context.user_data["pending_closeall"] = False
    context.user_data["pending_close_instrument"] = instrument
    context.user_data["closeall_time"] = time.time()
    await update.message.reply_text(
        f"\u26a0\ufe0f Close all positions on {_b(instrument)}? Reply YES within 30s.",
        parse_mode="HTML",
    )


@_authorized
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause new signals (keep managing open positions)."""
    context.bot_data["paused"] = True
    await update.message.reply_text("\u23f8\ufe0f Paused \u2014 no new signals taken.")


@_authorized
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume signals."""
    context.bot_data["paused"] = False
    await update.message.reply_text("\u25b6\ufe0f Resumed \u2014 signals active.")


@_authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all available commands."""
    text = (
        "\U0001f4cb " + _b("Commands") + "\n\n"
        "<b>Read-only:</b>\n"
        "/status \u2014 Portfolio snapshot\n"
        "/positions \u2014 Open positions detail\n"
        "/today \u2014 Today's closed trades\n"
        "/last N \u2014 Last N trades (default 5)\n"
        "/risk \u2014 Risk guard state\n"
        "/ping \u2014 Health check\n"
        "/config \u2014 Running configuration\n"
        "\n<b>Actions:</b>\n"
        "/closeall \u2014 Flatten all positions (confirm YES)\n"
        "/close SYM \u2014 Close positions on instrument (confirm YES)\n"
        "/pause \u2014 Stop new signals\n"
        "/resume \u2014 Resume signals\n"
        "/help \u2014 This message"
    )
    await update.message.reply_text(text, parse_mode="HTML")


@_authorized
async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle YES/NO replies for action commands."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip().upper()
    if text != "YES":
        # If there's a pending action and they said something else, cancel it
        if context.user_data.get("pending_closeall") or context.user_data.get(
            "pending_close_instrument"
        ):
            context.user_data["pending_closeall"] = False
            context.user_data["pending_close_instrument"] = None
            await update.message.reply_text("\u274c Cancelled.")
        return

    # Check timeout (30 seconds)
    closeall_time = context.user_data.get("closeall_time", 0)
    if time.time() - closeall_time > 30:
        context.user_data["pending_closeall"] = False
        context.user_data["pending_close_instrument"] = None
        await update.message.reply_text("\u23f0 Timed out. Send the command again.")
        return

    mgr = context.bot_data["manager"]

    if context.user_data.get("pending_closeall"):
        context.user_data["pending_closeall"] = False
        from app.cfd_execution.base import ExitReason

        mgr.flatten_all(ExitReason.MANUAL)
        # Count what was flattened
        all_pos = mgr.open_positions()
        remaining = sum(len(p) for p in all_pos.values())
        await update.message.reply_text(
            f"\u2705 All positions flattened. ({remaining} remaining)"
        )

    elif context.user_data.get("pending_close_instrument"):
        instrument = context.user_data["pending_close_instrument"]
        context.user_data["pending_close_instrument"] = None
        from app.cfd_execution.base import ExitReason

        # Count positions on this instrument, then flatten all (only way via
        # the public BaseExecutor interface). If only this instrument is open,
        # it's equivalent. If other instruments are open too, warn the user.
        all_pos = mgr.open_positions(instrument)
        total_on_instrument = sum(len(p) for p in all_pos.values())

        if total_on_instrument == 0:
            await update.message.reply_text(f"No open positions on {instrument}.")
            return

        # Close ONLY this instrument. PaperExecutor exposes a per-position close
        # internally; a live/demo (cTrader) executor does NOT offer a safe
        # per-instrument close via any public API — and its flatten_all() would
        # close EVERY instrument on that account. We must never silently close
        # unrelated live positions, so live streams are skipped with a warning.
        closed_count = 0
        skipped_live: list[str] = []
        for ex in mgr.executors():
            positions = ex.open_positions(instrument)
            if not positions:
                continue
            if hasattr(ex, "_close_position") and hasattr(ex, "_last_price"):
                # PaperExecutor — close each matching position at last price.
                for pos in list(positions):
                    try:
                        price = ex._last_price.get(pos.instrument, pos.entry_price)
                        ts = ex._now_ms() if hasattr(ex, "_now_ms") else time.time() * 1000
                        ex._close_position(pos, price, ExitReason.MANUAL, ts)
                        closed_count += 1
                    except Exception as e:  # noqa: BLE001
                        logger.error("per-instrument close failed on %s: %s",
                                     ex.account_id, e)
            else:
                # Live/demo executor — refuse to guess (would close everything).
                skipped_live.append(ex.account_id)

        msg = f"\u2705 Closed {closed_count} position(s) on {instrument}."
        if skipped_live:
            msg += (
                f"\n\u26a0\ufe0f Skipped live/demo account(s): {', '.join(skipped_live)}. "
                f"Per-instrument close isn't supported there — use /closeall or "
                f"close it directly in cTrader."
            )
        await update.message.reply_text(msg)


# ─── Boot (background thread with own asyncio loop) ──────────────────────────

_bot_app: Application | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_thread: threading.Thread | None = None
_bot_task: asyncio.Task | None = None


def start_command_bot(token: str, user_ids: list[int], bot_data: dict) -> None:
    """
    Start the Telegram command listener on a BACKGROUND THREAD.

    The main app is synchronous/threaded (feed.consume() blocks). We spin up
    a dedicated daemon thread with its own asyncio loop for the bot polling.

    Call this AFTER the feed is connected and the manager is built.

    bot_data should contain:
      - "manager": MultiAccountManager instance
      - "store": ResearchStore instance
      - "app_ref": reference to CFDPaperTradingApp (for ping/config)
      - "paused": False (mutable flag checked by the signal handler)
      - "boot_time": time.time() at app startup
    """
    global AUTHORIZED_USER_IDS, _bot_app, _bot_loop, _bot_thread
    AUTHORIZED_USER_IDS = set(user_ids)

    if not token:
        logger.warning("Command bot disabled: no bot token provided")
        return
    if not user_ids:
        logger.warning("Command bot disabled: no authorized user IDs (CFD_TELEGRAM_USER_ID)")
        return

    def _run_bot():
        global _bot_loop, _bot_task
        _bot_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_bot_loop)
        _bot_task = _bot_loop.create_task(_start_polling(token, bot_data))
        try:
            _bot_loop.run_until_complete(_bot_task)
        except asyncio.CancelledError:
            pass

    _bot_thread = threading.Thread(target=_run_bot, name="telegram-cmd-bot", daemon=True)
    _bot_thread.start()
    logger.info(
        "Telegram command bot started (polling, %d authorized user(s))",
        len(user_ids),
    )


async def _start_polling(token: str, bot_data: dict) -> None:
    """Internal: build the Application and run polling forever."""
    global _bot_app

    app = Application.builder().token(token).build()
    app.bot_data.update(bot_data)
    _bot_app = app

    # Register command handlers
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("closeall", cmd_closeall))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("help", cmd_help))
    # Confirmation handler (catches plain text YES/NO after an action command)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirmation))

    # Initialize and start polling (non-blocking within this loop)
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep the loop alive until cancelled by stop_command_bot()
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        # Clean shutdown sequence (official lifecycle from PTB docs)
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.warning("Command bot cleanup error: %s", e)


def stop_command_bot() -> None:
    """Gracefully stop the command bot (call from the main thread on shutdown)."""
    global _bot_app, _bot_loop, _bot_thread, _bot_task

    if _bot_loop is None or _bot_task is None:
        return

    # Cancel the polling task — triggers the finally block for clean shutdown
    _bot_loop.call_soon_threadsafe(_bot_task.cancel)

    # Wait for the thread to finish (the task's finally block does cleanup)
    if _bot_thread and _bot_thread.is_alive():
        _bot_thread.join(timeout=15)

    _bot_app = None
    _bot_loop = None
    _bot_thread = None
    _bot_task = None
    logger.info("Telegram command bot stopped")
