"""
Dukascopy CFD historical fetcher — 5m bid candles for the 10 CFD instruments.

Downloads Dukascopy history via the dukascopy-node CLI wrapper
(tools/dukascopy/fetch.js), builds/loads 5-minute BID candles, tags each with
FX trading-day + session (via forex_hours, identical to the live feed), and
stores them in research_db.cfd_historical_candles.

Design (mirrors the Groww fetcher app/backtest/fetch.py):
  * Chunked by day / month / year so you can fetch a small slice to verify
    before committing to the full 10-year pull.
  * Resumable — each completed chunk is marked in fetch_progress; re-running
    skips finished chunks (use --force to re-fetch).
  * Never dies mid-run — a failed chunk is retried with backoff, then logged
    and skipped so one bad chunk can't abort a multi-hour job.
  * Idempotent writes — ON CONFLICT DO NOTHING on (instrument, timeframe, ts).

Usage:
    # full 10 years, all 10 instruments (default range)
    python -m app.backtest.fetch_dukascopy

    # a single month for one instrument (verify before the big run)
    python -m app.backtest.fetch_dukascopy --instruments EURUSD --month 2020-06

    # a single day / a whole year / an explicit range
    python -m app.backtest.fetch_dukascopy --instruments XAUUSD --day 2022-03-15
    python -m app.backtest.fetch_dukascopy --instruments US500 --year 2021
    python -m app.backtest.fetch_dukascopy --from 2019-01-01 --to 2019-12-31

    # just print what's already stored (verification)
    python -m app.backtest.fetch_dukascopy --summary

Run it in the background with tools/dukascopy/fetch_cfd_history.sh (nohup +
logfile + pidfile) since the full pull takes hours.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from datetime import date, datetime, timedelta, timezone

from app.db.research_store import ResearchStore
from app.utils import forex_hours
from app.utils.logger import get_logger

logger = get_logger("backtest.fetch_dukascopy")

# ─── Canonical symbol → dukascopy-node instrument id ─────────────────────────
# Verified against dukascopy-node's instrument-meta-data (Jul 2026). Candles are
# stored under the canonical name (left) so they line up with live_candles.
SYMBOL_MAP: dict[str, str] = {
    "XAUUSD": "xauusd",        # Gold
    "XAGUSD": "xagusd",        # Silver
    "EURUSD": "eurusd",        # Euro
    "GBPUSD": "gbpusd",        # Pound
    "USDJPY": "usdjpy",        # Yen
    "US30":   "usa30idxusd",   # Dow Jones (US 30)
    "US500":  "usa500idxusd",  # S&P 500 (US 500)
    "USTEC":  "usatechidxusd", # Nasdaq 100 (US Tech)
    "DE40":   "deuidxeur",     # DAX / Germany 40 (EUR-quoted)
    "XTIUSD": "lightcmdusd",   # WTI / Light Sweet Crude Oil
}

# Earliest minute/5m data available per instrument (from dukascopy metadata).
# We clamp the fetch start to this so we don't hammer the server for periods
# that predate the instrument's history.
EARLIEST: dict[str, date] = {
    "XAUUSD": date(2003, 5, 5),
    "XAGUSD": date(2003, 5, 4),
    "EURUSD": date(2003, 5, 4),
    "GBPUSD": date(2003, 5, 4),
    "USDJPY": date(2003, 5, 4),
    "US30":   date(2013, 9, 30),
    "US500":  date(2011, 9, 18),
    "USTEC":  date(2011, 9, 18),
    "DE40":   date(2013, 9, 30),
    "XTIUSD": date(2011, 9, 23),
}

# Default 10-year window (inclusive end).
DEFAULT_FROM = date(2016, 8, 1)
DEFAULT_TO = date(2026, 7, 31)

TIMEFRAME = "5m"                 # stored timeframe (matches live_candles / Timeframe.M5)
PROGRESS_TF = "5m_cfd"           # fetch_progress key, kept distinct from NSE "5m"
DEFAULT_DELAY_MS = 250
MAX_CHUNK_RETRIES = 4
NODE_TIMEOUT_S = 900             # per-chunk node call ceiling (a month of 5m is small)

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SCRIPT = os.path.normpath(os.path.join(_HERE, "..", "..", "tools", "dukascopy", "fetch.js"))
_DEFAULT_CACHE = os.path.normpath(os.path.join(_HERE, "..", "..", "tools", "dukascopy", ".dukascache"))


# ─── Chunk period generation ─────────────────────────────────────────────────

def _first_of_next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def iter_periods(
    start_date: date, end_excl: date, granularity: str,
) -> list[tuple[date, date]]:
    """Yield (fetch_from, fetch_to_excl) chunks over [start_date, end_excl).

    ``fetch_from`` doubles as the resume marker (see fetch_instrument). It is
    the actual clamped start of the chunk, NOT the calendar-aligned period
    start — so a later, wider run re-fetches (idempotently) instead of skipping
    a partially-covered period, which would otherwise leave a silent gap.
    """
    periods: list[tuple[date, date]] = []
    if start_date >= end_excl:
        return periods

    if granularity == "day":
        cur = start_date
        while cur < end_excl:
            nxt = cur + timedelta(days=1)
            periods.append((cur, min(nxt, end_excl)))
            cur = nxt
    elif granularity == "year":
        cur = date(start_date.year, 1, 1)
        while cur < end_excl:
            nxt = date(cur.year + 1, 1, 1)
            periods.append((max(cur, start_date), min(nxt, end_excl)))
            cur = nxt
    else:  # month (default)
        cur = date(start_date.year, start_date.month, 1)
        while cur < end_excl:
            nxt = _first_of_next_month(cur)
            periods.append((max(cur, start_date), min(nxt, end_excl)))
            cur = nxt
    return periods


# ─── Node bridge ─────────────────────────────────────────────────────────────

class DukascopyFetcher:
    def __init__(
        self,
        store: ResearchStore,
        node_bin: str = "node",
        script_path: str = _DEFAULT_SCRIPT,
        cache_dir: str = _DEFAULT_CACHE,
        delay_ms: int = DEFAULT_DELAY_MS,
        force: bool = False,
    ) -> None:
        self._store = store
        self._node = node_bin
        self._script = script_path
        self._cache = cache_dir
        self._delay_ms = delay_ms
        self._force = force

        self.total_candles = 0
        self.chunks_done = 0
        self.chunks_skipped = 0
        self.errors = 0

        os.makedirs(self._cache, exist_ok=True)

    # ── one chunk via the node CLI, with retries ──
    def _run_node(self, duka_id: str, frm: date, to_excl: date, out_path: str) -> bool:
        cmd = [
            self._node, self._script,
            "--instrument", duka_id,
            "--from", frm.isoformat(),
            "--to", to_excl.isoformat(),
            "--out", out_path,
            "--cache", self._cache,
        ]
        for attempt in range(1, MAX_CHUNK_RETRIES + 1):
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=NODE_TIMEOUT_S,
                )
                if proc.returncode == 0:
                    return True
                stderr = (proc.stderr or "").strip()
                if "Cannot find module 'dukascopy-node'" in stderr:
                    logger.error(
                        "dukascopy-node not installed. Run: (cd tools/dukascopy && npm install)"
                    )
                    raise SystemExit(1)
                logger.warning(
                    "  node fetch rc=%d (attempt %d/%d) %s %s..%s: %s",
                    proc.returncode, attempt, MAX_CHUNK_RETRIES, duka_id,
                    frm, to_excl, stderr[-300:],
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "  node fetch TIMEOUT (attempt %d/%d) %s %s..%s",
                    attempt, MAX_CHUNK_RETRIES, duka_id, frm, to_excl,
                )
            if attempt < MAX_CHUNK_RETRIES:
                time.sleep(2 ** attempt)
        return False

    @staticmethod
    def _rows_from_json(raw: list) -> list[tuple]:
        """Convert dukascopy array rows to DB tuples with session tagging.

        Input row: [timestampMsUTC, open, high, low, close, volume]
        Output:    (timestamp_ms, open, high, low, close, volume, session_date, session)
        """
        out: list[tuple] = []
        for r in raw:
            if not r or len(r) < 5:
                continue
            ts, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
            if ts is None or o is None or h is None or l is None or c is None:
                continue
            vol = r[5] if len(r) > 5 and r[5] is not None else 0
            ts_ms = int(ts)
            dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
            session_date = forex_hours.trading_day(dt)
            session = forex_hours.session_tag(dt)
            out.append((ts_ms, float(o), float(h), float(l), float(c), int(round(float(vol))), session_date, session))
        return out

    def fetch_instrument(
        self, canonical: str, start_date: date, end_incl: date, granularity: str,
    ) -> dict:
        duka_id = SYMBOL_MAP.get(canonical)
        if duka_id is None:
            logger.error("Unknown instrument %s (known: %s)", canonical, list(SYMBOL_MAP))
            return {"instrument": canonical, "error": "unknown"}

        # Clamp start to the instrument's earliest available data.
        earliest = EARLIEST.get(canonical)
        clamped_start = max(start_date, earliest) if earliest else start_date
        if earliest and start_date < earliest:
            logger.info("  %s: no data before %s — clamping start", canonical, earliest)

        end_excl = end_incl + timedelta(days=1)
        periods = iter_periods(clamped_start, end_excl, granularity)

        logger.info(
            "═══ %s (%s): %d %s-chunks over %s..%s ═══",
            canonical, duka_id, len(periods), granularity, clamped_start, end_incl,
        )

        inst_candles = 0
        for frm, to_excl in periods:
            marker_str = frm.isoformat()  # resume marker = actual chunk start

            if not self._force and self._store.is_date_fetched(canonical, marker_str, PROGRESS_TF):
                self.chunks_skipped += 1
                continue

            fd, out_path = tempfile.mkstemp(prefix=f"duka_{canonical}_", suffix=".json", dir=self._cache)
            os.close(fd)
            try:
                ok = self._run_node(duka_id, frm, to_excl, out_path)
                if not ok:
                    logger.error("  FAILED chunk %s %s..%s (skipping)", canonical, frm, to_excl)
                    self.errors += 1
                    continue

                with open(out_path) as f:
                    raw = json.load(f)
                rows = self._rows_from_json(raw)

                if rows:
                    written = self._store.write_cfd_historical_candles_batch(rows, canonical, TIMEFRAME)
                    inst_candles += written
                    self.total_candles += written

                # Mark the chunk done even if empty (weekend/holiday span) so we
                # don't re-attempt it on resume.
                self._store.mark_fetched(canonical, marker_str, len(rows), PROGRESS_TF)
                self.chunks_done += 1

                if self.chunks_done % 10 == 0:
                    logger.info("  %s: %d candles so far (%d chunks done)", canonical, inst_candles, self.chunks_done)

            except Exception as e:  # noqa: BLE001 - never let one chunk kill the run
                logger.error("  chunk error %s %s..%s: %s", canonical, frm, to_excl, e)
                self.errors += 1
            finally:
                try:
                    os.remove(out_path)
                except OSError:
                    pass
                time.sleep(self._delay_ms / 1000.0)

        logger.info("═══ %s complete: %d candles ═══", canonical, inst_candles)
        return {"instrument": canonical, "candles": inst_candles}


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fetch Dukascopy 5m CFD history into cfd_historical_candles")
    p.add_argument("--from", dest="from_date", help="start date YYYY-MM-DD (inclusive)")
    p.add_argument("--to", dest="to_date", help="end date YYYY-MM-DD (inclusive)")
    p.add_argument("--year", help="convenience: fetch a whole year YYYY")
    p.add_argument("--month", help="convenience: fetch a whole month YYYY-MM")
    p.add_argument("--day", help="convenience: fetch a single day YYYY-MM-DD")
    p.add_argument("--instruments", help="CSV of canonical symbols (default: all 10)")
    p.add_argument("--granularity", choices=["day", "month", "year"], default="month",
                   help="chunk size for fetching + resume markers (default month)")
    p.add_argument("--force", action="store_true", help="re-fetch chunks even if already marked done")
    p.add_argument("--delay-ms", type=int, default=DEFAULT_DELAY_MS, help="pause between chunks (ms)")
    p.add_argument("--node", default="node", help="node binary (default 'node')")
    p.add_argument("--script", default=_DEFAULT_SCRIPT, help="path to fetch.js")
    p.add_argument("--cache", default=_DEFAULT_CACHE, help="dukascopy-node cache dir")
    p.add_argument("--summary", action="store_true", help="print stored CFD candle summary and exit")
    return p


def _resolve_range(args: argparse.Namespace) -> tuple[date, date, str]:
    """Return (from_date, to_date_inclusive, granularity) from the CLI flags."""
    gran = args.granularity
    if args.day:
        d = date.fromisoformat(args.day)
        return d, d, "day"
    if args.month:
        y, m = args.month.split("-")
        start = date(int(y), int(m), 1)
        end = _first_of_next_month(start) - timedelta(days=1)
        return start, end, "month"
    if args.year:
        y = int(args.year)
        return date(y, 1, 1), date(y, 12, 31), "year"
    frm = date.fromisoformat(args.from_date) if args.from_date else DEFAULT_FROM
    to = date.fromisoformat(args.to_date) if args.to_date else DEFAULT_TO
    return frm, to, gran


def _print_summary(store: ResearchStore) -> None:
    rows = store.get_cfd_historical_summary(TIMEFRAME)
    print(f"\n  {'INSTRUMENT':<10} {'CANDLES':>10}  {'FIRST':<12} {'LAST':<12}")
    print(f"  {'-'*10} {'-'*10}  {'-'*12} {'-'*12}")
    total = 0
    for r in rows:
        total += r["candles"]
        print(f"  {r['instrument']:<10} {r['candles']:>10,}  {str(r['first_date']):<12} {str(r['last_date']):<12}")
    print(f"  {'-'*10} {'-'*10}")
    print(f"  {'TOTAL':<10} {total:>10,}  ({len(rows)} instruments)\n")


def main() -> None:
    args = _build_parser().parse_args()

    store = ResearchStore()
    store.start()

    if args.summary:
        _print_summary(store)
        store.stop()
        return

    from_date, to_date, gran = _resolve_range(args)

    if args.instruments:
        instruments = [s.strip().upper() for s in args.instruments.split(",") if s.strip()]
    else:
        instruments = list(SYMBOL_MAP.keys())

    print("=" * 70)
    print("  DUKASCOPY CFD HISTORICAL FETCHER (5m bid)")
    print("=" * 70)
    print(f"  Range:       {from_date} → {to_date}  (granularity: {gran})")
    print(f"  Instruments: {', '.join(instruments)}")
    print(f"  Store:       {'postgres' if store.is_postgres else 'sqlite'}")
    print(f"  Force:       {args.force}")
    print("=" * 70)

    fetcher = DukascopyFetcher(
        store, node_bin=args.node, script_path=args.script,
        cache_dir=args.cache, delay_ms=args.delay_ms, force=args.force,
    )

    t0 = time.time()
    for inst in instruments:
        try:
            fetcher.fetch_instrument(inst, from_date, to_date, gran)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - keep going to the next instrument
            logger.error("Fatal error on %s: %s", inst, e)

    elapsed = time.time() - t0
    print(f"\n{'═' * 70}")
    print("  FETCH COMPLETE")
    print(f"{'═' * 70}")
    print(f"  Candles written: {fetcher.total_candles:,}")
    print(f"  Chunks done:     {fetcher.chunks_done}")
    print(f"  Chunks skipped:  {fetcher.chunks_skipped} (already fetched)")
    print(f"  Errors:          {fetcher.errors}")
    print(f"  Time:            {elapsed:.0f}s ({elapsed/60:.1f} min)")
    _print_summary(store)
    store.stop()


if __name__ == "__main__":
    main()
