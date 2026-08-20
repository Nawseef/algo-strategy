"""
cTrader LIVE executor — places and MANAGES real orders via the Open API.

Mirrors ``PaperExecutor``'s interface (``BaseExecutor``) and reuses the SAME
risk engine (RiskGuard + calculate_lot_size), the SAME exit decision logic
(``evaluate_exit`` / ``apply_dynamic_stop`` / ``time_stop_reached`` from
cfd_execution.base), and the SAME persistence. The only difference from paper is
that decisions are applied to a real account: a market order with a broker-side
stop, plus client-managed take-profits / trailing / time-stop translated into
``AmendPositionRequest`` (move the stop) and ``ClosePositionRequest`` (partial or
full close). So **paper == live**: the same policy drives both.

MONEY-SAFETY:
    * The stop is ALWAYS on the broker (placed at entry, moved via amend), so a
      crash can't leave an unprotected position.
    * ``flatten_all`` closes ONLY positions this executor opened (tracked by
      cTrader position_id) — never other positions on the account.
    * All fills (open, partial close, full close, server stop) are reconciled
      from ``ExecutionEvent``s (the authoritative source), so bookkeeping matches
      what the broker actually did.

EXIT HANDLING:
    * Simple plan (one TP, full close, no dynamic policy): SL + TP go server-side.
      The broker manages the whole exit; we just reconcile.
    * Managed plan (multiple TPs, partial fractions, breakeven, trailing, or
      time-stop): only the SL goes server-side (safety net). Take-profits are
      hit client-side (partial ``close_position``), the stop is moved by
      ``AmendPositionRequest`` (breakeven / trailing), and the time-stop closes
      the remainder at market. This supports the ScaleRunner / BreakevenAfter1R /
      AtrTrailing / TimeStop exit models live.

Threading: sync interface methods schedule coroutines on the broker's asyncio
loop via ``run_coroutine_threadsafe`` (state is only mutated on that one loop).

OPT-IN: the runner + MultiAccountManager still use PaperExecutor by default. Use
this only after a strategy proves out in paper, and start on the demo account.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.broker.ctrader import CTraderBroker
from app.cfd_execution.account import AccountConfig
from app.cfd_execution.base import (
    BaseExecutor,
    ExecutionMode,
    ExitReason,
    ManagedPosition,
    PartialClose,
    PositionStatus,
    apply_dynamic_stop,
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


class CTraderExecutor(BaseExecutor):
    """Single-account LIVE executor over the cTrader Open API, with managed exits."""

    mode = ExecutionMode.LIVE

    def __init__(
        self,
        account: AccountConfig,
        broker: CTraderBroker,
        store: ResearchStore | None = None,
        notifier=None,
        cost_model: CFDCostModel | None = None,
        alert_trades: bool = True,
        kind: str = "demo",
    ) -> None:
        self._account = account
        self._broker = broker
        self._store = store
        self._notifier = notifier
        self._cost_model = cost_model or COST_MODEL_INTRADAY
        self._alert_trades = alert_trades
        # Alert label / behaviour tag: "demo" (real orders on a demo account) or
        # "live" (real orders on a funded/prop account). Both place real orders;
        # this only changes how the alert is titled (DEMO ENTRY vs LIVE ENTRY).
        self._kind = kind

        self._risk = RiskGuard(account.to_risk_guard_config())
        self._risk_pct = account.effective_risk_per_trade_pct()

        # Positions we opened, keyed by cTrader position_id. Per-position live
        # state lives in the side dicts below (kept off ManagedPosition).
        self._positions: dict[int, ManagedPosition] = {}
        self._open_order_id: dict[int, int] = {}      # opening order_id (distinguishes open vs close fills)
        self._entry_volume: dict[int, int] = {}       # opening filled volume (for fraction math)
        self._client_managed: dict[int, bool] = {}    # TPs managed client-side?
        self._server_tp: dict[int, Decimal | None] = {}  # current broker TP (to preserve on amend)
        self._tp_requested: dict[int, set[int]] = {}  # TP indices already close-requested
        self._amend_last: dict[int, float] = {}       # last stop we amended to (dedup)
        self._closing_full: set[int] = set()          # full-close already requested (time-stop/flatten)

        self._open_keys: dict[tuple[str, str, str, str], int] = {}
        self._arms: dict[tuple[str, str, str, str], tuple[CFDSignal, int]] = {}
        self._last_price: dict[str, float] = {}
        self._risk_stash: dict[int, float] = {}       # entry risk_usd for the entry alert
        self._intended_entry: dict[int, float] = {}   # signal price (to show fill slippage)

        self._trades_opened = 0
        self._trades_closed = 0
        self._started = False
        # The REAL account balance, tracked from cTrader (seeded at start() from
        # the live account, updated from each close deal's CloseDetail.balance).
        # Net PnL is booked as the balance DELTA, which is authoritative and
        # sidesteps any commission/swap sign convention.
        self._last_real_balance: float | None = None

    # ─── Properties ──────────────────────────────────────────────

    @property
    def account_id(self) -> str:
        return self._account.account_id

    @property
    def risk_guard(self) -> RiskGuard:
        return self._risk

    # ─── Lifecycle ───────────────────────────────────────────────

    def start(self) -> None:
        """Register the ExecutionEvent handler. Call AFTER broker.authenticate()
        and BEFORE the feed's consume() (the loop must exist)."""
        if self._started:
            return
        from ctrader_api_client import ExecutionEvent

        client = self._broker.client
        acct_id = self._broker.account_id

        @client.on(ExecutionEvent, account_id=acct_id)
        async def _on_exec(event) -> None:  # runs on the broker loop
            try:
                to_finalize = self._handle_execution(event)
                if to_finalize is not None:
                    # Fully closed — fetch the REAL close accounting (commission,
                    # swap, gross) from cTrader before finalising, then book it.
                    await self._finalize_async(*to_finalize)
            except Exception as e:  # noqa: BLE001
                logger.error("[%s] execution reconcile error: %s", self.account_id, e)

        # Seed the RiskGuard with the REAL account balance so risk-% sizing is a
        # % of what the account ACTUALLY has (e.g. 0.5% of a real $8,143 demo
        # balance, not a configured $100k). Falls back to the configured balance
        # if the fetch fails.
        info = None
        try:
            info = self._broker.get_account_info()
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] account-info fetch failed: %s", self.account_id, e)
        if info and info.get("balance") is not None:
            real_bal = float(info["balance"])
            self._risk.reset_account(real_bal)
            self._last_real_balance = real_bal
            lev = info.get("leverage")
            logger.info(
                "[%s] REAL account balance $%.2f seeded (leverage %s, %s) — "
                "risk %.2f%%/trade sizes off THIS balance",
                self.account_id, real_bal,
                f"1:{lev:.0f}" if lev else "?", info.get("broker_name") or "",
                self._risk_pct,
            )
        else:
            logger.warning(
                "[%s] could not read real balance — using configured $%.2f for sizing",
                self.account_id, self._risk.balance,
            )

        self._started = True
        logger.info("[%s] CTraderExecutor started (%s order routing armed)",
                    self.account_id, self._kind.upper())

    # ─── Signal handling ─────────────────────────────────────────

    def on_signal(self, signal: CFDSignal) -> None:
        if not self._account.enabled:
            return
        key = (signal.strategy_id, signal.variant_id, signal.instrument, signal.direction.value)
        if key in self._open_keys:
            return
        if key in self._arms:
            self._arms[key] = (signal, signal.expiry_candles)
            return
        if not self._risk.can_trade():
            logger.info("[%s] Trade blocked by risk guard (%s)", self.account_id, self._risk.status.value)
            return

        if signal.entry_mode is EntryMode.CANDLE_CLOSE:
            self._submit_entry(signal, signal.entry_price)
        else:
            self._arms[key] = (signal, signal.expiry_candles)
            logger.info("[%s] ARMED (live) %s %s @ %.5g", self.account_id,
                        signal.direction.value, signal.instrument, signal.entry_price)

    def on_tick(self, instrument: str, bid: float, ask: float, timestamp_ms: float) -> None:
        if bid <= 0:
            return
        self._last_price[instrument] = bid

        # 1) Fill armed intrabar entries whose trigger was touched.
        for key, (sig, _) in list(self._arms.items()):
            if sig.instrument != instrument:
                continue
            reached = (bid >= sig.entry_price) if sig.direction is Direction.LONG else (bid <= sig.entry_price)
            if reached:
                self._arms.pop(key, None)
                if key not in self._open_keys and self._risk.can_trade():
                    self._submit_entry(sig, sig.entry_price)

        # 2) Manage open positions on this instrument.
        for pos_id, pos in list(self._positions.items()):
            if pos.instrument != instrument or pos.status is not PositionStatus.OPEN:
                continue
            pos.update_excursion(bid)
            # Dynamic stop (breakeven / trailing) -> broker amend.
            if apply_dynamic_stop(pos, bid):
                self._maybe_amend_stop(pos_id, pos)
            # Client-managed take-profits -> partial closes.
            if self._client_managed.get(pos_id):
                self._check_client_tps(pos_id, pos, bid)

    def on_candle_close(self, instrument: str, timestamp_ms: float) -> None:
        # Age arms.
        for key, (sig, remaining) in list(self._arms.items()):
            if sig.instrument != instrument:
                continue
            remaining -= 1
            if remaining <= 0:
                self._arms.pop(key, None)
                logger.info("[%s] Arm expired unfilled: %s", self.account_id, key)
            else:
                self._arms[key] = (sig, remaining)

        # Advance bar counters + honour the time-stop (close the remainder).
        for pos_id, pos in list(self._positions.items()):
            if pos.instrument != instrument or pos.status is not PositionStatus.OPEN:
                continue
            pos.bars_open += 1
            if time_stop_reached(pos) and pos_id not in self._closing_full:
                self._closing_full.add(pos_id)
                logger.info("[%s] time-stop -> closing remainder of pos %s", self.account_id, pos_id)
                self._run(self._close_volume(pos_id, self._remaining_volume(pos_id, pos)))

    def on_day_reset(self, timestamp_ms: float | None = None) -> None:
        self._risk.check_daily_reset(timestamp_ms)

    # ─── Order submission ────────────────────────────────────────

    def _submit_entry(self, signal: CFDSignal, ref_price: float) -> None:
        inst = get_instrument(signal.instrument)
        sl_distance = abs(ref_price - signal.stop_loss)
        # ALWAYS size from the INITIAL balance (not current balance) so risk $
        # is constant regardless of running P&L.
        sizing = calculate_lot_size(
            symbol=signal.instrument, account_balance=self._risk.config.initial_balance,
            risk_pct=self._risk_pct, sl_distance_price=sl_distance, instrument=inst,
        )
        if sizing.rejected:
            logger.info("[%s] Live trade rejected by sizing: %s", self.account_id, sizing.reject_reason)
            return
        ok, reason = self._risk.check_trade_risk(sizing.risk_usd)
        if not ok:
            logger.info("[%s] Live trade blocked: %s", self.account_id, reason)
            return
        self._run(self._place_entry(signal, sizing.lot_size, sizing.risk_usd))

    async def _place_entry(self, signal: CFDSignal, lots: float, risk_usd: float) -> None:
        from ctrader_api_client import NewOrderRequest, OrderSide, OrderType

        details = self._broker.symbol_details(signal.instrument)
        if details is None:
            self._broker.get_symbol_spec(signal.instrument)
            details = self._broker.symbol_details(signal.instrument)
        if details is None:
            logger.error("[%s] no symbol details for %s — cannot place order",
                         self.account_id, signal.instrument)
            return

        symbol_id = self._broker.symbol_map.get(signal.instrument)
        volume = int(details.lots_to_volume(lots))
        if volume < details.min_volume:
            logger.info("[%s] sized volume %d < min %d for %s — skipping",
                        self.account_id, volume, details.min_volume, signal.instrument)
            return
        side = OrderSide.BUY if signal.direction is Direction.LONG else OrderSide.SELL
        plan = signal.exit_plan

        # Managed plan? (multiple TPs, a partial fraction, or a dynamic policy).
        client_managed = (
            len(plan.take_profits) > 1
            or any(tp.close_fraction < 1.0 for tp in plan.take_profits)
            or (plan.exit_policy is not None and plan.exit_policy.is_dynamic())
        )
        sl = details.quantize_price(Decimal(str(plan.stop_loss)))
        # Server TP only for a simple, single, full-close plan; otherwise TPs are
        # managed client-side (a server TP would close 100% at the first target).
        server_tp = None
        if not client_managed:
            server_tp = details.quantize_price(Decimal(str(plan.take_profit_prices[-1])))

        req = NewOrderRequest(
            symbol_id=symbol_id, side=side, volume=volume, order_type=OrderType.MARKET,
            stop_loss=sl, take_profit=server_tp,
            label=signal.strategy_id[:40], comment=signal.variant_id[:40],
            client_order_id=f"CFD-{uuid.uuid4().hex[:10]}",
        )
        try:
            result = await self._broker.client.trading.place_order(self._broker.account_id, req)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] place_order failed for %s: %s", self.account_id, signal.instrument, e)
            return

        pos_id = getattr(result, "position_id", None)
        order_id = getattr(result, "order_id", None)
        err = getattr(result, "error_code", None)
        if err or pos_id is None:
            logger.error("[%s] order rejected for %s: %s", self.account_id, signal.instrument, err)
            return

        pos = ManagedPosition(
            position_id=str(pos_id), strategy_id=signal.strategy_id, variant_id=signal.variant_id,
            instrument=signal.instrument, direction=signal.direction,
            entry_price=float(signal.entry_price), entry_time_ms=float(signal.timestamp_ms),
            lots=lots, exit_plan=plan, account_id=self.account_id,
            status=PositionStatus.PENDING,
        )
        self._positions[pos_id] = pos
        self._open_order_id[pos_id] = order_id
        self._entry_volume[pos_id] = volume
        self._client_managed[pos_id] = client_managed
        self._server_tp[pos_id] = server_tp
        self._tp_requested[pos_id] = set()
        self._risk_stash[pos_id] = risk_usd
        self._intended_entry[pos_id] = float(signal.entry_price)
        self._open_keys[(signal.strategy_id, signal.variant_id, signal.instrument, signal.direction.value)] = pos_id
        self._trades_opened += 1
        logger.info("[%s] LIVE ORDER placed %s %s %.2f lots (vol %d) SL=%.5g TP=%s managed=%s pos=%s",
                    self.account_id, side.value, signal.instrument, lots, volume, float(sl),
                    f"{float(server_tp):.5g}" if server_tp is not None else "client", client_managed, pos_id)

    # ─── Live management actions ─────────────────────────────────

    def _maybe_amend_stop(self, pos_id: int, pos: ManagedPosition) -> None:
        """Amend the broker stop to the newly-moved managed stop (deduped)."""
        new_stop = pos.current_stop
        last = self._amend_last.get(pos_id)
        if last is not None and abs(last - new_stop) < 1e-9:
            return
        self._amend_last[pos_id] = new_stop
        self._run(self._amend_stop(pos_id, new_stop))

    async def _amend_stop(self, pos_id: int, new_stop: float) -> None:
        from ctrader_api_client import AmendPositionRequest

        trading = self._broker.client.trading
        if not hasattr(trading, "amend_position"):
            logger.error("[%s] library has no amend_position — cannot move stop", self.account_id)
            return
        details = self._broker.symbol_details(self._positions[pos_id].instrument)
        sl = details.quantize_price(Decimal(str(new_stop))) if details else Decimal(str(new_stop))
        try:
            await trading.amend_position(
                self._broker.account_id,
                AmendPositionRequest(position_id=pos_id, stop_loss=sl, take_profit=self._server_tp.get(pos_id)),
            )
            logger.info("[%s] amended stop -> %.5g on pos %s", self.account_id, float(sl), pos_id)
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] amend_position failed for %s: %s", self.account_id, pos_id, e)

    def _check_client_tps(self, pos_id: int, pos: ManagedPosition, price: float) -> None:
        """Fire a partial close for each client-managed TP whose price is reached."""
        requested = self._tp_requested.setdefault(pos_id, set())
        sign = pos.direction.sign
        for idx, tp in enumerate(pos.exit_plan.take_profits):
            if idx in requested:
                continue
            reached = (price >= tp.price) if sign > 0 else (price <= tp.price)
            if not reached:
                continue
            requested.add(idx)
            close_vol = self._fraction_volume(pos_id, pos, tp.close_fraction)
            if close_vol <= 0:
                logger.warning("[%s] TP%d fraction too small to close on pos %s (min volume) — skipping",
                               self.account_id, idx, pos_id)
                continue
            logger.info("[%s] client TP%d hit @ %.5g -> closing %d vol on pos %s",
                        self.account_id, idx, tp.price, close_vol, pos_id)
            self._run(self._close_volume(pos_id, close_vol))

    async def _close_volume(self, pos_id: int, volume: int) -> None:
        from ctrader_api_client import ClosePositionRequest

        if volume <= 0:
            return
        try:
            await self._broker.client.trading.close_position(
                self._broker.account_id, ClosePositionRequest(position_id=pos_id, volume=int(volume))
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] close_position failed for %s: %s", self.account_id, pos_id, e)

    # ─── Volume helpers ──────────────────────────────────────────

    def _round_volume(self, details, volume: float) -> int:
        step = getattr(details, "step_volume", 1) or 1
        v = int(round(volume / step) * step)
        return v

    def _fraction_volume(self, pos_id: int, pos: ManagedPosition, fraction: float) -> int:
        """Volume for a fraction of the ORIGINAL position, clamped to the remainder
        and rounded to the step. Returns 0 if below the broker minimum."""
        details = self._broker.symbol_details(pos.instrument)
        entry_vol = self._entry_volume.get(pos_id, 0)
        if not details or not entry_vol:
            return 0
        remaining_vol = self._remaining_volume(pos_id, pos)
        want = self._round_volume(details, fraction * entry_vol)
        want = min(want, remaining_vol)
        if want < getattr(details, "min_volume", 1):
            # Can't scale below the broker minimum; if this is effectively the
            # whole remainder, close it all instead.
            if remaining_vol >= getattr(details, "min_volume", 1) and fraction >= pos.remaining_fraction - 1e-9:
                return remaining_vol
            return 0
        return want

    def _remaining_volume(self, pos_id: int, pos: ManagedPosition) -> int:
        details = self._broker.symbol_details(pos.instrument)
        entry_vol = self._entry_volume.get(pos_id, 0)
        if not details or not entry_vol:
            return 0
        return self._round_volume(details, pos.remaining_fraction * entry_vol)

    # ─── Execution reconciliation ────────────────────────────────

    def _handle_execution(self, event) -> None:
        from ctrader_api_client import ExecutionType

        pos_id = getattr(event, "position_id", None)
        if pos_id is None or pos_id not in self._positions:
            return
        if getattr(event, "execution_type", None) != ExecutionType.ORDER_FILLED:
            return

        pos = self._positions[pos_id]
        order_id = getattr(event, "order_id", None)
        fill_price = getattr(event, "fill_price", None)
        fill = float(fill_price) if fill_price is not None else pos.entry_price

        if pos.status is PositionStatus.PENDING and order_id == self._open_order_id.get(pos_id):
            # Opening fill.
            pos.entry_price = fill
            pos.status = PositionStatus.OPEN
            filled_vol = getattr(event, "filled_volume", None)
            details = self._broker.symbol_details(pos.instrument)
            if filled_vol:
                self._entry_volume[pos_id] = int(filled_vol)
                if details is not None:
                    try:
                        pos.lots = float(details.volume_to_lots(int(filled_vol)))
                    except Exception:  # noqa: BLE001
                        pass
            logger.info("[%s] LIVE FILL %s %s %.2f lots @ %.5g pos=%s",
                        self.account_id, pos.direction.value, pos.instrument, pos.lots, fill, pos_id)
            self._notify_entry(pos)
            return

        # Otherwise: a close fill (partial client TP, full close, or server stop/TP).
        filled_vol = getattr(event, "filled_volume", None)
        entry_vol = self._entry_volume.get(pos_id, 0)
        if filled_vol and entry_vol:
            frac = min(float(filled_vol) / entry_vol, pos.remaining_fraction)
        else:
            frac = pos.remaining_fraction  # unknown volume -> assume it closed the rest
        if frac <= 0:
            return

        reason = self._classify_close(pos, fill)
        pos.partial_closes.append(PartialClose(
            price=fill, fraction=frac, reason=reason,
            timestamp_ms=self._event_ms(event), rr=pos.rr_at(fill),
            pnl_price=pos.price_pnl_per_unit(fill),
        ))
        pos.remaining_fraction -= frac
        logger.info("[%s] close fill %.0f%% @ %.5g (%s) pos=%s rem=%.2f",
                    self.account_id, frac * 100, fill, reason.value, pos_id, pos.remaining_fraction)

        if pos.remaining_fraction <= 1e-6:
            # Signal the caller (_on_exec) to finalise asynchronously so it can
            # await the real close-deal accounting from cTrader.
            return (pos_id, pos)
        return None

    def _classify_close(self, pos: ManagedPosition, price: float) -> ExitReason:
        sign = pos.direction.sign
        # At/through the managed stop?
        if (sign > 0 and price <= pos.current_stop + 1e-9) or (sign < 0 and price >= pos.current_stop - 1e-9):
            moved = abs(pos.current_stop - pos.exit_plan.stop_loss) > 1e-9
            return ExitReason.TRAILING_STOP if moved else ExitReason.STOP_LOSS
        # At/through any take-profit?
        for tp in pos.exit_plan.take_profits:
            if (sign > 0 and price >= tp.price - 1e-9) or (sign < 0 and price <= tp.price + 1e-9):
                return ExitReason.TAKE_PROFIT
        return ExitReason.MANUAL

    async def _finalize_async(self, pos_id: int, pos: ManagedPosition) -> None:
        """Fetch the REAL close accounting from cTrader, then book the close.

        The authoritative commission / swap / gross-profit for the trade live on
        the closing deal(s) (``Deal.close_detail``), NOT on the ORDER_FILLED
        event — so we query them here (by position id) and report the broker's
        real numbers. Falls back to the modeled cost model if the deals can't be
        fetched (network / timing), so a close is never lost."""
        real = await self._fetch_real_costs(pos_id)
        self._finalize(pos_id, pos, real=real)

    async def _fetch_real_costs(self, pos_id: int) -> dict | None:
        """Return the real close accounting for a position, or None to fall back.

        Aggregates every closing deal's ``CloseDetail`` (gross profit, swap,
        commission, pnl-conversion fee — all already in USD deposit currency) plus
        the commission booked on the OPENING deal(s), so the reported cost is the
        full round-trip the broker actually charged.
        """
        trading = getattr(self._broker.client, "trading", None)
        if trading is None or not hasattr(trading, "get_deals_by_position_id"):
            return None

        # The closing deal can lag the ORDER_FILLED event by a moment, so retry
        # a few times until a deal carrying a close_detail shows up.
        gross = swap = close_comm = conv = 0.0
        open_comm = 0.0
        balance = None
        found_close = False
        for attempt in range(4):
            try:
                deals = await trading.get_deals_by_position_id(self._broker.account_id, pos_id)
            except Exception as e:  # noqa: BLE001 - never let a reporting fetch break a close
                logger.warning("[%s] could not fetch deals for pos %s (%s) — using modeled cost",
                               self.account_id, pos_id, e)
                return None

            gross = swap = close_comm = conv = 0.0
            open_comm = 0.0
            balance = None
            found_close = False
            for d in deals or []:
                cd = getattr(d, "close_detail", None)
                if cd is not None:
                    found_close = True
                    gross += float(cd.gross_profit)
                    swap += float(cd.swap)
                    close_comm += float(cd.commission)
                    conv += float(cd.pnl_conversion_fee)
                    balance = float(cd.balance)
                else:
                    # Opening deal: its commission is the entry-leg charge.
                    open_comm += float(getattr(d, "commission", 0) or 0)
            if found_close:
                break
            await asyncio.sleep(0.4)

        if not found_close:
            logger.warning("[%s] no closing deal found for pos %s after retries — "
                           "using modeled cost", self.account_id, pos_id)
            return None

        commission = close_comm + open_comm          # full round-trip commission (broker-signed)
        net = gross + swap + commission - conv        # cTrader signs charges negative
        # Loud, one-time-per-trade log of the raw broker numbers so the FIRST real
        # demo trades can be sanity-checked against the cTrader statement (sign /
        # scope conventions differ per broker entity).
        logger.info(
            "[%s] REAL close accounting pos=%s: gross=%.2f swap=%.2f "
            "commission=%.2f (close=%.2f open=%.2f) convFee=%.2f -> net=%.2f bal=%s",
            self.account_id, pos_id, gross, swap, commission, close_comm, open_comm,
            conv, net, f"{balance:.2f}" if balance is not None else "?",
        )
        return {
            "gross": gross, "swap": swap, "commission": commission,
            "conv_fee": conv, "net": net, "balance": balance,
        }

    def _finalize(self, pos_id: int, pos: ManagedPosition, real: dict | None = None) -> None:
        pos.status = PositionStatus.CLOSED
        legs = pos.partial_closes
        pos.exit_price = legs[-1].price if legs else pos.entry_price
        pos.exit_time_ms = legs[-1].timestamp_ms if legs else self._now_ms()
        # Overall reason = the last leg's reason (the event that flattened it).
        pos.final_reason = legs[-1].reason if legs else ExitReason.MANUAL

        inst = get_instrument(pos.instrument)
        pnl_price_weighted = sum(pc.pnl_price * pc.fraction for pc in legs)
        realized_rr = sum(pc.rr * pc.fraction for pc in legs)

        if real is not None:
            # REAL broker accounting — no modeling. Prefer the account-balance
            # DELTA as the authoritative net PnL (it can't be wrong about signs);
            # fall back to the component sum if a balance wasn't returned.
            gross_usd = real["gross"]
            commission_usd = real["commission"]
            swap_usd = real["swap"]
            real_bal = real.get("balance")
            if real_bal is not None and self._last_real_balance is not None:
                net_pnl_usd = real_bal - self._last_real_balance
            else:
                net_pnl_usd = real["net"]
            if real_bal is not None:
                self._last_real_balance = real_bal
            # Total charges as a positive number for display/persistence.
            cost_usd = gross_usd - net_pnl_usd
        else:
            # Fallback: modeled cost (used only if the deal fetch failed).
            gross_usd = pnl_price_weighted * inst.point_value_per_lot * pos.lots
            cost = calculate_trade_cost(symbol=pos.instrument, lot_size=pos.lots,
                                        cost_model=self._cost_model, instrument=inst)
            cost_usd = cost.total_usd
            net_pnl_usd = gross_usd - cost_usd
            commission_usd = -cost.commission_usd
            swap_usd = -cost.swap_usd

        self._risk.add_realized_pnl(net_pnl_usd)

        # Cleanup all per-position state.
        key = (pos.strategy_id, pos.variant_id, pos.instrument, pos.direction.value)
        for d in (self._positions, self._open_order_id, self._entry_volume, self._client_managed,
                  self._server_tp, self._tp_requested, self._amend_last, self._risk_stash,
                  self._intended_entry):
            d.pop(pos_id, None)
        self._closing_full.discard(pos_id)
        self._open_keys.pop(key, None)
        self._trades_closed += 1

        src = "REAL" if real is not None else "modeled"
        logger.info("[%s] %s CLOSE %s %s @ %.5g | %s | RR=%.2f netPnL=$%.2f bal=$%.2f",
                    self.account_id, src, pos.direction.value, pos.instrument, pos.exit_price,
                    pos.final_reason.value, realized_rr, net_pnl_usd, self._risk.balance)
        self._persist_trade(pos, realized_rr, pnl_price_weighted, gross_usd, cost_usd, net_pnl_usd)
        self._notify_exit(pos, realized_rr, net_pnl_usd, pos.final_reason,
                          commission_usd=commission_usd, swap_usd=swap_usd)

    # ─── BaseExecutor interface ──────────────────────────────────

    def open_positions(self, instrument: str | None = None) -> list[ManagedPosition]:
        out = [p for p in self._positions.values() if p.status is PositionStatus.OPEN]
        if instrument is not None:
            out = [p for p in out if p.instrument == instrument]
        return out

    def flatten_all(self, reason: ExitReason = ExitReason.EOD_FLATTEN) -> None:
        """Close ONLY the positions this executor opened (never the whole account)."""
        for pos_id, pos in list(self._positions.items()):
            if pos.status is PositionStatus.OPEN and pos_id not in self._closing_full:
                self._closing_full.add(pos_id)
                self._run(self._close_volume(pos_id, self._remaining_volume(pos_id, pos)))

    # ─── Persistence + alerts (shared shape with PaperExecutor) ───

    def _persist_trade(self, pos, realized_rr, pnl_price, pnl_usd, cost_usd, net_pnl_usd) -> None:
        if self._store is None:
            return
        open_dt = datetime.fromtimestamp(pos.entry_time_ms / 1000, timezone.utc)
        try:
            self._store.write_cfd_paper_trade({
                "position_id": pos.position_id, "account_id": pos.account_id, "mode": self.mode.value,
                "strategy_id": pos.strategy_id, "variant_id": pos.variant_id,
                "instrument": pos.instrument, "direction": pos.direction.value, "entry_mode": "",
                "entry_price": pos.entry_price, "entry_time_ms": int(pos.entry_time_ms),
                "exit_price": pos.exit_price, "exit_time_ms": int(pos.exit_time_ms),
                "stop_loss": pos.exit_plan.stop_loss,
                "take_profits": ",".join(f"{p:.5g}" for p in pos.exit_plan.take_profit_prices),
                "planned_rr": round(pos.exit_plan.max_rr, 4), "lots": pos.lots,
                "exit_reason": pos.final_reason.value if pos.final_reason else "",
                "realized_rr": round(realized_rr, 4), "pnl_price": round(pnl_price, 6),
                "pnl_usd": round(pnl_usd, 2), "cost_usd": round(cost_usd, 2),
                "net_pnl_usd": round(net_pnl_usd, 2),
                "mfe_price": round(pos.mfe_price, 6), "mae_price": round(pos.mae_price, 6),
                "session": forex_hours.session_tag(open_dt),
                "session_date": str(forex_hours.trading_day(open_dt)), "reason": "",
            })
        except Exception as e:  # noqa: BLE001
            logger.error("cfd_paper_trades (LIVE) write failed (%s): %s", pos.position_id, e)

    def _notify_entry(self, pos: ManagedPosition) -> None:
        if not (self._notifier and self._alert_trades):
            return
        pid = int(pos.position_id)
        risk_usd = self._risk_stash.get(pid, 0.0)
        intended = self._intended_entry.get(pid)
        # Prefer the rich, multi-account notifier (same format as paper, tagged
        # LIVE + showing the real fill's slippage vs the intended signal price),
        # so the paper and demo alerts are directly comparable.
        if hasattr(self._notifier, "notify_entry"):
            self._notifier.notify_entry(
                account_id=self.account_id, pos=pos, risk_usd=risk_usd,
                open_count=len(self.open_positions()), guard_summary=self._risk.summary(),
                kind=self._kind, intended_price=intended,
            )
            return
        self._notifier.send(
            f"\U0001f4e5 {self._kind.upper()} ENTRY [{self.account_id}]\n"
            f"{pos.direction.value} {pos.instrument} {pos.lots:.2f} lots @ {pos.entry_price:.5g}\n"
            f"SL {pos.exit_plan.stop_loss:.5g} | TP {pos.exit_plan.take_profit_prices[-1]:.5g} "
            f"| RR {pos.exit_plan.max_rr:.2f} | risk ${risk_usd:.2f}"
        )

    def _notify_exit(self, pos, realized_rr, net_pnl_usd, reason,
                     commission_usd: float | None = None, swap_usd: float | None = None) -> None:
        if not (self._notifier and self._alert_trades):
            return
        if hasattr(self._notifier, "notify_exit"):
            self._notifier.notify_exit(
                account_id=self.account_id, pos=pos, realized_rr=realized_rr,
                net_pnl_usd=net_pnl_usd, reason=reason, guard_summary=self._risk.summary(),
                kind=self._kind, commission_usd=commission_usd, swap_usd=swap_usd,
            )
            return
        emoji = "\u2705" if net_pnl_usd > 0 else "\u274c"
        self._notifier.send(
            f"{emoji} {self._kind.upper()} EXIT [{self.account_id}]\n"
            f"{pos.direction.value} {pos.instrument} @ {pos.exit_price:.5g} ({reason.value})\n"
            f"RR {realized_rr:+.2f} | net ${net_pnl_usd:+.2f} | bal ${self._risk.balance:.2f}"
        )

    # ─── Helpers ─────────────────────────────────────────────────

    def _run(self, coro) -> None:
        """Schedule a coroutine on the broker's loop (fire-and-forget, logged)."""
        fut = asyncio.run_coroutine_threadsafe(coro, self._broker.loop)

        def _log(f):
            try:
                f.result()
            except Exception as e:  # noqa: BLE001
                logger.error("[%s] async order op failed: %s", self.account_id, e)

        fut.add_done_callback(_log)

    @staticmethod
    def _event_ms(event) -> float:
        ts = getattr(event, "timestamp", None)
        return ts.timestamp() * 1000 if ts else CTraderExecutor._now_ms()

    @staticmethod
    def _now_ms() -> float:
        return datetime.now(timezone.utc).timestamp() * 1000

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
