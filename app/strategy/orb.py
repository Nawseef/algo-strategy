"""
Opening Range Breakout (ORB) Strategy.

Captures the first directional move of the day by:
1. Recording the high/low of the first 15 minutes (9:15–9:30 IST).
2. Trading the breakout above/below that range with volume confirmation.
3. Using the opposite end of the range as stop-loss.
4. Targeting 1.5× the range size as take-profit.

Best suited for Nifty/BankNifty — statistically the highest-probability
intraday setup in the Indian market.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from app.core.models import Candle, Signal, SignalType, Timeframe
from app.strategy.base import BaseStrategy
from app.strategy.cpr_filter import CPRFilter
from app.strategy.indicators import vwap
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ORB timing
ORB_START = dtime(9, 15)
ORB_END = dtime(9, 30)
# Allow range formation until 9:31 to capture the 9:25-9:30 candle
# (it's emitted when the first tick after 9:30 arrives)
ORB_FORMATION_END = dtime(9, 31)
TRADE_START = dtime(9, 30)
TRADE_END = dtime(15, 15)


class ORBStrategy(BaseStrategy):
    """
    Opening Range Breakout strategy.

    Parameters:
        instrument_tokens: Only trade these instruments.
        rr_ratio: Reward-to-risk ratio for take-profit (default 1.5).
        max_range_pct: Skip if range > this % of price (too volatile).
        min_range_pct: Skip if range < this % of price (no conviction).
        use_vwap_filter: Require VWAP alignment for entry.
    """

    def __init__(
        self,
        instrument_tokens: list[str] | None = None,
        rr_ratio: float = 1.5,
        max_range_pct: float = 1.5,
        min_range_pct: float = 0.1,
        use_vwap_filter: bool = True,
        cpr_filter: CPRFilter | None = None,
    ) -> None:
        self._instrument_tokens = instrument_tokens or []
        self._rr_ratio = rr_ratio
        self._max_range_pct = max_range_pct
        self._min_range_pct = min_range_pct
        self._use_vwap_filter = use_vwap_filter
        self._cpr_filter = cpr_filter

        # Per-instrument daily state (reset each day)
        self._range_high: dict[str, float] = {}
        self._range_low: dict[str, float] = {}
        self._range_ready: dict[str, bool] = {}
        self._range_volume: dict[str, list[int]] = {}  # volumes during range formation
        self._long_taken: dict[str, bool] = {}
        self._short_taken: dict[str, bool] = {}
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return "ORB_15min"

    @property
    def warmup_config(self) -> dict[str, int]:
        # ORB doesn't need historical warmup — builds range from live data
        return {}

    def on_candle(self, candle: Candle, history: list[Candle]) -> Signal | None:
        """Process completed candles for ORB logic."""
        # Only process 5-min candles for breakout detection
        if candle.timeframe != Timeframe.M5:
            return None

        if self._instrument_tokens and candle.exchange_token not in self._instrument_tokens:
            return None

        # Reset state at start of new day
        self._maybe_reset_daily()

        token = candle.exchange_token
        now = datetime.now().time()

        # Phase 1: Range formation (9:15–9:30, extended to 9:31 for last candle)
        if ORB_START <= now <= ORB_FORMATION_END:
            self._update_range(token, candle)
            return None

        # Mark range as ready after formation period
        if now > ORB_FORMATION_END and not self._range_ready.get(token, False):
            if token in self._range_high and token in self._range_low:
                self._range_ready[token] = True
                range_size = self._range_high[token] - self._range_low[token]
                mid_price = (self._range_high[token] + self._range_low[token]) / 2.0
                range_pct = (range_size / mid_price) * 100 if mid_price > 0 else 0

                logger.info(
                    "ORB range set for %s: High=%.2f Low=%.2f Size=%.2f (%.2f%%)",
                    token, self._range_high[token], self._range_low[token],
                    range_size, range_pct,
                )

                # Validate range size
                if range_pct > self._max_range_pct:
                    logger.info("ORB range too wide (%.2f%% > %.2f%%), skipping %s today",
                                range_pct, self._max_range_pct, token)
                    self._range_ready[token] = False
                    return None
                if range_pct < self._min_range_pct:
                    logger.info("ORB range too tight (%.2f%% < %.2f%%), skipping %s today",
                                range_pct, self._min_range_pct, token)
                    self._range_ready[token] = False
                    return None

        # Phase 2: Breakout detection (9:30–3:15)
        if not self._range_ready.get(token, False):
            return None
        if not (TRADE_START <= now <= TRADE_END):
            return None

        return self._check_breakout(candle, history)

    def _update_range(self, token: str, candle: Candle) -> None:
        """Update the opening range with new candle data."""
        if token not in self._range_high:
            self._range_high[token] = candle.high
            self._range_low[token] = candle.low
            self._range_volume[token] = [candle.volume]
        else:
            self._range_high[token] = max(self._range_high[token], candle.high)
            self._range_low[token] = min(self._range_low[token], candle.low)
            self._range_volume[token].append(candle.volume)

    def _check_breakout(self, candle: Candle, history: list[Candle]) -> Signal | None:
        """Check if current candle breaks the opening range."""
        token = candle.exchange_token
        range_high = self._range_high[token]
        range_low = self._range_low[token]
        range_size = range_high - range_low

        # Volume confirmation: breakout candle volume > average range volume
        avg_range_vol = 0
        if self._range_volume.get(token):
            avg_range_vol = sum(self._range_volume[token]) / len(self._range_volume[token])

        has_volume = candle.volume > avg_range_vol if avg_range_vol > 0 else True

        # VWAP filter
        vwap_ok_long = True
        vwap_ok_short = True
        if self._use_vwap_filter and history:
            # Get today's candles for VWAP calculation
            today_candles = self._get_today_candles(history, candle)
            vwap_val = vwap(today_candles)
            if vwap_val is not None:
                vwap_ok_long = candle.close > vwap_val
                vwap_ok_short = candle.close < vwap_val

        # Bullish breakout: candle CLOSES above range high
        if (
            not self._long_taken.get(token, False)
            and candle.close > range_high
            and has_volume
            and vwap_ok_long
        ):
            # CPR filter
            if self._cpr_filter and not self._cpr_filter.allows_signal(SignalType.BUY, candle.close):
                logger.debug("ORB LONG on %s blocked by CPR (bearish day)", token)
                return None

            self._long_taken[token] = True
            entry = candle.close
            sl = range_low
            tp = entry + (self._rr_ratio * range_size)

            logger.info(
                "ORB LONG breakout on %s: entry=%.2f SL=%.2f TP=%.2f range=[%.2f-%.2f]",
                token, entry, sl, tp, range_low, range_high,
            )

            return Signal(
                signal_type=SignalType.BUY,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"ORB bullish breakout above {range_high:.2f}",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "range_high": range_high,
                    "range_low": range_low,
                    "range_size": range_size,
                    "breakout_volume": candle.volume,
                    "avg_range_volume": avg_range_vol,
                },
            )

        # Bearish breakout: candle CLOSES below range low
        if (
            not self._short_taken.get(token, False)
            and candle.close < range_low
            and has_volume
            and vwap_ok_short
        ):
            # CPR filter
            if self._cpr_filter and not self._cpr_filter.allows_signal(SignalType.SELL, candle.close):
                logger.debug("ORB SHORT on %s blocked by CPR (bullish day)", token)
                return None

            self._short_taken[token] = True
            entry = candle.close
            sl = range_high
            tp = entry - (self._rr_ratio * range_size)

            logger.info(
                "ORB SHORT breakout on %s: entry=%.2f SL=%.2f TP=%.2f range=[%.2f-%.2f]",
                token, entry, sl, tp, range_low, range_high,
            )

            return Signal(
                signal_type=SignalType.SELL,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"ORB bearish breakout below {range_low:.2f}",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "range_high": range_high,
                    "range_low": range_low,
                    "range_size": range_size,
                    "breakout_volume": candle.volume,
                    "avg_range_volume": avg_range_vol,
                },
            )

        return None

    def _get_today_candles(self, history: list[Candle], current: Candle) -> list[Candle]:
        """Extract today's candles from history for VWAP calculation."""
        # Market opens at 9:15, so today's candles have timestamps from today
        today_start_ms = self._get_today_market_open_ms()
        candles = [c for c in history if c.timestamp_ms >= today_start_ms]
        candles.append(current)
        return candles

    @staticmethod
    def _get_today_market_open_ms() -> float:
        """Get today's 9:15 AM as milliseconds timestamp."""
        now = datetime.now()
        market_open = datetime.combine(now.date(), ORB_START)
        return market_open.timestamp() * 1000

    def _maybe_reset_daily(self) -> None:
        """Reset all daily state at the start of a new trading day."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._range_high.clear()
            self._range_low.clear()
            self._range_ready.clear()
            self._range_volume.clear()
            self._long_taken.clear()
            self._short_taken.clear()
            self._last_reset_date = today
            logger.info("ORB daily state reset for %s", today)
