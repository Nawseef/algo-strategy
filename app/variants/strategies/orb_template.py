"""
ORB (Opening Range Breakout) strategy template.

Logic:
1. Record high/low of first 15 minutes (9:15–9:30 IST).
2. After range is set, look for breakout above high or below low.
3. Entry mode: INTRABAR — arms a price level trigger.

Output:
- CandidateSignal with trigger_type=PRICE_LEVEL
- trigger_value = range high (for long) or range low (for short)
- Both long and short candidates can be generated simultaneously.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

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

ORB_START = dtime(9, 15)
ORB_END = dtime(9, 30)


class ORBTemplate(BaseStrategyTemplate):
    """
    Opening Range Breakout template.

    State: tracks opening range per instrument (reset daily).
    Evaluates on every candle after range is formed.
    """

    def __init__(self) -> None:
        # Per-instrument daily state.
        # range_high/low/ready are token-only because the ORB range itself is the
        # same price levels regardless of timeframe — correct to share.
        # _long_fired/_short_fired MUST be keyed by (token, timeframe.value) because
        # a 5m ORB firing sets True and then the 15m ORB for the same instrument
        # never generates a candidate. Each timeframe should arm independently.
        self._range_high: dict[str, float] = {}
        self._range_low: dict[str, float] = {}
        self._range_ready: dict[str, bool] = {}
        self._long_fired: dict[tuple[str, str], bool] = {}
        self._short_fired: dict[tuple[str, str], bool] = {}
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return "ORB"

    @property
    def warmup_candles(self) -> int:
        return 5  # ORB doesn't need much history

    def evaluate(
        self,
        timeframe: ResearchTimeframe,
        candle: Candle,
        history: list[Candle],
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
    ) -> list[CandidateSignal]:
        """Evaluate ORB setup."""
        # Use candle timestamp for time check (not system clock)
        # This ensures correct behavior in both live and backtest modes
        from datetime import datetime
        candle_dt = datetime.fromtimestamp(candle.timestamp_ms / 1000)
        candle_time = candle_dt.time()

        # Daily reset based on candle date (not system date)
        candle_date = candle_dt.strftime("%Y-%m-%d")
        if candle_date != self._last_reset_date:
            self._range_high.clear()
            self._range_low.clear()
            self._range_ready.clear()
            self._long_fired.clear()
            self._short_fired.clear()
            self._last_reset_date = candle_date

        token = candle.exchange_token

        # Phase 1: Build range during 9:15-9:30
        if ORB_START <= candle_time <= ORB_END:
            self._update_range(token, candle)
            return []

        # Mark range ready after formation period
        if not self._range_ready.get(token, False):
            if token in self._range_high and token in self._range_low:
                range_size = self._range_high[token] - self._range_low[token]
                mid = (self._range_high[token] + self._range_low[token]) / 2.0
                range_pct = (range_size / mid) * 100 if mid > 0 else 0

                # Validate: skip if range too wide (>2%) or too tight (<0.05%)
                if 0.05 <= range_pct <= 2.0:
                    self._range_ready[token] = True
                else:
                    return []
            else:
                return []

        # Phase 2: Generate breakout candidates
        candidates: list[CandidateSignal] = []
        range_high = self._range_high[token]
        range_low = self._range_low[token]

        # Long candidate: if price hasn't already broken above
        tf_key = (token, timeframe.value)
        if not self._long_fired.get(tf_key, False):
            self._long_fired[tf_key] = True
            candidates.append(
                CandidateSignal(
                    direction=Direction.LONG,
                    entry_mode=EntryMode.INTRABAR,
                    trigger_type=TriggerType.PRICE_LEVEL,
                    trigger_value=range_high,
                    entry_price_hint=range_high,
                    metadata={
                        "range_high": range_high,
                        "range_low": range_low,
                        "range_size": range_high - range_low,
                    },
                )
            )

        # Short candidate
        if not self._short_fired.get(tf_key, False):
            self._short_fired[tf_key] = True
            candidates.append(
                CandidateSignal(
                    direction=Direction.SHORT,
                    entry_mode=EntryMode.INTRABAR,
                    trigger_type=TriggerType.PRICE_LEVEL,
                    trigger_value=range_low,
                    entry_price_hint=range_low,
                    metadata={
                        "range_high": range_high,
                        "range_low": range_low,
                        "range_size": range_high - range_low,
                    },
                )
            )

        return candidates

    def _update_range(self, token: str, candle: Candle) -> None:
        """Update opening range with candle data."""
        if token not in self._range_high:
            self._range_high[token] = candle.high
            self._range_low[token] = candle.low
        else:
            self._range_high[token] = max(self._range_high[token], candle.high)
            self._range_low[token] = min(self._range_low[token], candle.low)

    def _maybe_reset_daily(self) -> None:
        """Reset daily state. Called by orchestrator's daily reset."""
        self._range_high.clear()
        self._range_low.clear()
        self._range_ready.clear()
        self._long_fired.clear()
        self._short_fired.clear()
        self._last_reset_date = ""
