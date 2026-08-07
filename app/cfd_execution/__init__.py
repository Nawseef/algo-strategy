"""
CFD Execution — turns strategy signals into trades.

Three execution modes are supported behind one interface:
  * PAPER    — track trades in-memory + Postgres + Telegram, no broker orders.
  * LIVE     — place real orders via the cTrader Open API (filled in later).
  * BOTH     — paper-track AND live-execute simultaneously (for comparison).

An executor consumes CFDSignals (from the strategy framework) and ticks (from
the feed), manages open positions against their mandatory SL/TP, and reports
via Telegram. Position sizing and risk gating come from app.cfd_risk.

Modules:
    base           — ExecutionMode, ManagedPosition, BaseExecutor
    account        — AccountConfig + PropFirmRules (pluggable placeholder)
    paper_executor — Telegram + Postgres paper trading
    multi_account  — route one signal to N accounts
"""

from app.cfd_execution.base import (
    BaseExecutor,
    ExecutionMode,
    ExitReason,
    ManagedPosition,
    PositionStatus,
)
from app.cfd_execution.account import AccountConfig, PropFirmRules

__all__ = [
    "BaseExecutor",
    "ExecutionMode",
    "ExitReason",
    "ManagedPosition",
    "PositionStatus",
    "AccountConfig",
    "PropFirmRules",
]
