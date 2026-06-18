"""
Parallel Backtest Launcher — splits date range across N workers.

Divides the full date range into N equal chunks and runs each chunk
as an independent process. Each worker has its own replay engine,
writes to the same DB (trade_ids are UUIDs, no conflicts).

Usage:
    python -m app.backtest.parallel --from 2021-01-01 --to 2026-06-12 --workers 2
    python -m app.backtest.parallel --from 2021-01-01 --to 2026-06-12 --workers 2 --skip-fetch
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from multiprocessing import Process

from app.utils.logger import get_logger

logger = get_logger("backtest.parallel")


def run_chunk(start_date: date, end_date: date, worker_id: int, instruments: list[str] | None) -> None:
    """Run a single chunk of the backtest in its own process."""
    import os
    os.environ["LOG_LEVEL"] = "WARNING"  # Suppress TRIGGER/DEBUG spam for speed

    from app.db.research_store import ResearchStore
    from app.backtest.replay import BacktestReplayEngine

    print(f"  [Worker {worker_id}] Starting: {start_date} → {end_date}")

    store = ResearchStore()
    store.start()

    try:
        engine = BacktestReplayEngine(store, instruments)
        results = engine.run(start_date, end_date, run_id=f"BT-W{worker_id}")
        print(
            f"  [Worker {worker_id}] Done: {results['days_processed']} days, "
            f"{results['trades']:,} trades in {results['time_seconds']:.0f}s"
        )
    except Exception as e:
        print(f"  [Worker {worker_id}] ERROR: {e}")
    finally:
        store.stop()


def main() -> None:
    """Launch parallel backtest workers."""
    print("=" * 70)
    print("  PARALLEL BACKTEST LAUNCHER")
    print("=" * 70)

    args = sys.argv[1:]
    start_date = date(2021, 1, 1)
    end_date = date.today()
    num_workers = 2
    instruments: list[str] | None = None
    skip_fetch = False

    i = 0
    while i < len(args):
        if args[i] == "--from" and i + 1 < len(args):
            start_date = date.fromisoformat(args[i + 1])
            i += 2
        elif args[i] == "--to" and i + 1 < len(args):
            end_date = date.fromisoformat(args[i + 1])
            i += 2
        elif args[i] == "--workers" and i + 1 < len(args):
            num_workers = int(args[i + 1])
            i += 2
        elif args[i] in ("--instruments", "--instrument") and i + 1 < len(args):
            instruments = [x.strip().upper() for x in args[i + 1].split(",")]
            i += 2
        elif args[i] == "--skip-fetch":
            skip_fetch = True
            i += 1
        else:
            i += 1

    # Split date range into chunks
    total_days = (end_date - start_date).days
    chunk_size = total_days // num_workers

    chunks: list[tuple[date, date]] = []
    for w in range(num_workers):
        chunk_start = start_date + timedelta(days=w * chunk_size)
        if w == num_workers - 1:
            chunk_end = end_date  # last worker takes remainder
        else:
            chunk_end = start_date + timedelta(days=(w + 1) * chunk_size - 1)
        chunks.append((chunk_start, chunk_end))

    print(f"\n  Date range:  {start_date} → {end_date} ({total_days} days)")
    print(f"  Workers:     {num_workers}")
    print(f"  Instruments: {instruments or 'ALL'}")
    print(f"  Skip fetch:  {skip_fetch}")
    print(f"\n  Chunks:")
    for w, (cs, ce) in enumerate(chunks):
        days = (ce - cs).days + 1
        print(f"    Worker {w}: {cs} → {ce} ({days} days)")

    # Fetch step (single-threaded, skip if flag set)
    if not skip_fetch:
        print("\n─── Fetching Historical Data (single thread) ───")
        try:
            from app.backtest.fetch import HistoricalFetcher, INSTRUMENT_MAP
            from app.broker.groww import GrowwBroker
            from app.db.research_store import ResearchStore
            from app.utils.config import load_config

            config = load_config()
            broker = GrowwBroker(config.groww)
            broker.authenticate()
            print("  ✅ Authenticated")

            store = ResearchStore()
            store.start()
            fetcher = HistoricalFetcher(broker, store)
            fetch_instruments = instruments or list(INSTRUMENT_MAP.keys())
            for inst in fetch_instruments:
                fetcher.fetch_instrument(inst, start_date, end_date)
            store.stop()
            print("  ✅ Fetch complete")
        except Exception as e:
            print(f"  ⚠️ Fetch error: {e} — continuing with existing data")
    else:
        print("\n─── Fetch SKIPPED ───")

    # Launch workers
    print(f"\n─── Launching {num_workers} parallel replay workers ───")
    t0 = time.time()

    processes: list[Process] = []
    for w, (cs, ce) in enumerate(chunks):
        p = Process(target=run_chunk, args=(cs, ce, w, instruments))
        p.start()
        processes.append(p)
        print(f"  Worker {w} started (PID: {p.pid})")

    # Wait for all to complete
    for p in processes:
        p.join()

    elapsed = time.time() - t0

    # ─── Post-processing: fill htf_trend_1h from EMA slopes ─────────────
    # The 30m warmup doesn't have enough history for EMA50, so htf_trend_1h
    # stays empty during replay. Derive it from stored ema_20_slope/ema_50_slope.
    print(f"\n─── Post-fill: Computing htf_trend_1h from EMA slopes ───")
    try:
        from app.db.research_store import ResearchStore
        store = ResearchStore()
        store.start()
        store._query("""
            UPDATE trades SET htf_trend_1h = CASE
                WHEN ema_20_slope > 0 AND ema_50_slope > 0 THEN 'BULLISH'
                WHEN ema_20_slope < 0 AND ema_50_slope < 0 THEN 'BEARISH'
                ELSE 'NEUTRAL'
            END
            WHERE htf_trend_1h = '' OR htf_trend_1h IS NULL
        """, ())
        store.stop()
        print("  ✅ htf_trend_1h filled")
    except Exception as e:
        print(f"  ⚠️ htf_trend_1h post-fill failed: {e}")
        print("  Run manually: UPDATE trades SET htf_trend_1h = CASE WHEN ema_20_slope > 0 AND ema_50_slope > 0 THEN 'BULLISH' WHEN ema_20_slope < 0 AND ema_50_slope < 0 THEN 'BEARISH' ELSE 'NEUTRAL' END WHERE htf_trend_1h = '';")

    print(f"\n{'═' * 70}")
    print(f"  ALL WORKERS COMPLETE in {elapsed:.0f}s ({elapsed/3600:.1f} hours)")
    print(f"{'═' * 70}")


if __name__ == "__main__":
    main()
