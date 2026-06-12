"""
Bollinger Bands strategy template.

Logic:
1. Detect squeeze (BB inside Keltner Channels for 5+ candles).
2. When squeeze releases, generate breakout candidate.
3. Direction determined by which band price breaks through.
4. Entry mode: INTRABAR — arms a price level trigger at the band.

Output:
- CandidateSignal with trigger_type=PRICE_LEVEL
- trigger_value = upper band (long) or lower band (short)
"""

from __future__ import annotations

from app.core.models import Candle
from app.strategy.indicators import bollinger_bands, is_squeeze
from app.variants.models import (
    Direction,
    EntryMode,
    IndicatorSnapshot,
    MetadataSnapshot,
    ResearchTimeframe,
    TriggerType,
)
from app.variants.strategies.base_template import BaseStrategyTemplate, CandidateSignal

MIN_SQUEEZE_CANDLES = 5


class BBTemplate(BaseStrategyTemplate):
    """
    Bollinger Band Squeeze Breakout template.

    Tracks squeeze state per instrument. When squeeze releases,
    generates both long and short candidates at band levels.
    """

    def __init__(self) -> None:
        # Per-instrument squeeze tracking
        self._squeeze_count: dict[str, int] = {}
        self._was_in_squeeze: dict[str, bool] = {}
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return "BB"

    @property
    def warmup_candles(self) -> int:
        return 30  # Need 20 for BB + buffer

    def evaluate(
        self,
        timeframe: ResearchTimeframe,
        candle: Candle,
        history: list[Candle],
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
    ) -> list[CandidateSignal]:
        """Evaluate BB squeeze breakout."""
        # Daily reset based on candle date (backtest-safe)
        from datetime import datetime
        candle_date = datetime.fromtimestamp(candle.timestamp_ms / 1000).strftime("%Y-%m-%d")
        if candle_date != self._last_reset_date:
            self._squeeze_count.clear()
            self._was_in_squeeze.clear()
            self._last_reset_date = candle_date

        token = candle.exchange_token

        if len(history) < 25:
            return []

        all_candles = history[-25:] + [candle]

        # Check current squeeze state
        squeeze_active = is_squeeze(all_candles)
        if squeeze_active is None:
            return []

        prev_in_squeeze = self._was_in_squeeze.get(token, False)

        if squeeze_active:
            # Currently in squeeze — count
            self._squeeze_count[token] = self._squeeze_count.get(token, 0) + 1
            self._was_in_squeeze[token] = True
            return []

        # Squeeze just released
        if prev_in_squeeze and not squeeze_active:
            self._was_in_squeeze[token] = False
            squeeze_duration = self._squeeze_count.get(token, 0)
            self._squeeze_count[token] = 0

            if squeeze_duration < MIN_SQUEEZE_CANDLES:
                return []

            # Squeeze released — generate breakout candidates at band levels
            candidates: list[CandidateSignal] = []

            bb_upper = snapshot.bb_upper
            bb_lower = snapshot.bb_lower

            if bb_upper > 0 and bb_lower > 0:
                # Long: breakout above upper band
                candidates.append(
                    CandidateSignal(
                        direction=Direction.LONG,
                        entry_mode=EntryMode.INTRABAR,
                        trigger_type=TriggerType.PRICE_LEVEL,
                        trigger_value=bb_upper,
                        entry_price_hint=bb_upper,
                        metadata={
                            "squeeze_duration": squeeze_duration,
                            "bb_upper": bb_upper,
                            "bb_lower": bb_lower,
                            "bb_middle": snapshot.bb_middle,
                        },
                    )
                )

                # Short: breakout below lower band
                candidates.append(
                    CandidateSignal(
                        direction=Direction.SHORT,
                        entry_mode=EntryMode.INTRABAR,
                        trigger_type=TriggerType.PRICE_LEVEL,
                        trigger_value=bb_lower,
                        entry_price_hint=bb_lower,
                        metadata={
                            "squeeze_duration": squeeze_duration,
                            "bb_upper": bb_upper,
                            "bb_lower": bb_lower,
                            "bb_middle": snapshot.bb_middle,
                        },
                    )
                )

            return candidates

        # Not in squeeze and wasn't before — reset
        self._was_in_squeeze[token] = False
        self._squeeze_count[token] = 0
        return []

    def _maybe_reset_daily(self) -> None:
        """Reset daily state. Called by orchestrator or internally from evaluate."""
        self._squeeze_count.clear()
        self._was_in_squeeze.clear()
        self._last_reset_date = ""
