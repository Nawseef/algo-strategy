"""
Trade Recorder — batches trade writes to the research database.

From File 6 (Safety):
    "No DB writes inside tick loop"
    "DB writes only at: Trade creation, End-of-day exit simulation, Batch summary updates"

The trade recorder:
1. Accepts trade records from the tick engine (queued, not immediate)
2. Accepts immediate trades from CANDLE_CLOSE mode evaluations
3. Flushes to SQLite in batches (every N seconds or on buffer full)
4. Provides deduplication (same variant + same instrument + same candle = one trade)
"""

from __future__ import annotations

import time
import threading
import uuid

from app.db.research_store import ResearchStore
from app.utils.logger import get_logger
from app.variants.models import (
    IndicatorSnapshot,
    MetadataSnapshot,
    TradeRecord,
    Variant,
)
from app.variants.strategies.base_template import CandidateSignal

logger = get_logger(__name__)


class TradeRecorder:
    """
    Batches and writes trade records to the research database.

    Thread-safe. Flushes on a timer or when buffer is full.
    """

    def __init__(
        self,
        store: ResearchStore,
        flush_interval_seconds: float = 5.0,
        max_buffer_size: int = 500,
    ) -> None:
        self._store = store
        self._flush_interval = flush_interval_seconds
        self._max_buffer = max_buffer_size

        self._buffer: list[TradeRecord] = []
        self._lock = threading.Lock()

        # Deduplication: track (variant_id, instrument, candle_timestamp) combos
        # to prevent the same variant from recording twice on the same candle
        self._seen_today: set[tuple[str, str, int]] = set()

        # Stats
        self._total_recorded: int = 0
        self._total_deduplicated: int = 0
        self._flush_count: int = 0

        self._running = False
        self._flush_timer: threading.Timer | None = None

    def start(self) -> None:
        """Start the flush timer."""
        self._running = True
        self._schedule_flush()
        logger.info(
            "TradeRecorder started (flush every %.1fs, buffer max %d)",
            self._flush_interval, self._max_buffer,
        )

    def stop(self) -> None:
        """Stop and flush remaining trades."""
        self._running = False
        if self._flush_timer:
            self._flush_timer.cancel()
        self._flush()  # Final flush
        logger.info(
            "TradeRecorder stopped (total recorded: %d, deduplicated: %d, flushes: %d)",
            self._total_recorded, self._total_deduplicated, self._flush_count,
        )

    def record_trade(self, trade: TradeRecord) -> bool:
        """
        Add a trade to the buffer. Returns False if deduplicated (skipped).
        """
        # Dedup key: variant + instrument + candle time (rounded to minute)
        candle_key = int(trade.entry_time_ms / 60000)  # 1-minute buckets
        dedup_key = (trade.variant_id, trade.instrument, candle_key)

        with self._lock:
            if dedup_key in self._seen_today:
                self._total_deduplicated += 1
                return False

            self._seen_today.add(dedup_key)
            self._buffer.append(trade)

            # Flush if buffer full
            if len(self._buffer) >= self._max_buffer:
                self._flush()

        return True

    def record_immediate_trades(
        self,
        variants_and_signals: list[tuple[Variant, CandidateSignal]],
        instrument: str,
        timestamp_ms: float,
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
    ) -> int:
        """
        Record trades from CANDLE_CLOSE mode variants.
        Creates TradeRecord for each (variant, signal) pair and buffers them.
        Returns count of trades added (after deduplication).
        """
        recorded = 0
        for variant, candidate in variants_and_signals:
            trade = TradeRecord(
                trade_id=f"T-{uuid.uuid4().hex[:12]}",
                variant_id=variant.variant_id,
                strategy=variant.strategy.value,
                timeframe=variant.timeframe.value,
                instrument=instrument,
                direction=candidate.direction.value,
                entry_time_ms=timestamp_ms,
                entry_price=candidate.entry_price_hint,
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
            if self.record_trade(trade):
                recorded += 1

        return recorded

    def record_tick_trades(self, trades: list[TradeRecord]) -> int:
        """
        Record trades from the tick trigger engine (already formed TradeRecords).
        Returns count after deduplication.
        """
        recorded = 0
        for trade in trades:
            if self.record_trade(trade):
                recorded += 1
        return recorded

    def _flush(self) -> None:
        """Write buffered trades to database."""
        with self._lock:
            if not self._buffer:
                return

            trades_to_write = self._buffer.copy()

        # Write OUTSIDE the lock (DB write may be slow)
        count = self._store.write_trades_batch(trades_to_write)

        # Only clear buffer if write succeeded
        if count > 0:
            with self._lock:
                # Remove only the trades we successfully wrote
                # (new trades may have been added while we were writing)
                self._buffer = self._buffer[len(trades_to_write):]
            self._total_recorded += count
            self._flush_count += 1
            if count > 0:
                logger.info("TradeRecorder flushed %d trades to DB", count)
        else:
            # Write failed — trades stay in buffer for retry on next flush
            logger.warning("TradeRecorder flush failed — %d trades retained in buffer", len(trades_to_write))

    def _schedule_flush(self) -> None:
        """Schedule the next periodic flush."""
        if not self._running:
            return
        self._flush_timer = threading.Timer(self._flush_interval, self._periodic_flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _periodic_flush(self) -> None:
        """Timer callback — flush and reschedule."""
        if not self._running:
            return
        self._flush()
        self._schedule_flush()

    def reset_daily(self) -> None:
        """Reset dedup set for new day."""
        with self._lock:
            self._seen_today.clear()
        logger.info("TradeRecorder daily reset (dedup cleared)")

    def get_stats(self) -> dict[str, int]:
        """Get recorder statistics."""
        return {
            "total_recorded": self._total_recorded,
            "total_deduplicated": self._total_deduplicated,
            "buffer_pending": len(self._buffer),
            "flush_count": self._flush_count,
        }
