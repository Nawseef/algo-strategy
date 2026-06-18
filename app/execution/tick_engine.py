"""
Tick Trigger Engine — fires trades when price crosses group levels.

From File 4:
    "For each incoming tick:
     System checks: Does price break any active trigger level?
     Then: Trigger all variants in that group."

From File 6 (Safety):
    - No DB writes inside tick loop
    - No full variant scan happens on tick updates
    - Tick processing uses grouped hash lookup logic

The tick engine:
1. Receives tick events (price updates)
2. Checks against grouped trigger levels (O(groups) not O(variants))
3. When a group triggers → queue trade records for batch write
4. Triggered groups are removed from monitoring

Trade records are QUEUED, not written immediately. The trade recorder
flushes them periodically (every N seconds or on buffer full).
"""

from __future__ import annotations

import time
from collections import defaultdict

from app.broker.base import Tick
from app.execution.armed_state import ArmedStateManager
from app.execution.grouping import GroupingEngine, PriceGroup
from app.utils.logger import get_logger
from app.variants.models import (
    ArmedVariant,
    Direction,
    IndicatorSnapshot,
    MetadataSnapshot,
    TradeRecord,
)

logger = get_logger(__name__)


class TickTriggerEngine:
    """
    Processes ticks and fires trades when price crosses trigger levels.

    Architecture:
        Tick → GroupingEngine.check_triggers() → queue TradeRecords

    Does NOT write to DB directly. Queues trades for batch write.

    Usage:
        engine = TickTriggerEngine(armed_state, grouping_engine)
        engine.on_tick(tick)  # Called on every tick
        trades = engine.flush_trades()  # Periodic batch flush
    """

    def __init__(
        self,
        armed_state: ArmedStateManager,
        grouping_engine: GroupingEngine,
    ) -> None:
        self._armed_state = armed_state
        self._grouping = grouping_engine

        # Trade queue — flushed periodically, NOT written per tick
        self._trade_queue: list[TradeRecord] = []

        # Current indicator/metadata snapshots per instrument (set by orchestrator)
        self._snapshots: dict[str, IndicatorSnapshot] = {}
        self._metadata: dict[str, MetadataSnapshot] = {}

        # Stats
        self._ticks_processed: int = 0
        self._groups_triggered: int = 0
        self._trades_created: int = 0

    @staticmethod
    def _make_trade_id(variant_id: str, instrument: str, entry_time_ms: float, direction: str) -> str:
        """Generate deterministic trade_id from signal data. Same signal = same ID."""
        import hashlib
        raw = f"{variant_id}|{instrument}|{int(entry_time_ms)}|{direction}"
        return f"T-{hashlib.md5(raw.encode()).hexdigest()[:12]}"

    def on_tick(self, tick: Tick) -> int:
        """
        Process a tick event. Check if any trigger groups fire.

        Returns the number of trades created (0 in most cases).
        This is the HOT PATH — must be fast, no allocations when nothing triggers.
        """
        self._ticks_processed += 1
        instrument = tick.exchange_token
        price = tick.ltp

        # Check trigger groups for this instrument
        triggered_groups = self._grouping.check_triggers(instrument, price)

        if not triggered_groups:
            return 0  # Most common path — nothing fires

        # Process triggered groups
        trades_created = 0
        for group in triggered_groups:
            trades_created += self._fire_group(
                group=group,
                instrument=instrument,
                price=price,
                timestamp_ms=tick.timestamp_ms,
            )

        return trades_created

    def _fire_group(
        self,
        group: PriceGroup,
        instrument: str,
        price: float,
        timestamp_ms: float,
    ) -> int:
        """
        Fire all variants in a triggered group.
        Creates TradeRecord for each variant and queues them.
        """
        self._groups_triggered += 1

        # Get current snapshot/metadata for this instrument
        snapshot = self._snapshots.get(instrument, IndicatorSnapshot())
        metadata = self._metadata.get(instrument, MetadataSnapshot())

        # Collect variant IDs that triggered (for armed state cleanup)
        triggered_ids: list[str] = []

        for armed_variant in group.members:
            # Create trade record
            trade = self._create_trade_record(
                armed_variant=armed_variant,
                instrument=instrument,
                price=price,
                timestamp_ms=timestamp_ms,
                snapshot=snapshot,
                metadata=metadata,
            )
            self._trade_queue.append(trade)
            triggered_ids.append(armed_variant.variant_id)
            self._trades_created += 1

        # Remove triggered variants from armed state
        self._armed_state.disarm_triggered(instrument, triggered_ids)

        # Remove the group from monitoring
        self._grouping.remove_group(instrument, group.direction, group.trigger_value)

        logger.info(
            "TRIGGER | %s %s @ %.2f | %d variants fired | group level=%.2f",
            instrument,
            group.direction.value,
            price,
            len(group.members),
            group.trigger_value,
        )

        return len(group.members)

    def _create_trade_record(
        self,
        armed_variant: ArmedVariant,
        instrument: str,
        price: float,
        timestamp_ms: float,
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
    ) -> TradeRecord:
        """Create a TradeRecord from a triggered armed variant."""
        variant = armed_variant.variant

        return TradeRecord(
            trade_id=self._make_trade_id(variant.variant_id, instrument, timestamp_ms, armed_variant.direction.value),
            variant_id=variant.variant_id,
            strategy=variant.strategy.value,
            timeframe=variant.timeframe.value,
            instrument=instrument,
            direction=armed_variant.direction.value,
            entry_time_ms=timestamp_ms,
            entry_price=price,
            # Indicator snapshot
            atr_entry=snapshot.atr,
            adx_entry=snapshot.adx,
            rsi_entry=snapshot.rsi,
            vix_entry=snapshot.vix,
            volume_ratio_entry=snapshot.volume_ratio,
            vwap_entry=snapshot.vwap,
            # Metadata
            gap_size=metadata.gap_size,
            gap_direction=metadata.gap_direction,
            session=metadata.session,
            day_of_week=metadata.day_of_week,
            month=metadata.month,
            market_structure=metadata.market_structure,
            volatility_regime=metadata.volatility_regime,
            htf_trend_1h=metadata.htf_trend_1h,
            ema_20_slope=snapshot.ema_20_slope,
            ema_50_slope=snapshot.ema_50_slope,
            opening_range_size=metadata.opening_range_size,
        )

    # ─── Snapshot Management ─────────────────────────────────────────────────

    def update_snapshot(self, instrument: str, snapshot: IndicatorSnapshot) -> None:
        """Update the indicator snapshot for an instrument (called on candle close)."""
        self._snapshots[instrument] = snapshot

    def update_metadata(self, instrument: str, metadata: MetadataSnapshot) -> None:
        """Update metadata for an instrument."""
        self._metadata[instrument] = metadata

    # ─── Trade Queue ─────────────────────────────────────────────────────────

    def flush_trades(self) -> list[TradeRecord]:
        """
        Flush and return all queued trades.
        Called periodically by the trade recorder (every N seconds).
        """
        if not self._trade_queue:
            return []

        trades = self._trade_queue.copy()
        self._trade_queue.clear()
        return trades

    def pending_trade_count(self) -> int:
        """Number of trades waiting to be flushed."""
        return len(self._trade_queue)

    # ─── Stats ───────────────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, int]:
        """Get tick engine statistics."""
        return {
            "ticks_processed": self._ticks_processed,
            "groups_triggered": self._groups_triggered,
            "trades_created": self._trades_created,
            "pending_trades": len(self._trade_queue),
        }

    def reset_daily(self) -> None:
        """Daily reset."""
        self._trade_queue.clear()
        self._snapshots.clear()
        self._metadata.clear()
        self._ticks_processed = 0
        self._groups_triggered = 0
        self._trades_created = 0
