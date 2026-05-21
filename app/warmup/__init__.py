"""
Historical data warmup module.

Fetches historical candles from the broker on startup and pre-loads
the candle builder so strategies have full indicator context immediately.
"""

from app.warmup.data_manager import DataManager, WarmupResult

__all__ = ["DataManager", "WarmupResult"]
