"""
Exit engine scheduler — triggers exit simulation after market close.

Can be:
1. Called programmatically from main_research after 3:30 PM
2. Run manually: python -m app.exit_engine.run
3. Scheduled via cron or systemd timer

Idempotent: re-running for same day overwrites previous results.
"""

from __future__ import annotations

from datetime import datetime

from app.db.research_store import ResearchStore
from app.exit_engine.engine import ExitSimulationEngine, ExitEngineStats
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ExitScheduler:
    """
    Manages scheduling and execution of post-market exit simulation.
    """

    def __init__(self, store: ResearchStore) -> None:
        self._store = store
        self._engine = ExitSimulationEngine(store)
        self._last_run_date: str = ""

    def run_today(self) -> ExitEngineStats:
        """Run exit simulation for today's trades."""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.run_for_date(today)

    def run_for_date(self, date_str: str) -> ExitEngineStats:
        """Run exit simulation for a specific date."""
        logger.info("Exit scheduler: running for %s", date_str)
        stats = self._engine.run_for_date(date_str)
        self._last_run_date = date_str
        return stats

    def should_run(self) -> bool:
        """
        Check if exit simulation should run now.
        Returns True if:
        - It's after 15:35 IST (5 min buffer after market close)
        - We haven't run for today yet
        """
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # Already ran today
        if self._last_run_date == today:
            return False

        # Must be after 15:35
        market_close_buffer = now.replace(hour=15, minute=35, second=0, microsecond=0)
        return now >= market_close_buffer

    @property
    def last_run_date(self) -> str:
        return self._last_run_date
