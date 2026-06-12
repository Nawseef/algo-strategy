"""
CLI for backtest replay only (assumes data already fetched).

Usage:
    python -m app.backtest.run --from 2024-01-01 --to 2024-12-31
    python -m app.backtest.run --from 2024-01-01 --to 2024-03-31 --instruments NIFTY,BANKNIFTY
"""

from __future__ import annotations

import sys
from datetime import date

from app.backtest.replay import BacktestReplayEngine
from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

logger = get_logger("backtest.run")


def main() -> None:
    """CLI entry point for backtest replay."""
    print("=" * 70)
    print("  BACKTEST REPLAY ENGINE")
    print("=" * 70)

    # Parse args
    args = sys.argv[1:]
    start_date = date(2024, 1, 1)
    end_date = date(2024, 12, 31)
    instruments: list[str] | None = None

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
        else:
            i += 1

    print(f"\n  Date range: {start_date} → {end_date}")
    if instruments:
        print(f"  Instruments: {', '.join(instruments)}")
    else:
        print(f"  Instruments: ALL (10 + VIX)")

    # Initialize
    store = ResearchStore()
    store.start()

    engine = BacktestReplayEngine(store, instruments)

    # Run
    try:
        results = engine.run(start_date, end_date)

        # Summary
        print(f"\n{'═' * 70}")
        print(f"  BACKTEST RESULTS")
        print(f"{'═' * 70}")
        print(f"  Run ID:          {results['run_id']}")
        print(f"  Days processed:  {results['days_processed']}")
        print(f"  Trades created:  {results['trades']}")
        print(f"  Candles replayed:{results['candles']:,}")
        print(f"  Total time:      {results['time_seconds']:.1f}s ({results['time_seconds']/60:.1f} min)")
        if results['days_processed'] > 0:
            print(f"  Rate:            {results['days_processed']/results['time_seconds']:.1f} days/sec")
            print(f"  Trades/day avg:  {results['trades']/results['days_processed']:.0f}")
        print(f"\n  Next step: python -m app.scoring.run --from {start_date} --to {end_date}")
        print(f"{'═' * 70}")

    except KeyboardInterrupt:
        print("\n  Interrupted. Progress saved — resume with same command.")
    except Exception as e:
        logger.error("Backtest failed: %s", e, exc_info=True)
        print(f"\n  ❌ Error: {e}")
    finally:
        store.stop()


if __name__ == "__main__":
    main()
