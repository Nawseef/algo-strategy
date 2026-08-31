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
  * �⬆/🔴⬇ ENTRY — direction (color-coded), lots, entry, SL, TP(s), RR, risk $,
                    open count, balance, recent form
  * ✅💰/❌💸 EXIT — entry→exit, hold time + exit-reason icon (🎯 TP / 🛑 SL /
                    🔄 trail / ⏰ time-stop / 🌙 EOD flatten / 🚨 risk-halt /
                    ✋ manual), RR, net $, MFE ("peak / missed TP by"), plus
                    RUNNING day totals (day PnL %, W/L, win rate), win/loss
                    STREAK + FORM strip, a risk warning if the day is deep in
                    drawdown, and a 🚫 halt banner if the risk guard tripped.
  * 📊 PERIODIC SUMMARY — every N minutes during market hours, per-account snapshot
  * 🏁 END-OF-DAY REPORT — GREEN/RED day verdict, per-account leaderboard
  * 🟢/� SESSION START / STOP — accounts, strategies, market state, session PnL recap

Authoritative money figures (balance, day PnL, drawdown-used) come from each
account's RiskGuard summary. Win/loss counts, win rate and streak are tallied
in-memory here (reset on ``on_day_reset``), because the guard doesn't track them.

Thread-safety: ``notify_exit`` runs on the feed/tick thread while
``periodic_summary`` / ``eod_report`` run on the runner's schedule thread, so the
per-account tally is guarded by a lock.
"""

from __future__ import annotations

import os
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

# The trading engine's display name (shown in start/stop/summary banners).
# Generic on purpose — it runs paper + demo + multiple live firms, so it isn't
# tied to any one strategy or account. Override with CFD_ENGINE_NAME.
ENGINE_NAME = os.getenv("CFD_ENGINE_NAME", "SENTINEL").strip() or "SENTINEL"


# How many recent trades to show in the "form" strip, e.g. "L L W L W W L".
_FORM_WINDOW = 10


@dataclass
class _AccountDay:
    """In-memory tally of an account's CLOSED trades for the current FX day."""

    outcomes: list[bool] = field(default_factory=list)   # True=win, False=loss, in close order
    realized_pnl: float = 0.0
    # ALL-TIME (not reset daily) outcomes across the whole paper-trading session,
    # used for the "last N trades" form strip so it isn't emptied at every day
    # boundary (a strategy trading a few times/week would otherwise show nothing
    # most days).
    all_time_outcomes: list[bool] = field(default_factory=list)

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

    def form(self, window: int = _FORM_WINDOW) -> str:
        """Last ``window`` all-time outcomes as colored squares (oldest -> newest)."""
        recent = self.all_time_outcomes[-window:]
        return "".join("🟩" if w else "🟥" for w in recent)

    def reset_day(self) -> None:
        """Clear the DAILY tally but keep ``all_time_outcomes`` for the form strip."""
        self.outcomes.clear()
        self.realized_pnl = 0.0


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _price(price: float, instrument: str | None = None) -> str:
    """Format a price with FULL instrument-aware decimal precision.

    Uses the instrument's pip_size to determine the correct number of decimals:
      EURUSD/GBPUSD (pip 0.0001) → 5 decimals (e.g. 1.16911)
      USDJPY (pip 0.01)          → 3 decimals (e.g. 158.299)
      XAUUSD (pip 0.01)          → 2 decimals (e.g. 4478.55)
      XAGUSD (pip 0.001)         → 3 decimals (e.g. 66.531)
      XTIUSD (pip 0.01)          → 2 decimals (e.g. 87.19)
      US30 (pip 1.0)             → 1 decimal  (e.g. 53070.6)
      US500/USTEC/DE40 (pip 0.1) → 1 decimal  (e.g. 7680.4)
    Falls back to Python's default repr if the instrument is unknown.
    """
    if instrument:
        try:
            inst = get_instrument(instrument)
            # Determine decimals from pip_size: 0.0001 → 5, 0.001 → 4, 0.01 → 3,
            # 0.1 → 1, 1.0 → 1 (min 1 decimal for readability)
            import math
            decimals = max(1, -int(math.floor(math.log10(inst.pip_size))) + 1)
            return f"{price:.{decimals}f}"
        except Exception:  # noqa: BLE001
            pass
    # Fallback: strip trailing zeros from a generous format
    return f"{price:.6f}".rstrip("0").rstrip(".")


# ─── HTML formatting (Telegram parse_mode=HTML) ─────────────────────────
# Telegram has no font-size or arbitrary-color control — the only "emphasis"
# tools any client renders are bold / italic / underline / strikethrough /
# monospace. We use them sparingly so the important numbers (verdict, net
# PnL, warnings, halts) pop, while routine detail lines stay plain — bolding
# everything is the same as bolding nothing.
def _esc(text: str) -> str:
    """Escape the 3 characters HTML parse_mode treats specially."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _b(text: str) -> str:
    """Bold — used for verdicts, net PnL, and anything safety-critical."""
    return f"<b>{text}</b>"


def _i(text: str) -> str:
    """Italic — used for secondary/contextual detail (variant id, timestamps)."""
    return f"<i>{text}</i>"


def _code(text: str) -> str:
    """Monospace — used for price levels so digits align and stand out."""
    return f"<code>{text}</code>"


# ─── Icons ─────────────────────────────────────────────────────────────
# Direction icons — up arrow for LONG, down arrow for SHORT.
_DIR_ICON = {
    "LONG": "\u2b06\ufe0f",    # ⬆️ up   (long)
    "SHORT": "\u2b07\ufe0f",   # ⬇️ down (short)
}

# Per-KIND header icon (the account TYPE the trade ran on).
_KIND_ICON = {
    "paper": "\U0001f4c4",   # 📄 paper  (simulated, no broker orders)
    "demo": "\U0001f4e5",    # 📥 demo   (real orders on a demo account)
    "live": "\u26a1",        # ⚡ live   (real prop-firm / funded money)
}

# Entry alert icon: 🚪 door + kind icon (composed at call site).
_ENTRY_ARROW = "\U0001f6aa"   # 🚪 door (entry)

# Exit outcome icons: 💹💰 for win, 📛💰 for loss (+ kind icon at call site).
_EXIT_ICON_WIN = "\U0001f4b9\U0001f4b0"    # 💹💰
_EXIT_ICON_LOSS = "\U0001f4db\U0001f4b0"   # 📛💰

# Message border — top: 〰️ × 13, bottom: ♾️ × 13.
_MSG_BORDER_TOP = "\u3030\ufe0f" * 13
_MSG_BORDER = "\u267e\ufe0f" * 13

# Why a trade closed — a small icon per ExitReason so the reason is scannable.
_EXIT_REASON_ICON = {
    "STOP_LOSS": "\U0001f6d1",       # 🛑
    "TAKE_PROFIT": "\U0001f3af",     # 🎯
    "TRAILING_STOP": "\U0001f504",   # 🔄
    "TIME_STOP": "\u23f0",           # ⏰
    "MANUAL": "\u270b",              # ✋
    "EOD_FLATTEN": "\U0001f319",     # 🌙
    "RISK_HALT": "\U0001f6a8",       # 🚨
    "EXPIRED": "\u231b",             # ⌛
}


def _exit_reason_icon(reason: ExitReason) -> str:
    return _EXIT_REASON_ICON.get(reason.value, "\u2022")


def _compose_mode(kinds: list[str]) -> str:
    """Human mode summary from the running streams' kinds, e.g.
    ['paper','demo'] -> 'paper, demo';  ['paper','live','live'] -> 'paper, live (2)'.
    A single stream of a kind shows just the kind; multiples show a count."""
    order = ["paper", "demo", "live"]
    counts: dict[str, int] = {}
    for k in kinds:
        counts[k] = counts.get(k, 0) + 1
    parts = []
    for k in order:
        n = counts.pop(k, 0)
        if n == 1:
            parts.append(k)
        elif n > 1:
            parts.append(f"{k} ({n})")
    for k, n in counts.items():   # any non-standard kinds, keep them too
        parts.append(k if n == 1 else f"{k} ({n})")
    return ", ".join(parts) or "none"


class CFDTradeNotifier:
    """Rich, multi-account CFD trade + portfolio notifications over Telegram."""

    def __init__(self, transport, alert_trades: bool = True) -> None:
        # transport: object with .send(text, block=False) (e.g. MT5Notifier).
        self._transport = transport
        self._alert_trades = alert_trades
        self._lock = threading.Lock()
        # Per-ACCOUNT tally (used by periodic_summary/eod_report — the account is
        # what actually has a balance/drawdown).
        self._days: dict[str, _AccountDay] = {}
        # Per-(ACCOUNT, STRATEGY) tally (used by entry/exit alerts — so running
        # more than one strategy on one account doesn't blend their W/L/streak/
        # form into a single meaningless number).
        self._strategy_days: dict[tuple[str, str], _AccountDay] = {}
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

    def _strategy_day(self, account_id: str, strategy_id: str) -> _AccountDay:
        key = (account_id, strategy_id)
        d = self._strategy_days.get(key)
        if d is None:
            d = _AccountDay()
            self._strategy_days[key] = d
        return d

    # ─── Trade alerts (called by the executor) ───────────────────

    def notify_entry(
        self,
        account_id: str,
        pos: ManagedPosition,
        signal: CFDSignal | None = None,
        risk_usd: float = 0.0,
        open_count: int = 0,
        guard_summary: dict | None = None,
        kind: str = "paper",
        intended_price: float | None = None,
    ) -> None:
        """Entry alert. Everything is derived from ``pos`` (``signal`` is optional
        and kept only for backwards compatibility), so the cTrader executor —
        which only has the ManagedPosition, not the original signal — can use this
        same rich alert. ``kind`` ("paper"/"demo"/"live"/…) titles the alert
        (PAPER/DEMO/LIVE ENTRY); ``intended_price`` (the signal price) lets a real
        fill show its slippage vs the intended entry.
        """
        if not self._alert_trades:
            return
        gs = guard_summary or {}
        plan = pos.exit_plan
        sym = pos.instrument
        tps = ", ".join(_price(p, sym) for p in plan.take_profit_prices)

        dir_icon = _DIR_ICON.get(pos.direction.value, "\u2b06\ufe0f")
        kind_icon = _KIND_ICON.get(kind, "\U0001f4e5")

        # Compact card — no words like ENTRY/LONG/SHORT, emojis say it
        lines = [
            _MSG_BORDER_TOP,
            f"{_ENTRY_ARROW}{kind_icon} {dir_icon} {_b(pos.instrument)}",
            "",
            f"@ {_code(_price(pos.entry_price, sym))}  |  {pos.lots:.2f} lots",
            f"SL {_code(_price(plan.stop_loss, sym))}  |  TP {_code(tps)}",
        ]
        # Show R as price distance + dollar risk
        sl_dist = abs(pos.entry_price - plan.stop_loss)
        lines.append(f"R {_price(sl_dist, sym)} ({_b(_money(risk_usd))})  |  RR {plan.max_rr:.1f}")
        # For a live fill, show the entry slippage vs the intended signal price.
        if intended_price and intended_price > 0:
            slip = (pos.entry_price - intended_price) * pos.direction.sign
            lines.append(
                f"{_i('slip')} {_b(f'{slip:+.5g}')}"
            )
        lines.append(_i(pos.strategy_id))
        lines.append(_MSG_BORDER)
        self.send("\n".join(lines))

    def notify_exit(
        self,
        account_id: str,
        pos: ManagedPosition,
        realized_rr: float,
        net_pnl_usd: float,
        reason: ExitReason,
        guard_summary: dict | None = None,
        kind: str = "paper",
        commission_usd: float | None = None,
        swap_usd: float | None = None,
    ) -> None:
        won = net_pnl_usd > 0
        strategy_id = pos.strategy_id

        # Record the outcome first (so running totals include this trade) — both
        # the ACCOUNT-level tally (balance/day-PnL is account-wide) and the
        # STRATEGY-level tally (so W/L/streak/form aren't blended across
        # multiple strategies sharing one account).
        with self._lock:
            day = self._day(account_id)
            day.outcomes.append(won)
            day.realized_pnl += net_pnl_usd
            day_trades = day.trades
            day_wins = day.wins
            day_losses = day.losses
            day_win_rate = day.win_rate
            day_realized = day.realized_pnl

            sday = self._strategy_day(account_id, strategy_id)
            sday.outcomes.append(won)
            sday.all_time_outcomes.append(won)
            sday.realized_pnl += net_pnl_usd
            strat_trades = sday.trades
            strat_wins = sday.wins
            strat_losses = sday.losses
            strat_win_rate = sday.win_rate
            strat_streak = sday.streak()
            strat_form = sday.form()
            strat_realized = sday.realized_pnl

        if not self._alert_trades:
            return

        gs = guard_summary or {}
        balance = gs.get("balance")
        init_bal = gs.get("initial_balance") or 0.0
        status = gs.get("status", "ACTIVE")

        emoji = _EXIT_ICON_WIN if won else _EXIT_ICON_LOSS
        reason_icon = _exit_reason_icon(reason)
        sym = pos.instrument
        dir_icon = _DIR_ICON.get(pos.direction.value, "")
        kind_icon = _KIND_ICON.get(kind, "\U0001f4e5")

        hold_min = 0.0
        if pos.exit_time_ms and pos.entry_time_ms:
            hold_min = max(0.0, (pos.exit_time_ms - pos.entry_time_ms) / 60_000.0)

        # Big money headline first
        lines = [
            _MSG_BORDER_TOP,
            f"{emoji}{kind_icon} {_b(_signed(net_pnl_usd))} ({realized_rr:+.1f}R) {dir_icon}{pos.instrument}",
            "",
            f"{_code(_price(pos.entry_price, sym))} \u2192 {_code(_price(pos.exit_price, sym))}  "
            f"{reason_icon}  \u23f1\ufe0f{hold_min:.0f}m",
        ]

        # Real broker charges
        if commission_usd is not None or swap_usd is not None:
            parts = []
            if commission_usd is not None:
                parts.append(f"comm {_signed(commission_usd)}")
            if swap_usd is not None:
                parts.append(f"swap {_signed(swap_usd)}")
            lines.append(_i(" | ".join(parts)))

        mfe_line = self._mfe_line(pos)
        if mfe_line:
            lines.append(_i(mfe_line))

        lines.append("")
        # Per-STRATEGY stats with colored form strip
        form_strip = f"  {strat_form}" if strat_form else ""
        lines.append(
            f"{strategy_id}: W:{strat_wins} L:{strat_losses} ({strat_win_rate:.0f}%){form_strip}"
        )
        if strat_streak:
            lines.append(_i(strat_streak))

        # Per-ACCOUNT day summary
        day_pct = (day_realized / init_bal * 100.0) if init_bal else 0.0
        lines.append(
            f"Day {_b(_signed(day_realized))} ({day_pct:+.2f}%) | "
            f"{day_trades}t W:{day_wins} L:{day_losses}"
        )

        # Risk warning
        if init_bal and day_realized < 0:
            loss_pct = abs(day_realized) / init_bal * 100.0
            if loss_pct >= _WARNING_LOSS_PCT:
                lines.append(_b(f"\u26a0\ufe0f RISK: down {loss_pct:.1f}% today"))
            elif loss_pct >= _CAUTION_LOSS_PCT:
                lines.append(f"Caution: down {loss_pct:.1f}% today")

        # Halt banner
        if status and status != "ACTIVE":
            lines.append(_b(f"\U0001f6ab ACCOUNT {status} — new trades blocked"))

        lines.append(_MSG_BORDER)
        self.send("\n".join(lines))

    def _mfe_line(self, pos: ManagedPosition) -> str:
        """Compact MFE: '⛰️ +$12 (missed TP by 0.415)' or '⛰️ +$12 (beyond TP)'."""
        try:
            inst = get_instrument(pos.instrument)
            point_value = inst.point_value_per_lot
        except Exception:  # noqa: BLE001
            return ""
        peak_price = pos.max_favorable_price
        if not peak_price or peak_price == pos.entry_price:
            return ""
        peak_usd = pos.mfe_price * point_value * pos.lots
        line = f"\u26f0\ufe0f {_signed(peak_usd)}"
        tps = pos.exit_plan.take_profit_prices
        if tps:
            furthest = tps[-1]
            reached_beyond = (
                peak_price >= furthest if pos.direction.sign > 0 else peak_price <= furthest
            )
            if reached_beyond:
                line += " (beyond TP)"
            else:
                missed = abs(furthest - peak_price)
                line += f" (missed TP by {_price(missed, pos.instrument)})"
        return line

    # ─── Portfolio summary / EOD (called by the runner) ──────────

    def periodic_summary(self, summaries: list[dict], sessions: str = "") -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        lines = [_MSG_BORDER_TOP, _b(f"\U0001f4ca {ENGINE_NAME} \u2014 {now}")]
        if sessions:
            lines.append(_i(sessions))
        lines.append("")
        for s in summaries:
            kind_icon = _KIND_ICON.get(s.get("kind", "paper"), "\U0001f4c4")
            lines.extend(self._account_block(s, kind_icon))
        # If no trades today on any account, show waiting indicator
        has_trades = any(s.get("trades_today", 0) > 0 for s in summaries)
        if not has_trades:
            lines.append("")
            lines.append("\u23f3 waiting for signal...")
        lines.append(_MSG_BORDER)
        self.send("\n".join(lines))

    def eod_report(self, summaries: list[dict], date_str: str) -> None:
        with self._lock:
            realized_by_acct = {aid: d.realized_pnl for aid, d in self._days.items()}
            outcomes_by_acct = {
                aid: (d.wins, d.losses, d.trades, d.win_rate) for aid, d in self._days.items()
            }
        portfolio_pnl = sum(realized_by_acct.values())
        verdict_dot = "\U0001f7e2" if portfolio_pnl > 0 else (
            "\U0001f534" if portfolio_pnl < 0 else "\u26aa")

        lines = [
            _MSG_BORDER_TOP,
            f"\U0001f3c1 {date_str}  {verdict_dot} {_b(_signed(portfolio_pnl))}",
            "",
        ]
        for s in summaries:
            aid = s.get("account_id", "?")
            realized = realized_by_acct.get(aid, 0.0)
            wins, losses, trades, wr = outcomes_by_acct.get(aid, (0, 0, 0, 0.0))
            init_bal = s.get("initial_balance") or 0.0
            bal = s.get("balance", 0.0)
            day_pct = (realized / init_bal * 100.0) if init_bal else 0.0
            kind_icon = _KIND_ICON.get(s.get("kind", "paper"), "\U0001f4c4")
            # Color dot for this account's day
            acct_dot = "\U0001f7e9" if realized > 0 else ("\U0001f7e5" if realized < 0 else "\u26aa")
            lines.append(
                f"{kind_icon} {_money(bal)}  Day {_b(_signed(realized))} ({day_pct:+.1f}%)"
                f"  | {trades}t {acct_dot}"
            )
            if s.get("status") and s["status"] != "ACTIVE":
                lines.append(_b(f"   \U0001f6ab {s['status']}"))
        lines.append(_MSG_BORDER)
        self.send("\n".join(lines))

    def _account_block(self, s: dict, kind_icon: str = "\U0001f4c4") -> list[str]:
        aid = s.get("account_id", "?")
        bal = s.get("balance", 0.0)
        day = s.get("daily_pnl", 0.0)
        init_bal = s.get("initial_balance") or 0.0
        day_pct = (day / init_bal * 100.0) if init_bal else 0.0
        dd = s.get("daily_dd_used_pct", 0.0)
        trades = s.get("trades_today", 0)
        with self._lock:
            wr = self._day(aid).win_rate if aid in self._days else 0.0
        # DD color: green < 2%, yellow 2-4%, red > 4%
        dd_dot = "\U0001f7e2" if dd < 2.0 else ("\U0001f7e1" if dd < 4.0 else "\U0001f534")
        block = [
            f"{kind_icon} {_money(bal)}  Day {_b(_signed(day))} ({day_pct:+.1f}%)  {dd_dot}",
            f"   {trades}t  WR {wr:.0f}%  DD {dd:.1f}%",
        ]
        if s.get("status") and s["status"] != "ACTIVE":
            block.append(_b(f"   \U0001f6ab {s['status']}"))
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
        kinds: list[str] | None = None,
    ) -> None:
        self._session_start_ms = datetime.now(timezone.utc).timestamp() * 1000
        kinds = kinds or []

        # Build compact account summary: 📄 $10,000  📥 $8,102
        acct_parts = []
        for s in summaries:
            ki = _KIND_ICON.get(s.get("kind", "paper"), "\U0001f4c4")
            acct_parts.append(f"{ki} {_money(s.get('initial_balance') or 0.0)}")

        market_icon = "\U0001f310" if market_open else "\U0001f319"
        lines = [
            _MSG_BORDER_TOP,
            f"\U0001f680 {_b(f'{ENGINE_NAME} ON')}",
            "  ".join(acct_parts),
            f"{market_icon} {sessions or 'closed'}",
            _i(", ".join(strategies) if strategies else "(none)"),
            _MSG_BORDER,
        ]
        self.send("\n".join(lines))

    def session_end(self, summaries: list[dict]) -> None:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        dur_min = max(0.0, (now_ms - self._session_start_ms) / 60_000.0)
        with self._lock:
            realized_by_acct = {aid: d.realized_pnl for aid, d in self._days.items()}
            trades_by_acct = {aid: d.trades for aid, d in self._days.items()}

        total_pnl = sum(realized_by_acct.values())
        total_trades = sum(trades_by_acct.values())
        lines = [
            _MSG_BORDER_TOP,
            f"\u23f9\ufe0f {_b(f'{ENGINE_NAME} OFF')}  {_signed(total_pnl)}  {total_trades}t  {dur_min:.0f}m",
        ]
        for s in summaries:
            aid = s.get("account_id", "?")
            realized = realized_by_acct.get(aid, 0.0)
            kind_icon = _KIND_ICON.get(s.get("kind", "paper"), "\U0001f4c4")
            lines.append(
                f"{kind_icon} {_money(s.get('balance', 0.0))}  {_signed(realized)}"
            )
        lines.append(_MSG_BORDER)
        self.send("\n".join(lines), block=True)

    # ─── Day boundary ────────────────────────────────────────────

    def on_day_reset(self) -> None:
        """Reset the per-account and per-strategy DAILY tallies (call at the FX
        trading-day boundary, AFTER sending the EOD report). The all-time form
        strip (last-N W/L) is preserved across day boundaries — clearing it
        every day would make the "last 7 trades" strip mostly empty for a
        strategy that only trades a few times a week."""
        with self._lock:
            for d in self._days.values():
                d.reset_day()
            for d in self._strategy_days.values():
                d.reset_day()
