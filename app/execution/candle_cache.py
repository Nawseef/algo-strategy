"""
Candle Cache — stores intraday candles for post-market exit simulation.

From File 2:
    "During market hours: Store current session candles.
     Used to reconstruct trade paths.
     Can be deleted after exit processing."

Listens to candle events and writes to the candle_cache table
in the research database. Lightweight — just OHLCV + timestamp.
"""

from __future__ import annotations

from datetime import datetime

from app.core.models import Candle
from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

logger = get_logger(__name__)


class CandleCache:
    """
    Caches intraday candles for exit simulation.

    On candle close event: write to research.db candle_cache table.
    After exit engine runs: cleanup old data.
    """

    def __init__(self, store: ResearchStore) -> None:
        self._store = store
        self._candles_cached: int = 0
        self._today_str: str = datetime.now().strftime("%Y-%m-%d")

    def on_candle(self, candle: Candle) -> None:
        """Cache a completed candle. Called on every candle close event."""
        self._store.cache_candle(
            instrument=candle.exchange_token,
            timeframe=candle.timeframe.value,
            timestamp_ms=candle.timestamp_ms,
            o=candle.open,
            h=candle.high,
            l=candle.low,
            c=candle.close,
            volume=candle.volume,
            session_date=self._today_str,
        )
        self._candles_cached += 1

    def get_candle_path(
        self,
        instrument: str,
        timeframe: str,
        start_ms: float,
        end_ms: float,
    ) -> list[dict]:
        """
        Get candle path for exit simulation.
        Returns candles between entry_time and market_close.
        """
        return self._store.get_cached_candles(instrument, timeframe, start_ms, end_ms)

    def cleanup(self, days_to_keep: int = 7) -> int:
        """
        Remove old cached candles.
        Keeps the last N days of data.
        """
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days_to_keep)).strftime("%Y-%m-%d")
        removed = self._store.cleanup_candle_cache(cutoff)
        if removed > 0:
            logger.info("CandleCache cleanup: removed %d old candle rows", removed)
        return removed

    def reset_daily(self) -> None:
        """Update session date for new day."""
        self._today_str = datetime.now().strftime("%Y-%m-%d")
        self._candles_cached = 0

    @property
    def candles_cached_today(self) -> int:
        return self._candles_cached
