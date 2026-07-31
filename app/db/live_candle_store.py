"""
LiveCandleStore — persists live-captured candles to the research DB.

Subscribes to 'candle' events from the EventBus and writes each completed
candle into the permanent ``live_candles`` table via ResearchStore (Postgres on
the VM, SQLite locally). This is the CFD/MT5 counterpart to the NSE CandleCache,
but permanent (a growing archive of our own feed) rather than a 7-day cache.

session_date is derived from the candle's UTC open time (candles are stored in
real UTC), so it does not depend on the host machine's timezone.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.core.models import Candle
from app.db.research_store import ResearchStore
from app.utils import forex_hours
from app.utils.logger import get_logger

logger = get_logger(__name__)


class LiveCandleStore:
    """Writes completed candles to research_db.live_candles.

    Each candle is tagged (from its open time) with:
      * session_date = FX trading day (rolls at 17:00 NY, not UTC midnight)
      * session      = active FX session(s), e.g. 'london', 'tokyo+london'
    so session-dependent strategies/research can group and filter directly.
    """

    def __init__(self, store: ResearchStore) -> None:
        self._store = store
        self._stored = 0

    def on_candle(self, candle: Candle) -> None:
        """EventBus handler: persist one completed candle."""
        # Tag from the candle's open time (a real-UTC datetime the helpers expect).
        open_dt = datetime.fromtimestamp(candle.timestamp_ms / 1000, timezone.utc)
        session_date = forex_hours.trading_day(open_dt)
        session = forex_hours.session_tag(open_dt)
        try:
            self._store.write_live_candle(
                instrument=candle.exchange_token,
                timeframe=candle.timeframe.value,
                timestamp_ms=candle.timestamp_ms,
                o=candle.open,
                h=candle.high,
                l=candle.low,
                c=candle.close,
                volume=candle.volume,
                session_date=session_date,
                session=session,
            )
            self._stored += 1
        except Exception as e:  # noqa: BLE001 - never let a DB hiccup kill the feed
            logger.error("live_candles write failed (%s %s): %s",
                         candle.exchange_token, candle.timeframe.value, e)

    @property
    def stored(self) -> int:
        return self._stored
