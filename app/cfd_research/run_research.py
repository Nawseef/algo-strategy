"""
CFD research CLI — run the whole pipeline over the stored 10-year data.

    entry strategy (Session ORB)
      -> entry replay + exit sweep      (every entry x every exit model, tagged)
      -> slice scorer                   (challenge pass-rate per slice)
      -> ranked report

Example (on the VM, where Postgres + the 10y candles live):
    python -m app.cfd_research.run_research \
        --instruments XAUUSD,US30,EURUSD --from 2016-08-01 --to 2026-07-31 \
        --sessions london,new_york --dimensions instrument,session,exit_model

    # slice deeper (by market regime too):
    python -m app.cfd_research.run_research --instruments XAUUSD \
        --from 2020-01-01 --to 2025-12-31 --dimensions instrument,session,regime,exit_model

Notes:
    * Entries are UNCONSTRAINED (all recorded); prop-firm caps are applied by the
      challenge sim during scoring. Risk is swept (default 0.5% and 1%).
    * The generic challenge ruleset defaults to 8%/5% targets, 5% daily / 10% max
      DD (static). Override per firm with the flags.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

from app.cfd_research.challenge_sim import ChallengeRules
from app.cfd_research.entries.session_orb import SessionORB
from app.cfd_research.entry_replay import replay_entries
from app.cfd_research.slice_scorer import format_slices, score_slices
from app.cfd_risk.costs import (
    COST_MODEL_CONSERVATIVE,
    COST_MODEL_INTRADAY,
    COST_MODEL_SESSION_OPEN,
    COST_MODEL_ZERO,
)
from app.core.models import Candle, Timeframe
from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

logger = get_logger("cfd_research.run")

_COST_MODELS = {
    "session_open": COST_MODEL_SESSION_OPEN,
    "intraday": COST_MODEL_INTRADAY,
    "conservative": COST_MODEL_CONSERVATIVE,
    "zero": COST_MODEL_ZERO,
}
_DEFAULT_INSTRUMENTS = ["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USDJPY",
                        "US30", "US500", "USTEC", "DE40", "XTIUSD"]


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date '{s}' (expected YYYY-MM-DD)")


def _date_to_ms(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000


def _load_candles(store: ResearchStore, instrument: str, start_ms: float, end_ms: float) -> list[Candle]:
    rows = store.get_cfd_historical_candles(instrument, "5m", start_ms, end_ms)
    return [
        Candle(exchange="ICMARKETS", segment="CFD", exchange_token=instrument,
               timeframe=Timeframe.M5, timestamp_ms=r["timestamp_ms"],
               open=r["open"], high=r["high"], low=r["low"], close=r["close"],
               volume=r.get("volume", 0) or 0)
        for r in rows
    ]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cfd_research.run_research",
                                description="Run the CFD ORB research pipeline over stored 5m data.")
    p.add_argument("--instruments", "-i", default=",".join(_DEFAULT_INSTRUMENTS))
    p.add_argument("--from", dest="start", type=_parse_date, required=True)
    p.add_argument("--to", dest="end", type=_parse_date, required=True)
    p.add_argument("--sessions", default="london,new_york",
                   help="Sessions to run ORB on (comma-separated).")
    p.add_argument("--range-bars", type=int, default=6, help="Opening-range length in 5m bars.")
    p.add_argument("--buffer-frac", type=float, default=0.0,
                   help="Breakout buffer as a fraction of range (e.g. 0.1 = must clear the "
                        "range by 10%% of its size; filters marginal breakouts).")
    p.add_argument("--trend-ema", type=int, default=0,
                   help="If >0, only take breakouts aligned with the EMA(N) trend on the 5m "
                        "closes (long above, short below). 0 = off. Filters against-trend entries.")
    p.add_argument("--dimensions", default="instrument,strategy_id,exit_model",
                   help="Slice dimensions (instrument,strategy_id,session,regime,volatility,"
                        "exit_model,timeframe). MUST include exit_model. Use strategy_id (the "
                        "configured variant, e.g. orb_london_6b) for clean attribution.")
    p.add_argument("--risk", type=float, default=1.0, help="Reference risk %% used to size trades.")
    p.add_argument("--risk-levels", default="0.5,1.0", help="Risk %% levels to score.")
    p.add_argument("--min-trades", type=int, default=30)
    p.add_argument("--step-days", type=int, default=7)
    p.add_argument("--cost-model", default="session_open", choices=sorted(_COST_MODELS),
                   help="Cost model. Default 'session_open' (widened spread + realistic "
                        "slippage) because ORB fires at the open. Use 'conservative' or "
                        "'intraday' to compare; an edge that only survives cheap costs isn't real.")
    p.add_argument("--allow-overnight", action="store_true",
                   help="Allow trades to hold past the FX day boundary (NOT intraday; "
                        "swap is NOT modelled, so results would overstate trend exits).")
    p.add_argument("--top", type=int, default=40, help="How many top slices to print.")
    p.add_argument("--deployable-only", action="store_true",
                   help="Only print slices that pass the challenge AND all 4 deployability gates.")
    p.add_argument("--workers", type=int, default=1,
                   help="Parallel worker processes (one instrument each). 1 = sequential.")
    # Deployability gates (owner's conditions).
    p.add_argument("--min-trades-month", type=float, default=5.0,
                   help="Min avg trades/month for a slice (frequency gate).")
    p.add_argument("--min-active-months", type=int, default=10,
                   help="Min active months per full year (consistency gate).")
    p.add_argument("--max-day-conc", type=float, default=0.30,
                   help="Max share of a month's trades on one day (concentration gate).")
    p.add_argument("--min-wr", type=float, default=0.40,
                   help="Min win rate (quality gate; OR positive expectancy).")
    # Challenge-survival thresholds (required for a slice to be DEPLOYABLE).
    p.add_argument("--min-pass-rate", type=float, default=0.60,
                   help="Min challenge pass-rate for DEPLOYABLE (default 0.60).")
    p.add_argument("--max-blowup-rate", type=float, default=0.05,
                   help="Max blow-up (max-DD breach) rate for DEPLOYABLE (default 0.05).")
    # Challenge ruleset.
    p.add_argument("--p1", type=float, default=8.0, help="Phase-1 profit target %%.")
    p.add_argument("--p2", type=float, default=5.0, help="Phase-2 target %% (0 = one-step).")
    p.add_argument("--daily-dd", type=float, default=5.0)
    p.add_argument("--max-dd", type=float, default=10.0)
    p.add_argument("--dd-mode", default="static", choices=("static", "trailing"))
    return p


def _replay_one_instrument(payload: dict) -> list:
    """Worker: load one instrument's candles and replay ORB (all sessions) under
    the exit sweep. Opens its own DB connection so it's safe in a subprocess."""
    instrument = payload["instrument"]
    cost_model = _COST_MODELS[payload["cost_model"]]
    store = ResearchStore()
    store.start()
    try:
        candles = _load_candles(store, instrument, payload["start_ms"], payload["end_ms"])
    finally:
        store.stop()
    if len(candles) < 100:
        logger.warning("%s: only %d candles — skipping", instrument, len(candles))
        return []

    trades: list = []
    for session in payload["sessions"]:
        strat = SessionORB(session=session, range_bars=payload["range_bars"],
                           buffer_frac=payload["buffer_frac"],
                           trend_ema=payload["trend_ema"] or None)
        t = replay_entries(instrument, candles, strat,
                           risk_pct=payload["risk"], cost_model=cost_model,
                           intraday_only=payload["intraday_only"])
        trades.extend(t)
        logger.info("%s / %s: %d candles -> %d trades (x exits)",
                    instrument, session, len(candles), len(t))
    return trades


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    sessions = [s.strip() for s in args.sessions.split(",") if s.strip()]
    dimensions = tuple(s.strip() for s in args.dimensions.split(",") if s.strip())
    risk_levels = tuple(float(x) for x in args.risk_levels.split(",") if x.strip())
    rules = ChallengeRules(
        phase1_target_pct=args.p1, phase2_target_pct=args.p2,
        daily_dd_pct=args.daily_dd, max_dd_pct=args.max_dd, dd_mode=args.dd_mode,
    )
    start_ms, end_ms = _date_to_ms(args.start), _date_to_ms(args.end)

    # Each instrument is independent -> replay them in parallel (CPU-bound work,
    # so real processes). Each worker opens its own DB connection, loads its
    # instrument's candles, and returns tagged trades; the parent scores.
    payloads = [
        {"instrument": inst, "start_ms": start_ms, "end_ms": end_ms,
         "sessions": sessions, "range_bars": args.range_bars,
         "buffer_frac": args.buffer_frac, "trend_ema": args.trend_ema, "risk": args.risk,
         "cost_model": args.cost_model, "intraday_only": not args.allow_overnight}
        for inst in instruments
    ]

    all_trades: list = []
    if args.workers > 1 and len(payloads) > 1:
        import multiprocessing as mp
        with mp.Pool(min(args.workers, len(payloads))) as pool:
            for trades in pool.imap_unordered(_replay_one_instrument, payloads):
                all_trades.extend(trades)
    else:
        for payload in payloads:
            all_trades.extend(_replay_one_instrument(payload))

    if not all_trades:
        print("No trades produced. Check instruments/date range/sessions.")
        return 1

    results = score_slices(
        all_trades, dimensions, rules,
        ref_risk_pct=args.risk, risk_levels=risk_levels,
        step_days=args.step_days, min_trades=args.min_trades,
        min_pass_rate=args.min_pass_rate, max_blowup_rate=args.max_blowup_rate,
        deploy_kwargs={
            "min_trades_per_month": args.min_trades_month,
            "min_active_months_per_year": args.min_active_months,
            "max_day_concentration": args.max_day_conc,
            "min_win_rate": args.min_wr,
            "data_start_ms": start_ms,   # G3: judge consistency over the full window
            "data_end_ms": end_ms,
        },
    )

    print()
    print("=" * 100)
    print(f"CFD ORB RESEARCH  {args.start} .. {args.end}")
    print(f"Instruments: {', '.join(instruments)} | sessions: {', '.join(sessions)} "
          f"| range={args.range_bars}b buffer={args.buffer_frac:g} "
          f"trendEMA={args.trend_ema or 'off'} | workers={args.workers}")
    print(f"Rules: P1={args.p1}% P2={args.p2}% dailyDD={args.daily_dd}% maxDD={args.max_dd}% "
          f"({args.dd_mode}) | cost={args.cost_model}")
    print(f"Total trades (entries x exits): {len(all_trades):,} | slices scored: {len(results)}")
    print(f"Sliced by: {', '.join(dimensions)}  (deployable first, then best pass-rate)")
    print(f"Deployability gates: >={args.min_trades_month:g}/mo, >={args.min_active_months} active "
          f"mo/full-yr, <={args.max_day_conc*100:g}% day-conc, WR>={args.min_wr*100:g}% or exp>0")
    print(f"Challenge-survival (for DEPLOYABLE): pass>={args.min_pass_rate*100:g}% and "
          f"blowup<={args.max_blowup_rate*100:g}%")
    print("-" * 100)
    print(format_slices(results, top=args.top, deployable_only=args.deployable_only))
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
