"""
Abstract base class for strategy templates.

A strategy template evaluates whether its specific pattern/setup
is present on the current candle. It outputs a CandidateSignal
describing what was found and what trigger to watch for.

Strategy templates are STATELESS per variant — they receive
all needed context as parameters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.models import Candle
from app.variants.models import (
    Direction,
    EntryMode,
    IndicatorSnapshot,
    MetadataSnapshot,
    ResearchTimeframe,
    TriggerType,
)


@dataclass
class CandidateSignal:
    """
    Output of a strategy template evaluation.

    This is NOT a trade — it's a "candidate setup" that passed
    strategy logic. Filters are applied separately.

    For CANDLE_CLOSE entry mode: the trade is created immediately.
    For INTRABAR entry mode: the variant becomes ARMED with trigger info.
    """

    direction: Direction
    entry_mode: EntryMode
    trigger_type: TriggerType
    trigger_value: float  # Price level, indicator threshold, etc.
    entry_price_hint: float  # Expected entry price (for SL sizing)
    confidence: float = 1.0  # 0-1, for future ranking
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseStrategyTemplate(ABC):
    """
    Abstract strategy template.

    Each strategy evaluates a specific market pattern and returns
    a CandidateSignal if the pattern is present, or None if not.

    Strategy templates:
    - Receive shared indicator snapshot (computed once)
    - Receive candle history (shared, read-only)
    - Must be FAST (called once per strategy per instrument per candle)
    - Must NOT allocate per-variant (they're called for the strategy group)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy template name."""
        ...

    @abstractmethod
    def evaluate(
        self,
        timeframe: ResearchTimeframe,
        candle: Candle,
        history: list[Candle],
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
    ) -> list[CandidateSignal]:
        """
        Evaluate the strategy on the current candle.

        Returns a list of CandidateSignals (can be 0, 1, or 2 — e.g. both
        long and short setups may be valid simultaneously for some strategies).

        Args:
            timeframe: The timeframe being evaluated.
            candle: The just-completed candle.
            history: Recent candle history for this instrument/timeframe.
            snapshot: Pre-computed indicator values.
            metadata: Market context metadata.

        Returns:
            List of CandidateSignal objects. Empty list = no setup found.
        """
        ...

    @property
    def warmup_candles(self) -> int:
        """Minimum candle history needed for this strategy to evaluate."""
        return 30
