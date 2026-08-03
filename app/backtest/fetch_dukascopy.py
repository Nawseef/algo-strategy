"""
Dukascopy CFD historical fetcher — 10 years of 5m bid candles for the 10 CFDs.

Fetches Dukascopy's binary .bi5 tick feed directly (app/backtest/dukascopy_bi5.py),
aggregates to 5-minute BID candles, tags each with FX trading-day + session
(via forex_hours, identical to the live feed), and stores them in
research_db.cfd_historical_candles. The .bi5 feed is reliable for every
instrument, including the index/oil CFDs whose aggregated endpoints are patchy.

Built to run unattended for hours without dying:
  * Chunked by day; resumable — finished days are skipped on re-run.
  * A day that can't be fully fetched (some hourly files unavailable) is left
    UN-marked and retried on the next run, so a day with holes is never stored.
  * One bad day is logged and skipped — it never aborts the whole run.
  * Idempotent writes (ON CONFLICT DO NOTHING) — safe to re-run any range.

Usage:
    python -m app.backtest.fetch_dukascopy                     # full 10y, all 10
    python -m app.backtest.fetch_dukascopy --month 2020-06     # one month
    python -m app.backtest.fetch_dukascopy --day 2024-06-11 --instruments US30
    python -m app.backtest.fetch_dukascopy --year 2021
    python -m app.backtest.fetch_dukascopy --from 2019-01-01 --to 2019-12-31
    python -m app.backtest.fetch_dukascopy --summary           # show stored counts

Run in the background (survives disconnects): tools/dukascopy/fetch_cfd_history.sh
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta, timezone

from app.backtest import dukascopy_bi5
from app.db.research_store import ResearchStore
from app.utils import forex_hours
from app.utils.logger import get_logger

logger = get_logger("backtest.fetch_dukascopy")

# ─── Canonical symbol → Dukascopy .bi5 instrument name + price point ─────────
# point: integer price in the .bi5 file * point = real price (verified via the
# jetta "multiplier" field). Candles are stored under the canonical name so they
# line up with live_candles.
BI5_NAME: dict[str, str] = {
    "XAUUSD": "XAUUSD",         # Gold
    "XAGUSD": "XAGUSD",         # Silver
    "EURUSD": "EURUSD",         # Euro
    "GBPUSD": "GBPUSD",         # Pound
    "USDJPY": "USDJPY",         # Yen
    "US30":   "USA30IDXUSD",    # Dow Jones
    "US500":  "USA500IDXUSD",   # S&P 500
    "USTEC":  "USATECHIDXUSD",  # Nasdaq 100
    "DE40":   "DEUIDXEUR",      # DAX / Germany 40 (EUR-quoted)
    "XTIUSD": "LIGHTCMDUSD",    # WTI / Light Sweet Crude Oil
}
BI5_POINT: dict[str, float] = {
    "XAUUSD": 0.001, "XAGUSD": 0.001, "EURUSD": 0.00001, "GBPUSD": 0.00001,
    "USDJPY": 0.001, "US30": 0.001, "US500": 0.001, "USTEC": 0.001,
    "DE40": 0.001, "XTIUSD": 0.001,
}

# Earliest available .bi5 history per instrument — clamp the start so we don't
# hammer the server for periods that predate the instrument.
EARLIEST: dict[str, date] = {
    "XAUUSD": date(2003, 5, 5), "XAGUSD": date(2003, 5, 4), "EURUSD": date(2003, 5, 4),
    "GBPUSD": date(2003, 5, 4), "USDJPY": date(2003, 5, 4),
    "US30": date(2013, 9, 30), "US500": date(2011, 9, 18), "USTEC": date(2011, 9, 18),
    "DE40": date(2013, 9, 30), "XTIUSD": date(2011, 9, 23),
}

DEFAULT_FROM = date(2016, 8, 1)
DEFAULT_TO = date(2026, 7, 31)

TIMEFRAME = "5m"          # stored timeframe (matches live_candles / Timeframe.M5)
PROGRESS_TF = "5m_cfd"    # fetch_progress key, kept distinct from NSE "5m"
DEFAULT_DELAY_MS = 100    # gentle pause between day-fetches (avoid rate limiting)
DEFAULT_WORKERS = 16      # concurrent hourly fetches per day


def _iter_days(start_date: date, end_incl: date):
    """Yield each date in [start_date, end_incl] (chunk = one UTC day)."""
    cur = start_date
    while cur <= end_incl:
        yield cur
        cur += timedelta(days=1)


class DukascopyFetcher:
    def __init__(
        self,
        store: ResearchStore,
        delay_ms: int = DEFAULT_DELAY_MS,
        workers: int = DEFAULT_WORKERS,
        force: bool = False,
    ) -> None:
        self._store = store
        self._delay_ms = delay_ms
        self._workers = workers
        self._force = force

        self.total_candles = 0
        self.days_done = 0
        self.days_skipped = 0
        self.errors = 0

    @staticmethod
    def _tag(candles) -> list[tuple]:
        """Add FX trading-day + session tags to raw (ts,o,h,l,c,vol) candles.

        Output: (timestamp_ms, open, high, low, close, volume, session_date, session)
        """
        out: list[tuple] = []
        for c in candles:
            ts = int(c[0])
            dt = datetime.fromtimestamp(ts / 1000, timezone.utc)
            out.append((ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]),
                        int(c[5]), forex_hours.trading_day(dt), forex_hours.session_tag(dt)))
        return out

    def fetch_instrument(self, canonical: str, start_date: date, end_incl: date) -> None:
        bi5_name = BI5_NAME.get(canonical)
        if bi5_name is None:
            logger.error("Unknown instrument %s (known: %s)", canonical, list(BI5_NAME))
            return
        point = BI5_POINT[canonical]

        earliest = EARLIEST.get(canonical)
        clamped = max(start_date, earliest) if earliest else start_date
        if earliest and start_date < earliest:
            logger.info("  %s: no data before %s — clamping start", canonical, earliest)

        days = list(_iter_days(clamped, end_incl))
        logger.info("═══ %s (%s): %d days over %s..%s ═══", canonical, bi5_name, len(days), clamped, end_incl)

        inst_candles = 0
        for i, day in enumerate(days, 1):
            marker = day.isoformat()
            if not self._force and self._store.is_date_fetched(canonical, marker, PROGRESS_TF):
                self.days_skipped += 1
                continue
            try:
                candles, failed = dukascopy_bi5.fetch_day_m5(bi5_name, day, point, self._workers)
                if failed:
                    # incomplete day -> don't mark done, retry next run (no silent holes)
                    logger.error("  INCOMPLETE %s %s: %d/24 hours unfetched (retry later)", canonical, day, failed)
                    self.errors += 1
                    continue
                rows = self._tag(candles)
                if rows:
                    written = self._store.write_cfd_historical_candles_batch(rows, canonical, TIMEFRAME)
                    inst_candles += written
                    self.total_candles += written
                # mark done even if empty (weekend/holiday) so we don't re-attempt it
                self._store.mark_fetched(canonical, marker, len(rows), PROGRESS_TF)
                self.days_done += 1
                if self.days_done % 50 == 0:
                    logger.info("  %s: %d candles so far (%d/%d days)", canonical, inst_candles, i, len(days))
            except Exception as e:  # noqa: BLE001 - never let one day kill the run
                logger.error("  day error %s %s: %s", canonical, day, e)
                self.errors += 1
            finally:
                if self._delay_ms:
                    time.sleep(self._delay_ms / 1000.0)

        logger.info("═══ %s complete: %d candles ═══", canonical, inst_candles)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch Dukascopy 5m CFD history (.bi5) into cfd_historical_candles")
    p.add_argument("--from", dest="from_date", help="start date YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="to_date", help="end date YYYY-MM-DD (inclusive)")
    p.add_argument("--year", help="convenience: a whole year YYYY")
    p.add_argument("--month", help="convenience: a whole month YYYY-MM")
    p.add_argument("--day", help="convenience: a single day YYYY-MM-DD")
    p.add_argument("--instruments", help="CSV of canonical symbols (default: all 10)")
    p.add_argument("--force", action="store_true", help="re-fetch days even if already marked done")
    p.add_argument("--delay-ms", type=int, default=DEFAULT_DELAY_MS, help="pause between day-fetches (ms)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="concurrent hourly fetches per day")
    p.add_argument("--summary", action="store_true", help="print stored CFD candle counts and exit")
    return p


def _first_of_next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _resolve_range(args: argparse.Namespace) -> tuple[date, date]:
    if args.day:
        d = date.fromisoformat(args.day)
        return d, d
    if args.month:
        y, m = args.month.split("-")
        start = date(int(y), int(m), 1)
        return start, _first_of_next_month(start) - timedelta(days=1)
    if args.year:
        y = int(args.year)
        return date(y, 1, 1), date(y, 12, 31)
    frm = date.fromisoformat(args.from_date) if args.from_date else DEFAULT_FROM
    to = date.fromisoformat(args.to_date) if args.to_date else DEFAULT_TO
    return frm, to


def _print_summary(store: ResearchStore) -> None:
    rows = store.get_cfd_historical_summary(TIMEFRAME)
    print(f"\n  {'INSTRUMENT':<10} {'CANDLES':>10}  {'FIRST':<12} {'LAST':<12}")
    print(f"  {'-'*10} {'-'*10}  {'-'*12} {'-'*12}")
    total = 0
    for r in rows:
        total += r["candles"]
        print(f"  {r['instrument']:<10} {r['candles']:>10,}  {str(r['first_date']):<12} {str(r['last_date']):<12}")
    print(f"  {'-'*10} {'-'*10}\n  {'TOTAL':<10} {total:>10,}  ({len(rows)} instruments)\n")


def main() -> None:
    args = _build_parser().parse_args()

    store = ResearchStore()
    store.start()

    if args.summary:
        _print_summary(store)
        store.stop()
        return

    from_date, to_date = _resolve_range(args)
    instruments = ([s.strip().upper() for s in args.instruments.split(",") if s.strip()]
                   if args.instruments else list(BI5_NAME.keys()))

    print("=" * 70)
    print("  DUKASCOPY CFD HISTORICAL FETCHER (5m bid, .bi5)")
    print("=" * 70)
    print(f"  Range:       {from_date} → {to_date}")
    print(f"  Instruments: {', '.join(instruments)}")
    print(f"  Store:       {'postgres' if store.is_postgres else 'sqlite'}   workers: {args.workers}   force: {args.force}")
    print("=" * 70)

    fetcher = DukascopyFetcher(store, delay_ms=args.delay_ms, workers=args.workers, force=args.force)

    t0 = time.time()
    for inst in instruments:
        try:
            fetcher.fetch_instrument(inst, from_date, to_date)
        except Exception as e:  # noqa: BLE001 - keep going to the next instrument
            logger.error("Fatal error on %s: %s", inst, e)

    elapsed = time.time() - t0
    print(f"\n{'═' * 70}\n  FETCH COMPLETE\n{'═' * 70}")
    print(f"  Candles written: {fetcher.total_candles:,}")
    print(f"  Days done:       {fetcher.days_done}")
    print(f"  Days skipped:    {fetcher.days_skipped} (already fetched)")
    print(f"  Errors:          {fetcher.errors}")
    print(f"  Time:            {elapsed:.0f}s ({elapsed/60:.1f} min)")
    _print_summary(store)
    store.stop()


if __name__ == "__main__":
    main()
