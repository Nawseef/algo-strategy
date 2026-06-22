"""
Indicator Engine — computes all indicators ONCE per instrument per candle close.

This is the heart of the "shared computation" principle:
- ATR, ADX, RSI, VWAP, Volume Ratio, EMAs, Bollinger Bands
- Computed once per instrument per timeframe per candle close
- Stored as IndicatorSnapshot objects
- All 150K variants read from these snapshots

Also manages:
- India VIX value (global, updated from live feed)
- Metadata snapshots (session, gap, etc.)

Usage:
    engine = IndicatorEngine(candle_builder)
    engine.on_candle(candle)  # computes snapshot
    snapshot = engine.get_snapshot("2885", ResearchTimeframe.M5)
"""

from __future__ import annotations

from datetime import datetime, time as dtime

from app.core.candle_builder import CandleBuilder
from app.core.models import Candle, Timeframe
from app.strategy.indicators import (
    adx,
    atr,
    bollinger_bands,
    ema,
    is_squeeze,
    rsi,
    vwap,
)
from app.utils.logger import get_logger
from app.variants.models import IndicatorSnapshot, MetadataSnapshot, ResearchTimeframe

logger = get_logger(__name__)

# Map ResearchTimeframe to core Timeframe enum
TIMEFRAME_MAP: dict[ResearchTimeframe, Timeframe] = {
    ResearchTimeframe.M5: Timeframe.M5,
    ResearchTimeframe.M15: Timeframe.M15,
    ResearchTimeframe.M30: Timeframe.M30,
}

# Market session boundaries
MORNING_START = dtime(9, 15)
MORNING_END = dtime(11, 30)
MIDDAY_START = dtime(11, 30)
MIDDAY_END = dtime(14, 0)
CLOSING_START = dtime(14, 0)
CLOSING_END = dtime(15, 30)


class IndicatorEngine:
    """
    Shared indicator computation engine.

    Subscribes to candle events and computes IndicatorSnapshot
    for each instrument/timeframe combination. Also tracks global
    India VIX from live feed.
    """

    def __init__(self, candle_builder: CandleBuilder) -> None:
        self._candle_builder = candle_builder

        # Current snapshots: (exchange_token, ResearchTimeframe) → IndicatorSnapshot
        self._snapshots: dict[tuple[str, ResearchTimeframe], IndicatorSnapshot] = {}

        # Metadata: exchange_token → MetadataSnapshot (updated once per day / per session)
        self._metadata: dict[str, MetadataSnapshot] = {}

        # Global India VIX value (updated from live feed tick)
        self._vix_value: float = 0.0

        # Previous close prices for gap calculation (token → prev day close)
        self._prev_day_close: dict[str, float] = {}

        # Opening range tracking for metadata
        self._opening_range: dict[str, tuple[float, float]] = {}  # token → (high, low)

        # Volume history for ratio calculation: token → list of recent volumes
        self._volume_history: dict[tuple[str, ResearchTimeframe], list[int]] = {}
        self._volume_history_size = 20  # lookback for average volume

        # EMA history for slope calculation
        self._prev_ema20: dict[tuple[str, ResearchTimeframe], float] = {}
        self._prev_ema50: dict[tuple[str, ResearchTimeframe], float] = {}

        self._last_metadata_date: str = ""

        # Track latest candle timestamp PER INSTRUMENT (not shared scalar)
        # Shared scalar caused cross-instrument session contamination: processing
        # instrument B's 9:30 candle would overwrite the timestamp used when
        # get_metadata(A) is called, making A's session reflect B's time.
        self._last_candle_timestamp_ms: dict[str, float] = {}

    # ─── Public API ──────────────────────────────────────────────────────────

    def on_candle(self, candle: Candle) -> IndicatorSnapshot | None:
        """
        Compute indicator snapshot when a candle closes.

        Called by the event bus on 'candle' events.
        Returns the computed snapshot (or None if insufficient history).
        """
        # Determine research timeframe
        rtf = self._to_research_timeframe(candle.timeframe)
        if rtf is None:
            return None  # Not a timeframe we care about (e.g. 1m)

        token = candle.exchange_token
        key = (token, rtf)

        # Get history from candle builder
        core_tf = TIMEFRAME_MAP[rtf]
        history = self._candle_builder.get_history(token, core_tf)

        if len(history) < 30:
            return None  # Not enough history for reliable indicators

        # Use history directly if the current candle is already at the end
        # (CandleBuilder appends to history BEFORE emitting the event)
        # Otherwise append it (backtest mode may inject after)
        if history and history[-1].timestamp_ms == candle.timestamp_ms:
            all_candles = history[-50:]
        else:
            all_candles = history[-50:] + [candle]
        closes = [c.close for c in all_candles]

        # ─── Compute all indicators ONCE ─────────────────────────────────
        snapshot = IndicatorSnapshot()

        # ATR (14 period)
        atr_val = atr(all_candles, 14)
        snapshot.atr = atr_val if atr_val is not None else 0.0

        # ADX (14 period)
        adx_val = adx(all_candles, 14)
        snapshot.adx = adx_val if adx_val is not None else 0.0

        # RSI (14 period)
        rsi_val = rsi(closes, 14)
        snapshot.rsi = rsi_val if rsi_val is not None else 50.0

        # VWAP (today's candles only)
        today_candles = self._get_today_candles(all_candles)
        vwap_val = vwap(today_candles) if today_candles else None
        snapshot.vwap = vwap_val if vwap_val is not None else candle.close

        # Volume ratio (current volume / average of last N candles)
        snapshot.volume_ratio = self._compute_volume_ratio(key, candle.volume)

        # VIX (global value from feed)
        snapshot.vix = self._vix_value

        # EMAs
        ema9_val = ema(closes, 9)
        ema21_val = ema(closes, 21)
        ema20_val = ema(closes, 20)
        ema50_val = ema(closes, 50) if len(closes) >= 50 else None

        snapshot.ema_9 = ema9_val if ema9_val is not None else 0.0
        snapshot.ema_21 = ema21_val if ema21_val is not None else 0.0
        snapshot.ema_20 = ema20_val if ema20_val is not None else 0.0
        snapshot.ema_50 = ema50_val if ema50_val is not None else 0.0

        # EMA slopes (change from previous candle's EMA)
        prev_ema20 = self._prev_ema20.get(key, 0.0)
        prev_ema50 = self._prev_ema50.get(key, 0.0)

        if prev_ema20 > 0 and snapshot.ema_20 > 0:
            snapshot.ema_20_slope = snapshot.ema_20 - prev_ema20
        if prev_ema50 > 0 and snapshot.ema_50 > 0:
            snapshot.ema_50_slope = snapshot.ema_50 - prev_ema50

        self._prev_ema20[key] = snapshot.ema_20
        self._prev_ema50[key] = snapshot.ema_50

        # Bollinger Bands (20, 2.0)
        bb = bollinger_bands(all_candles, 20, 2.0)
        if bb is not None:
            snapshot.bb_upper, snapshot.bb_middle, snapshot.bb_lower = bb

        # BB Squeeze detection
        squeeze = is_squeeze(all_candles)
        snapshot.bb_squeeze = squeeze if squeeze is not None else False

        # Price vs VWAP (normalized by ATR)
        if snapshot.vwap > 0 and snapshot.atr > 0:
            snapshot.price_vs_vwap = (candle.close - snapshot.vwap) / snapshot.atr
        elif snapshot.vwap > 0:
            # If ATR is 0, just use sign
            snapshot.price_vs_vwap = 1.0 if candle.close > snapshot.vwap else -1.0

        # Store snapshot
        self._snapshots[key] = snapshot

        # Track latest candle timestamp per instrument for metadata derivation
        self._last_candle_timestamp_ms[token] = candle.timestamp_ms

        return snapshot

    def get_snapshot(
        self, exchange_token: str, timeframe: ResearchTimeframe
    ) -> IndicatorSnapshot | None:
        """Get the latest computed snapshot for an instrument/timeframe."""
        return self._snapshots.get((exchange_token, timeframe))

    def get_metadata(self, exchange_token: str) -> MetadataSnapshot:
        """Get the metadata snapshot for an instrument."""
        self._maybe_update_metadata(exchange_token)
        return self._metadata.get(exchange_token, MetadataSnapshot())

    def update_vix(self, vix_value: float) -> None:
        """Update the global India VIX value from live feed."""
        self._vix_value = vix_value

    def set_prev_day_close(self, exchange_token: str, close_price: float) -> None:
        """Set previous day's close for gap calculation."""
        self._prev_day_close[exchange_token] = close_price

    def update_opening_range(self, exchange_token: str, candle: Candle) -> None:
        """Update opening range during 9:15-9:30 for metadata."""
        if exchange_token not in self._opening_range:
            self._opening_range[exchange_token] = (candle.high, candle.low)
        else:
            h, l = self._opening_range[exchange_token]
            self._opening_range[exchange_token] = (
                max(h, candle.high),
                min(l, candle.low),
            )

    # ─── Internal ────────────────────────────────────────────────────────────

    def _compute_volume_ratio(
        self, key: tuple[str, ResearchTimeframe], current_volume: int
    ) -> float:
        """Compute volume ratio = current / average of last N candles."""
        if key not in self._volume_history:
            self._volume_history[key] = []

        hist = self._volume_history[key]
        hist.append(current_volume)

        # Keep only last N
        if len(hist) > self._volume_history_size:
            hist.pop(0)

        if len(hist) < 2:
            return 1.0  # Not enough history

        # Average excludes current candle
        avg = sum(hist[:-1]) / len(hist[:-1])
        if avg == 0:
            return 1.0

        return current_volume / avg

    def _get_today_candles(self, candles: list[Candle]) -> list[Candle]:
        """Filter to today's candles only (for VWAP calculation).
        Uses the LAST candle's date to determine 'today' (works for both live and backtest).
        """
        if not candles:
            return []
        # Determine 'today' from the most recent candle in the list
        last_candle_dt = datetime.fromtimestamp(candles[-1].timestamp_ms / 1000)
        market_open = datetime.combine(last_candle_dt.date(), dtime(9, 15))
        open_ms = market_open.timestamp() * 1000
        return [c for c in candles if c.timestamp_ms >= open_ms]

    def _maybe_update_metadata(self, exchange_token: str) -> None:
        """Update metadata snapshot (session, gap, etc.)."""
        # Derive date/time from last processed candle for THIS instrument (backtest-safe)
        # Using per-instrument timestamp prevents cross-instrument contamination where
        # processing instrument B's later candle corrupts instrument A's session tag.
        ts = self._last_candle_timestamp_ms.get(exchange_token, 0.0)
        if ts > 0:
            candle_dt = datetime.fromtimestamp(ts / 1000)
            today = candle_dt.strftime("%Y-%m-%d")
            now = candle_dt.time()
            day_of_week = candle_dt.strftime("%a").upper()[:3]
            month = candle_dt.strftime("%b").upper()[:3]
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            now = datetime.now().time()
            day_of_week = datetime.now().strftime("%a").upper()[:3]
            month = datetime.now().strftime("%b").upper()[:3]

        # Determine session
        if MORNING_START <= now <= MORNING_END:
            session = "MORNING"
        elif MIDDAY_START <= now <= MIDDAY_END:
            session = "MIDDAY"
        elif CLOSING_START <= now <= CLOSING_END:
            session = "CLOSING"
        else:
            session = "PRE_MARKET"

        # Build or update metadata
        if exchange_token not in self._metadata:
            self._metadata[exchange_token] = MetadataSnapshot()

        meta = self._metadata[exchange_token]
        meta.session = session
        meta.day_of_week = day_of_week
        meta.month = month

        # Gap (if we have prev day close and opening range)
        prev_close = self._prev_day_close.get(exchange_token, 0)
        if prev_close > 0 and exchange_token in self._opening_range:
            open_high, open_low = self._opening_range[exchange_token]
            open_mid = (open_high + open_low) / 2.0
            gap_pct = ((open_mid - prev_close) / prev_close) * 100
            meta.gap_size = abs(gap_pct)
            if gap_pct > 0.1:
                meta.gap_direction = "UP"
            elif gap_pct < -0.1:
                meta.gap_direction = "DOWN"
            else:
                meta.gap_direction = "FLAT"

        # Opening range size
        if exchange_token in self._opening_range:
            h, l = self._opening_range[exchange_token]
            meta.opening_range_size = h - l

        # Volatility regime (based on current VIX)
        if self._vix_value > 0:
            if self._vix_value < 13:
                meta.volatility_regime = "LOW"
            elif self._vix_value < 18:
                meta.volatility_regime = "NORMAL"
            else:
                meta.volatility_regime = "HIGH"

        # Higher timeframe trend (from EMA slopes — matches backtest SQL derivation)
        # Uses slope direction only (not position) for consistency between
        # backtest post-fill and live computation.
        snapshot_30m = self._snapshots.get(
            (exchange_token, ResearchTimeframe.M30)
        )
        if snapshot_30m and snapshot_30m.ema_20_slope != 0:
            if snapshot_30m.ema_20_slope > 0 and snapshot_30m.ema_50_slope > 0:
                meta.htf_trend_1h = "BULLISH"
                meta.higher_timeframe_bias = "BULLISH"
            elif snapshot_30m.ema_20_slope < 0 and snapshot_30m.ema_50_slope < 0:
                meta.htf_trend_1h = "BEARISH"
                meta.higher_timeframe_bias = "BEARISH"
            else:
                meta.htf_trend_1h = "NEUTRAL"
                meta.higher_timeframe_bias = "NEUTRAL"

        # Market structure (simplified: trending if ADX>25, ranging otherwise)
        snapshot_5m = self._snapshots.get(
            (exchange_token, ResearchTimeframe.M5)
        )
        if snapshot_5m:
            if snapshot_5m.adx > 25:
                meta.market_structure = "TRENDING"
            elif snapshot_5m.adx < 15:
                meta.market_structure = "RANGING"
            else:
                meta.market_structure = "TRANSITIONING"

    @staticmethod
    def _to_research_timeframe(tf: Timeframe) -> ResearchTimeframe | None:
        """Convert core Timeframe to ResearchTimeframe (returns None if not relevant)."""
        mapping = {
            Timeframe.M5: ResearchTimeframe.M5,
            Timeframe.M15: ResearchTimeframe.M15,
            Timeframe.M30: ResearchTimeframe.M30,
        }
        return mapping.get(tf)

    def reset_daily(self) -> None:
        """Reset daily state (call at start of new trading day)."""
        self._opening_range.clear()
        self._prev_ema20.clear()
        self._prev_ema50.clear()
        self._volume_history.clear()
        self._snapshots.clear()
        self._metadata.clear()
        logger.info("IndicatorEngine daily reset")
