"""
SuperTrend Strategy.

A trend-following strategy using the ATR-based SuperTrend indicator:
1. SuperTrend(10, 3) on 5-min candles provides trend direction.
2. Entry on SuperTrend flip (direction change).
3. EMA(20) as confirmation filter.
4. SuperTrend line itself serves as the stop-loss level.
5. Take-profit at 2:1 reward-to-risk.

SuperTrend adapts to volatility automatically via ATR, making it
effective across different market conditions. Combined with EMA filter,
it avoids entries in choppy/ranging markets.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from app.core.models import Candle, Signal, SignalType, Timeframe
from app.strategy.base import BaseStrategy
from app.strategy.cpr_filter import CPRFilter
from app.strategy.indicators import ema, supertrend_with_prev
from app.utils.logger import get_logger

logger = get_logger(__name__)

NO_TRADE_BEFORE = dtime(9, 30)
NO_TRADE_AFTER = dtime(15, 15)


class SuperTrendStrategy(BaseStrategy):
    """
    SuperTrend strategy with EMA confirmation.

    Parameters:
        instrument_tokens: Only trade these instruments.
        atr_period: ATR period for SuperTrend (default 10).
        multiplier: SuperTrend multiplier (default 3.0).
        ema_period: EMA period for trend confirmation (default 20).
        rr_ratio: Reward-to-risk ratio (default 2.0).
        max_flips_in_window: Max SuperTrend flips in last N candles before
                            considering market choppy (default 3 flips in 10 candles).
        chop_window: Number of candles to check for choppiness (default 10).
    """

    def __init__(
        self,
        instrument_tokens: list[str] | None = None,
        atr_period: int = 10,
        multiplier: float = 3.0,
        ema_period: int = 20,
        rr_ratio: float = 2.0,
        max_flips_in_window: int = 3,
        chop_window: int = 10,
        cpr_filter: CPRFilter | None = None,
    ) -> None:
        self._instrument_tokens = instrument_tokens or []
        self._atr_period = atr_period
        self._multiplier = multiplier
        self._ema_period = ema_period
        self._rr_ratio = rr_ratio
        self._max_flips = max_flips_in_window
        self._chop_window = chop_window
        self._cpr_filter = cpr_filter

        # State: track recent SuperTrend directions for chop detection
        self._direction_history: dict[str, list[bool]] = {}
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return f"SuperTrend({self._atr_period},{self._multiplier})"

    @property
    def warmup_config(self) -> dict[str, int]:
        # Need ATR(10) + some buffer for SuperTrend to stabilize + EMA(20)
        return {"5m": 50}

    def on_candle(self, candle: Candle, history: list[Candle]) -> Signal | None:
        """Evaluate SuperTrend on each 5-min candle."""
        if candle.timeframe != Timeframe.M5:
            return None

        if self._instrument_tokens and candle.exchange_token not in self._instrument_tokens:
            return None

        self._maybe_reset_daily()

        # Time filter — skip first 3 candles (9:15–9:30)
        now = datetime.now().time()
        if not (NO_TRADE_BEFORE <= now <= NO_TRADE_AFTER):
            return None

        token = candle.exchange_token

        # Need enough history
        if len(history) < 25:
            return None

        # All candles including current
        all_candles = history[-25:] + [candle]

        # Calculate SuperTrend with previous values
        st_result = supertrend_with_prev(
            all_candles, self._atr_period, self._multiplier
        )
        if st_result is None:
            return None

        current_st, current_uptrend, prev_st, prev_uptrend = st_result

        # Track direction history for chop detection
        if token not in self._direction_history:
            self._direction_history[token] = []
        self._direction_history[token].append(current_uptrend)
        # Keep only last chop_window entries
        if len(self._direction_history[token]) > self._chop_window:
            self._direction_history[token] = self._direction_history[token][-self._chop_window:]

        # Detect flip
        flipped_to_up = not prev_uptrend and current_uptrend
        flipped_to_down = prev_uptrend and not current_uptrend

        if not flipped_to_up and not flipped_to_down:
            return None

        # Chop filter: count direction changes in recent history
        dir_history = self._direction_history[token]
        if len(dir_history) >= self._chop_window:
            flips = sum(
                1 for i in range(1, len(dir_history))
                if dir_history[i] != dir_history[i - 1]
            )
            if flips >= self._max_flips:
                logger.debug(
                    "SuperTrend on %s: choppy market (%d flips in %d candles), skipping",
                    token, flips, self._chop_window,
                )
                return None

        # EMA confirmation
        closes = [c.close for c in all_candles]
        ema_val = ema(closes, self._ema_period)
        if ema_val is None:
            return None

        entry = candle.close

        if flipped_to_up:
            # Confirm: price should be above EMA
            if entry < ema_val:
                logger.debug("SuperTrend UP flip on %s rejected: price %.2f < EMA %.2f",
                             token, entry, ema_val)
                return None

            # CPR filter
            if self._cpr_filter and not self._cpr_filter.allows_signal(SignalType.BUY, entry):
                logger.debug("SuperTrend LONG on %s blocked by CPR", token)
                return None

            # SL at SuperTrend line (lower band)
            sl = current_st
            # Ensure SL is below entry
            if sl >= entry:
                sl = entry * 0.995  # fallback: 0.5% below entry

            risk = entry - sl
            tp = entry + (self._rr_ratio * risk)

            logger.info(
                "SuperTrend LONG flip on %s: entry=%.2f SL=%.2f TP=%.2f | ST=%.2f EMA=%.2f",
                token, entry, sl, tp, current_st, ema_val,
            )

            return Signal(
                signal_type=SignalType.BUY,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"SuperTrend flipped UP (ST={current_st:.2f}, EMA={ema_val:.2f})",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "supertrend": current_st,
                    "ema": ema_val,
                    "direction": "UP",
                },
            )

        if flipped_to_down:
            # Confirm: price should be below EMA
            if entry > ema_val:
                logger.debug("SuperTrend DOWN flip on %s rejected: price %.2f > EMA %.2f",
                             token, entry, ema_val)
                return None

            # CPR filter
            if self._cpr_filter and not self._cpr_filter.allows_signal(SignalType.SELL, entry):
                logger.debug("SuperTrend SHORT on %s blocked by CPR", token)
                return None

            # SL at SuperTrend line (upper band)
            sl = current_st
            # Ensure SL is above entry
            if sl <= entry:
                sl = entry * 1.005  # fallback: 0.5% above entry

            risk = sl - entry
            tp = entry - (self._rr_ratio * risk)

            logger.info(
                "SuperTrend SHORT flip on %s: entry=%.2f SL=%.2f TP=%.2f | ST=%.2f EMA=%.2f",
                token, entry, sl, tp, current_st, ema_val,
            )

            return Signal(
                signal_type=SignalType.SELL,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"SuperTrend flipped DOWN (ST={current_st:.2f}, EMA={ema_val:.2f})",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "supertrend": current_st,
                    "ema": ema_val,
                    "direction": "DOWN",
                },
            )

        return None

    def _maybe_reset_daily(self) -> None:
        """Reset daily state."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._direction_history.clear()
            self._last_reset_date = today
            logger.info("SuperTrend daily state reset for %s", today)
