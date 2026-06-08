"""
DataManager — Historical data warmup engine.

Responsibilities:
- Collects warmup requirements from all registered strategies
- Fetches historical candles from Groww's Backtesting API (get_historical_candles)
- Falls back to the deprecated get_historical_candle_data() if the new API fails
- Injects them into the CandleBuilder's history buffer
- Handles rate limiting, retries, and graceful degradation

Architecture:
    Strategies declare warmup needs → DataManager merges & deduplicates
    → Fetches from Groww API (with concurrency control) → Injects into CandleBuilder
    → Strategies start with full indicator context

New API (primary): get_historical_candles()
- Requires ₹499/month Backtesting subscription
- Uses groww_symbol format: "NSE-RELIANCE"
- Uses candle_interval SDK constants: groww.CANDLE_INTERVAL_MIN_5
- Timestamps in response are ISO strings: "2025-09-24T10:30:00"
- Each candle: [timestamp_iso, open, high, low, close, volume, open_interest]
- Data available from 2020 onwards

Old API (fallback): get_historical_candle_data()
- Deprecated — may be removed by Groww at any time
- Uses trading_symbol directly: "RELIANCE"
- Uses interval_in_minutes integers
- Timestamps in response are epoch seconds
- Each candle: [epoch_seconds, open, high, low, close, volume]
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from growwapi import GrowwAPI

from app.broker.groww import GrowwBroker, is_index_token
from app.core.candle_builder import CandleBuilder
from app.core.models import Candle, Timeframe
from app.strategy.base import BaseStrategy
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ─── Timeframe → new API candle_interval string mapping ──────────────
TIMEFRAME_TO_INTERVAL_NEW: dict[Timeframe, str] = {
    Timeframe.M1:  "1minute",
    Timeframe.M5:  "5minute",
    Timeframe.M15: "15minute",
    Timeframe.M30: "30minute",
    Timeframe.H1:  "1hour",
    Timeframe.D1:  "1day",
}

# ─── Timeframe → old API interval_in_minutes mapping (fallback) ──────
TIMEFRAME_TO_INTERVAL_OLD: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.D1: 1440,
}

# Maximum lookback days per timeframe (new API limits)
MAX_LOOKBACK_DAYS: dict[Timeframe, int] = {
    Timeframe.M1:  30,
    Timeframe.M5:  30,
    Timeframe.M15: 90,
    Timeframe.M30: 90,
    Timeframe.H1:  180,
    Timeframe.D1:  180,
}

# Timeframe durations in minutes (for estimating days needed)
TIMEFRAME_MINUTES: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.D1: 1440,
}


@dataclass
class WarmupRequest:
    """A single warmup fetch request."""

    exchange_token: str
    trading_symbol: str
    exchange: str
    segment: str
    timeframe: Timeframe
    candles_needed: int


@dataclass
class WarmupResult:
    """Summary of warmup execution."""

    total_instruments: int = 0
    total_requests: int = 0
    successful: int = 0
    failed: int = 0
    candles_loaded: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary."""
        status = "✅" if self.failed == 0 else "⚠️"
        return (
            f"{status} Warmup complete: "
            f"{self.successful}/{self.total_requests} requests OK, "
            f"{self.candles_loaded} candles loaded, "
            f"{self.duration_seconds:.1f}s elapsed"
            + (f" | {self.failed} failures" if self.failed > 0 else "")
        )


class DataManager:
    """
    Historical data warmup engine.

    Usage:
        dm = DataManager(broker, candle_builder, config)
        result = dm.warmup(strategies, instruments)
    """

    def __init__(
        self,
        broker: GrowwBroker,
        candle_builder: CandleBuilder,
        concurrency: int = 5,
        delay_between_requests_ms: int = 150,
        max_retries: int = 3,
        retry_backoff_base: float = 1.0,
    ) -> None:
        self._broker = broker
        self._candle_builder = candle_builder
        self._concurrency = concurrency
        self._delay_ms = delay_between_requests_ms
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base

    def warmup(
        self,
        strategies: list[BaseStrategy],
        exchange_tokens: list[str],
        instrument_map: dict[str, dict],
    ) -> WarmupResult:
        """
        Execute warmup synchronously (blocking).

        Args:
            strategies: Registered strategies (to read warmup_config from).
            exchange_tokens: List of exchange tokens to warm up.
            instrument_map: Token → {"symbol": "RELIANCE", "name": "..."} mapping.

        Returns:
            WarmupResult with stats.
        """
        start_time = time.time()
        result = WarmupResult()

        # 1. Merge warmup requirements from all strategies
        merged_requirements = self._merge_requirements(strategies)
        if not merged_requirements:
            logger.info("No warmup requirements declared by strategies. Skipping.")
            result.duration_seconds = time.time() - start_time
            return result

        logger.info(
            "Warmup requirements: %s",
            {tf.value: count for tf, count in merged_requirements.items()},
        )

        # 2. Build fetch requests (skipping indices — historical API doesn't support them)
        requests = self._build_requests(
            exchange_tokens, instrument_map, merged_requirements
        )
        result.total_instruments = len(exchange_tokens)
        result.total_requests = len(requests)

        if not requests:
            logger.warning("No valid warmup requests could be built.")
            result.duration_seconds = time.time() - start_time
            return result

        logger.info(
            "Starting warmup: %d requests for %d instruments...",
            len(requests),
            result.total_instruments,
        )

        # 3. Execute fetches with concurrency control
        try:
            fetch_results = asyncio.run(self._execute_fetches(requests))
        except RuntimeError:
            # Fallback if an event loop is already running (e.g., Jupyter, nested async)
            loop = asyncio.new_event_loop()
            try:
                fetch_results = loop.run_until_complete(
                    self._execute_fetches(requests)
                )
            finally:
                loop.close()

        # 4. Inject results into candle builder
        for req, candles in fetch_results:
            if candles is not None:
                self._candle_builder.inject_history(
                    exchange_token=req.exchange_token,
                    timeframe=req.timeframe,
                    candles=candles,
                )
                result.successful += 1
                result.candles_loaded += len(candles)
            else:
                result.failed += 1
                result.errors.append(
                    f"{req.trading_symbol}/{req.timeframe.value}: fetch failed"
                )

        result.duration_seconds = time.time() - start_time
        logger.info(result.summary())
        return result

    def _merge_requirements(
        self, strategies: list[BaseStrategy]
    ) -> dict[Timeframe, int]:
        """
        Merge warmup_config from all strategies.
        Takes the maximum candle count per timeframe.
        """
        merged: dict[Timeframe, int] = {}

        for strategy in strategies:
            config = strategy.warmup_config
            for tf_str, count in config.items():
                tf = self._parse_timeframe(tf_str)
                if tf is None:
                    logger.warning(
                        "Strategy '%s' declared unknown timeframe '%s' in warmup_config",
                        strategy.name,
                        tf_str,
                    )
                    continue
                merged[tf] = max(merged.get(tf, 0), count)

        return merged

    def _build_requests(
        self,
        exchange_tokens: list[str],
        instrument_map: dict[str, dict],
        requirements: dict[Timeframe, int],
    ) -> list[WarmupRequest]:
        """Build WarmupRequest objects for each instrument × timeframe."""
        requests = []

        for token in exchange_tokens:
            # Skip indices — historical API doesn't support them
            if is_index_token(token):
                logger.debug(
                    "Skipping index token '%s' for warmup (not supported by historical API)",
                    token,
                )
                continue

            inst_data = instrument_map.get(token, {})
            symbol = inst_data.get("symbol", token)
            exchange = "NSE"
            segment = "CASH"

            for timeframe, candles_needed in requirements.items():
                requests.append(
                    WarmupRequest(
                        exchange_token=token,
                        trading_symbol=symbol,
                        exchange=exchange,
                        segment=segment,
                        timeframe=timeframe,
                        candles_needed=candles_needed,
                    )
                )

        return requests

    async def _execute_fetches(
        self, requests: list[WarmupRequest]
    ) -> list[tuple[WarmupRequest, list[Candle] | None]]:
        """Execute all fetch requests with concurrency control."""
        semaphore = asyncio.Semaphore(self._concurrency)
        results: list[tuple[WarmupRequest, list[Candle] | None]] = []

        async def fetch_one(
            req: WarmupRequest,
        ) -> tuple[WarmupRequest, list[Candle] | None]:
            async with semaphore:
                candles = await self._fetch_with_retry(req)
                await asyncio.sleep(self._delay_ms / 1000.0)
                return (req, candles)

        tasks = [fetch_one(req) for req in requests]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for i, item in enumerate(completed):
            if isinstance(item, Exception):
                logger.error(
                    "Warmup fetch exception for %s: %s",
                    requests[i].trading_symbol,
                    item,
                )
                results.append((requests[i], None))
            else:
                results.append(item)

        return results

    async def _fetch_with_retry(
        self, req: WarmupRequest
    ) -> list[Candle] | None:
        """Fetch historical candles with retry and exponential backoff."""
        for attempt in range(1, self._max_retries + 1):
            try:
                candles = await asyncio.to_thread(self._fetch_candles, req)
                if candles:
                    logger.debug(
                        "Fetched %d candles for %s/%s",
                        len(candles),
                        req.trading_symbol,
                        req.timeframe.value,
                    )
                return candles

            except Exception as e:
                is_rate_limit = "RateLimit" in type(e).__name__

                if attempt < self._max_retries:
                    backoff = self._retry_backoff_base * (2 ** (attempt - 1))
                    if is_rate_limit:
                        backoff *= 2
                    logger.warning(
                        "Warmup fetch failed for %s/%s (attempt %d/%d): %s. "
                        "Retrying in %.1fs...",
                        req.trading_symbol,
                        req.timeframe.value,
                        attempt,
                        self._max_retries,
                        e,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "Warmup fetch FAILED for %s/%s after %d attempts: %s",
                        req.trading_symbol,
                        req.timeframe.value,
                        self._max_retries,
                        e,
                    )
                    return None

        return None

    def _fetch_candles(self, req: WarmupRequest) -> list[Candle]:
        """
        Fetch historical candles from Groww API (blocking call).
        Tries the new get_historical_candles() API first (Backtesting subscription).
        Falls back to the deprecated get_historical_candle_data() if it fails.
        """
        start_str, end_str = self._calculate_time_range(req)

        # Try new API first
        try:
            return self._fetch_candles_new_api(req, start_str, end_str)
        except Exception as e:
            logger.warning(
                "New API failed for %s/%s (%s), falling back to old API",
                req.trading_symbol,
                req.timeframe.value,
                e,
            )

        # Fall back to old deprecated API
        return self._fetch_candles_old_api(req, start_str, end_str)

    def _fetch_candles_new_api(
        self, req: WarmupRequest, start_str: str, end_str: str
    ) -> list[Candle]:
        """
        Fetch using the new get_historical_candles() Backtesting API.

        Response candle format:
        ["2025-09-24T10:30:00", open, high, low, close, volume, open_interest]
        """
        api = self._broker.api
        candle_interval = TIMEFRAME_TO_INTERVAL_NEW[req.timeframe]
        groww_symbol = f"{req.exchange}-{req.trading_symbol}"

        logger.debug(
            "New API: fetching %s candles for %s (%s): %s to %s",
            candle_interval,
            req.trading_symbol,
            groww_symbol,
            start_str,
            end_str,
        )

        response = api.get_historical_candles(
            exchange=req.exchange,
            segment=req.segment,
            groww_symbol=groww_symbol,
            start_time=start_str,
            end_time=end_str,
            candle_interval=candle_interval,
        )

        raw_candles = response.get("candles", [])
        if not raw_candles:
            logger.debug(
                "No candles returned (new API) for %s/%s",
                req.trading_symbol,
                req.timeframe.value,
            )
            return []

        candles = self._parse_candles_new_api(raw_candles, req)

        if len(candles) > req.candles_needed:
            candles = candles[-req.candles_needed:]

        return candles

    def _fetch_candles_old_api(
        self, req: WarmupRequest, start_str: str, end_str: str
    ) -> list[Candle]:
        """
        Fetch using the deprecated get_historical_candle_data() API (fallback).

        Response candle format:
        [epoch_seconds, open, high, low, close, volume]
        """
        api = self._broker.api
        candle_interval = TIMEFRAME_TO_INTERVAL_OLD[req.timeframe]

        logger.debug(
            "Old API (fallback): fetching %d-min candles for %s: %s to %s",
            candle_interval,
            req.trading_symbol,
            start_str,
            end_str,
        )

        response = api.get_historical_candle_data(
            trading_symbol=req.trading_symbol,
            exchange=req.exchange,
            segment=req.segment,
            start_time=start_str,
            end_time=end_str,
            interval_in_minutes=candle_interval,
        )

        raw_candles = response.get("candles", [])
        if not raw_candles:
            logger.debug(
                "No candles returned (old API) for %s/%s",
                req.trading_symbol,
                req.timeframe.value,
            )
            return []

        candles = self._parse_candles_old_api(raw_candles, req)

        if len(candles) > req.candles_needed:
            candles = candles[-req.candles_needed:]

        return candles

    def _calculate_time_range(self, req: WarmupRequest) -> tuple[str, str]:
        """
        Calculate start/end time strings for the historical data fetch.

        If called before market open, uses the previous trading day's close
        as end_time so we get complete historical candles rather than an
        empty current session.
        """
        from app.utils.market_hours import MARKET_OPEN, MARKET_CLOSE, is_trading_day

        now = datetime.now()
        today = now.date()
        current_time = now.time()

        if is_trading_day(today) and current_time >= MARKET_OPEN:
            end_time = now
        else:
            # Before market open or weekend — use last trading day's close
            check_date = today - timedelta(days=1)
            for _ in range(10):
                if is_trading_day(check_date):
                    break
                check_date -= timedelta(days=1)
            end_time = datetime.combine(check_date, MARKET_CLOSE)

        timeframe_minutes = TIMEFRAME_MINUTES[req.timeframe]
        max_lookback = MAX_LOOKBACK_DAYS[req.timeframe]

        if req.timeframe == Timeframe.D1:
            days_needed = req.candles_needed
        else:
            trading_minutes_per_day = 375
            candles_per_day = trading_minutes_per_day // timeframe_minutes
            trading_days_needed = (req.candles_needed // candles_per_day) + 1
            days_needed = int(trading_days_needed * 7 / 5) + 2

        days_needed = min(days_needed, max_lookback)
        start_time = end_time - timedelta(days=days_needed)

        return (
            start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _parse_candles_new_api(
        self, raw_candles: list[list], req: WarmupRequest
    ) -> list[Candle]:
        """
        Parse candles from new get_historical_candles() API.

        New API response format per candle:
        ["2025-09-24T10:30:00", open, high, low, close, volume, open_interest]
        Timestamp is an ISO string, not epoch seconds.
        """
        candles = []

        for raw in raw_candles:
            if len(raw) < 6:
                continue

            # Timestamp is ISO string — parse and convert to epoch milliseconds
            try:
                ts_str = str(raw[0])
                # Handle both "2025-09-24T10:30:00" and "2025-09-24 10:30:00"
                ts_str = ts_str.replace("T", " ")
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                timestamp_ms = dt.timestamp() * 1000
            except (ValueError, TypeError):
                continue

            candle = Candle(
                exchange=req.exchange,
                segment=req.segment,
                exchange_token=req.exchange_token,
                timeframe=req.timeframe,
                timestamp_ms=timestamp_ms,
                open=float(raw[1]),
                high=float(raw[2]),
                low=float(raw[3]),
                close=float(raw[4]),
                volume=int(raw[5]) if raw[5] is not None else 0,
            )
            candles.append(candle)

        return candles

    def _parse_candles_old_api(
        self, raw_candles: list[list], req: WarmupRequest
    ) -> list[Candle]:
        """
        Parse candles from deprecated get_historical_candle_data() API.

        Old API response format per candle:
        [epoch_seconds, open, high, low, close, volume]
        """
        candles = []

        for raw in raw_candles:
            if len(raw) < 6:
                continue

            # Timestamp is epoch seconds — convert to milliseconds
            try:
                timestamp_ms = float(raw[0]) * 1000
            except (ValueError, TypeError):
                continue

            candle = Candle(
                exchange=req.exchange,
                segment=req.segment,
                exchange_token=req.exchange_token,
                timeframe=req.timeframe,
                timestamp_ms=timestamp_ms,
                open=float(raw[1]),
                high=float(raw[2]),
                low=float(raw[3]),
                close=float(raw[4]),
                volume=int(raw[5]) if raw[5] is not None else 0,
            )
            candles.append(candle)

        return candles

    @staticmethod
    def _parse_timeframe(tf_str: str) -> Timeframe | None:
        """Parse timeframe string to enum."""
        mapping = {
            "1m": Timeframe.M1,
            "5m": Timeframe.M5,
            "15m": Timeframe.M15,
            "30m": Timeframe.M30,
            "1h": Timeframe.H1,
            "1d": Timeframe.D1,
        }
        return mapping.get(tf_str)
