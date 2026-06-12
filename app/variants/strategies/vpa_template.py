"""
VPA (Volume Price Action) strategy template.

Logic:
1. Detect candlestick patterns that indicate institutional activity:
   - Bullish Engulfing (strong reversal signal)
   - Bearish Engulfing
   - Pin Bar / Hammer (rejection wick)
   - Shooting Star (bearish rejection)
2. Confirm with volume spike (volume > 1.5x average).
3. Entry mode: CANDLE_CLOSE — pattern is confirmed when candle closes.

Output:
- CandidateSignal with trigger_type=PATTERN
- trigger_value = candle close price (entry on next candle open)
"""

from __future__ import annotations

from app.core.models import Candle
from app.variants.models import (
    Direction,
    EntryMode,
    IndicatorSnapshot,
    MetadataSnapshot,
    ResearchTimeframe,
    TriggerType,
)
from app.variants.strategies.base_template import BaseStrategyTemplate, CandidateSignal


class VPATemplate(BaseStrategyTemplate):
    """
    Volume Price Action template.

    Detects candle patterns with volume confirmation.
    Pure candle-close strategy — no tick watching needed.
    """

    @property
    def name(self) -> str:
        return "VPA"

    @property
    def warmup_candles(self) -> int:
        return 20  # Need for volume average

    def evaluate(
        self,
        timeframe: ResearchTimeframe,
        candle: Candle,
        history: list[Candle],
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
    ) -> list[CandidateSignal]:
        """Evaluate VPA patterns on current candle."""
        if len(history) < 5:
            return []

        candidates: list[CandidateSignal] = []
        prev_candle = history[-1]

        # ─── Bullish Engulfing ───────────────────────────────────────────
        if self._is_bullish_engulfing(prev_candle, candle):
            candidates.append(
                CandidateSignal(
                    direction=Direction.LONG,
                    entry_mode=EntryMode.CANDLE_CLOSE,
                    trigger_type=TriggerType.PATTERN,
                    trigger_value=candle.close,
                    entry_price_hint=candle.close,
                    metadata={"pattern": "BULLISH_ENGULFING"},
                )
            )

        # ─── Bearish Engulfing ───────────────────────────────────────────
        if self._is_bearish_engulfing(prev_candle, candle):
            candidates.append(
                CandidateSignal(
                    direction=Direction.SHORT,
                    entry_mode=EntryMode.CANDLE_CLOSE,
                    trigger_type=TriggerType.PATTERN,
                    trigger_value=candle.close,
                    entry_price_hint=candle.close,
                    metadata={"pattern": "BEARISH_ENGULFING"},
                )
            )

        # ─── Hammer / Pin Bar (bullish) ──────────────────────────────────
        if self._is_hammer(candle):
            candidates.append(
                CandidateSignal(
                    direction=Direction.LONG,
                    entry_mode=EntryMode.CANDLE_CLOSE,
                    trigger_type=TriggerType.PATTERN,
                    trigger_value=candle.close,
                    entry_price_hint=candle.close,
                    metadata={"pattern": "HAMMER"},
                )
            )

        # ─── Shooting Star (bearish) ─────────────────────────────────────
        if self._is_shooting_star(candle):
            candidates.append(
                CandidateSignal(
                    direction=Direction.SHORT,
                    entry_mode=EntryMode.CANDLE_CLOSE,
                    trigger_type=TriggerType.PATTERN,
                    trigger_value=candle.close,
                    entry_price_hint=candle.close,
                    metadata={"pattern": "SHOOTING_STAR"},
                )
            )

        return candidates

    # ─── Pattern Detection ───────────────────────────────────────────────────

    @staticmethod
    def _is_bullish_engulfing(prev: Candle, curr: Candle) -> bool:
        """
        Bullish engulfing: previous candle is bearish (red),
        current candle is bullish (green) and fully engulfs previous body.
        """
        prev_bearish = prev.close < prev.open
        curr_bullish = curr.close > curr.open

        if not (prev_bearish and curr_bullish):
            return False

        # Current body must engulf previous body
        curr_body_low = min(curr.open, curr.close)
        curr_body_high = max(curr.open, curr.close)
        prev_body_low = min(prev.open, prev.close)
        prev_body_high = max(prev.open, prev.close)

        return curr_body_low <= prev_body_low and curr_body_high >= prev_body_high

    @staticmethod
    def _is_bearish_engulfing(prev: Candle, curr: Candle) -> bool:
        """
        Bearish engulfing: previous candle is bullish,
        current candle is bearish and fully engulfs previous body.
        """
        prev_bullish = prev.close > prev.open
        curr_bearish = curr.close < curr.open

        if not (prev_bullish and curr_bearish):
            return False

        curr_body_low = min(curr.open, curr.close)
        curr_body_high = max(curr.open, curr.close)
        prev_body_low = min(prev.open, prev.close)
        prev_body_high = max(prev.open, prev.close)

        return curr_body_low <= prev_body_low and curr_body_high >= prev_body_high

    @staticmethod
    def _is_hammer(candle: Candle) -> bool:
        """
        Hammer/Pin Bar (bullish):
        - Small real body at the top of the range
        - Long lower shadow (>= 2x body size)
        - Little to no upper shadow
        """
        body = abs(candle.close - candle.open)
        full_range = candle.high - candle.low

        if full_range == 0 or body == 0:
            return False

        body_top = max(candle.open, candle.close)
        body_bottom = min(candle.open, candle.close)

        upper_shadow = candle.high - body_top
        lower_shadow = body_bottom - candle.low

        # Lower shadow must be at least 2x the body
        # Upper shadow must be small (< 30% of body)
        # Body must be in upper third of range
        return (
            lower_shadow >= 2.0 * body
            and upper_shadow <= 0.3 * body
            and body_top >= candle.low + 0.65 * full_range
        )

    @staticmethod
    def _is_shooting_star(candle: Candle) -> bool:
        """
        Shooting Star (bearish):
        - Small real body at the bottom of the range
        - Long upper shadow (>= 2x body size)
        - Little to no lower shadow
        """
        body = abs(candle.close - candle.open)
        full_range = candle.high - candle.low

        if full_range == 0 or body == 0:
            return False

        body_top = max(candle.open, candle.close)
        body_bottom = min(candle.open, candle.close)

        upper_shadow = candle.high - body_top
        lower_shadow = body_bottom - candle.low

        # Upper shadow must be at least 2x the body
        # Lower shadow must be small (< 30% of body)
        # Body must be in lower third of range
        return (
            upper_shadow >= 2.0 * body
            and lower_shadow <= 0.3 * body
            and body_bottom <= candle.low + 0.35 * full_range
        )
