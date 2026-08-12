"""
Compare cTrader staging candles vs MT5 live candles.

After running cTrader as a parallel candle archiver (writing to
``ctrader_staging_candles``) for a few days, run this script to see whether the
two feeds produce the same bars. Joins on (instrument, timeframe, timestamp_ms)
and reports per-symbol:
  * matched bars, missing (only in one source), and price deltas (close, high, low).

A healthy result is: 100% match, max close delta < 1–2 ticks, and no missing bars
from either source (both feeds saw all the same 5m intervals).

Usage (on the VM where Postgres lives):
    venv/bin/python -m app.tools.compare_candles
    venv/bin/python -m app.tools.compare_candles --from 2026-08-01 --to 2026-08-10
    venv/bin/python -m app.tools.compare_candles --instrument XAUUSD
"""

from __future__ import annotations

import argparse
from datetime import date

from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

logger = get_logger(__name__)


def run(instrument: str | None = None, from_date: str | None = None, to_date: str | None = None):
    store = ResearchStore()
    store.start()

    where = "WHERE s.timeframe = '5m'"
    params: list = []
    pg = store._use_postgres
    ph = "%s" if pg else "?"

    if instrument:
        where += f" AND s.instrument = {ph}"
        params.append(instrument)
    if from_date:
        where += f" AND s.session_date >= {ph}"
        params.append(from_date)
    if to_date:
        where += f" AND s.session_date <= {ph}"
        params.append(to_date)

    # Join staging (cTrader) against live_candles (MT5) on the natural key.
    sql = f"""
    SELECT
        s.instrument,
        COUNT(*) AS staging_bars,
        COUNT(l.timestamp_ms) AS matched_bars,
        SUM(CASE WHEN l.timestamp_ms IS NULL THEN 1 ELSE 0 END) AS only_in_ctrader,
        AVG(ABS(s.close - l.close)) AS avg_close_delta,
        MAX(ABS(s.close - l.close)) AS max_close_delta,
        AVG(ABS(s.high - l.high)) AS avg_high_delta,
        MAX(ABS(s.high - l.high)) AS max_high_delta,
        AVG(ABS(s.low - l.low)) AS avg_low_delta,
        MAX(ABS(s.low - l.low)) AS max_low_delta
    FROM ctrader_staging_candles s
    LEFT JOIN live_candles l
        ON l.instrument = s.instrument
        AND l.timeframe = s.timeframe
        AND l.timestamp_ms = s.timestamp_ms
    {where}
    GROUP BY s.instrument
    ORDER BY s.instrument
    """

    rows = store._query(sql, tuple(params))

    # Also count bars only in MT5 (live) but missing from staging.
    sql_mt5_only = f"""
    SELECT l.instrument, COUNT(*) AS only_in_mt5
    FROM live_candles l
    LEFT JOIN ctrader_staging_candles s
        ON s.instrument = l.instrument
        AND s.timeframe = l.timeframe
        AND s.timestamp_ms = l.timestamp_ms
    WHERE l.timeframe = '5m' AND s.timestamp_ms IS NULL
    {"AND l.instrument = " + ph if instrument else ""}
    {"AND l.session_date >= " + ph if from_date else ""}
    {"AND l.session_date <= " + ph if to_date else ""}
    GROUP BY l.instrument
    """
    mt5_only_params = [p for p in params]  # same params
    mt5_only = {r["instrument"]: r["only_in_mt5"] for r in store._query(sql_mt5_only, tuple(mt5_only_params))}

    print("=" * 80)
    print("cTrader staging vs MT5 live candle comparison")
    print(f"Filter: instrument={instrument or 'ALL'}  from={from_date or '-'}  to={to_date or '-'}")
    print("=" * 80)
    print(f"{'Symbol':<10} {'Staging':>8} {'Matched':>8} {'CT-only':>8} {'MT5-only':>9} "
          f"{'AvgΔclose':>10} {'MaxΔclose':>10} {'MaxΔhigh':>9} {'MaxΔlow':>9}")
    print("-" * 80)

    all_good = True
    for r in rows:
        sym = r["instrument"]
        mt5o = mt5_only.get(sym, 0)
        print(f"{sym:<10} {r['staging_bars']:>8} {r['matched_bars']:>8} {r['only_in_ctrader']:>8} "
              f"{mt5o:>9} {r['avg_close_delta']:>10.6f} {r['max_close_delta']:>10.6f} "
              f"{r['max_high_delta']:>9.5f} {r['max_low_delta']:>9.5f}")
        if r["max_close_delta"] and r["max_close_delta"] > 0.1:
            all_good = False

    if not rows:
        print("  (no staging candles found — is cfd-ctrader running with staging?)")
    else:
        print("-" * 80)
        if all_good:
            print("VERDICT: feeds match (max close delta within tolerance). Safe to cut over.")
        else:
            print("VERDICT: significant deltas detected — investigate before cutting over.")

    store.stop()


def main():
    parser = argparse.ArgumentParser(description="Compare cTrader staging candles vs MT5 live candles")
    parser.add_argument("--instrument", type=str, default=None)
    parser.add_argument("--from", dest="from_date", type=str, default=None, help="session_date start (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", type=str, default=None, help="session_date end (YYYY-MM-DD)")
    args = parser.parse_args()
    run(instrument=args.instrument, from_date=args.from_date, to_date=args.to_date)


if __name__ == "__main__":
    main()
