"""
Mean Reversion strategy template.

Logic:
1. Determine trend context using VWAP (price above VWAP = uptrend context).
2. Wait for RSI to reach extreme (oversold in uptrend, overbought in downtrend).
3. Entry when RSI crosses back from extreme — momentum resumption toward mean.
4. Entry mode: CANDLE_CLOSE — RSI crossover is confirmed on candle close.

This is the VWAP+RSI pullback concept — the textbook mean reversion setup.

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

# RSI levels (regime-adjusted per Cardwell method)
# In uptrends, RSI stays in 40-80 range
# In downtrends, RSI stays in 20-60 range
RSI_OVERSOLD_IN_UPTREND = 40.0
RSI_OVERBOUGHT_IN_DOWNTREND = 60.0


class MeanReversionTemplate(BaseStrategyTemplate):
    """
    Mean Reversion (VWAP + RSI Pullback) template.

    Detects when RSI reaches an extreme and crosses back,
    signaling momentum resumption toward VWAP (the mean).
    """

    def __init__(self) -> None:
        # Track previous RSI for crossover detection
        self._prev_rsi: dict[str, float] = {}
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return "MR"

    @property
    def warmup_candles(self) -> int:
        return 30  # Need RSI(14) + ADX(14) to stabilize

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
        prev_rsi = self._prev_rsi.get(token)
        self._prev_rsi[token] = rsi_val

        if prev_rsi is None or rsi_val == 0:
            return []

        candidates: list[CandidateSignal] = []

        # Determine VWAP context
        price_above_vwap = snapshot.price_vs_vwap > 0
        price_below_vwap = snapshot.price_vs_vwap < 0

        # ─── Long: Uptrend context (above VWAP) + RSI recovers from oversold
        if price_above_vwap:
            # RSI was below oversold threshold and now crosses back above
            if prev_rsi < RSI_OVERSOLD_IN_UPTREND and rsi_val >= RSI_OVERSOLD_IN_UPTREND:
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
                            "vwap": snapshot.vwap,
                            "price_vs_vwap": snapshot.price_vs_vwap,
                            "setup": "RSI_RECOVERY_LONG",
                        },
                    )
                )

        # ─── Short: Downtrend context (below VWAP) + RSI drops from overbought
        if price_below_vwap:
            # RSI was above overbought threshold and now crosses back below
            if prev_rsi > RSI_OVERBOUGHT_IN_DOWNTREND and rsi_val <= RSI_OVERBOUGHT_IN_DOWNTREND:
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
                            "vwap": snapshot.vwap,
                            "price_vs_vwap": snapshot.price_vs_vwap,
                            "setup": "RSI_RECOVERY_SHORT",
                        },
                    )
                )

        return candidates

    def _maybe_reset_daily(self) -> None:
        """Reset daily state. Called by orchestrator or internally from evaluate."""
        self._prev_rsi.clear()
        self._last_reset_date = ""
