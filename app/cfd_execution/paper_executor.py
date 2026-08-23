"""
Paper executor — Telegram + Postgres paper trading for CFD signals.

Consumes CFDSignals and price ticks, manages positions against their mandatory
SL/TP (via the shared ``evaluate_exit``), sizes positions with the CFD risk
engine, gates trades through the account's RiskGuard, persists every closed
trade to ``cfd_paper_trades``, and alerts on Telegram.

NO broker orders are placed. This is a faithful simulation of what a live trade
would do, running on the SAME feed and the SAME exit logic as the backtest and
the future live executor — so paper results are directly comparable.

Price convention: management uses the BID price (same basis as the stored 5m
candles). Spread/commission/slippage are accounted for separately by the CFD
cost model, so we do not double-count the spread by filling at ask.

Arm expiry: INTRABAR signals arm a trigger price and expire after
``signal.expiry_candles`` candles. The runner must call ``on_candle_close`` once
per completed candle per instrument to age arms. If never called, arms persist
until filled (acceptable, but call it for correct behaviour).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.cfd_execution.account import AccountConfig
from app.cfd_execution.base import (
    BaseExecutor,
    ExecutionMode,
    ExitReason,
    ManagedPosition,
    PartialClose,
    PositionStatus,
    apply_dynamic_stop,
    evaluate_exit,
    time_stop_reached,
)
from app.cfd_risk.costs import COST_MODEL_INTRADAY, CFDCostModel, calculate_trade_cost
from app.cfd_risk.instruments import get_instrument
from app.cfd_risk.position_sizing import calculate_lot_size
from app.cfd_risk.risk_guard import RiskGuard
from app.cfd_strategy.base import CFDSignal, Direction, EntryMode
from app.db.research_store import ResearchStore
from app.utils import forex_hours
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class _Arm:
    """A pending INTRABAR entry waiting for price to reach the trigger."""

    signal: CFDSignal
    candles_remaining: int

    @property
    def key(self) -> tuple[str, str, str, str]:
        s = self.signal
        return (s.strategy_id, s.variant_id, s.instrument, s.direction.value)


class PaperExecutor(BaseExecutor):
    """
    Single-account paper-trading executor.

    One PaperExecutor manages exactly one account (its RiskGuard, balance, and
    trade log). The MultiAccountManager wraps N of these to fan a signal out to
    several accounts.
    """

    mode = ExecutionMode.PAPER

    def __init__(
        self,
        account: AccountConfig,
        store: ResearchStore | None = None,
        notifier=None,                       # MT5Notifier or any object with .send(str)
        cost_model: CFDCostModel | None = None,
        alert_trades: bool = True,
        kind: str = "paper",
    ) -> None:
        self._account = account
        self._store = store
        self._notifier = notifier
        self._cost_model = cost_model or COST_MODEL_INTRADAY
        self._alert_trades = alert_trades
        # Alert label tag (PAPER ENTRY / …). Simulated fills only.
        self._kind = kind

        self._risk = RiskGuard(account.to_risk_guard_config())
        self._risk_pct = account.effective_risk_per_trade_pct()
        # The balance used for SIZING (constant, never changes with P&L).
        # This is the stream's configured balance (e.g. $10k).
        self._sizing_balance = account.initial_balance

        # Open positions by position_id.
        self._positions: dict[str, ManagedPosition] = {}
        # Pending intrabar arms by position-key (dedup: one arm per strat/var/inst/dir).
        self._arms: dict[tuple[str, str, str, str], _Arm] = {}
        # Keys of currently OPEN positions (dedup entries).
        self._open_keys: set[tuple[str, str, str, str]] = set()

        # Latest known price per instrument (bid).
        self._last_price: dict[str, float] = {}

        # Stats.
        self._trades_opened = 0
        self._trades_closed = 0

    # ─── Properties ──────────────────────────────────────────────

    @property
    def account_id(self) -> str:
        return self._account.account_id

    @property
    def risk_guard(self) -> RiskGuard:
        return self._risk

    # ─── Signal handling ─────────────────────────────────────────

    def on_signal(self, signal: CFDSignal) -> None:
        """Open (CANDLE_CLOSE) or arm (INTRABAR) a position for this signal."""
        if not self._account.enabled:
            return

        key = (signal.strategy_id, signal.variant_id, signal.instrument, signal.direction.value)

        # Dedup: don't stack the same strategy/variant/instrument/direction.
        if key in self._open_keys:
            logger.debug("Signal ignored: %s already has an open position", key)
            return
        if key in self._arms:
            # Refresh the arm (latest signal wins, reset expiry).
            self._arms[key] = _Arm(signal=signal, candles_remaining=signal.expiry_candles)
            return

        # Risk gate: is the account allowed to trade at all?
        if not self._risk.can_trade():
            logger.info(
                "[%s] Trade blocked by risk guard (%s) — signal %s dropped",
                self.account_id, self._risk.status.value, key,
            )
            return

        if signal.entry_mode is EntryMode.CANDLE_CLOSE:
            # Fill immediately at the signal's entry price.
            self._fill(signal, fill_price=signal.entry_price, fill_time_ms=signal.timestamp_ms)
        else:
            # Arm for an intrabar trigger.
            self._arms[key] = _Arm(signal=signal, candles_remaining=signal.expiry_candles)
            logger.info(
                "[%s] ARMED %s %s @ trigger %.5g (expires in %d candles)",
                self.account_id, signal.direction.value, signal.instrument,
                signal.entry_price, signal.expiry_candles,
            )

    # ─── Tick handling ───────────────────────────────────────────

    def on_tick(self, instrument: str, bid: float, ask: float, timestamp_ms: float) -> None:
        """Fill armed entries and manage open positions against SL/TP."""
        price = bid  # management basis (see module docstring)
        if price <= 0:
            return
        self._last_price[instrument] = price

        # 1) Check armed entries for this instrument — did price reach the trigger?
        self._check_arms(instrument, price, timestamp_ms)

        # 2) Manage open positions for this instrument.
        self._manage_positions(instrument, price, timestamp_ms)

        # 3) Update floating PnL on the risk guard (across ALL open positions).
        self._risk.update_unrealized_pnl(self._total_floating_pnl_usd())

    def _check_arms(self, instrument: str, price: float, timestamp_ms: float) -> None:
        """Fill any armed entry whose trigger price has been reached."""
        to_fill: list[_Arm] = []
        for key, arm in list(self._arms.items()):
            sig = arm.signal
            if sig.instrument != instrument:
                continue
            trigger = sig.entry_price
            # Trigger logic: for a LONG, fill when price rises to/through trigger;
            # for a SHORT, fill when price falls to/through trigger. This matches
            # a stop/breakout entry. (Limit-style entries would invert, but our
            # strategies use trigger-on-touch semantics like the NSE engine.)
            reached = (price >= trigger) if sig.direction is Direction.LONG else (price <= trigger)
            if reached:
                to_fill.append(arm)

        for arm in to_fill:
            key = arm.key
            self._arms.pop(key, None)
            # Re-check risk + dedup at fill time.
            if key in self._open_keys or not self._risk.can_trade():
                continue
            # Fill at the trigger price (conservative; ignores favourable slippage).
            self._fill(arm.signal, fill_price=arm.signal.entry_price, fill_time_ms=timestamp_ms)

    def _manage_positions(self, instrument: str, price: float, timestamp_ms: float) -> None:
        for pos in list(self._positions.values()):
            if pos.instrument != instrument or not pos.is_open:
                continue
            pos.update_excursion(price)
            # Move the managed stop per the exit policy (breakeven / trailing)
            # before checking exits, so a raised stop can take this same tick.
            apply_dynamic_stop(pos, price)
            # For a tick, high == low == price.
            decisions = evaluate_exit(pos, high=price, low=price)
            for d in decisions:
                self._apply_exit(pos, d.price, d.fraction, d.reason, timestamp_ms, d.tp_index)
                if d.fully_closed:
                    break

    # ─── Candle aging (for arm expiry) ───────────────────────────

    def on_candle_close(self, instrument: str, timestamp_ms: float) -> None:
        """Age INTRABAR arms for an instrument; expire those past their TTL.

        Call once per completed candle per instrument.
        """
        for key, arm in list(self._arms.items()):
            if arm.signal.instrument != instrument:
                continue
            arm.candles_remaining -= 1
            if arm.candles_remaining <= 0:
                self._arms.pop(key, None)
                logger.info("[%s] Arm expired unfilled: %s", self.account_id, key)

        # Advance the bar counter on open positions and honour the time-stop.
        for pos in list(self._positions.values()):
            if pos.instrument != instrument or not pos.is_open:
                continue
            pos.bars_open += 1
            if time_stop_reached(pos):
                price = self._last_price.get(instrument, pos.entry_price)
                self._apply_exit(
                    pos, price, pos.remaining_fraction, ExitReason.TIME_STOP,
                    timestamp_ms, -1,
                )

    def on_day_reset(self, timestamp_ms: float | None = None) -> None:
        """Run the risk guard's daily reset (call at the firm's reset time)."""
        self._risk.check_daily_reset(timestamp_ms)

    # ─── Fills / exits ───────────────────────────────────────────

    def _fill(self, signal: CFDSignal, fill_price: float, fill_time_ms: float) -> None:
        """Open a position from a signal at the given fill price."""
        inst = get_instrument(signal.instrument)

        # ALWAYS size from the INITIAL balance (not current balance) so risk $
        # is constant regardless of running P&L.
        sl_distance = abs(fill_price - signal.stop_loss)
        sizing = calculate_lot_size(
            symbol=signal.instrument,
            account_balance=self._sizing_balance,
            risk_pct=self._risk_pct,
            sl_distance_price=sl_distance,
            instrument=inst,
        )
        if sizing.rejected:
            logger.info(
                "[%s] Trade rejected by sizing: %s (%s)",
                self.account_id, sizing.reject_reason, signal.instrument,
            )
            return

        # Risk-guard per-trade check (USD risk at this size).
        risk_usd = sizing.risk_usd
        ok, reason = self._risk.check_trade_risk(risk_usd)
        if not ok:
            logger.info("[%s] Trade blocked: %s", self.account_id, reason)
            return

        pos = ManagedPosition(
            position_id=f"CFD-{uuid.uuid4().hex[:10]}",
            strategy_id=signal.strategy_id,
            variant_id=signal.variant_id,
            instrument=signal.instrument,
            direction=signal.direction,
            entry_price=fill_price,
            entry_time_ms=fill_time_ms,
            lots=sizing.lot_size,
            exit_plan=signal.exit_plan,
            account_id=self.account_id,
        )
        self._positions[pos.position_id] = pos
        key = (signal.strategy_id, signal.variant_id, signal.instrument, signal.direction.value)
        self._open_keys.add(key)
        self._trades_opened += 1

        logger.info(
            "[%s] ENTRY %s %s %.2f lots @ %.5g | SL=%.5g TP=%s RR=%.2f risk=$%.2f",
            self.account_id, signal.direction.value, signal.instrument,
            sizing.lot_size, fill_price, signal.stop_loss,
            signal.exit_plan.take_profit_prices, signal.exit_plan.max_rr, risk_usd,
        )
        self._notify_entry(pos, signal, risk_usd)

    def _apply_exit(
        self,
        pos: ManagedPosition,
        price: float,
        fraction: float,
        reason: ExitReason,
        timestamp_ms: float,
        tp_index: int,
    ) -> None:
        """Apply a (partial or full) close to a position."""
        if fraction <= 0 or not pos.is_open:
            return

        rr = pos.rr_at(price)
        pnl_price = pos.price_pnl_per_unit(price)
        pos.partial_closes.append(PartialClose(
            price=price, fraction=fraction, reason=reason,
            timestamp_ms=timestamp_ms, rr=rr, pnl_price=pnl_price,
        ))
        if tp_index >= 0:
            pos._tp_taken.add(tp_index)
        pos.remaining_fraction -= fraction

        if pos.remaining_fraction <= 1e-9 or reason is ExitReason.STOP_LOSS:
            # Fully closed (stop always closes everything remaining).
            self._close_position(pos, price, reason, timestamp_ms)

    def _close_position(
        self, pos: ManagedPosition, price: float, reason: ExitReason, timestamp_ms: float,
    ) -> None:
        pos.status = PositionStatus.CLOSED
        pos.exit_price = price
        pos.exit_time_ms = timestamp_ms
        pos.final_reason = reason

        inst = get_instrument(pos.instrument)

        # USD PnL: sum each partial leg's price PnL × its fraction × lots × point value.
        pnl_price_weighted = 0.0
        realized_rr = 0.0
        for pc in pos.partial_closes:
            pnl_price_weighted += pc.pnl_price * pc.fraction
            realized_rr += pc.rr * pc.fraction
        # Any remainder that never hit a TP was closed by this final event already
        # (partial_closes includes it). pnl in USD:
        pnl_usd = pnl_price_weighted * inst.point_value_per_lot * pos.lots

        cost = calculate_trade_cost(
            symbol=pos.instrument, lot_size=pos.lots, cost_model=self._cost_model,
            instrument=inst,
        )
        net_pnl_usd = pnl_usd - cost.total_usd

        # Update risk guard with realized PnL.
        self._risk.add_realized_pnl(net_pnl_usd)

        # Dedup key freed.
        key = (pos.strategy_id, pos.variant_id, pos.instrument, pos.direction.value)
        self._open_keys.discard(key)
        self._positions.pop(pos.position_id, None)
        self._trades_closed += 1

        logger.info(
            "[%s] EXIT %s %s @ %.5g | %s | RR=%.2f netPnL=$%.2f bal=$%.2f",
            self.account_id, pos.direction.value, pos.instrument, price,
            reason.value, realized_rr, net_pnl_usd, self._risk.balance,
        )
        self._persist_trade(pos, realized_rr, pnl_price_weighted, pnl_usd, cost.total_usd, net_pnl_usd)
        self._notify_exit(pos, realized_rr, net_pnl_usd, reason)

    # ─── Floating PnL ────────────────────────────────────────────

    def _total_floating_pnl_usd(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            price = self._last_price.get(pos.instrument)
            if price is None:
                continue
            inst = get_instrument(pos.instrument)
            total += pos.price_pnl_per_unit(price) * pos.remaining_fraction * inst.point_value_per_lot * pos.lots
        return total

    # ─── BaseExecutor interface ──────────────────────────────────

    def open_positions(self, instrument: str | None = None) -> list[ManagedPosition]:
        positions = [p for p in self._positions.values() if p.is_open]
        if instrument is not None:
            positions = [p for p in positions if p.instrument == instrument]
        return positions

    def flatten_all(self, reason: ExitReason = ExitReason.EOD_FLATTEN) -> None:
        """Force-close every open position at the last known price."""
        for pos in list(self._positions.values()):
            if not pos.is_open:
                continue
            price = self._last_price.get(pos.instrument, pos.entry_price)
            self._close_position(pos, price, reason, self._now_ms())

    def flatten_instrument(
        self, instrument: str, reason: ExitReason = ExitReason.MANUAL,
    ) -> int:
        """Force-close open positions on ONE instrument at the last known price."""
        closed = 0
        for pos in list(self._positions.values()):
            if pos.instrument != instrument or not pos.is_open:
                continue
            price = self._last_price.get(pos.instrument, pos.entry_price)
            self._close_position(pos, price, reason, self._now_ms())
            closed += 1
        return closed

    # ─── Persistence + alerts ────────────────────────────────────

    def _persist_trade(
        self, pos: ManagedPosition, realized_rr: float, pnl_price: float,
        pnl_usd: float, cost_usd: float, net_pnl_usd: float,
    ) -> None:
        if self._store is None:
            return
        open_dt = datetime.fromtimestamp(pos.entry_time_ms / 1000, timezone.utc)
        try:
            self._store.write_cfd_paper_trade({
                "position_id": pos.position_id,
                "account_id": pos.account_id,
                "mode": self.mode.value,
                "strategy_id": pos.strategy_id,
                "variant_id": pos.variant_id,
                "instrument": pos.instrument,
                "direction": pos.direction.value,
                "entry_mode": "",  # filled by signal metadata if needed
                "entry_price": pos.entry_price,
                "entry_time_ms": int(pos.entry_time_ms),
                "exit_price": pos.exit_price,
                "exit_time_ms": int(pos.exit_time_ms),
                "stop_loss": pos.exit_plan.stop_loss,
                "take_profits": ",".join(f"{p:.5g}" for p in pos.exit_plan.take_profit_prices),
                "planned_rr": round(pos.exit_plan.max_rr, 4),
                "lots": pos.lots,
                "exit_reason": pos.final_reason.value if pos.final_reason else "",
                "realized_rr": round(realized_rr, 4),
                "pnl_price": round(pnl_price, 6),
                "pnl_usd": round(pnl_usd, 2),
                "cost_usd": round(cost_usd, 2),
                "net_pnl_usd": round(net_pnl_usd, 2),
                "mfe_price": round(pos.mfe_price, 6),
                "mae_price": round(pos.mae_price, 6),
                "session": forex_hours.session_tag(open_dt),
                "session_date": str(forex_hours.trading_day(open_dt)),
                "reason": "",
            })
        except Exception as e:  # noqa: BLE001 - never let a DB hiccup kill the loop
            logger.error("cfd_paper_trades write failed (%s): %s", pos.position_id, e)

    def _notify_entry(self, pos: ManagedPosition, signal: CFDSignal, risk_usd: float) -> None:
        if not (self._notifier and self._alert_trades):
            return
        # Prefer the rich, multi-account notifier if available; else plain text.
        if hasattr(self._notifier, "notify_entry"):
            self._notifier.notify_entry(
                account_id=self.account_id, pos=pos, signal=signal, risk_usd=risk_usd,
                open_count=len(self.open_positions()), guard_summary=self._risk.summary(),
                kind=self._kind,
            )
            return
        self._notifier.send(
            f"\U0001f4e5 ENTRY [{self.account_id}]\n"
            f"{pos.direction.value} {pos.instrument} {pos.lots:.2f} lots @ {pos.entry_price:.5g}\n"
            f"SL {signal.stop_loss:.5g} | TP {signal.exit_plan.take_profit_prices} "
            f"| RR {signal.exit_plan.max_rr:.2f}\n"
            f"Risk ${risk_usd:.2f} | {signal.strategy_id}/{signal.variant_id}"
        )

    def _notify_exit(
        self, pos: ManagedPosition, realized_rr: float, net_pnl_usd: float, reason: ExitReason,
    ) -> None:
        if not (self._notifier and self._alert_trades):
            return
        if hasattr(self._notifier, "notify_exit"):
            self._notifier.notify_exit(
                account_id=self.account_id, pos=pos, realized_rr=realized_rr,
                net_pnl_usd=net_pnl_usd, reason=reason, guard_summary=self._risk.summary(),
                kind=self._kind,
            )
            return
        emoji = "\u2705" if net_pnl_usd > 0 else "\u274c"
        self._notifier.send(
            f"{emoji} EXIT [{self.account_id}]\n"
            f"{pos.direction.value} {pos.instrument} @ {pos.exit_price:.5g} ({reason.value})\n"
            f"RR {realized_rr:+.2f} | net ${net_pnl_usd:+.2f} | bal ${self._risk.balance:.2f}"
        )

    @staticmethod
    def _now_ms() -> float:
        return datetime.now(timezone.utc).timestamp() * 1000

    # ─── Reporting ───────────────────────────────────────────────

    def summary(self) -> dict:
        s = self._risk.summary()
        s.update({
            "account_id": self.account_id,
            "kind": self._kind,
            "open_positions": len(self.open_positions()),
            "pending_arms": len(self._arms),
            "trades_opened": self._trades_opened,
            "trades_closed": self._trades_closed,
        })
        return s
