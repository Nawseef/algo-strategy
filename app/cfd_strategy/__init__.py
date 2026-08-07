"""
CFD Strategy Framework — lightweight, hand-written strategies for CFD trading.

Unlike the NSE 150K-variant research engine, this framework is deliberately
simple: you write each strategy by hand, and a strategy may have 1 to a handful
of variants (not tens of thousands). Every signal carries a MANDATORY stop-loss
and take-profit, and the exit plan is enforced to achieve at least 1:2 R:R.

Modules:
    base      — Direction, EntryMode, TakeProfit, ExitPlan, CFDSignal, CFDStrategy
    registry  — register + discover strategies and their variants
"""

from app.cfd_strategy.base import (
    CFDSignal,
    CFDStrategy,
    Direction,
    EntryMode,
    ExitPlan,
    StrategyContext,
    TakeProfit,
    build_rr_exit_plan,
)
from app.cfd_strategy.registry import (
    StrategyRegistry,
    get_registry,
    register_strategy,
)

__all__ = [
    "CFDSignal",
    "CFDStrategy",
    "Direction",
    "EntryMode",
    "ExitPlan",
    "StrategyContext",
    "TakeProfit",
    "build_rr_exit_plan",
    "StrategyRegistry",
    "get_registry",
    "register_strategy",
]
