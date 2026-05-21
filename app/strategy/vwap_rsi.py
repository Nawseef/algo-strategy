"""
VWAP + RSI Pullback Strategy.

Trades pullbacks to VWAP in a trending market, confirmed by RSI:
1. Determine trend direction using VWAP (price above = bullish, below = bearish).
2. Wait for RSI to show pullback exhaustion (oversold in uptrend, overbought in downtrend).
3. Enter when RSI crosses back from extreme, confirming momentum resumption.
4. SL at VWAP level, TP at 2:1 reward-to-risk.

Based on research showing VWAP-based exits with momentum entries achieve
Sharpe ratios over 3.0 and annualized returns over 50%.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from app.core.models import Candle, Signal, SignalType, Timeframe
from app.strategy.base import BaseStrategy
from app.strategy.indicators import adx, rsi, vwap
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Don't trade in the first 15 minutes (let ORB handle that)
NO_TRADE_BEFORE = dtime(9, 30)
NO_TRADE_AFTER = dtime(15, 15)


class VWAPRSIStrategy(BaseStrategy):
    """
    VWAP + RSI Pullback strategy.

    Parameters:
        instrument_tokens: Only trade these instruments.
        rsi_period: RSI lookback period (default 14).
        rsi_oversold: RSI level for oversold in uptrend (default 40 — regime-adjusted).
        rsi_overbought: RSI level for overbought in downtrend (default 60 — regime-adjusted).
        adx_threshold: Minimum ADX for trend confirmation (default 20).
        min_sl_pct: Minimum stop-loss distance as % of price (default 0.3).
        rr_ratio: Reward-to-risk ratio (default 2.0).
        max_trades_per_day: Max trades per instrument per day (default 2).

    Note on RSI levels:
        We use regime-adjusted levels (Cardwell method) instead of standard 30/70.
        In uptrends (price > VWAP), RSI tends to stay in 40-80 range.
        In downtrends (price < VWAP), RSI tends to stay in 20-60 range.
        So we use 40 as oversold in uptrends and 60 as overbought in downtrends.
    """

    def __init__(
        self,
        instrument_tokens: list[str] | None = None,
        rsi_period: int = 14,
        rsi_oversold: float = 40.0,
        rsi_overbought: float = 60.0,
        adx_threshold: float = 20.0,
        min_sl_pct: float = 0.3,
        rr_ratio: float = 2.0,
        max_trades_per_day: int = 2,
    ) -> None:
        self._instrument_tokens = instrument_tokens or []
        self._rsi_period = rsi_period
        self._rsi_oversold = rsi_oversold
        self._rsi_overbought = rsi_overbought
        self._adx_threshold = adx_threshold
        self._min_sl_pct = min_sl_pct
        self._rr_ratio = rr_ratio
        self._max_trades_per_day = max_trades_per_day

        # State tracking
        self._prev_rsi: dict[str, float] = {}  # token -> previous RSI value
        self._trades_today: dict[str, int] = {}  # token -> count
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return "VWAP_RSI_Pullback"

    @property
    def warmup_config(self) -> dict[str, int]:
        # Need enough 5m candles for RSI(14) + ADX(14) to stabilize
        # RSI needs 15 candles, ADX needs 29 candles
        return {"5m": 35}

    def on_candle(self, candle: Candle, history: list[Candle]) -> Signal | None:
        """Evaluate VWAP + RSI pullback on each 5-min candle."""
        if candle.timeframe != Timeframe.M5:
            return None

        if self._instrument_tokens and candle.exchange_token not in self._instrument_tokens:
            return None

        # Reset daily state
        self._maybe_reset_daily()

        # Time filter
        now = datetime.now().time()
        if not (NO_TRADE_BEFORE <= now <= NO_TRADE_AFTER):
            return None

        token = candle.exchange_token

        # Check daily trade limit
        if self._trades_today.get(token, 0) >= self._max_trades_per_day:
            return None

        # Need enough history
        if len(history) < 30:
            return None

        # Calculate indicators
        # VWAP from today's candles only
        today_candles = self._get_today_candles(history, candle)
        vwap_val = vwap(today_candles)
        if vwap_val is None:
            return None

        # RSI from recent closes
        closes = [c.close for c in history[-self._rsi_period - 5:]] + [candle.close]
        rsi_val = rsi(closes, self._rsi_period)
        if rsi_val is None:
            return None

        # ADX for trend strength
        adx_candles = history[-30:] + [candle]
        adx_val = adx(adx_candles, 14)
        if adx_val is None or adx_val < self._adx_threshold:
            # Store RSI for next iteration
            self._prev_rsi[token] = rsi_val
            return None

        # Get previous RSI for crossover detection
        prev_rsi_val = self._prev_rsi.get(token)
        self._prev_rsi[token] = rsi_val

        if prev_rsi_val is None:
            return None

        # Determine trend from VWAP
        price_above_vwap = candle.close > vwap_val
        price_below_vwap = candle.close < vwap_val

        signal = None

        # LONG: Price above VWAP (uptrend) + RSI was oversold and crosses back up
        if (
            price_above_vwap
            and prev_rsi_val < self._rsi_oversold
            and rsi_val >= self._rsi_oversold
        ):
            entry = candle.close
            # SL at VWAP (if price falls below VWAP, thesis is broken)
            sl = vwap_val
            # Enforce minimum SL distance
            min_sl_distance = entry * (self._min_sl_pct / 100)
            if entry - sl < min_sl_distance:
                sl = entry - min_sl_distance

            risk = entry - sl
            tp = entry + (self._rr_ratio * risk)

            logger.info(
                "VWAP_RSI LONG on %s: entry=%.2f SL=%.2f TP=%.2f | RSI=%.1f VWAP=%.2f ADX=%.1f",
                token, entry, sl, tp, rsi_val, vwap_val, adx_val,
            )

            signal = Signal(
                signal_type=SignalType.BUY,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"RSI pullback recovery ({prev_rsi_val:.1f}→{rsi_val:.1f}) above VWAP",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "rsi": rsi_val,
                    "prev_rsi": prev_rsi_val,
                    "vwap": vwap_val,
                    "adx": adx_val,
                },
            )

        # SHORT: Price below VWAP (downtrend) + RSI was overbought and crosses back down
        elif (
            price_below_vwap
            and prev_rsi_val > self._rsi_overbought
            and rsi_val <= self._rsi_overbought
        ):
            entry = candle.close
            # SL at VWAP
            sl = vwap_val
            # Enforce minimum SL distance
            min_sl_distance = entry * (self._min_sl_pct / 100)
            if sl - entry < min_sl_distance:
                sl = entry + min_sl_distance

            risk = sl - entry
            tp = entry - (self._rr_ratio * risk)

            logger.info(
                "VWAP_RSI SHORT on %s: entry=%.2f SL=%.2f TP=%.2f | RSI=%.1f VWAP=%.2f ADX=%.1f",
                token, entry, sl, tp, rsi_val, vwap_val, adx_val,
            )

            signal = Signal(
                signal_type=SignalType.SELL,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"RSI pullback recovery ({prev_rsi_val:.1f}→{rsi_val:.1f}) below VWAP",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "rsi": rsi_val,
                    "prev_rsi": prev_rsi_val,
                    "vwap": vwap_val,
                    "adx": adx_val,
                },
            )

        if signal:
            self._trades_today[token] = self._trades_today.get(token, 0) + 1

        return signal

    def _get_today_candles(self, history: list[Candle], current: Candle) -> list[Candle]:
        """Get only today's candles for VWAP calculation."""
        today_open_ms = self._get_today_market_open_ms()
        candles = [c for c in history if c.timestamp_ms >= today_open_ms]
        candles.append(current)
        return candles

    @staticmethod
    def _get_today_market_open_ms() -> float:
        """Get today's 9:15 AM as milliseconds timestamp."""
        now = datetime.now()
        market_open = datetime.combine(now.date(), dtime(9, 15))
        return market_open.timestamp() * 1000

    def _maybe_reset_daily(self) -> None:
        """Reset daily counters."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._trades_today.clear()
            self._prev_rsi.clear()
            self._last_reset_date = today
            logger.info("VWAP_RSI daily state reset for %s", today)
