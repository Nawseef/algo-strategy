"""
Full backtest pipeline — fetch + replay + scoring in one command.

Usage:
    python -m app.backtest.full --from 2021-01-01 --to 2026-06-01 --instruments NIFTY,BANKNIFTY,2885
    python -m app.backtest.full --from 2024-01-01 --to 2024-12-31  # all instruments

Steps:
    1. Fetch missing historical candles from Groww API
    2. Replay through the 150K variant pipeline
    3. Run exit engine per day (already done in replay)
    4. Print summary + suggest scoring command
"""

from __future__ import annotations

import sys
import time
from datetime import date

from app.db.research_store import ResearchStore
from app.utils.config import load_config
from app.utils.logger import get_logger

logger = get_logger("backtest.full")


def main() -> None:
    """Full backtest pipeline."""
    print("=" * 70)
    print("  FULL BACKTEST PIPELINE")
    print("  Fetch → Replay → Exit Simulation → Ready for Scoring")
    print("=" * 70)

    # Parse args
    args = sys.argv[1:]
    start_date = date(2024, 1, 1)
    end_date = date.today()
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
        elif args[i] in ("--instruments", "--instrument") and i + 1 < len(args):
            instruments = [x.strip().upper() for x in args[i + 1].split(",")]
            i += 2
        elif args[i] == "--skip-fetch":
            skip_fetch = True
            i += 1
        else:
            i += 1

    print(f"\n  Date range: {start_date} → {end_date}")
    if instruments:
        print(f"  Instruments: {', '.join(instruments)}")
    else:
        print(f"  Instruments: ALL")
    print()

    t_total = time.time()

    # ─── Step 1: Fetch ───────────────────────────────────────────────────
    if not skip_fetch:
        print("─── STEP 1: Fetching Historical Data ───")
        try:
            from app.backtest.fetch import HistoricalFetcher, INSTRUMENT_MAP
            from app.broker.groww import GrowwBroker

            config = load_config()
            broker = GrowwBroker(config.groww)

            print("  Authenticating...")
            broker.authenticate()
            print("  ✅ Authenticated")

            store = ResearchStore()
            store.start()

            fetcher = HistoricalFetcher(broker, store)

            fetch_instruments = instruments or list(INSTRUMENT_MAP.keys())
            for inst in fetch_instruments:
                fetcher.fetch_instrument(inst, start_date, end_date)

            stats = fetcher.get_stats()
            print(f"  ✅ Fetch complete: {stats['total_candles']:,} candles")
            store.stop()

        except ImportError as e:
            print(f"  ⚠️ Groww SDK not available ({e}). Skipping fetch.")
            print("     Run on VM with growwapi installed, or use --skip-fetch")
        except Exception as e:
            print(f"  ❌ Fetch error: {e}")
            print("     Continuing with whatever data is already in DB...")
    else:
        print("─── STEP 1: Fetch SKIPPED (--skip-fetch) ───")

    # ─── Step 2: Replay ──────────────────────────────────────────────────
    print("\n─── STEP 2: Replay Through Pipeline ───")

    from app.backtest.replay import BacktestReplayEngine

    store = ResearchStore()
    store.start()

    try:
        engine = BacktestReplayEngine(store, instruments)
        results = engine.run(start_date, end_date)

        print(f"  ✅ Replay complete:")
        print(f"     Days: {results['days_processed']}")
        print(f"     Trades: {results['trades']:,}")
        print(f"     Time: {results['time_seconds']:.0f}s")

    except Exception as e:
        logger.error("Replay failed: %s", e, exc_info=True)
        print(f"  ❌ Replay error: {e}")
        results = {"days_processed": 0, "trades": 0, "time_seconds": 0}
    finally:
        store.stop()

    # ─── Summary ─────────────────────────────────────────────────────────
    total_time = time.time() - t_total

    print(f"\n{'═' * 70}")
    print(f"  FULL BACKTEST COMPLETE")
    print(f"{'═' * 70}")
    print(f"  Total time:  {total_time:.0f}s ({total_time/60:.1f} min)")
    print(f"  Days:        {results['days_processed']}")
    print(f"  Trades:      {results['trades']:,}")
    print(f"\n  ─── NEXT STEPS ───")
    print(f"  Run scoring:")
    print(f"    python -m app.scoring.run --from {start_date} --to {end_date}")
    print(f"\n  Score specific period:")
    print(f"    python -m app.scoring.run --from {start_date} --to {end_date} --top 50")
    print(f"{'═' * 70}")


if __name__ == "__main__":
    main()
