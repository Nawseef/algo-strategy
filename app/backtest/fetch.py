"""
Historical Data Fetcher — downloads 5m candles from Groww API.

Fetches instrument candle data in 30-day chunks (API limit for 5m candles)
and stores in the historical_candles table. Resumable — tracks progress
per instrument per date.

Usage:
    python -m app.backtest.fetch
    python -m app.backtest.fetch --from 2024-01-01 --to 2024-12-31
    python -m app.backtest.fetch --instruments NIFTY,RELIANCE
    python -m app.backtest.fetch --instrument NIFTY --from 2021-01-01 --to 2026-06-12

Instruments are configured via BACKTEST_INSTRUMENTS in .env or passed via CLI.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

from app.broker.groww import GrowwBroker
from app.db.research_store import ResearchStore
from app.utils.config import load_config
from app.utils.logger import get_logger
from app.utils.market_hours import is_trading_day

logger = get_logger("backtest.fetch")


# ─── Instrument mapping ──────────────────────────────────────────────────────
# exchange_token (internal) → groww_symbol (for backtesting API)

INSTRUMENT_MAP = {
    "NIFTY": {
        "groww_symbol": "NSE-NIFTY",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "26000",
        "type": "index",
    },
    "BANKNIFTY": {
        "groww_symbol": "NSE-BANKNIFTY",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "26009",
        "type": "index",
    },
    "RELIANCE": {
        "groww_symbol": "NSE-RELIANCE",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "2885",
        "type": "stock",
    },
    "HDFCBANK": {
        "groww_symbol": "NSE-HDFCBANK",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "1333",
        "type": "stock",
    },
    "ICICIBANK": {
        "groww_symbol": "NSE-ICICIBANK",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "4963",
        "type": "stock",
    },
    "SBIN": {
        "groww_symbol": "NSE-SBIN",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "3045",
        "type": "stock",
    },
    "AXISBANK": {
        "groww_symbol": "NSE-AXISBANK",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "5900",
        "type": "stock",
    },
    "INFY": {
        "groww_symbol": "NSE-INFY",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "1594",
        "type": "stock",
    },
    "TCS": {
        "groww_symbol": "NSE-TCS",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "11536",
        "type": "stock",
    },
    "BHARTIARTL": {
        "groww_symbol": "NSE-BHARTIARTL",
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "10604",
        "type": "stock",
    },
    "INDIAVIX": {
        "groww_symbol": "NSE-INDIA VIX",  # will try alternatives if this fails
        "exchange": "NSE",
        "segment": "CASH",
        "exchange_token": "26017",
        "type": "index",
        "alternatives": ["NSE-INDIAVIX", "NSE-India VIX"],
    },
}

# Groww backtesting API limits for 5m candles
MAX_DAYS_PER_REQUEST = 30
CANDLE_INTERVAL = "5"  # 5 minutes
DEFAULT_DELAY_MS = 300  # delay between API calls (ms)
MAX_RETRIES = 3


# ─── Fetcher class ───────────────────────────────────────────────────────────


class HistoricalFetcher:
    """
    Downloads 5m candle data from Groww Backtesting API.

    Features:
    - Fetches in 30-day chunks (API limit)
    - Resumable: tracks progress per instrument per date
    - Skips weekends and known holidays
    - Retries on failure with backoff
    - Stores to historical_candles table
    """

    def __init__(
        self,
        broker: GrowwBroker,
        store: ResearchStore,
        delay_ms: int = DEFAULT_DELAY_MS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._broker = broker
        self._store = store
        self._delay_ms = delay_ms
        self._max_retries = max_retries
        self._api = broker.api

        # Stats
        self._total_candles_fetched = 0
        self._total_requests = 0
        self._errors = 0
        self._skipped_dates = 0

    def fetch_instrument(
        self,
        instrument_name: str,
        start_date: date,
        end_date: date,
    ) -> dict[str, int]:
        """
        Fetch all 5m candles for an instrument between start_date and end_date.

        Returns stats dict with total_candles, requests, errors, skipped.
        """
        if instrument_name not in INSTRUMENT_MAP:
            logger.error("Unknown instrument: %s. Available: %s", instrument_name, list(INSTRUMENT_MAP.keys()))
            return {"error": f"Unknown instrument: {instrument_name}"}

        info = INSTRUMENT_MAP[instrument_name]
        exchange_token = info["exchange_token"]
        groww_symbol = info["groww_symbol"]

        logger.info(
            "═══ Fetching %s (%s) from %s to %s ═══",
            instrument_name, groww_symbol, start_date, end_date,
        )

        # Process in 30-day chunks
        current_start = start_date
        chunk_count = 0
        instrument_candles = 0

        while current_start <= end_date:
            current_end = min(current_start + timedelta(days=MAX_DAYS_PER_REQUEST - 1), end_date)

            # Check if this chunk is already fetched (check first and last day)
            chunk_start_str = current_start.strftime("%Y-%m-%d")
            chunk_end_str = current_end.strftime("%Y-%m-%d")

            # Skip if the start date is already fetched
            if self._store.is_date_fetched(exchange_token, chunk_start_str):
                # Check end too — if both fetched, skip entire chunk
                if self._store.is_date_fetched(exchange_token, chunk_end_str):
                    logger.debug("  Chunk %s to %s already fetched — skipping", chunk_start_str, chunk_end_str)
                    current_start = current_end + timedelta(days=1)
                    self._skipped_dates += MAX_DAYS_PER_REQUEST
                    continue

            # Fetch this chunk
            candles = self._fetch_chunk(
                instrument_name=instrument_name,
                groww_symbol=groww_symbol,
                exchange=info["exchange"],
                segment=info["segment"],
                exchange_token=exchange_token,
                start_date=current_start,
                end_date=current_end,
            )

            if candles is not None:
                instrument_candles += candles
                chunk_count += 1

                if chunk_count % 5 == 0:
                    logger.info(
                        "  %s: %d chunks done, %d candles so far",
                        instrument_name, chunk_count, instrument_candles,
                    )

            # Rate limit
            time.sleep(self._delay_ms / 1000.0)

            # Move to next chunk
            current_start = current_end + timedelta(days=1)

        logger.info(
            "═══ %s complete: %d candles in %d chunks ═══",
            instrument_name, instrument_candles, chunk_count,
        )

        return {
            "instrument": instrument_name,
            "candles": instrument_candles,
            "chunks": chunk_count,
            "errors": self._errors,
        }

    def _fetch_chunk(
        self,
        instrument_name: str,
        groww_symbol: str,
        exchange: str,
        segment: str,
        exchange_token: str,
        start_date: date,
        end_date: date,
    ) -> int | None:
        """
        Fetch one 30-day chunk from the API. Returns candle count or None on failure.
        """
        start_str = f"{start_date.strftime('%Y-%m-%d')} 09:15:00"
        end_str = f"{end_date.strftime('%Y-%m-%d')} 15:30:00"

        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._api.get_historical_candles(
                    exchange=exchange,
                    segment=segment,
                    groww_symbol=groww_symbol,
                    start_time=start_str,
                    end_time=end_str,
                    candle_interval=self._api.CANDLE_INTERVAL_MIN_5,
                )

                self._total_requests += 1
                return self._process_response(response, exchange_token, instrument_name)

            except Exception as e:
                error_str = str(e)

                # If symbol not found, try alternatives (for VIX)
                if "not found" in error_str.lower() or "invalid" in error_str.lower():
                    alternatives = INSTRUMENT_MAP.get(instrument_name, {}).get("alternatives", [])
                    if alternatives and attempt == 1:
                        for alt_symbol in alternatives:
                            logger.info("  Trying alternative symbol: %s", alt_symbol)
                            try:
                                response = self._api.get_historical_candles(
                                    exchange=exchange,
                                    segment=segment,
                                    groww_symbol=alt_symbol,
                                    start_time=start_str,
                                    end_time=end_str,
                                    candle_interval=self._api.CANDLE_INTERVAL_MIN_5,
                                )
                                # Update the symbol in the map for future calls
                                INSTRUMENT_MAP[instrument_name]["groww_symbol"] = alt_symbol
                                logger.info("  ✅ Alternative symbol %s works!", alt_symbol)
                                self._total_requests += 1
                                return self._process_response(response, exchange_token, instrument_name)
                            except Exception:
                                continue

                if attempt < self._max_retries:
                    backoff = 2 ** attempt
                    logger.warning(
                        "  Fetch error (attempt %d/%d): %s. Retrying in %ds...",
                        attempt, self._max_retries, error_str[:100], backoff,
                    )
                    time.sleep(backoff)
                else:
                    logger.error(
                        "  FAILED after %d attempts: %s (%s to %s): %s",
                        self._max_retries, instrument_name, start_date, end_date, error_str[:100],
                    )
                    self._errors += 1
                    return None

        return None

    def _process_response(self, response: dict[str, Any], exchange_token: str, instrument_name: str) -> int:
        """
        Process API response and store candles in DB.
        Returns number of candles stored.
        """
        candles_raw = response.get("candles", [])

        if not candles_raw:
            return 0

        # Convert to DB format: (timestamp_ms, open, high, low, close, volume, session_date)
        batch: list[tuple] = []
        dates_seen: set[str] = set()

        skipped_none = 0
        for candle in candles_raw:
            # Backtesting API returns: [timestamp_str, open, high, low, close, volume, oi]
            # Timestamp format: "2025-09-24T10:30:00"
            ts_str = candle[0]

            # Skip candles with None OHLC values (API returns incomplete data sometimes)
            if ts_str is None or candle[1] is None or candle[2] is None or candle[3] is None or candle[4] is None:
                skipped_none += 1
                continue

            o = float(candle[1])
            h = float(candle[2])
            l = float(candle[3])
            c = float(candle[4])
            vol = int(candle[5]) if candle[5] is not None else 0

            # Parse timestamp
            dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
            timestamp_ms = int(dt.timestamp() * 1000)
            session_date = dt.strftime("%Y-%m-%d")
            dates_seen.add(session_date)

            batch.append((timestamp_ms, o, h, l, c, vol, session_date))

        if skipped_none > 0:
            logger.debug("  Skipped %d candles with None OHLC values for %s", skipped_none, instrument_name)

        # Write to DB in one batch
        if batch:
            count = self._store.write_historical_candles_batch(batch, exchange_token, "5m")
            self._total_candles_fetched += count

            # Mark all dates in this chunk as fetched
            for d in dates_seen:
                candles_for_date = sum(1 for b in batch if b[6] == d)
                self._store.mark_fetched(exchange_token, d, candles_for_date)

            return count

        return 0

    def get_stats(self) -> dict[str, int]:
        """Get fetcher statistics."""
        return {
            "total_candles": self._total_candles_fetched,
            "total_requests": self._total_requests,
            "errors": self._errors,
            "skipped_dates": self._skipped_dates,
        }


# ─── CLI Entry Point ─────────────────────────────────────────────────────────


def main() -> None:
    """CLI for fetching historical data."""
    print("=" * 70)
    print("  HISTORICAL DATA FETCHER — Groww Backtesting API")
    print("=" * 70)

    # Parse args
    args = sys.argv[1:]
    start_date = date(2021, 1, 1)
    end_date = date.today()
    instruments: list[str] = list(INSTRUMENT_MAP.keys())

    i = 0
    delay_ms = DEFAULT_DELAY_MS
    while i < len(args):
        if args[i] == "--from" and i + 1 < len(args):
            start_date = date.fromisoformat(args[i + 1])
            i += 2
        elif args[i] == "--to" and i + 1 < len(args):
            end_date = date.fromisoformat(args[i + 1])
            i += 2
        elif args[i] in ("--instruments", "--instrument") and i + 1 < len(args):
            instruments = [x.strip().upper() for x in args[i + 1].split(",")]
            i += 2
        elif args[i] == "--delay" and i + 1 < len(args):
            delay_ms = int(args[i + 1])
            i += 2
        else:
            i += 1

    print(f"\n  Date range: {start_date} → {end_date}")
    print(f"  Instruments ({len(instruments)}): {', '.join(instruments)}")
    print(f"  API limit: {MAX_DAYS_PER_REQUEST} days per request (5m candles)")

    # Calculate estimated work
    total_days = (end_date - start_date).days
    est_chunks = (total_days // MAX_DAYS_PER_REQUEST + 1) * len(instruments)
    est_time_s = est_chunks * (delay_ms / 1000 + 0.5)
    print(f"  Estimated: ~{est_chunks} API calls, ~{est_time_s/60:.0f} minutes")
    print()

    # Authenticate
    config = load_config()
    broker = GrowwBroker(config.groww)

    print("  Authenticating with Groww...")
    try:
        broker.authenticate()
        print("  ✅ Authenticated")
    except Exception as e:
        print(f"  ❌ Authentication failed: {e}")
        sys.exit(1)

    # Initialize DB
    store = ResearchStore()
    store.start()

    # Create fetcher
    fetcher = HistoricalFetcher(broker, store, delay_ms=delay_ms)

    # Fetch each instrument
    t0 = time.time()
    results: list[dict] = []

    for instrument in instruments:
        try:
            result = fetcher.fetch_instrument(instrument, start_date, end_date)
            results.append(result)
        except Exception as e:
            logger.error("Fatal error fetching %s: %s", instrument, e)
            results.append({"instrument": instrument, "error": str(e)})

    total_time = time.time() - t0

    # Summary
    print(f"\n{'═' * 70}")
    print(f"  FETCH COMPLETE")
    print(f"{'═' * 70}")
    print(f"\n  Results:")
    for r in results:
        if "error" in r and isinstance(r["error"], str):
            print(f"    ❌ {r['instrument']}: {r['error']}")
        else:
            print(f"    ✅ {r.get('instrument', '?')}: {r.get('candles', 0):,} candles in {r.get('chunks', 0)} chunks")

    stats = fetcher.get_stats()
    print(f"\n  Total candles: {stats['total_candles']:,}")
    print(f"  API requests:  {stats['total_requests']}")
    print(f"  Errors:        {stats['errors']}")
    print(f"  Time:          {total_time:.1f}s ({total_time/60:.1f} min)")

    # Verify: show first few candles of first instrument
    if instruments and results and results[0].get("candles", 0) > 0:
        first_token = INSTRUMENT_MAP[instruments[0]]["exchange_token"]
        sample = store.get_historical_candles(first_token, "5m", 0, 9999999999999)
        if sample:
            print(f"\n  Sample data ({instruments[0]}, first 3 candles):")
            for c in sample[:3]:
                print(f"    {c.get('timestamp_ms')} | O={c.get('open')} H={c.get('high')} L={c.get('low')} C={c.get('close')} V={c.get('volume')}")

    store.stop()
    print(f"\n{'═' * 70}")


if __name__ == "__main__":
    main()
