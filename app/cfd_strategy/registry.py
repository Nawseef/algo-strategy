"""
Strategy registry — register and discover CFD strategies.

A tiny in-process registry so the paper executor / backtest replay can pick up
strategies by id without hard-coding imports. Register a strategy instance and
it becomes available to whatever runner asks for it.

Usage:
    from app.cfd_strategy.registry import register_strategy, get_registry

    @register_strategy
    class MyGoldStrategy(CFDStrategy):
        strategy_id = "gold_london_orb"
        ...

    # elsewhere:
    strategies = get_registry().all()
"""

from __future__ import annotations

from typing import Iterable

from app.cfd_strategy.base import CFDStrategy
from app.utils.logger import get_logger

logger = get_logger(__name__)


class StrategyRegistry:
    """Holds registered strategy instances keyed by strategy_id."""

    def __init__(self) -> None:
        self._strategies: dict[str, CFDStrategy] = {}

    def register(self, strategy: CFDStrategy) -> CFDStrategy:
        """Register a strategy instance. Rejects duplicate ids."""
        sid = strategy.strategy_id
        if sid in self._strategies:
            raise ValueError(
                f"Strategy id '{sid}' is already registered "
                f"({type(self._strategies[sid]).__name__})"
            )
        self._strategies[sid] = strategy
        logger.info(
            "Registered CFD strategy '%s' (%s) tf=%s instruments=%s variants=%s",
            sid, type(strategy).__name__, strategy.timeframe.value,
            strategy.instruments or "ALL", strategy.variants,
        )
        return strategy

    def get(self, strategy_id: str) -> CFDStrategy:
        if strategy_id not in self._strategies:
            raise KeyError(f"No strategy registered with id '{strategy_id}'")
        return self._strategies[strategy_id]

    def all(self) -> list[CFDStrategy]:
        return list(self._strategies.values())

    def ids(self) -> list[str]:
        return list(self._strategies.keys())

    def for_instrument(self, instrument: str) -> list[CFDStrategy]:
        """All registered strategies that trade the given instrument."""
        return [s for s in self._strategies.values() if s.applies_to(instrument)]

    def clear(self) -> None:
        """Remove all registrations (used in tests)."""
        self._strategies.clear()

    def __len__(self) -> int:
        return len(self._strategies)


# Module-level singleton registry.
_REGISTRY = StrategyRegistry()


def get_registry() -> StrategyRegistry:
    """Return the process-wide strategy registry."""
    return _REGISTRY


def register_strategy(strategy_cls: type[CFDStrategy]) -> type[CFDStrategy]:
    """
    Class decorator: instantiate and register a strategy.

    The strategy class must be constructible with no arguments. For strategies
    that need parameters, register an instance manually via
    ``get_registry().register(MyStrategy(param=...))``.
    """
    instance = strategy_cls()
    _REGISTRY.register(instance)
    return strategy_cls


def register_instances(strategies: Iterable[CFDStrategy]) -> None:
    """Register multiple pre-built strategy instances."""
    for s in strategies:
        _REGISTRY.register(s)
