"""
Trend Following strategy template.

Logic:
1. Determine trend direction using EMA alignment (EMA9 > EMA21 = bullish).
2. Wait for pullback to EMA zone (price retraces toward EMA21).
3. Entry on price bouncing off the EMA zone (resuming trend).
4. Entry mode: INTRABAR — arms a price level at the EMA pullback zone.

This captures "buy the dip in an uptrend" and "sell the rally in a downtrend"
with EMA-defined trend context.

Output:
- CandidateSignal with trigger_type=STRUCTURE (pullback zone)
- trigger_value = EMA21 level (pullback target)
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


class TrendTemplate(BaseStrategyTemplate):
    """
    Trend Following (EMA Pullback) template.

    Detects established trends and generates entry candidates
    when price pulls back to the EMA support/resistance zone.
    """

    def __init__(self) -> None:
        # Keyed by (token, timeframe.value) — prevents 5m evaluation stomping
        # 15m state for the same instrument (same bug that existed in _prev_rsi).
        self._in_pullback: dict[tuple[str, str], bool] = {}
        self._trend_direction: dict[tuple[str, str], str] = {}  # "BULL" / "BEAR" / ""
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return "TREND"

    @property
    def warmup_candles(self) -> int:
        return 50  # Need for EMA50 to stabilize

    def evaluate(
        self,
        timeframe: ResearchTimeframe,
        candle: Candle,
        history: list[Candle],
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
    ) -> list[CandidateSignal]:
        """Evaluate trend pullback setup."""
        # Daily reset based on candle date (backtest-safe)
        from datetime import datetime
        candle_date = datetime.fromtimestamp(candle.timestamp_ms / 1000).strftime("%Y-%m-%d")
        if candle_date != self._last_reset_date:
            self._in_pullback.clear()
            self._trend_direction.clear()
            self._last_reset_date = candle_date

        token = candle.exchange_token

        if len(history) < 25:
            return []

        # ─── Determine trend direction ───────────────────────────────────
        # Bullish: EMA9 > EMA21 AND EMA20 slope positive
        # Bearish: EMA9 < EMA21 AND EMA20 slope negative
        ema9 = snapshot.ema_9
        ema21 = snapshot.ema_21

        if ema9 == 0 or ema21 == 0:
            return []

        is_bullish = ema9 > ema21 and snapshot.ema_20_slope > 0
        is_bearish = ema9 < ema21 and snapshot.ema_20_slope < 0

        if not is_bullish and not is_bearish:
            self._trend_direction[(token, timeframe.value)] = ""
            self._in_pullback[(token, timeframe.value)] = False
            return []

        candidates: list[CandidateSignal] = []

        if is_bullish:
            # ─── Bullish Trend Pullback ──────────────────────────────────
            pullback_zone = ema21 * 1.003
            price_near_ema = candle.low <= pullback_zone

            tf_key = (token, timeframe.value)
            was_in_pullback = self._in_pullback.get(tf_key, False)

            if price_near_ema and not was_in_pullback:
                self._in_pullback[tf_key] = True
                self._trend_direction[tf_key] = "BULL"

                entry_level = ema21 * 1.001

                candidates.append(
                    CandidateSignal(
                        direction=Direction.LONG,
                        entry_mode=EntryMode.INTRABAR,
                        trigger_type=TriggerType.STRUCTURE,
                        trigger_value=entry_level,
                        entry_price_hint=candle.close,
                        metadata={
                            "ema9": ema9,
                            "ema21": ema21,
                            "ema20_slope": snapshot.ema_20_slope,
                            "setup": "EMA_PULLBACK_LONG",
                        },
                    )
                )
            elif not price_near_ema:
                self._in_pullback[tf_key] = False

        elif is_bearish:
            # ─── Bearish Trend Pullback ──────────────────────────────────
            pullback_zone = ema21 * 0.997
            price_near_ema = candle.high >= pullback_zone

            tf_key = (token, timeframe.value)
            was_in_pullback = self._in_pullback.get(tf_key, False)

            if price_near_ema and not was_in_pullback:
                self._in_pullback[tf_key] = True
                self._trend_direction[tf_key] = "BEAR"

                entry_level = ema21 * 0.999

                candidates.append(
                    CandidateSignal(
                        direction=Direction.SHORT,
                        entry_mode=EntryMode.INTRABAR,
                        trigger_type=TriggerType.STRUCTURE,
                        trigger_value=entry_level,
                        entry_price_hint=candle.close,
                        metadata={
                            "ema9": ema9,
                            "ema21": ema21,
                            "ema20_slope": snapshot.ema_20_slope,
                            "setup": "EMA_PULLBACK_SHORT",
                        },
                    )
                )
            elif not price_near_ema:
                self._in_pullback[tf_key] = False

        return candidates

    def _maybe_reset_daily(self) -> None:
        """Reset daily state. Called by orchestrator or internally from evaluate."""
        self._in_pullback.clear()
        self._trend_direction.clear()
        self._last_reset_date = ""
