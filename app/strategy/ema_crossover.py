"""
EMA 9/21 Crossover + ADX Filter Strategy.

A trend-following scalping strategy:
1. EMA(9) crosses EMA(21) on 5-min candles for entry signal.
2. ADX > 25 confirms a trending market (avoids whipsaws in chop).
3. Volume spike confirms institutional participation.
4. ATR-based stop-loss and take-profit for volatility-adaptive risk management.
5. VWAP alignment as directional filter.

Based on research showing 74% win rate and Profit Factor 2.25
when combining EMA crossover with candlestick/momentum confirmation.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from app.core.models import Candle, Signal, SignalType, Timeframe
from app.strategy.base import BaseStrategy
from app.strategy.cpr_filter import CPRFilter
from app.strategy.indicators import adx, atr, ema, sma, vwap
from app.utils.logger import get_logger

logger = get_logger(__name__)

NO_TRADE_BEFORE = dtime(9, 30)
NO_TRADE_AFTER = dtime(15, 15)


class EMACrossoverStrategy(BaseStrategy):
    """
    EMA 9/21 Crossover with ADX and VWAP filters.

    Parameters:
        instrument_tokens: Only trade these instruments.
        fast_period: Fast EMA period (default 9).
        slow_period: Slow EMA period (default 21).
        adx_threshold: Minimum ADX for trend confirmation (default 25).
        atr_period: ATR period for SL/TP calculation (default 14).
        sl_atr_multiplier: SL distance in ATR multiples (default 1.5).
        tp_atr_multiplier: TP distance in ATR multiples (default 3.0).
        volume_multiplier: Breakout candle volume must be > this × avg (default 1.3).
        use_vwap_filter: Require VWAP alignment (default True).
        cooldown_candles: Min candles between signals per instrument (default 5).
    """

    def __init__(
        self,
        instrument_tokens: list[str] | None = None,
        fast_period: int = 9,
        slow_period: int = 21,
        adx_threshold: float = 25.0,
        atr_period: int = 14,
        sl_atr_multiplier: float = 1.5,
        tp_atr_multiplier: float = 3.0,
        volume_multiplier: float = 1.3,
        use_vwap_filter: bool = True,
        cooldown_candles: int = 5,
        cpr_filter: CPRFilter | None = None,
    ) -> None:
        self._instrument_tokens = instrument_tokens or []
        self._fast_period = fast_period
        self._slow_period = slow_period
        self._adx_threshold = adx_threshold
        self._atr_period = atr_period
        self._sl_atr_mult = sl_atr_multiplier
        self._tp_atr_mult = tp_atr_multiplier
        self._volume_mult = volume_multiplier
        self._use_vwap_filter = use_vwap_filter
        self._cooldown_candles = cooldown_candles
        self._cpr_filter = cpr_filter

        # State
        self._prev_fast_ema: dict[str, float] = {}
        self._prev_slow_ema: dict[str, float] = {}
        self._candles_since_signal: dict[str, int] = {}
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return f"EMA_Crossover({self._fast_period}/{self._slow_period})"

    @property
    def warmup_config(self) -> dict[str, int]:
        # Need enough for EMA(21) + ADX(14) + ATR(14) to stabilize
        return {"5m": 50}

    def on_candle(self, candle: Candle, history: list[Candle]) -> Signal | None:
        """Evaluate EMA crossover on each 5-min candle."""
        if candle.timeframe != Timeframe.M5:
            return None

        if self._instrument_tokens and candle.exchange_token not in self._instrument_tokens:
            return None

        self._maybe_reset_daily()

        # Time filter
        now = datetime.now().time()
        if not (NO_TRADE_BEFORE <= now <= NO_TRADE_AFTER):
            return None

        token = candle.exchange_token

        # Increment cooldown counter
        if token in self._candles_since_signal:
            self._candles_since_signal[token] += 1

        # Need enough history
        if len(history) < 35:
            return None

        # All candles including current
        all_candles = history[-35:] + [candle]
        closes = [c.close for c in all_candles]

        # Calculate EMAs
        fast_ema = ema(closes, self._fast_period)
        slow_ema = ema(closes, self._slow_period)

        if fast_ema is None or slow_ema is None:
            return None

        # Get previous EMA values for crossover detection
        prev_fast = self._prev_fast_ema.get(token)
        prev_slow = self._prev_slow_ema.get(token)

        # Store current for next iteration
        self._prev_fast_ema[token] = fast_ema
        self._prev_slow_ema[token] = slow_ema

        if prev_fast is None or prev_slow is None:
            return None

        # Detect crossover
        bullish_cross = prev_fast <= prev_slow and fast_ema > slow_ema
        bearish_cross = prev_fast >= prev_slow and fast_ema < slow_ema

        if not bullish_cross and not bearish_cross:
            return None

        # Cooldown check
        candles_since = self._candles_since_signal.get(token, self._cooldown_candles + 1)
        if candles_since < self._cooldown_candles:
            logger.debug("EMA crossover cooldown active for %s (%d/%d candles)",
                         token, candles_since, self._cooldown_candles)
            return None

        # ADX filter — need trending market
        adx_val = adx(all_candles, 14)
        if adx_val is None or adx_val < self._adx_threshold:
            logger.debug("EMA crossover on %s rejected: ADX=%.1f < %.1f",
                         token, adx_val or 0, self._adx_threshold)
            return None

        # Volume confirmation
        avg_volume = sma([float(c.volume) for c in all_candles[-10:]], 10)
        if avg_volume and candle.volume < avg_volume * self._volume_mult:
            logger.debug("EMA crossover on %s rejected: volume %d < %.0f (%.1f× avg)",
                         token, candle.volume, avg_volume * self._volume_mult, self._volume_mult)
            return None

        # ATR for SL/TP
        atr_val = atr(all_candles, self._atr_period)
        if atr_val is None or atr_val <= 0:
            return None

        # VWAP filter
        if self._use_vwap_filter:
            today_candles = self._get_today_candles(history, candle)
            vwap_val = vwap(today_candles)
            if vwap_val is not None:
                if bullish_cross and candle.close < vwap_val:
                    logger.debug("EMA bullish cross on %s rejected: price %.2f < VWAP %.2f",
                                 token, candle.close, vwap_val)
                    return None
                if bearish_cross and candle.close > vwap_val:
                    logger.debug("EMA bearish cross on %s rejected: price %.2f > VWAP %.2f",
                                 token, candle.close, vwap_val)
                    return None

        # Generate signal
        entry = candle.close
        self._candles_since_signal[token] = 0

        if bullish_cross:
            # CPR filter
            if self._cpr_filter and not self._cpr_filter.allows_signal(SignalType.BUY, entry):
                return None

            sl = entry - (self._sl_atr_mult * atr_val)
            tp = entry + (self._tp_atr_mult * atr_val)

            logger.info(
                "EMA LONG crossover on %s: entry=%.2f SL=%.2f TP=%.2f | "
                "EMA9=%.2f EMA21=%.2f ADX=%.1f ATR=%.2f",
                token, entry, sl, tp, fast_ema, slow_ema, adx_val, atr_val,
            )

            return Signal(
                signal_type=SignalType.BUY,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"EMA {self._fast_period} crossed above EMA {self._slow_period} (ADX={adx_val:.1f})",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "fast_ema": fast_ema,
                    "slow_ema": slow_ema,
                    "adx": adx_val,
                    "atr": atr_val,
                    "volume": candle.volume,
                },
            )

        if bearish_cross:
            # CPR filter
            if self._cpr_filter and not self._cpr_filter.allows_signal(SignalType.SELL, entry):
                return None

            sl = entry + (self._sl_atr_mult * atr_val)
            tp = entry - (self._tp_atr_mult * atr_val)

            logger.info(
                "EMA SHORT crossover on %s: entry=%.2f SL=%.2f TP=%.2f | "
                "EMA9=%.2f EMA21=%.2f ADX=%.1f ATR=%.2f",
                token, entry, sl, tp, fast_ema, slow_ema, adx_val, atr_val,
            )

            return Signal(
                signal_type=SignalType.SELL,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"EMA {self._fast_period} crossed below EMA {self._slow_period} (ADX={adx_val:.1f})",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "fast_ema": fast_ema,
                    "slow_ema": slow_ema,
                    "adx": adx_val,
                    "atr": atr_val,
                    "volume": candle.volume,
                },
            )

        return None

    def _get_today_candles(self, history: list[Candle], current: Candle) -> list[Candle]:
        """Get today's candles for VWAP."""
        today_open_ms = self._get_today_market_open_ms()
        candles = [c for c in history if c.timestamp_ms >= today_open_ms]
        candles.append(current)
        return candles

    @staticmethod
    def _get_today_market_open_ms() -> float:
        now = datetime.now()
        market_open = datetime.combine(now.date(), dtime(9, 15))
        return market_open.timestamp() * 1000

    def _maybe_reset_daily(self) -> None:
        """Reset daily state."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._prev_fast_ema.clear()
            self._prev_slow_ema.clear()
            self._candles_since_signal.clear()
            self._last_reset_date = today
            logger.info("EMA_Crossover daily state reset for %s", today)
