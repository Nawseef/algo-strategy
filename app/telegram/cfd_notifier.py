"""
CFD trade notifier — the "dashboard on your phone" for CFD paper/live trading.

This brings CFD Telegram alerts to parity with the NSE ``TelegramNotifier``, but
adapted for CFDs (USD, lots, RR) and MULTI-ACCOUNT (one signal can trade several
prop-firm accounts, each reported independently).

It is a thin FORMATTING + STATE layer on top of a transport object (normally
``MT5Notifier``) that actually delivers to the dedicated CFD Telegram channel.
Any object with ``send(text, block=False)`` works as the transport, so this is
easy to test with a fake.

What it sends:
  * 📥 ENTRY  — direction, lots, entry, SL, TP(s), RR, risk $, open count, balance
  * ✅/❌ EXIT — entry→exit, hold time, RR, net $, MFE ("peak / missed TP by"),
                plus RUNNING day totals (day PnL %, W/L, win rate), win/loss
                STREAK, a risk warning if the day is deep in drawdown, and a
                halt banner if the account's risk guard tripped.
  * 📊 PERIODIC SUMMARY — every N minutes during market hours, per-account snapshot
  * 🏁 END-OF-DAY REPORT — GREEN/RED day verdict, per-account leaderboard
  * 🟢/🛑 SESSION START / STOP — accounts, strategies, market state, session PnL recap

Authoritative money figures (balance, day PnL, drawdown-used) come from each
account's RiskGuard summary. Win/loss counts, win rate and streak are tallied
in-memory here (reset on ``on_day_reset``), because the guard doesn't track them.

Thread-safety: ``notify_exit`` runs on the feed/tick thread while
``periodic_summary`` / ``eod_report`` run on the runner's schedule thread, so the
per-account tally is guarded by a lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.cfd_execution.base import ExitReason, ManagedPosition
from app.cfd_risk.instruments import get_instrument
from app.cfd_strategy.base import CFDSignal
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Day-loss thresholds (as % of an account's initial balance) for exit-alert warnings.
_CAUTION_LOSS_PCT = 1.0
_WARNING_LOSS_PCT = 2.0


@dataclass
class _AccountDay:
    """In-memory tally of an account's CLOSED trades for the current FX day."""

    outcomes: list[bool] = field(default_factory=list)   # True=win, False=loss, in close order
    realized_pnl: float = 0.0

    @property
    def trades(self) -> int:
        return len(self.outcomes)

    @property
    def wins(self) -> int:
        return sum(1 for w in self.outcomes if w)

    @property
    def losses(self) -> int:
        return sum(1 for w in self.outcomes if not w)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.outcomes else 0.0

    def streak(self) -> str:
        """Trailing win/loss streak, e.g. '3 wins in a row' (>= 2 to report)."""
        if not self.outcomes:
            return ""
        last = self.outcomes[-1]
        n = 0
        for w in reversed(self.outcomes):
            if w == last:
                n += 1
            else:
                break
        if n >= 2:
            return f"{n} {'wins' if last else 'losses'} in a row"
        return ""


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


class CFDTradeNotifier:
    """Rich, multi-account CFD trade + portfolio notifications over Telegram."""

    def __init__(self, transport, alert_trades: bool = True) -> None:
        # transport: object with .send(text, block=False) (e.g. MT5Notifier).
        self._transport = transport
        self._alert_trades = alert_trades
        self._lock = threading.Lock()
        self._days: dict[str, _AccountDay] = {}
        self._session_start_ms: float = datetime.now(timezone.utc).timestamp() * 1000

    # ─── Transport passthrough ───────────────────────────────────

    def send(self, text: str, block: bool = False) -> None:
        """Passthrough so the runner can use ONE notifier for ops alerts too."""
        if self._transport is not None:
            self._transport.send(text, block=block)

    @property
    def enabled(self) -> bool:
        return getattr(self._transport, "_enabled", True)

    def _day(self, account_id: str) -> _AccountDay:
        d = self._days.get(account_id)
        if d is None:
            d = _AccountDay()
            self._days[account_id] = d
        return d

    # ─── Trade alerts (called by the executor) ───────────────────

    def notify_entry(
        self,
        account_id: str,
        pos: ManagedPosition,
        signal: CFDSignal,
        risk_usd: float,
        open_count: int,
        guard_summary: dict | None = None,
    ) -> None:
        if not self._alert_trades:
            return
        gs = guard_summary or {}
        balance = gs.get("balance")
        risk_pct = ""
        init_bal = gs.get("initial_balance") or 0.0
        if init_bal:
            risk_pct = f" ({risk_usd / init_bal * 100:.1f}%)"
        tps = ", ".join(f"{p:.5g}" for p in signal.exit_plan.take_profit_prices)
        when = datetime.fromtimestamp(pos.entry_time_ms / 1000, timezone.utc).strftime("%H:%M UTC")

        lines = [
            f"\U0001f4e5 ENTRY [{account_id}]",
            f"{pos.direction.value} {pos.instrument}  {pos.lots:.2f} lots @ {pos.entry_price:.5g}",
            f"SL {signal.stop_loss:.5g} | TP {tps} | RR {signal.exit_plan.max_rr:.2f}",
            f"Risk {_money(risk_usd)}{risk_pct} | {signal.strategy_id}/{signal.variant_id}",
        ]
        tail = f"Open: {open_count}"
        if balance is not None:
            tail += f" | Bal {_money(balance)}"
        lines.append(tail)
        lines.append(when)
        self.send("\n".join(lines))

    def notify_exit(
        self,
        account_id: str,
        pos: ManagedPosition,
        realized_rr: float,
        net_pnl_usd: float,
        reason: ExitReason,
        guard_summary: dict | None = None,
    ) -> None:
        # Record the outcome first (so running totals include this trade).
        with self._lock:
            day = self._day(account_id)
            day.outcomes.append(net_pnl_usd > 0)
            day.realized_pnl += net_pnl_usd
            trades = day.trades
            wins = day.wins
            losses = day.losses
            win_rate = day.win_rate
            streak = day.streak()
            day_realized = day.realized_pnl

        if not self._alert_trades:
            return

        gs = guard_summary or {}
        balance = gs.get("balance")
        init_bal = gs.get("initial_balance") or 0.0
        status = gs.get("status", "ACTIVE")

        won = net_pnl_usd > 0
        emoji = "\u2705" if won else "\u274c"
        verdict = "WIN" if won else "LOSS"

        hold_min = 0.0
        if pos.exit_time_ms and pos.entry_time_ms:
            hold_min = max(0.0, (pos.exit_time_ms - pos.entry_time_ms) / 60_000.0)

        lines = [
            f"{emoji} EXIT [{account_id}] — {verdict}",
            f"{pos.direction.value} {pos.instrument} @ {pos.exit_price:.5g} ({reason.value})",
            f"Entry {pos.entry_price:.5g} \u2192 Exit {pos.exit_price:.5g} | hold {hold_min:.0f}m",
            f"RR {realized_rr:+.2f} | net {_signed(net_pnl_usd)}",
        ]

        mfe_line = self._mfe_line(pos)
        if mfe_line:
            lines.append(mfe_line)

        lines.append("\u2500" * 13)
        day_pct = (day_realized / init_bal * 100.0) if init_bal else 0.0
        lines.append(
            f"Today: {_signed(day_realized)} ({day_pct:+.2f}%) | "
            f"{trades} trades  W:{wins} L:{losses} ({win_rate:.0f}%)"
        )
        if streak:
            lines.append(f"Streak: {streak}")
        if balance is not None:
            lines.append(f"Bal {_money(balance)}")

        # Risk warning based on the day's realized drawdown.
        if init_bal and day_realized < 0:
            loss_pct = abs(day_realized) / init_bal * 100.0
            if loss_pct >= _WARNING_LOSS_PCT:
                lines.append(f"\u26a0\ufe0f RISK: down {loss_pct:.1f}% today")
            elif loss_pct >= _CAUTION_LOSS_PCT:
                lines.append(f"Caution: down {loss_pct:.1f}% today")

        # Halt banner if the risk guard tripped.
        if status and status != "ACTIVE":
            lines.append(f"\U0001f6d1 ACCOUNT {status} — new trades blocked")

        self.send("\n".join(lines))

    def _mfe_line(self, pos: ManagedPosition) -> str:
        """'Peak: <price> (+$x)' plus whether the peak beat the furthest TP."""
        try:
            inst = get_instrument(pos.instrument)
            point_value = inst.point_value_per_lot
        except Exception:  # noqa: BLE001
            return ""
        peak_price = pos.max_favorable_price
        if not peak_price or peak_price == pos.entry_price:
            return ""
        peak_usd = pos.mfe_price * point_value * pos.lots
        line = f"Peak {peak_price:.5g} ({_signed(peak_usd)})"
        tps = pos.exit_plan.take_profit_prices
        if tps:
            furthest = tps[-1]
            reached_beyond = (
                peak_price >= furthest if pos.direction.sign > 0 else peak_price <= furthest
            )
            if reached_beyond:
                line += " — beyond furthest TP"
            else:
                missed = abs(furthest - peak_price)
                line += f" — missed top TP by {missed:.5g}"
        return line

    # ─── Portfolio summary / EOD (called by the runner) ──────────

    def periodic_summary(self, summaries: list[dict], sessions: str = "") -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        lines = ["\u2550" * 24, f"\U0001f4ca PORTFOLIO — {now}", "\u2550" * 24]
        if sessions:
            lines.append(sessions)
        lines.append("")
        for s in summaries:
            lines.extend(self._account_block(s))
        best = self._best(summaries)
        if best:
            lines.append("")
            lines.append(f"Best: {best[0]} ({_signed(best[1])})")
        self.send("\n".join(lines))

    def eod_report(self, summaries: list[dict], date_str: str) -> None:
        with self._lock:
            realized_by_acct = {aid: d.realized_pnl for aid, d in self._days.items()}
            outcomes_by_acct = {
                aid: (d.wins, d.losses, d.trades, d.win_rate) for aid, d in self._days.items()
            }
        portfolio_pnl = sum(realized_by_acct.values())
        verdict = "\U0001f7e2 GREEN DAY" if portfolio_pnl > 0 else (
            "\U0001f534 RED DAY" if portfolio_pnl < 0 else "\u26aa FLAT DAY")

        lines = [
            "\u2550" * 24,
            f"\U0001f3c1 END OF DAY — {date_str}",
            "\u2550" * 24,
            f"{verdict} (portfolio {_signed(portfolio_pnl)})",
            "",
        ]
        for s in summaries:
            aid = s.get("account_id", "?")
            realized = realized_by_acct.get(aid, 0.0)
            wins, losses, trades, wr = outcomes_by_acct.get(aid, (0, 0, 0, 0.0))
            init_bal = s.get("initial_balance") or 0.0
            bal = s.get("balance", 0.0)
            day_pct = (realized / init_bal * 100.0) if init_bal else 0.0
            tag = "GREEN" if realized > 0 else ("RED" if realized < 0 else "FLAT")
            lines.append(f"[{aid}] {tag}")
            lines.append(f"  Start {_money(init_bal)} \u2192 End {_money(bal)}")
            lines.append(f"  Day {_signed(realized)} ({day_pct:+.2f}%)")
            lines.append(f"  Trades {trades}  W:{wins} L:{losses}  WR {wr:.0f}%")
            if s.get("status") and s["status"] != "ACTIVE":
                lines.append(f"  \U0001f6d1 {s['status']}")
        best = self._best(summaries, realized_by_acct)
        worst = self._worst(summaries, realized_by_acct)
        if best and worst and best[0] != worst[0]:
            lines.append("")
            lines.append(f"Best: {best[0]} ({_signed(best[1])})")
            lines.append(f"Worst: {worst[0]} ({_signed(worst[1])})")
        self.send("\n".join(lines))

    def _account_block(self, s: dict) -> list[str]:
        aid = s.get("account_id", "?")
        bal = s.get("balance", 0.0)
        day = s.get("daily_pnl", 0.0)
        init_bal = s.get("initial_balance") or 0.0
        day_pct = (day / init_bal * 100.0) if init_bal else 0.0
        dd = s.get("daily_dd_used_pct", 0.0)
        trades = s.get("trades_today", 0)
        with self._lock:
            wr = self._day(aid).win_rate if aid in self._days else 0.0
        block = [
            f"[{aid}]  Bal {_money(bal)}  Day {_signed(day)} ({day_pct:+.2f}%)",
            f"  Trades {trades}  WR {wr:.0f}%  DDused {dd:.1f}%",
        ]
        if s.get("status") and s["status"] != "ACTIVE":
            block.append(f"  \U0001f6d1 {s['status']}")
        return block

    def _best(self, summaries, realized_by_acct=None):
        pnls = self._realized_pnls(summaries, realized_by_acct)
        return max(pnls, key=lambda kv: kv[1]) if pnls else None

    def _worst(self, summaries, realized_by_acct=None):
        pnls = self._realized_pnls(summaries, realized_by_acct)
        return min(pnls, key=lambda kv: kv[1]) if pnls else None

    def _realized_pnls(self, summaries, realized_by_acct):
        if realized_by_acct is None:
            with self._lock:
                realized_by_acct = {aid: d.realized_pnl for aid, d in self._days.items()}
        return [(s.get("account_id", "?"), realized_by_acct.get(s.get("account_id", "?"), 0.0))
                for s in summaries]

    # ─── Session lifecycle ───────────────────────────────────────

    def session_start(
        self, summaries: list[dict], strategies: list[str], market_open: bool, sessions: str,
    ) -> None:
        self._session_start_ms = datetime.now(timezone.utc).timestamp() * 1000
        accts = ", ".join(
            f"{s.get('account_id','?')} ({_money(s.get('initial_balance') or 0.0)})"
            for s in summaries
        )
        lines = [
            "\u2550" * 24,
            "\U0001f7e2 CFD PAPER TRADER STARTED",
            "\u2550" * 24,
            f"Accounts: {accts or 'none'}",
            f"Strategies: {', '.join(strategies) or '(none)'}",
            f"Market: {'OPEN' if market_open else 'CLOSED'} | {sessions or 'closed'}",
            "Mode: PAPER (no real orders)",
        ]
        self.send("\n".join(lines))

    def session_end(self, summaries: list[dict]) -> None:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        dur_min = max(0.0, (now_ms - self._session_start_ms) / 60_000.0)
        with self._lock:
            realized_by_acct = {aid: d.realized_pnl for aid, d in self._days.items()}
            trades_by_acct = {aid: d.trades for aid, d in self._days.items()}
        lines = [
            "\u2550" * 24,
            "\U0001f6d1 CFD PAPER TRADER STOPPED",
            "\u2550" * 24,
            f"Session: {dur_min:.0f} min",
        ]
        for s in summaries:
            aid = s.get("account_id", "?")
            realized = realized_by_acct.get(aid, 0.0)
            lines.append(
                f"[{aid}] Bal {_money(s.get('balance', 0.0))} | "
                f"Session {_signed(realized)} | {trades_by_acct.get(aid, 0)} trades"
            )
        self.send("\n".join(lines), block=True)

    # ─── Day boundary ────────────────────────────────────────────

    def on_day_reset(self) -> None:
        """Clear the per-account day tally (call at the FX trading-day boundary,
        AFTER sending the EOD report)."""
        with self._lock:
            self._days.clear()
