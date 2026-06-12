"""
Domain models for the 150K variant research engine.

These are the core data structures that define variants, filters,
trade records, and execution state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ─── Enums ───────────────────────────────────────────────────────────────────


class StrategyType(Enum):
    """The 5 core strategies in the research engine."""

    ORB = "ORB"
    BOLLINGER_BANDS = "BB"
    VPA = "VPA"
    TREND_FOLLOWING = "TREND"
    MEAN_REVERSION = "MR"


class ResearchTimeframe(Enum):
    """Timeframes for variant evaluation."""

    M5 = "5m"
    M15 = "15m"
    M30 = "30m"


class EntryMode(Enum):
    """How a variant enters a trade."""

    CANDLE_CLOSE = "CANDLE_CLOSE"  # Triggered at candle close, no tick watching
    INTRABAR = "INTRABAR"  # Armed at candle close, triggered by tick event


class TriggerType(Enum):
    """What kind of event triggers an armed variant."""

    PRICE_LEVEL = "PRICE_LEVEL"  # Price breaks a specific level
    INDICATOR_EVENT = "INDICATOR_EVENT"  # Indicator crosses threshold
    PATTERN = "PATTERN"  # Candle pattern detected
    STRUCTURE = "STRUCTURE"  # Market structure condition (pullback zone, etc.)


class Direction(Enum):
    """Trade direction."""

    LONG = "LONG"
    SHORT = "SHORT"


class ArmedStatus(Enum):
    """Lifecycle state of a variant."""

    IDLE = "IDLE"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    DISARMED = "DISARMED"


# ─── Filter Values ───────────────────────────────────────────────────────────


class ATRFilter(Enum):
    """ATR filter thresholds."""

    NONE = None
    GT_10 = 10.0
    GT_15 = 15.0
    GT_20 = 20.0
    GT_25 = 25.0


class ADXFilter(Enum):
    """ADX filter thresholds."""

    NONE = None
    GT_15 = 15.0
    GT_20 = 20.0
    GT_25 = 25.0
    GT_30 = 30.0


class VIXFilter(Enum):
    """VIX (India VIX) filter thresholds."""

    NONE = None
    GT_12 = 12.0
    GT_15 = 15.0
    GT_18 = 18.0


class VolumeFilter(Enum):
    """Volume relative to average filter."""

    NONE = None
    GT_1_2X = 1.2
    GT_1_5X = 1.5
    GT_2X = 2.0


class RSIFilter(Enum):
    """RSI filter conditions."""

    NONE = "NONE"
    LT_30 = "LT_30"  # RSI < 30 (oversold — for mean reversion long)
    LT_35 = "LT_35"  # RSI < 35
    GT_65 = "GT_65"  # RSI > 65
    GT_70 = "GT_70"  # RSI > 70 (overbought — for mean reversion short)


class VWAPFilter(Enum):
    """VWAP position filter."""

    NONE = "NONE"
    ABOVE_VWAP = "ABOVE_VWAP"
    BELOW_VWAP = "BELOW_VWAP"
    DISTANCE_GT_0_5_ATR = "DIST_GT_0.5_ATR"
    DISTANCE_GT_1_ATR = "DIST_GT_1_ATR"


# ─── Filter Set ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FilterSet:
    """
    A specific combination of filter values.
    Frozen (immutable + hashable) so it can be used as dict key.
    """

    atr: ATRFilter = ATRFilter.NONE
    adx: ADXFilter = ADXFilter.NONE
    vix: VIXFilter = VIXFilter.NONE
    volume: VolumeFilter = VolumeFilter.NONE
    rsi: RSIFilter = RSIFilter.NONE
    vwap: VWAPFilter = VWAPFilter.NONE

    def to_dict(self) -> dict[str, str]:
        """Serialize to dict for storage."""
        return {
            "atr": self.atr.name,
            "adx": self.adx.name,
            "vix": self.vix.name,
            "volume": self.volume.name,
            "rsi": self.rsi.name,
            "vwap": self.vwap.name,
        }


# ─── Variant Definition ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Variant:
    """
    A single entry variant = Strategy + Timeframe + FilterSet + EntryMode.

    Frozen so it's hashable and can be used in sets/dicts.
    The variant_id is a deterministic hash of its components.
    """

    strategy: StrategyType
    timeframe: ResearchTimeframe
    filters: FilterSet
    entry_mode: EntryMode

    @property
    def variant_id(self) -> str:
        """
        Deterministic ID from variant parameters.
        Same params always produce same ID across runs.

        Only includes NON-NONE filters in the hash so that adding
        new filter dimensions doesn't change existing variant IDs.
        A variant with supertrend=NONE is the same as one without
        the supertrend field entirely.
        """
        parts = [self.strategy.value, self.timeframe.value, self.entry_mode.value]

        # Only include active filters (non-NONE) in deterministic order
        if self.filters.atr.name != "NONE":
            parts.append(f"ATR:{self.filters.atr.name}")
        if self.filters.adx.name != "NONE":
            parts.append(f"ADX:{self.filters.adx.name}")
        if self.filters.vix.name != "NONE":
            parts.append(f"VIX:{self.filters.vix.name}")
        if self.filters.volume.name != "NONE":
            parts.append(f"VOL:{self.filters.volume.name}")
        if self.filters.rsi.name != "NONE":
            parts.append(f"RSI:{self.filters.rsi.name}")
        if self.filters.vwap.name != "NONE":
            parts.append(f"VWAP:{self.filters.vwap.name}")

        raw = "|".join(parts)
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def short_name(self) -> str:
        """Human-readable short description."""
        parts = [self.strategy.value, self.timeframe.value]
        if self.filters.atr != ATRFilter.NONE:
            parts.append(f"ATR>{self.filters.atr.value}")
        if self.filters.adx != ADXFilter.NONE:
            parts.append(f"ADX>{self.filters.adx.value}")
        if self.filters.vix != VIXFilter.NONE:
            parts.append(f"VIX>{self.filters.vix.value}")
        if self.filters.volume != VolumeFilter.NONE:
            parts.append(f"Vol>{self.filters.volume.value}x")
        if self.filters.rsi != RSIFilter.NONE:
            parts.append(f"RSI:{self.filters.rsi.value}")
        if self.filters.vwap != VWAPFilter.NONE:
            parts.append(f"VWAP:{self.filters.vwap.value}")
        return " | ".join(parts)


# ─── Indicator Snapshot ──────────────────────────────────────────────────────


@dataclass
class IndicatorSnapshot:
    """
    All indicators computed ONCE per instrument per candle close.
    All variants reuse these values — no recalculation.
    """

    # Core indicators
    atr: float = 0.0
    adx: float = 0.0
    rsi: float = 0.0
    vwap: float = 0.0
    volume_ratio: float = 0.0  # current volume / average volume
    vix: float = 0.0  # India VIX value (global, not per-instrument)

    # EMAs
    ema_9: float = 0.0
    ema_21: float = 0.0
    ema_20: float = 0.0
    ema_50: float = 0.0

    # Slopes (positive = rising, negative = falling)
    ema_20_slope: float = 0.0
    ema_50_slope: float = 0.0

    # Bollinger Bands
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    bb_squeeze: bool = False  # True if BB inside Keltner

    # Price relative to indicators
    price_vs_vwap: float = 0.0  # (price - vwap) / atr, normalized distance

    # SuperTrend (kept for potential future use)
    supertrend: float = 0.0
    supertrend_direction: bool = True  # True = uptrend


# ─── Metadata Snapshot ───────────────────────────────────────────────────────


@dataclass
class MetadataSnapshot:
    """
    Market context metadata computed once per session/day.
    Used for post-analysis, NOT for filter combinations.
    Stored with each trade for regime analysis.
    """

    session: str = ""  # "MORNING" / "MIDDAY" / "CLOSING"
    day_of_week: str = ""  # "MON" / "TUE" / ...
    month: str = ""  # "JAN" / "FEB" / ...
    gap_size: float = 0.0  # % gap from previous close
    gap_direction: str = ""  # "UP" / "DOWN" / "FLAT"
    opening_range_size: float = 0.0  # High - Low of first 15 min
    market_structure: str = ""  # "TRENDING" / "RANGING" / "VOLATILE"
    volatility_regime: str = ""  # "LOW" / "NORMAL" / "HIGH"
    htf_trend_1h: str = ""  # "BULLISH" / "BEARISH" / "NEUTRAL"
    higher_timeframe_bias: str = ""  # "BULLISH" / "BEARISH" / "NEUTRAL"


# ─── Armed Variant (Runtime State) ──────────────────────────────────────────


@dataclass
class ArmedVariant:
    """
    A variant that passed candle-close evaluation and is waiting for trigger.
    Lives in memory only — never persisted to DB.
    """

    variant: Variant
    instrument: str  # exchange_token
    direction: Direction
    trigger_type: TriggerType
    trigger_value: float  # Price level, indicator threshold, etc.
    armed_at_candle: int  # Candle index when armed (for expiry)
    expiry_candles: int  # Max candles to stay armed
    entry_price_hint: float = 0.0  # Expected entry price (for SL calculation)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def variant_id(self) -> str:
        return self.variant.variant_id

    def is_expired(self, current_candle: int) -> bool:
        """Check if this armed variant has exceeded its validity window."""
        return (current_candle - self.armed_at_candle) >= self.expiry_candles


# ─── Trigger Group (Runtime Only) ───────────────────────────────────────────


@dataclass
class TriggerGroup:
    """
    A group of armed variants sharing the same trigger condition.
    Created dynamically at runtime, never stored in DB.
    """

    trigger_type: TriggerType
    trigger_value: float
    instrument: str
    direction: Direction
    members: list[ArmedVariant] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, TriggerType, float, str]:
        """Unique key for this group."""
        return (
            self.instrument,
            self.direction.value,
            self.trigger_type,
            self.trigger_value,
            "",  # reserved
        )


# ─── Trade Record ───────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """
    A recorded entry — one row per triggered trade.
    NO exit information. Exits are simulated post-market.
    """

    trade_id: str
    variant_id: str

    # Identity
    strategy: str
    timeframe: str
    instrument: str

    # Entry
    direction: str  # "LONG" / "SHORT"
    entry_time_ms: float
    entry_price: float

    # Indicator snapshot at entry
    atr_entry: float = 0.0
    adx_entry: float = 0.0
    rsi_entry: float = 0.0
    vix_entry: float = 0.0
    volume_ratio_entry: float = 0.0
    vwap_entry: float = 0.0

    # Metadata
    gap_size: float = 0.0
    gap_direction: str = ""
    session: str = ""
    day_of_week: str = ""
    month: str = ""
    market_structure: str = ""
    volatility_regime: str = ""
    htf_trend_1h: str = ""
    ema_20_slope: float = 0.0
    ema_50_slope: float = 0.0
    opening_range_size: float = 0.0
