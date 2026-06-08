"""
Bollinger Band Squeeze Breakout Strategy.

Detects volatility compression (squeeze) and trades the breakout:
1. Detect squeeze: BB inside Keltner Channels for 5+ consecutive candles.
2. Wait for squeeze to release (bands expand).
3. Enter in breakout direction with volume confirmation.
4. SL at middle band (SMA 20), TP at 1.5× squeeze range.

Catches mid-day consolidation breakouts that ORB and trend strategies miss.
Based on TTM Squeeze methodology (John Carter) adapted for 5-min intraday.
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from app.core.models import Candle, Signal, SignalType, Timeframe
from app.strategy.base import BaseStrategy
from app.strategy.cpr_filter import CPRFilter
from app.strategy.indicators import bollinger_bands, is_squeeze, sma, vwap
from app.utils.logger import get_logger

logger = get_logger(__name__)

NO_TRADE_BEFORE = dtime(9, 30)
NO_TRADE_AFTER = dtime(15, 15)


class BBSqueezeStrategy(BaseStrategy):
    """
    Bollinger Band Squeeze Breakout strategy.

    Parameters:
        instrument_tokens: Only trade these instruments.
        bb_period: Bollinger Band period (default 20).
        bb_std: Bollinger Band standard deviations (default 2.0).
        min_squeeze_candles: Minimum consecutive squeeze candles (default 5).
        rr_ratio: Reward-to-risk ratio (default 1.5).
        volume_multiplier: Breakout volume must be > this × squeeze avg (default 1.5).
        use_vwap_filter: Require VWAP alignment (default True).
        max_trades_per_day: Max trades per instrument per day (default 2).
        cpr_filter: Optional CPR filter for directional bias.
    """

    def __init__(
        self,
        instrument_tokens: list[str] | None = None,
        bb_period: int = 20,
        bb_std: float = 2.0,
        min_squeeze_candles: int = 5,
        rr_ratio: float = 1.5,
        volume_multiplier: float = 1.2,
        use_vwap_filter: bool = True,
        max_trades_per_day: int = 2,
        cpr_filter: CPRFilter | None = None,
    ) -> None:
        self._instrument_tokens = instrument_tokens or []
        self._bb_period = bb_period
        self._bb_std = bb_std
        self._min_squeeze_candles = min_squeeze_candles
        self._rr_ratio = rr_ratio
        self._volume_mult = volume_multiplier
        self._use_vwap_filter = use_vwap_filter
        self._max_trades_per_day = max_trades_per_day
        self._cpr_filter = cpr_filter

        # State per instrument
        self._squeeze_count: dict[str, int] = {}  # consecutive squeeze candles
        self._was_in_squeeze: dict[str, bool] = {}  # was squeezing last candle
        self._squeeze_volumes: dict[str, list[int]] = {}  # volumes during squeeze
        self._trades_today: dict[str, int] = {}
        self._last_reset_date: str = ""

    @property
    def name(self) -> str:
        return "BB_Squeeze"

    @property
    def warmup_config(self) -> dict[str, int]:
        # Need BB(20) + some buffer for squeeze detection
        return {"5m": 50}

    def warmup_history(self, exchange_token: str, candles: list[Candle]) -> None:
        """
        Replay historical candles through the squeeze state machine.

        Called after warmup injection so the strategy knows if a squeeze
        was already in progress when the live session starts. Without this,
        squeezes that began before market open are invisible to the strategy.
        """
        token = exchange_token
        for candle in candles:
            if candle.timeframe != Timeframe.M5:
                continue
            # Build a rolling window of the last 25 candles seen so far
            if not hasattr(self, '_warmup_buffer'):
                self._warmup_buffer: dict[str, list[Candle]] = {}
            if token not in self._warmup_buffer:
                self._warmup_buffer[token] = []
            self._warmup_buffer[token].append(candle)
            if len(self._warmup_buffer[token]) > 25:
                self._warmup_buffer[token].pop(0)

            window = self._warmup_buffer[token]
            if len(window) < 25:
                continue

            squeeze_active = is_squeeze(window)
            if squeeze_active is None:
                continue

            if squeeze_active:
                self._squeeze_count[token] = self._squeeze_count.get(token, 0) + 1
                self._was_in_squeeze[token] = True
                if token not in self._squeeze_volumes:
                    self._squeeze_volumes[token] = []
                self._squeeze_volumes[token].append(candle.volume)
            else:
                if self._was_in_squeeze.get(token, False):
                    # Squeeze ended during warmup — reset, don't fire signal
                    self._squeeze_count[token] = 0
                    self._squeeze_volumes[token] = []
                self._was_in_squeeze[token] = False

        logger.info(
            "BB_Squeeze warmup replay for %s: squeeze_active=%s, squeeze_count=%d",
            token,
            self._was_in_squeeze.get(token, False),
            self._squeeze_count.get(token, 0),
        )

    def on_candle(self, candle: Candle, history: list[Candle]) -> Signal | None:
        """Evaluate BB Squeeze on each 5-min candle."""
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

        # Check daily trade limit
        if self._trades_today.get(token, 0) >= self._max_trades_per_day:
            return None

        # Need enough history
        if len(history) < 25:
            return None

        all_candles = history[-25:] + [candle]

        # Detect squeeze state
        squeeze_active = is_squeeze(all_candles)
        if squeeze_active is None:
            return None

        prev_in_squeeze = self._was_in_squeeze.get(token, False)

        if squeeze_active:
            # Currently in squeeze — count and track
            self._squeeze_count[token] = self._squeeze_count.get(token, 0) + 1
            self._was_in_squeeze[token] = True

            # Track volumes during squeeze
            if token not in self._squeeze_volumes:
                self._squeeze_volumes[token] = []
            self._squeeze_volumes[token].append(candle.volume)

            return None  # No signal during squeeze

        # Squeeze just released (was in squeeze, now not)
        if prev_in_squeeze and not squeeze_active:
            self._was_in_squeeze[token] = False
            squeeze_duration = self._squeeze_count.get(token, 0)
            squeeze_vols = self._squeeze_volumes.get(token, [])

            # Reset squeeze tracking
            self._squeeze_count[token] = 0
            self._squeeze_volumes[token] = []

            # Check minimum squeeze duration
            if squeeze_duration < self._min_squeeze_candles:
                logger.debug(
                    "BB Squeeze on %s too short (%d < %d candles), skipping",
                    token, squeeze_duration, self._min_squeeze_candles,
                )
                return None

            # Squeeze released — check for breakout
            return self._check_breakout(candle, all_candles, squeeze_vols, squeeze_duration, token)

        # Not in squeeze and wasn't before — reset
        self._was_in_squeeze[token] = False
        self._squeeze_count[token] = 0
        self._squeeze_volumes[token] = []
        return None

    def _check_breakout(
        self,
        candle: Candle,
        all_candles: list[Candle],
        squeeze_volumes: list[int],
        squeeze_duration: int,
        token: str,
    ) -> Signal | None:
        """Check if the squeeze release is a valid breakout."""
        # Get current Bollinger Bands
        bb = bollinger_bands(all_candles, self._bb_period, self._bb_std)
        if bb is None:
            return None

        upper, middle, lower = bb
        squeeze_range = upper - lower

        # Volume confirmation
        avg_squeeze_vol = sum(squeeze_volumes) / len(squeeze_volumes) if squeeze_volumes else 0
        has_volume = candle.volume > avg_squeeze_vol * self._volume_mult if avg_squeeze_vol > 0 else True

        if not has_volume:
            logger.info("BB Squeeze breakout on %s rejected: low volume (%d < %.0f × %.1f avg)",
                        token, candle.volume, avg_squeeze_vol, self._volume_mult)
            return None

        # VWAP filter
        if self._use_vwap_filter:
            today_candles = self._get_today_candles(all_candles, candle)
            vwap_val = vwap(today_candles)
            if vwap_val is not None:
                if candle.close > upper and candle.close < vwap_val:
                    return None  # Bullish breakout but below VWAP
                if candle.close < lower and candle.close > vwap_val:
                    return None  # Bearish breakout but above VWAP

        # Determine breakout direction
        entry = candle.close

        # Bullish breakout: close above upper band
        if candle.close > upper:
            # CPR filter
            if self._cpr_filter and not self._cpr_filter.allows_signal(SignalType.BUY, entry):
                logger.debug("BB Squeeze LONG on %s blocked by CPR (bearish day)", token)
                return None

            sl = middle  # Middle band as SL
            min_sl = entry * 0.003  # 0.3% minimum
            if entry - sl < min_sl:
                sl = entry - min_sl

            tp = entry + (self._rr_ratio * squeeze_range)

            self._trades_today[token] = self._trades_today.get(token, 0) + 1

            logger.info(
                "BB_SQUEEZE LONG on %s: entry=%.2f SL=%.2f TP=%.2f | "
                "squeeze=%d candles, range=%.2f, vol=%d (avg=%d)",
                token, entry, sl, tp, squeeze_duration, squeeze_range,
                candle.volume, int(avg_squeeze_vol),
            )

            return Signal(
                signal_type=SignalType.BUY,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"BB Squeeze breakout UP after {squeeze_duration} candle compression",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "squeeze_duration": squeeze_duration,
                    "squeeze_range": squeeze_range,
                    "bb_upper": upper,
                    "bb_middle": middle,
                    "bb_lower": lower,
                    "breakout_volume": candle.volume,
                },
            )

        # Bearish breakout: close below lower band
        elif candle.close < lower:
            # CPR filter
            if self._cpr_filter and not self._cpr_filter.allows_signal(SignalType.SELL, entry):
                logger.debug("BB Squeeze SHORT on %s blocked by CPR (bullish day)", token)
                return None

            sl = middle  # Middle band as SL
            min_sl = entry * 0.003
            if sl - entry < min_sl:
                sl = entry + min_sl

            tp = entry - (self._rr_ratio * squeeze_range)

            self._trades_today[token] = self._trades_today.get(token, 0) + 1

            logger.info(
                "BB_SQUEEZE SHORT on %s: entry=%.2f SL=%.2f TP=%.2f | "
                "squeeze=%d candles, range=%.2f, vol=%d (avg=%d)",
                token, entry, sl, tp, squeeze_duration, squeeze_range,
                candle.volume, int(avg_squeeze_vol),
            )

            return Signal(
                signal_type=SignalType.SELL,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=token,
                price=entry,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason=f"BB Squeeze breakout DOWN after {squeeze_duration} candle compression",
                stop_loss=sl,
                take_profit=tp,
                metadata={
                    "squeeze_duration": squeeze_duration,
                    "squeeze_range": squeeze_range,
                    "bb_upper": upper,
                    "bb_middle": middle,
                    "bb_lower": lower,
                    "breakout_volume": candle.volume,
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
            self._squeeze_count.clear()
            self._was_in_squeeze.clear()
            self._squeeze_volumes.clear()
            self._trades_today.clear()
            self._last_reset_date = today
            logger.info("BB_Squeeze daily state reset for %s", today)
