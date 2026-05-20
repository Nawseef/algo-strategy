"""
Telegram notification service.

Your complete trading dashboard on your phone.
Designed so you NEVER need to check logs or SSH into the VM.

Alerts:
- Real-time: signals, position opens, position closes
- Periodic: portfolio snapshot every N minutes
- Scheduled: market open greeting, end-of-day report at 3:30 PM
- System: reconnections, errors
"""

import json
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, time as dtime

from app.core.events import EventBus
from app.core.models import OrderSide, Position, Signal
from app.utils.instruments import get_instrument_name
from app.utils.logger import get_logger

logger = get_logger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Indian market hours
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(16, 0)  # summaries until 4 PM


class TelegramNotifier:
    """
    Your algo trading assistant on Telegram.
    Sends everything you need to know without ever opening a terminal.
    """

    def __init__(
        self,
        event_bus: EventBus,
        bot_token: str,
        chat_id: str,
        paper_trader=None,
        starting_balance: float = 100000.0,
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
        self._starting_balance = starting_balance
        self._summary_interval = summary_interval_minutes * 60
        self._notify_signals = notify_signals
        self._notify_positions = notify_positions
        self._notify_reconnects = notify_reconnects
        self._notify_errors = notify_errors
        self._enabled = bool(bot_token and chat_id)

        # Timers
        self._summary_timer: threading.Timer | None = None
        self._eod_timer: threading.Timer | None = None
        self._running = False

        # Session tracking
        self._session_start: float = 0.0
        self._signals_count = 0
        self._trades_opened = 0
        self._trades_closed = 0
        self._eod_sent_today = False

    def start(self) -> None:
        """Subscribe to events and start timers."""
        if not self._enabled:
            logger.warning("TelegramNotifier disabled (missing bot_token or chat_id)")
            return

        self._running = True
        self._session_start = time.time()

        # Subscribe to events
        if self._notify_signals:
            self._event_bus.subscribe("signal", self._on_signal)
        if self._notify_positions:
            self._event_bus.subscribe("position_open", self._on_position_open)
            self._event_bus.subscribe("position_close", self._on_position_close)
        if self._notify_reconnects:
            self._event_bus.subscribe("reconnect", self._on_reconnect)
        if self._notify_errors:
            self._event_bus.subscribe("error", self._on_error)

        logger.info("TelegramNotifier started")

        # Send startup message
        now = datetime.now()
        instruments = []
        if self._paper_trader:
            # Count subscribed instruments from open + closed positions or config
            instruments = self._paper_trader._latest_prices.keys()

        self._send(
            f"{'='*30}\n"
            f"ALGO STRATEGY BOT STARTED\n"
            f"{'='*30}\n\n"
            f"Time: {now.strftime('%I:%M %p')}\n"
            f"Date: {now.strftime('%d %b %Y')}\n"
            f"Mode: Paper Trading\n"
            f"Balance: Rs.{self._starting_balance:,.2f}\n"
            f"Updates every: {self._summary_interval // 60} min\n\n"
            f"Waiting for market data..."
        )

        # Start periodic summary
        self._schedule_summary()
        # Start end-of-day checker
        self._schedule_eod_check()

    def stop(self) -> None:
        """Unsubscribe and send final summary."""
        if not self._enabled:
            return

        self._running = False

        # Cancel timers
        if self._summary_timer:
            self._summary_timer.cancel()
        if self._eod_timer:
            self._eod_timer.cancel()

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

        self._send_session_end()
        logger.info("TelegramNotifier stopped")

    # ─── Real-time Alerts ────────────────────────────────────────

    def _on_signal(self, signal: Signal) -> None:
        self._signals_count += 1
        name = get_instrument_name(signal.exchange_token)
        direction = "BUY" if signal.signal_type.value == "BUY" else "SELL"
        arrow = "^" if direction == "BUY" else "v"

        msg = (
            f"{'- '*15}\n"
            f"{arrow} SIGNAL: {direction}\n"
            f"{'- '*15}\n\n"
            f"Stock: {name}\n"
            f"Price: Rs.{signal.price:,.2f}\n"
            f"Time: {datetime.now().strftime('%I:%M %p')}\n\n"
            f"Strategy: {signal.strategy_name}\n"
            f"Why: {signal.reason}\n\n"
            f"(Signal #{self._signals_count} today)"
        )
        self._send(msg)

    def _on_position_open(self, position: Position) -> None:
        self._trades_opened += 1
        name = get_instrument_name(position.exchange_token)
        direction = "BOUGHT" if position.side.value == "BUY" else "SOLD SHORT"
        invested = position.quantity * position.entry_price

        msg = (
            f"{'- '*15}\n"
            f"TRADE OPENED #{self._trades_opened}\n"
            f"{'- '*15}\n\n"
            f"Action: {direction}\n"
            f"Stock: {name}\n"
            f"Qty: {position.quantity} shares\n"
            f"Entry Price: Rs.{position.entry_price:,.2f}\n"
            f"Invested: Rs.{invested:,.2f}\n"
            f"Time: {datetime.now().strftime('%I:%M %p')}\n\n"
            f"Strategy: {position.strategy_name}\n\n"
            f"Open positions: {len(self._paper_trader.open_positions) if self._paper_trader else '?'}"
        )
        self._send(msg)

    def _on_position_close(self, position: Position) -> None:
        self._trades_closed += 1
        name = get_instrument_name(position.exchange_token)
        won = position.pnl >= 0
        result = "PROFIT" if won else "LOSS"
        emoji = "+" if won else ""

        # Calculate hold time
        hold_ms = position.exit_time_ms - position.entry_time_ms
        hold_min = hold_ms / 60_000 if hold_ms > 0 else 0

        msg = (
            f"{'- '*15}\n"
            f"TRADE CLOSED - {result}\n"
            f"{'- '*15}\n\n"
            f"Stock: {name}\n"
            f"Side: {position.side.value}\n"
            f"Entry: Rs.{position.entry_price:,.2f}\n"
            f"Exit: Rs.{position.exit_price:,.2f}\n"
            f"Hold time: {hold_min:.0f} min\n\n"
            f"PnL: {emoji}Rs.{position.pnl:,.2f} ({emoji}{position.pnl_pct:.2f}%)\n\n"
            f"Why closed: Opposing signal (SMA crossed back)\n"
        )

        # Running totals + streak + risk
        if self._paper_trader:
            total_pnl = self._paper_trader.total_pnl
            balance = self._starting_balance + total_pnl
            closed = self._paper_trader.closed_positions
            wins = len([p for p in closed if p.pnl > 0])
            losses = len([p for p in closed if p.pnl < 0])

            # Calculate current streak
            streak = self._get_streak(closed)

            # Day loss percentage
            day_loss_pct = abs(total_pnl / self._starting_balance * 100) if total_pnl < 0 else 0

            msg += (
                f"{'- '*15}\n"
                f"TODAY SO FAR:\n"
                f"Balance: Rs.{balance:,.2f}\n"
                f"Day PnL: {'+' if total_pnl >= 0 else ''}Rs.{total_pnl:,.2f}\n"
                f"Trades: {len(closed)} (W:{wins} L:{losses})\n"
                f"Win rate: {(wins/len(closed)*100) if closed else 0:.0f}%\n"
            )

            if streak:
                msg += f"Streak: {streak}\n"

            if day_loss_pct >= 2:
                msg += f"\n!! RISK WARNING: Down {day_loss_pct:.1f}% today !!"
            elif day_loss_pct >= 1:
                msg += f"\nCaution: Down {day_loss_pct:.1f}% today"

        self._send(msg)

    @staticmethod
    def _get_streak(closed_positions: list) -> str:
        """Get current win/loss streak description."""
        if not closed_positions:
            return ""
        streak_count = 0
        streak_type = None
        for pos in reversed(closed_positions):
            current = "W" if pos.pnl > 0 else "L" if pos.pnl < 0 else None
            if current is None:
                break
            if streak_type is None:
                streak_type = current
                streak_count = 1
            elif current == streak_type:
                streak_count += 1
            else:
                break
        if streak_count >= 2:
            label = "wins" if streak_type == "W" else "losses"
            return f"{streak_count} {label} in a row"
        return ""

    def _on_reconnect(self, info: dict) -> None:
        # Only alert on first reconnect attempt, not every retry
        if info["attempt"] <= 1:
            self._send(
                f"WARNING: Feed disconnected\n"
                f"Reconnecting (attempt {info['attempt']})...\n"
                f"Backoff: {info['backoff_s']:.0f}s"
            )

    def _on_error(self, message: str) -> None:
        self._send(f"ERROR: {message}")

    # ─── Periodic Summary (every 30 min) ─────────────────────────

    def _schedule_summary(self) -> None:
        if not self._running:
            return
        self._summary_timer = threading.Timer(self._summary_interval, self._send_periodic_summary)
        self._summary_timer.daemon = True
        self._summary_timer.start()

    def _send_periodic_summary(self) -> None:
        if not self._running:
            return

        now = datetime.now()

        # Don't send outside market hours
        if now.time() < MARKET_OPEN or now.time() > MARKET_CLOSE:
            self._schedule_summary()
            return

        msg = (
            f"{'='*30}\n"
            f"PORTFOLIO UPDATE - {now.strftime('%I:%M %p')}\n"
            f"{'='*30}\n"
        )

        if self._paper_trader:
            realized = self._paper_trader.total_pnl
            unrealized = self._paper_trader.unrealized_pnl
            balance = self._starting_balance + realized
            open_pos = self._paper_trader.open_positions
            closed_pos = self._paper_trader.closed_positions

            msg += (
                f"\nBalance: Rs.{balance:,.2f}\n"
                f"Day PnL: {'+' if realized >= 0 else ''}Rs.{realized:,.2f}\n"
                f"Unrealized: {'+' if unrealized >= 0 else ''}Rs.{unrealized:,.2f}\n"
            )

            # Open positions
            if open_pos:
                msg += f"\n--- Open Positions ({len(open_pos)}) ---\n"
                for pos in open_pos:
                    name = get_instrument_name(pos.exchange_token)
                    current = self._paper_trader._latest_prices.get(pos.exchange_token)
                    if current:
                        if pos.side == OrderSide.BUY:
                            pos_pnl = (current - pos.entry_price) * pos.quantity
                        else:
                            pos_pnl = (pos.entry_price - current) * pos.quantity
                        pnl_str = f"{'+' if pos_pnl >= 0 else ''}Rs.{pos_pnl:,.2f}"
                        msg += f"\n{name}\n  {pos.side.value} @ Rs.{pos.entry_price:,.2f} -> Rs.{current:,.2f} ({pnl_str})\n"
                    else:
                        msg += f"\n{name}\n  {pos.side.value} @ Rs.{pos.entry_price:,.2f}\n"
            else:
                msg += "\nNo open positions\n"

            # Today's stats
            if closed_pos:
                wins = len([p for p in closed_pos if p.pnl > 0])
                losses = len([p for p in closed_pos if p.pnl < 0])
                msg += (
                    f"\n--- Today's Stats ---\n"
                    f"Trades closed: {len(closed_pos)}\n"
                    f"Win/Loss: {wins}W / {losses}L\n"
                    f"Win rate: {(wins/len(closed_pos)*100):.0f}%\n"
                    f"Signals: {self._signals_count}\n"
                )
        else:
            msg += "\nNo data available\n"

        self._send(msg)
        self._schedule_summary()

    # ─── End of Day Report (auto at 3:30 PM) ─────────────────────

    def _schedule_eod_check(self) -> None:
        """Check every minute if it's time to send EOD report."""
        if not self._running:
            return
        self._eod_timer = threading.Timer(60, self._check_eod)
        self._eod_timer.daemon = True
        self._eod_timer.start()

    def _check_eod(self) -> None:
        """Send EOD report at market close."""
        if not self._running:
            return

        now = datetime.now().time()
        # Send between 3:35 and 3:37 PM (after last ticks settle), once per day
        if dtime(15, 35) <= now <= dtime(15, 37) and not self._eod_sent_today:
            self._eod_sent_today = True
            self._send_eod_report()

        # Reset flag next morning
        if now < dtime(9, 0):
            self._eod_sent_today = False

        self._schedule_eod_check()

    def _send_eod_report(self) -> None:
        """Comprehensive end-of-day report."""
        now = datetime.now()

        msg = (
            f"{'='*30}\n"
            f"END OF DAY REPORT\n"
            f"{now.strftime('%d %b %Y')} | Market Closed\n"
            f"{'='*30}\n"
        )

        if not self._paper_trader:
            msg += "\nNo trading data available"
            self._send(msg)
            return

        realized = self._paper_trader.total_pnl
        balance = self._starting_balance + realized
        closed = self._paper_trader.closed_positions
        open_pos = self._paper_trader.open_positions

        # Overall performance
        day_return_pct = (realized / self._starting_balance) * 100 if self._starting_balance > 0 else 0
        verdict = "GREEN DAY" if realized > 0 else "RED DAY" if realized < 0 else "FLAT DAY"

        msg += (
            f"\n{verdict}\n\n"
            f"Starting Balance: Rs.{self._starting_balance:,.2f}\n"
            f"Ending Balance: Rs.{balance:,.2f}\n"
            f"Day PnL: {'+' if realized >= 0 else ''}Rs.{realized:,.2f} ({'+' if day_return_pct >= 0 else ''}{day_return_pct:.2f}%)\n"
        )

        # Trade stats
        if closed:
            wins = [p for p in closed if p.pnl > 0]
            losses = [p for p in closed if p.pnl < 0]
            win_rate = (len(wins) / len(closed)) * 100

            msg += (
                f"\n--- Trade Stats ---\n"
                f"Total trades: {len(closed)}\n"
                f"Winners: {len(wins)} | Losers: {len(losses)}\n"
                f"Win rate: {win_rate:.0f}%\n"
            )

            if wins:
                msg += f"Best trade: +Rs.{max(p.pnl for p in wins):,.2f}\n"
            if losses:
                msg += f"Worst trade: -Rs.{abs(min(p.pnl for p in losses)):,.2f}\n"

            avg_win = sum(p.pnl for p in wins) / len(wins) if wins else 0
            avg_loss = sum(p.pnl for p in losses) / len(losses) if losses else 0
            msg += f"Avg win: +Rs.{avg_win:,.2f}\n"
            msg += f"Avg loss: Rs.{avg_loss:,.2f}\n"

            # Per-stock breakdown
            stock_pnl: dict[str, float] = defaultdict(float)
            stock_trades: dict[str, int] = defaultdict(int)
            for p in closed:
                name = get_instrument_name(p.exchange_token)
                stock_pnl[name] += p.pnl
                stock_trades[name] += 1

            msg += f"\n--- By Stock ---\n"
            for name, pnl in sorted(stock_pnl.items(), key=lambda x: x[1], reverse=True):
                trades = stock_trades[name]
                msg += f"{name}: {'+' if pnl >= 0 else ''}Rs.{pnl:,.2f} ({trades} trades)\n"
        else:
            msg += "\nNo trades completed today\n"

        # Open positions carried forward
        if open_pos:
            msg += f"\n--- Carried Forward ({len(open_pos)} open) ---\n"
            for pos in open_pos:
                name = get_instrument_name(pos.exchange_token)
                msg += f"{name} {pos.side.value} @ Rs.{pos.entry_price:,.2f}\n"

        # Activity
        msg += (
            f"\n--- Activity ---\n"
            f"Signals generated: {self._signals_count}\n"
            f"Trades opened: {self._trades_opened}\n"
            f"Trades closed: {self._trades_closed}\n"
        )

        msg += f"\n{'='*30}\n"
        msg += "Bot continues running. See you tomorrow."

        self._send(msg)

    # ─── Session End (on shutdown/restart) ────────────────────────

    def _send_session_end(self) -> None:
        """Send when bot is stopped/restarted."""
        duration_min = (time.time() - self._session_start) / 60 if self._session_start else 0
        now = datetime.now()

        msg = (
            f"{'='*30}\n"
            f"BOT STOPPED\n"
            f"{'='*30}\n\n"
            f"Time: {now.strftime('%I:%M %p')}\n"
            f"Session duration: {duration_min:.0f} min\n"
            f"Signals: {self._signals_count}\n"
            f"Trades: {self._trades_opened} opened, {self._trades_closed} closed\n"
        )

        if self._paper_trader:
            realized = self._paper_trader.total_pnl
            balance = self._starting_balance + realized
            msg += (
                f"\nBalance: Rs.{balance:,.2f}\n"
                f"Session PnL: {'+' if realized >= 0 else ''}Rs.{realized:,.2f}\n"
            )

        self._send(msg)

    # ─── Send ────────────────────────────────────────────────────

    def _send(self, text: str) -> None:
        """Send a message via Telegram Bot API. Plain text, no markdown issues."""
        if not self._enabled:
            return

        url = TELEGRAM_API_URL.format(token=self._bot_token)
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": text,
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
