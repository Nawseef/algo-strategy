"""
Telegram notification service.

Your live trading dashboard on your phone.
Sends real-time alerts + periodic portfolio summaries so you
can stay informed without opening the trading app.

Messages sent:
- Platform start/stop
- Every signal (BUY/SELL)
- Every position open/close with PnL
- Periodic portfolio summary (configurable interval)
- Reconnection warnings
- Errors
- End-of-day summary with full stats
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from app.core.events import EventBus
from app.core.models import OrderSide, Position, Signal
from app.utils.logger import get_logger

logger = get_logger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """
    Sends Telegram messages for key trading events.
    Includes periodic portfolio summaries for passive monitoring.

    Usage:
        notifier = TelegramNotifier(event_bus, bot_token, chat_id, paper_trader)
        notifier.start()
    """

    def __init__(
        self,
        event_bus: EventBus,
        bot_token: str,
        chat_id: str,
        paper_trader=None,
        summary_interval_minutes: int = 30,
        notify_signals: bool = True,
        notify_positions: bool = True,
        notify_reconnects: bool = True,
        notify_errors: bool = True,
    ) -> None:
        self._event_bus = event_bus
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._paper_trader = paper_trader
        self._summary_interval = summary_interval_minutes * 60  # seconds
        self._notify_signals = notify_signals
        self._notify_positions = notify_positions
        self._notify_reconnects = notify_reconnects
        self._notify_errors = notify_errors
        self._enabled = bool(bot_token and chat_id)

        # Summary timer
        self._summary_timer: threading.Timer | None = None
        self._running = False

        # Track session stats for end-of-day
        self._session_start: float = 0.0
        self._signals_count = 0
        self._trades_opened = 0
        self._trades_closed = 0

    def start(self) -> None:
        """Subscribe to events and start periodic summaries."""
        if not self._enabled:
            logger.warning("TelegramNotifier disabled (missing bot_token or chat_id)")
            return

        self._running = True
        self._session_start = time.time()

        if self._notify_signals:
            self._event_bus.subscribe("signal", self._on_signal)
        if self._notify_positions:
            self._event_bus.subscribe("position_open", self._on_position_open)
            self._event_bus.subscribe("position_close", self._on_position_close)
        if self._notify_reconnects:
            self._event_bus.subscribe("reconnect", self._on_reconnect)
        if self._notify_errors:
            self._event_bus.subscribe("error", self._on_error)

        logger.info("TelegramNotifier started (summary every %d min)", self._summary_interval // 60)

        # Start message
        now = datetime.now().strftime("%H:%M")
        self._send(
            f"🟢 *ALGO-STRATEGY STARTED*\n"
            f"Time: {now}\n"
            f"Mode: Paper Trading\n"
            f"Summaries every {self._summary_interval // 60} min"
        )

        # Start periodic summary
        self._schedule_summary()

    def stop(self) -> None:
        """Unsubscribe, send final summary, and stop."""
        if not self._enabled:
            return

        self._running = False

        # Cancel timer
        if self._summary_timer:
            self._summary_timer.cancel()
            self._summary_timer = None

        # Unsubscribe
        if self._notify_signals:
            self._event_bus.unsubscribe("signal", self._on_signal)
        if self._notify_positions:
            self._event_bus.unsubscribe("position_open", self._on_position_open)
            self._event_bus.unsubscribe("position_close", self._on_position_close)
        if self._notify_reconnects:
            self._event_bus.unsubscribe("reconnect", self._on_reconnect)
        if self._notify_errors:
            self._event_bus.unsubscribe("error", self._on_error)

        # Send end-of-day summary
        self._send_session_summary()
        logger.info("TelegramNotifier stopped")

    # ─── Event Handlers ──────────────────────────────────────────

    def _on_signal(self, signal: Signal) -> None:
        self._signals_count += 1
        emoji = "🟢" if signal.signal_type.value == "BUY" else "🔴"
        msg = (
            f"{emoji} *SIGNAL: {signal.signal_type.value}*\n"
            f"Token: `{signal.exchange_token}`\n"
            f"Price: ₹{signal.price:.2f}\n"
            f"Strategy: {signal.strategy_name}\n"
            f"Reason: {signal.reason}"
        )
        self._send(msg)

    def _on_position_open(self, position: Position) -> None:
        self._trades_opened += 1
        emoji = "📈" if position.side.value == "BUY" else "📉"
        msg = (
            f"{emoji} *POSITION OPENED*\n"
            f"Side: {position.side.value}\n"
            f"Token: `{position.exchange_token}`\n"
            f"Qty: {position.quantity}\n"
            f"Entry: ₹{position.entry_price:.2f}\n"
            f"Strategy: {position.strategy_name}"
        )
        self._send(msg)

    def _on_position_close(self, position: Position) -> None:
        self._trades_closed += 1
        emoji = "✅" if position.pnl >= 0 else "❌"
        pnl_emoji = "+" if position.pnl >= 0 else ""
        msg = (
            f"{emoji} *POSITION CLOSED*\n"
            f"Token: `{position.exchange_token}`\n"
            f"Side: {position.side.value}\n"
            f"Entry: ₹{position.entry_price:.2f} → Exit: ₹{position.exit_price:.2f}\n"
            f"PnL: {pnl_emoji}₹{position.pnl:.2f} ({pnl_emoji}{position.pnl_pct:.1f}%)\n"
            f"Strategy: {position.strategy_name}"
        )

        # Add running total if paper trader available
        if self._paper_trader:
            total = self._paper_trader.total_pnl
            msg += f"\n\n📊 Day total: ₹{total:.2f}"

        self._send(msg)

    def _on_reconnect(self, info: dict) -> None:
        msg = (
            f"⚠️ *RECONNECTING*\n"
            f"Attempt: {info['attempt']}\n"
            f"Backoff: {info['backoff_s']:.1f}s"
        )
        self._send(msg)

    def _on_error(self, message: str) -> None:
        self._send(f"🚨 *ERROR*\n{message}")

    # ─── Periodic Summary ────────────────────────────────────────

    def _schedule_summary(self) -> None:
        """Schedule the next periodic summary."""
        if not self._running:
            return
        self._summary_timer = threading.Timer(
            self._summary_interval, self._send_periodic_summary
        )
        self._summary_timer.daemon = True
        self._summary_timer.start()

    def _send_periodic_summary(self) -> None:
        """Send a portfolio snapshot at regular intervals."""
        if not self._running:
            return

        now = datetime.now().strftime("%H:%M")
        msg = f"📋 *PORTFOLIO UPDATE* ({now})\n"

        if self._paper_trader:
            open_pos = self._paper_trader.open_positions
            closed_pos = self._paper_trader.closed_positions
            realized = self._paper_trader.total_pnl
            unrealized = self._paper_trader.unrealized_pnl

            msg += f"\n*Realized PnL:* ₹{realized:.2f}"
            msg += f"\n*Unrealized PnL:* ₹{unrealized:.2f}"
            msg += f"\n*Net:* ₹{realized + unrealized:.2f}"
            msg += f"\n\n*Open positions:* {len(open_pos)}"

            for pos in open_pos:
                current = self._paper_trader._latest_prices.get(pos.exchange_token)
                if current:
                    if pos.side == OrderSide.BUY:
                        pos_pnl = (current - pos.entry_price) * pos.quantity
                    else:
                        pos_pnl = (pos.entry_price - current) * pos.quantity
                    pnl_str = f"+₹{pos_pnl:.2f}" if pos_pnl >= 0 else f"-₹{abs(pos_pnl):.2f}"
                    msg += f"\n  • `{pos.exchange_token}` {pos.side.value} @{pos.entry_price:.2f} → {current:.2f} ({pnl_str})"
                else:
                    msg += f"\n  • `{pos.exchange_token}` {pos.side.value} @{pos.entry_price:.2f}"

            msg += f"\n\n*Closed today:* {len(closed_pos)}"
            if closed_pos:
                winners = len([p for p in closed_pos if p.pnl > 0])
                msg += f" (W:{winners} L:{len(closed_pos)-winners})"
        else:
            msg += "\nNo paper trader connected"

        self._send(msg)

        # Schedule next
        self._schedule_summary()

    def _send_session_summary(self) -> None:
        """Send end-of-session summary with full stats."""
        duration_min = (time.time() - self._session_start) / 60 if self._session_start else 0
        now = datetime.now().strftime("%H:%M")

        msg = (
            f"🔴 *SESSION ENDED* ({now})\n"
            f"Duration: {duration_min:.0f} min\n"
            f"\n*Session Stats:*\n"
            f"Signals: {self._signals_count}\n"
            f"Trades opened: {self._trades_opened}\n"
            f"Trades closed: {self._trades_closed}\n"
        )

        if self._paper_trader:
            realized = self._paper_trader.total_pnl
            unrealized = self._paper_trader.unrealized_pnl
            open_count = len(self._paper_trader.open_positions)
            closed = self._paper_trader.closed_positions

            msg += f"\n*PnL:*\n"
            msg += f"Realized: ₹{realized:.2f}\n"
            msg += f"Unrealized: ₹{unrealized:.2f}\n"
            msg += f"Net: ₹{realized + unrealized:.2f}\n"
            msg += f"\nOpen positions: {open_count}\n"

            if closed:
                winners = [p for p in closed if p.pnl > 0]
                losers = [p for p in closed if p.pnl < 0]
                win_rate = (len(winners) / len(closed)) * 100
                msg += f"\n*Performance:*\n"
                msg += f"Win rate: {win_rate:.0f}%\n"
                msg += f"Winners: {len(winners)} | Losers: {len(losers)}\n"
                if winners:
                    msg += f"Best trade: +₹{max(p.pnl for p in winners):.2f}\n"
                if losers:
                    msg += f"Worst trade: -₹{abs(min(p.pnl for p in losers)):.2f}\n"

        self._send(msg)

    # ─── Send ────────────────────────────────────────────────────

    def _send(self, text: str) -> None:
        """Send a message via Telegram Bot API."""
        if not self._enabled:
            return

        url = TELEGRAM_API_URL.format(token=self._bot_token)
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("Telegram API returned %d", resp.status)
        except Exception as e:
            logger.error("Telegram send failed: %s", e)
