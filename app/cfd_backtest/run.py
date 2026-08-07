"""
CFD backtest CLI — run registered strategies over stored 5m history.

A thin command-line wrapper around ``CFDBacktestReplay`` so you can iterate on
strategies without writing a Python script each time. Strategies are discovered
from the registry (importing ``app.cfd_strategy.strategies`` self-registers
them), selected by id, and run over ``cfd_historical_candles`` for a date range.

Examples:
    # One strategy, one instrument, a 3-year window:
    python -m app.cfd_backtest.run --strategy sma_cross_demo \
        --instruments XAUUSD --from 2023-01-01 --to 2025-12-31

    # All registered strategies over several instruments, compounding:
    python -m app.cfd_backtest.run --strategy all \
        --instruments XAUUSD,EURUSD,US30 --from 2024-01-01 --to 2024-12-31 \
        --compound

    # List what's registered / what data exists:
    python -m app.cfd_backtest.run --list
    python -m app.cfd_backtest.run --summary

Notes:
  * ``--persist`` writes each trade to ``cfd_paper_trades`` (mode=BACKTEST).
  * ``--cost-model`` is intraday (default) | conservative | zero.
  * Dates are inclusive and interpreted as UTC calendar days.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from app.cfd_backtest.replay import BacktestConfig, CFDBacktestReplay
from app.cfd_risk.costs import (
    COST_MODEL_CONSERVATIVE,
    COST_MODEL_INTRADAY,
    COST_MODEL_ZERO,
)
from app.cfd_strategy.base import CFDStrategy
from app.cfd_strategy.registry import get_registry
from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

# Registers all strategies via their @register_strategy decorators.
import app.cfd_strategy.strategies  # noqa: F401

logger = get_logger("cfd_backtest.run")

_COST_MODELS = {
    "intraday": COST_MODEL_INTRADAY,
    "conservative": COST_MODEL_CONSERVATIVE,
    "zero": COST_MODEL_ZERO,
}

_DEFAULT_INSTRUMENTS = [
    "XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY",
    "US30", "US500", "USTEC", "DE40", "XTIUSD",
]


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date '{s}' (expected YYYY-MM-DD)")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.cfd_backtest.run",
        description="Backtest CFD strategies over stored 5m historical candles.",
    )
    p.add_argument(
        "--strategy", "-s", default="all",
        help="Strategy id to run, comma-separated list, or 'all' (default: all).",
    )
    p.add_argument(
        "--instruments", "-i", default=",".join(_DEFAULT_INSTRUMENTS),
        help="Comma-separated instruments (default: all 10 CFD symbols).",
    )
    p.add_argument("--from", dest="start", type=_parse_date,
                   help="Start date YYYY-MM-DD (inclusive).")
    p.add_argument("--to", dest="end", type=_parse_date,
                   help="End date YYYY-MM-DD (inclusive).")
    p.add_argument("--balance", type=float, default=100_000.0,
                   help="Starting balance USD (default 100000).")
    p.add_argument("--risk", type=float, default=1.0,
                   help="Risk per trade %% (default 1.0).")
    p.add_argument("--compound", action="store_true",
                   help="Size off the running balance (default: fixed starting balance).")
    p.add_argument("--cost-model", default="intraday", choices=sorted(_COST_MODELS),
                   help="Cost model (default intraday).")
    p.add_argument("--persist", action="store_true",
                   help="Write trades to cfd_paper_trades (mode=BACKTEST).")
    p.add_argument("--list", action="store_true",
                   help="List registered strategies and exit.")
    p.add_argument("--summary", action="store_true",
                   help="Print per-instrument stored-candle summary and exit.")
    return p


def _select_strategies(spec: str) -> list[CFDStrategy]:
    registry = get_registry()
    if spec.strip().lower() == "all":
        return registry.all()
    selected: list[CFDStrategy] = []
    for sid in (s.strip() for s in spec.split(",") if s.strip()):
        try:
            selected.append(registry.get(sid))
        except KeyError:
            logger.error("Unknown strategy id '%s' (registered: %s)", sid, registry.ids())
    return selected


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    registry = get_registry()

    # --list needs only the registry, not the database.
    if args.list:
        ids = registry.ids()
        if not ids:
            print("No strategies registered.")
        else:
            print("Registered strategies:")
            for s in registry.all():
                print(f"  {s.strategy_id:20s} tf={s.timeframe.value} "
                      f"instruments={s.instruments or 'ALL'}  ({s.name})")
        return 0

    # Validate arguments BEFORE touching the database (fail fast, no DB needed),
    # unless we're only printing the stored-data summary.
    strategies: list[CFDStrategy] = []
    instruments: list[str] = []
    if not args.summary:
        strategies = _select_strategies(args.strategy)
        if not strategies:
            print("No strategies selected. Use --list to see registered ids.", file=sys.stderr)
            return 2
        if args.start is None or args.end is None:
            print("Both --from and --to are required for a backtest.", file=sys.stderr)
            return 2
        if args.end < args.start:
            print("--to must be on or after --from.", file=sys.stderr)
            return 2
        instruments = [i.strip() for i in args.instruments.split(",") if i.strip()]

    store = ResearchStore()
    store.start()
    try:
        if args.summary:
            rows = store.get_cfd_historical_summary("5m")
            if not rows:
                print("No CFD historical candles stored.")
            else:
                print(f"{'instrument':10s} {'candles':>10s}  {'first':>12s}  {'last':>12s}")
                for r in rows:
                    print(f"{r['instrument']:10s} {r['candles']:>10,}  "
                          f"{str(r['first_date']):>12s}  {str(r['last_date']):>12s}")
            return 0

        cfg = BacktestConfig(
            starting_balance=args.balance,
            risk_pct=args.risk,
            compound=args.compound,
            cost_model=_COST_MODELS[args.cost_model],
            persist=args.persist,
        )
        replay = CFDBacktestReplay(strategies, store, cfg)
        result = replay.run(instruments, args.start, args.end)

        print()
        print("=" * 72)
        print(f"CFD BACKTEST  {args.start} .. {args.end}")
        print(f"Strategies : {', '.join(s.strategy_id for s in strategies)}")
        print(f"Instruments: {', '.join(instruments)}")
        print(f"Cost model : {args.cost_model} | risk {args.risk:.2f}% | "
              f"compound={args.compound}")
        print("-" * 72)
        print(result.summary_text())
        print("=" * 72)
        return 0
    finally:
        store.stop()


if __name__ == "__main__":
    raise SystemExit(main())
