"""
Promote live_candles into cfd_historical_candles so research sees continuous history.

Dukascopy covers up to 31-07-2026. Your live feed (MT5 or cTrader) accumulates
candles from Aug 2026 onward in ``live_candles``. Research + backtest only read
``cfd_historical_candles``. This script copies live_candles (from a given date
onward) into cfd_historical_candles so the two periods are seamless for research.

Safe to re-run: uses ON CONFLICT DO NOTHING (same idempotent dedup as the batch
Dukascopy fetch). Does NOT delete the source rows in live_candles — they stay
there as your raw feed archive.

Usage (run manually when ready, e.g. after August completes):
    venv/bin/python -m app.tools.promote_live_to_historical --from 2026-08-01
    venv/bin/python -m app.tools.promote_live_to_historical --from 2026-08-01 --instrument XAUUSD
    venv/bin/python -m app.tools.promote_live_to_historical --from 2026-08-01 --to 2026-08-31
"""

from __future__ import annotations

import argparse
import time

from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

logger = get_logger(__name__)


def run(from_date: str, to_date: str | None = None, instrument: str | None = None):
    store = ResearchStore()
    store.start()
    pg = store._use_postgres
    ph = "%s" if pg else "?"

    where_parts = [f"session_date >= {ph}"]
    params: list = [from_date]
    if to_date:
        where_parts.append(f"session_date <= {ph}")
        params.append(to_date)
    if instrument:
        where_parts.append(f"instrument = {ph}")
        params.append(instrument)
    where = " AND ".join(where_parts)

    # Count source rows.
    count_sql = f"SELECT COUNT(*) AS c FROM live_candles WHERE {where}"
    rows = store._query(count_sql, tuple(params))
    total = rows[0]["c"] if rows else 0
    print(f"Source: {total} live_candles rows matching filter "
          f"(from={from_date}, to={to_date or 'now'}, instrument={instrument or 'ALL'})")
    if total == 0:
        print("Nothing to promote.")
        store.stop()
        return

    # Insert into cfd_historical_candles (idempotent).
    if pg:
        sql = f"""
        INSERT INTO cfd_historical_candles
            (instrument, timeframe, timestamp_ms, open, high, low, close, volume, session_date, session)
        SELECT instrument, timeframe, timestamp_ms, open, high, low, close, volume, session_date, session
        FROM live_candles
        WHERE {where}
        ON CONFLICT (instrument, timeframe, timestamp_ms) DO NOTHING
        """
    else:
        sql = f"""
        INSERT OR IGNORE INTO cfd_historical_candles
            (instrument, timeframe, timestamp_ms, open, high, low, close, volume, session_date, session)
        SELECT instrument, timeframe, timestamp_ms, open, high, low, close, volume, session_date, session
        FROM live_candles
        WHERE {where}
        """

    t0 = time.time()
    try:
        store._execute(sql, tuple(params))
        elapsed = time.time() - t0
        print(f"Promoted {total} candles into cfd_historical_candles in {elapsed:.1f}s "
              "(dupes skipped via ON CONFLICT DO NOTHING).")
    except Exception as e:
        print(f"ERROR: {e}")

    # Verify.
    verify_sql = f"SELECT COUNT(*) AS c FROM cfd_historical_candles WHERE {where}"
    vrows = store._query(verify_sql, tuple(params))
    hist_count = vrows[0]["c"] if vrows else 0
    print(f"cfd_historical_candles now has {hist_count} rows in that date range.")
    store.stop()


def main():
    parser = argparse.ArgumentParser(description="Promote live_candles -> cfd_historical_candles")
    parser.add_argument("--from", dest="from_date", type=str, required=True,
                        help="Start session_date (YYYY-MM-DD), e.g. 2026-08-01")
    parser.add_argument("--to", dest="to_date", type=str, default=None,
                        help="End session_date (YYYY-MM-DD), default = no upper bound")
    parser.add_argument("--instrument", type=str, default=None,
                        help="Limit to one instrument (default: all)")
    args = parser.parse_args()
    run(from_date=args.from_date, to_date=args.to_date, instrument=args.instrument)


if __name__ == "__main__":
    main()
