"""
Mean Reversion strategy template.

Logic:
1. Price has overextended away from the mean (EMA20 used as mean proxy).
   VWAP would be ideal but indices have no volume data, so EMA20 is universal.
2. RSI confirms the overextension (oversold for longs, overbought for shorts).
3. Entry when RSI crosses back from extreme — snap-back toward mean expected.
4. Entry mode: CANDLE_CLOSE — RSI crossover is confirmed on candle close.

Key insight: Mean reversion = price is AWAY from the mean and reverting BACK.
- LONG: Price BELOW EMA20 (by >0.3 ATR) + RSI recovering from oversold
- SHORT: Price ABOVE EMA20 (by >0.3 ATR) + RSI dropping from overbought

Output:
- CandidateSignal with trigger_type=INDICATOR_EVENT
- trigger_value = candle close price
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

# RSI thresholds for mean reversion
RSI_OVERSOLD = 35.0
RSI_OVERBOUGHT = 65.0

# Minimum distance from EMA20 (in ATR units) to consider price "overextended"
MIN_DISTANCE_ATR = 0.3


class MeanReversionTemplate(BaseStrategyTemplate):
    """
    Mean Reversion (EMA20 + RSI) template.

    Detects when price has overextended away from EMA20 and RSI confirms
    the extreme. Entry when RSI crosses back from the extreme zone,
    betting on a snap-back toward the mean.

    Uses EMA20 instead of VWAP because indices (NIFTY/BANKNIFTY) have
    no volume data, making VWAP unreliable.
    """

    def __init__(self) -> None:
        # Track previous RSI per (token, timeframe) for crossover detection.
        # MUST include timeframe in the key — research evaluates 5m/15m/30m candles
        # for the same token in rapid succession. Without timeframe in the key,
        # a 5m candle evaluation overwrites _prev_rsi for a token and the subsequent
        # 15m evaluation uses the wrong prev_rsi, silently missing crossovers.
        self._prev_rsi: dict[tuple[str, str], float] = {}
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return "MR"

    @property
    def warmup_candles(self) -> int:
        return 30  # Need RSI(14) + EMA(20) to stabilize

    def evaluate(
        self,
        timeframe: ResearchTimeframe,
        candle: Candle,
        history: list[Candle],
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
    ) -> list[CandidateSignal]:
        """Evaluate mean reversion RSI crossover."""
        # Daily reset based on candle date (backtest-safe)
        from datetime import datetime
        candle_date = datetime.fromtimestamp(candle.timestamp_ms / 1000).strftime("%Y-%m-%d")
        if candle_date != self._last_reset_date:
            self._prev_rsi.clear()
            self._last_reset_date = candle_date

        token = candle.exchange_token

        if len(history) < 15:
            return []

        rsi_val = snapshot.rsi
        # Key by (token, timeframe) — prevents 5m candle stomping 15m prev_rsi
        # for the same instrument when candles of different timeframes close close together
        rsi_key = (token, timeframe.value)
        prev_rsi = self._prev_rsi.get(rsi_key)
        self._prev_rsi[rsi_key] = rsi_val

        if prev_rsi is None or rsi_val == 0:
            return []

        # Need valid EMA20 and ATR
        ema20 = snapshot.ema_20
        atr_val = snapshot.atr
        if ema20 == 0 or atr_val == 0:
            return []

        candidates: list[CandidateSignal] = []

        # Distance from mean (EMA20), normalized by ATR
        distance_from_mean = (candle.close - ema20) / atr_val

        # ─── Long: Price BELOW EMA20 (overextended down) + RSI recovering
        # Price has fallen below the mean, RSI was oversold and is now
        # crossing back up → expect reversion back up toward EMA20
        if distance_from_mean < -MIN_DISTANCE_ATR:
            if prev_rsi < RSI_OVERSOLD and rsi_val >= RSI_OVERSOLD:
                candidates.append(
                    CandidateSignal(
                        direction=Direction.LONG,
                        entry_mode=EntryMode.CANDLE_CLOSE,
                        trigger_type=TriggerType.INDICATOR_EVENT,
                        trigger_value=candle.close,
                        entry_price_hint=candle.close,
                        metadata={
                            "rsi": rsi_val,
                            "prev_rsi": prev_rsi,
                            "ema20": ema20,
                            "distance_atr": distance_from_mean,
                            "setup": "MR_LONG_BELOW_EMA_RSI_RECOVERY",
                        },
                    )
                )

        # ─── Short: Price ABOVE EMA20 (overextended up) + RSI dropping
        # Price has risen above the mean, RSI was overbought and is now
        # crossing back down → expect reversion back down toward EMA20
        if distance_from_mean > MIN_DISTANCE_ATR:
            if prev_rsi > RSI_OVERBOUGHT and rsi_val <= RSI_OVERBOUGHT:
                candidates.append(
                    CandidateSignal(
                        direction=Direction.SHORT,
                        entry_mode=EntryMode.CANDLE_CLOSE,
                        trigger_type=TriggerType.INDICATOR_EVENT,
                        trigger_value=candle.close,
                        entry_price_hint=candle.close,
                        metadata={
                            "rsi": rsi_val,
                            "prev_rsi": prev_rsi,
                            "ema20": ema20,
                            "distance_atr": distance_from_mean,
                            "setup": "MR_SHORT_ABOVE_EMA_RSI_DROP",
                        },
                    )
                )

        return candidates

    def _maybe_reset_daily(self) -> None:
        """Reset daily state. Called by orchestrator or internally from evaluate."""
        self._prev_rsi.clear()
        self._last_reset_date = ""
