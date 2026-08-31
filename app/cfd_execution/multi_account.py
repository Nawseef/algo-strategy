"""
Multi-account manager — fan one signal out to N accounts.

Each account has its own PaperExecutor (its own balance, RiskGuard, sizing, and
trade log). A single strategy signal is delivered to every enabled account; each
sizes and gates the trade independently according to its own prop-firm rules.

This is the layer that lets you run FTMO + FundedNext + The5ers + demo at once
from a single strategy stream, each honouring its own daily/max DD limits. Until
you configure real firm accounts it simply wraps one account and behaves exactly
like a single PaperExecutor.

Ticks are fanned to all accounts so each manages its own open positions. Live
routing (placing real orders per account via cTrader) is a later addition; the
same fan-out structure will drive it.
"""

from __future__ import annotations

from app.cfd_execution.account import AccountConfig
from app.cfd_execution.base import BaseExecutor, ExitReason, ManagedPosition
from app.cfd_execution.paper_executor import PaperExecutor
from app.cfd_risk.costs import CFDCostModel
from app.cfd_strategy.base import CFDSignal
from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

logger = get_logger(__name__)


class MultiAccountManager:
    """
    Routes signals + ticks to one executor per configured account.

    Usage:
        mgr = MultiAccountManager(store=store, notifier=notifier)
        mgr.add_account(AccountConfig("demo", 100_000))          # PAPER (default)
        # later, when you join firms:
        mgr.add_account(AccountConfig("ftmo_100k", 100_000, rules=FTMO_RULES))

        mgr.on_signal(signal)                 # -> every enabled account
        mgr.on_tick("XAUUSD", bid, ask, ts)   # -> every account manages its own

    For LIVE order routing (real orders via cTrader — even on a demo account,
    this places actual trades), build a ``CTraderExecutor`` yourself (it needs
    the authenticated broker) and register it with ``add_executor`` instead of
    ``add_account``. See ``app/main_cfd_paper.py`` (``CFD_PAPER_EXECUTION_MODE``).
    """

    def __init__(
        self,
        store: ResearchStore | None = None,
        notifier=None,
        cost_model: CFDCostModel | None = None,
        alert_trades: bool = True,
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._cost_model = cost_model
        self._alert_trades = alert_trades
        self._executors: dict[str, BaseExecutor] = {}

    # ─── Account management ──────────────────────────────────────

    def add_account(self, account: AccountConfig) -> PaperExecutor:
        """Register an account with a PAPER executor (simulated fills, no
        broker orders). This is the default / safe path."""
        ex = PaperExecutor(
            account,
            store=self._store,
            notifier=self._notifier,
            cost_model=self._cost_model,
            alert_trades=self._alert_trades,
        )
        self.add_executor(ex)
        logger.info(
            "Added PAPER account '%s' (balance=$%.2f, firm=%s, risk/trade=%.2f%%)",
            account.account_id, account.initial_balance,
            account.rules.firm_name, account.effective_risk_per_trade_pct(),
        )
        return ex

    def add_executor(self, executor: BaseExecutor) -> BaseExecutor:
        """Register a pre-built executor directly (used for LIVE/CTraderExecutor,
        which needs the authenticated broker to construct)."""
        if executor.account_id in self._executors:
            raise ValueError(f"Account '{executor.account_id}' already added")
        self._executors[executor.account_id] = executor
        return executor

    def executor(self, account_id: str) -> BaseExecutor:
        return self._executors[account_id]

    def executors(self) -> list[BaseExecutor]:
        return list(self._executors.values())

    @property
    def cost_model(self) -> CFDCostModel | None:
        """The cost model shared by executors added via ``add_account``, so a
        LIVE executor built separately (see ``add_executor``) can reuse it."""
        return self._cost_model

    @property
    def account_ids(self) -> list[str]:
        return list(self._executors.keys())

    # ─── Signal / tick fan-out ───────────────────────────────────

    def on_signal(self, signal: CFDSignal) -> None:
        """Deliver a signal to every account (each sizes/gates independently)."""
        for ex in self._executors.values():
            try:
                ex.on_signal(signal)
            except Exception as e:  # noqa: BLE001 - one account must not break others
                logger.error("on_signal failed for account %s: %s", ex.account_id, e)

    def on_tick(self, instrument: str, bid: float, ask: float, timestamp_ms: float) -> None:
        """Deliver a tick to every account (each manages its own positions)."""
        for ex in self._executors.values():
            try:
                ex.on_tick(instrument, bid, ask, timestamp_ms)
            except Exception as e:  # noqa: BLE001
                logger.error("on_tick failed for account %s: %s", ex.account_id, e)

    def on_candle_close(self, instrument: str, timestamp_ms: float) -> None:
        """Age arms across all accounts."""
        for ex in self._executors.values():
            ex.on_candle_close(instrument, timestamp_ms)

    def on_day_reset(self, timestamp_ms: float | None = None) -> None:
        """Trigger each account's daily risk reset."""
        for ex in self._executors.values():
            ex.on_day_reset(timestamp_ms)

    def flatten_all(self, reason: ExitReason = ExitReason.EOD_FLATTEN) -> None:
        for ex in self._executors.values():
            ex.flatten_all(reason)

    def flatten_all_blocking(
        self, reason: ExitReason = ExitReason.EOD_FLATTEN, timeout: float = 10.0,
    ) -> None:
        """Flatten every account and block until each executor's closes are sent
        (used on shutdown so async/live closes actually leave the box)."""
        for ex in self._executors.values():
            try:
                ex.flatten_all_blocking(reason, timeout=timeout)
            except Exception as e:  # noqa: BLE001 - one account must not block others
                logger.error("flatten_all_blocking failed for account %s: %s",
                             ex.account_id, e)

    def flatten_instrument(
        self, instrument: str, reason: ExitReason = ExitReason.MANUAL,
    ) -> int:
        """Close open positions on ONE instrument across every account.

        Returns the total number of positions a close was initiated for (across
        all executors). For live/demo executors the close is asynchronous, so the
        count reflects requested closes, not settled fills.
        """
        total = 0
        for ex in self._executors.values():
            try:
                total += ex.flatten_instrument(instrument, reason)
            except Exception as e:  # noqa: BLE001 - one account must not break others
                logger.error("flatten_instrument failed for account %s: %s",
                             ex.account_id, e)
        return total

    # ─── Reporting ───────────────────────────────────────────────

    def open_positions(self, instrument: str | None = None) -> dict[str, list[ManagedPosition]]:
        """Open positions grouped by account."""
        return {aid: ex.open_positions(instrument) for aid, ex in self._executors.items()}

    def summaries(self) -> list[dict]:
        """Per-account risk/PnL summaries."""
        return [ex.summary() for ex in self._executors.values()]
