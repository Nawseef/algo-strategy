"""
EntryStrategy — research-side strategies that emit ENTRIES ONLY (no exit).

This is the entry/exit separation: a research strategy decides *where to get in*
and *where the stop is* (an ``EntryIntent`` — the stop defines 1R). The exit is
NOT baked in; the entry-replay runs the same entries through each exit model in
``exit_models`` so we can measure which exit fits the entry / session / regime.

Contrast with the live-side ``CFDStrategy`` (app/cfd_strategy/base.py), which
emits a full ``CFDSignal`` with the exit plan attached — that's for paper/live
execution. Research uses this leaner contract so one entry set is scored under
many exits without re-generating entries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.cfd_research.exit_models import EntryIntent
from app.core.models import Candle, Timeframe


@dataclass
class EntryContext:
    """What an entry strategy sees on a just-closed bar."""

    instrument: str
    timeframe: Timeframe
    candle: Candle                 # the just-closed bar
    history: list[Candle]          # oldest -> newest, INCLUDING the current bar

    @property
    def close(self) -> float:
        return self.candle.close


class EntryStrategy(ABC):
    """Base for research entry strategies (emit EntryIntent, no exit)."""

    strategy_id: str = "base_entry"
    name: str = "Base Entry Strategy"
    timeframe: Timeframe = Timeframe.M5
    instruments: tuple[str, ...] = ()      # empty = applies to all
    min_history: int = 50

    def applies_to(self, instrument: str) -> bool:
        return not self.instruments or instrument in self.instruments

    @abstractmethod
    def entries(self, ctx: EntryContext) -> list[EntryIntent]:
        """Return zero or more entries for the just-closed bar."""
        ...

    def on_day_reset(self) -> None:
        """Optional hook at the FX trading-day boundary (reset per-day state)."""
        ...
