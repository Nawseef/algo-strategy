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
from app.cfd_research.entry_replay import replay_entries
from app.cfd_research.strategy_registry import REGISTRY, available, build_variants  # noqa: F401
from app.cfd_research.slice_scorer import format_slices, score_slices
from app.cfd_risk.costs import (
    COST_MODEL_CONSERVATIVE,
    COST_MODEL_INTRADAY,
    COST_MODEL_RAW,
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
    "zero": COST_MODEL_ZERO,          # no spread/slippage/swap, but STILL charges commission
    "raw": COST_MODEL_RAW,            # truly no costs (commission zeroed too) — cost OFF
}
# Timeframes the ORB can be swept over. The 5m base is aggregated up to each one
# by the entry replay (exits still resolve on 5m). TF becomes a slice dimension.
_TIMEFRAMES = {
    "5m": Timeframe.M5,
    "15m": Timeframe.M15,
    "30m": Timeframe.M30,
    "1h": Timeframe.H1,
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
    p.add_argument("--strategies", "-s", default="orb",
                   help="Strategy key(s) to run, comma-separated (e.g. 'orb' or 'orb,fade'). "
                        "See --list-strategies.")
    p.add_argument("--list-strategies", action="store_true",
                   help="Print the available research strategies and exit.")
    p.add_argument("--from", dest="start", type=_parse_date, default=None,
                   help="Start date YYYY-MM-DD (required unless --score-from).")
    p.add_argument("--to", dest="end", type=_parse_date, default=None,
                   help="End date YYYY-MM-DD (required unless --score-from).")
    p.add_argument("--sessions", default="london,new_york",
                   help="Sessions to run ORB on (comma-separated).")
    p.add_argument("--timeframes", default="5m",
                   help="Timeframe(s) to sweep, comma-separated (5m,15m,30m,1h). The 5m "
                        "base is aggregated up to each; exits still resolve on 5m. When "
                        ">1 is given, 'timeframe' is auto-added as a slice dimension.")
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
    p.add_argument("--challenge-gated-only", action="store_true",
                   help="Skip the challenge Monte-Carlo for slices that FAIL the 4 gates "
                        "(they can never be deployable). Much faster for big sweeps "
                        "(TF x regime x volatility); trades off seeing gate-failers' pass%%.")
    # Trade persistence: generate once, re-slice/re-score forever.
    p.add_argument("--persist", default=None, metavar="PATH",
                   help="After generating, save all tagged trades to PATH (.jsonl or "
                        ".jsonl.gz). Re-score later with --score-from (no re-walk).")
    p.add_argument("--score-from", default=None, metavar="PATH",
                   help="Skip generation; load persisted trades from PATH and just score "
                        "them. Lets you change --dimensions / prop rules / risk / gates "
                        "instantly. Entry/exit/cost/TF changes still need a fresh run.")
    p.add_argument("--oos-split", type=_parse_date, default=None, metavar="YYYY-MM-DD",
                   help="Out-of-sample validation: discover deployable slices on trades "
                        "BEFORE this date, then confirm them on trades ON/AFTER it. Reports "
                        "which slices are ROBUST (deployable in both). e.g. --oos-split "
                        "2024-01-01 = discover 2016..2023, confirm 2024..end.")
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

    from app.cfd_research.strategy_registry import build_variants
    cfg = {"sessions": payload["sessions"], "timeframes": payload["timeframes"],
           "range_bars": payload["range_bars"], "buffer_frac": payload["buffer_frac"],
           "trend_ema": payload["trend_ema"]}

    trades: list = []
    for strat in build_variants(payload["strategies"], cfg):
        t = replay_entries(instrument, candles, strat,
                           risk_pct=payload["risk"], cost_model=cost_model,
                           intraday_only=payload["intraday_only"])
        trades.extend(t)
        logger.info("%s / %s / %s: %d 5m candles -> %d trades (x exits)",
                    instrument, strat.strategy_id, strat.timeframe.value, len(candles), len(t))
    return trades


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_strategies:
        print("Available research strategies:")
        for k in available():
            print(f"  {k:12s} {REGISTRY[k].description}")
        return 0

    instruments = [s.strip() for s in args.instruments.split(",") if s.strip()]
    strategy_keys = [s.strip() for s in args.strategies.split(",") if s.strip()]
    unknown_strats = [k for k in strategy_keys if k not in REGISTRY]
    if unknown_strats:
        raise SystemExit(f"unknown strategy(ies) {unknown_strats}; available: {available()}")
    sessions = [s.strip() for s in args.sessions.split(",") if s.strip()]
    dimensions = tuple(s.strip() for s in args.dimensions.split(",") if s.strip())

    tf_keys = [s.strip() for s in args.timeframes.split(",") if s.strip()]
    unknown = [k for k in tf_keys if k not in _TIMEFRAMES]
    if unknown:
        raise SystemExit(f"unknown timeframe(s) {unknown}; valid: {sorted(_TIMEFRAMES)}")
    timeframes = [_TIMEFRAMES[k] for k in tf_keys] or [Timeframe.M5]
    # Sweeping >1 timeframe: 'timeframe' MUST be a slice dimension, else trades
    # from different TFs pool together and the challenge sim treats them as one
    # stream. Auto-add it (like the exit_model guard in score_slices).
    if len(timeframes) > 1 and "timeframe" not in dimensions:
        dimensions = dimensions + ("timeframe",)
        logger.info("swept %d timeframes -> added 'timeframe' to slice dimensions",
                    len(timeframes))
    risk_levels = tuple(float(x) for x in args.risk_levels.split(",") if x.strip())
    rules = ChallengeRules(
        phase1_target_pct=args.p1, phase2_target_pct=args.p2,
        daily_dd_pct=args.daily_dd, max_dd_pct=args.max_dd, dd_mode=args.dd_mode,
    )

    # ── Get the trades: either LOAD persisted ones, or GENERATE by walking data ──
    if args.score_from:
        from app.cfd_research.trade_store import load_trades
        all_trades, meta = load_trades(args.score_from)
        ref_balance = float(meta.get("ref_balance") or 100_000.0)
        ref_risk = float(meta.get("ref_risk_pct") or args.risk)
        # Consistency gate window: prefer the generation window from the file
        # (G3), else fall back to any dates the user passed.
        data_start = meta.get("data_start_ms") or (_date_to_ms(args.start) if args.start else None)
        data_end = meta.get("data_end_ms") or (_date_to_ms(args.end) if args.end else None)
        # Robust auto-add: if the persisted trades span >1 timeframe, TF MUST be a
        # slice dimension or they'd pool as one stream.
        tfs_in_data = {t.timeframe for t in all_trades}
        if len(tfs_in_data) > 1 and "timeframe" not in dimensions:
            dimensions = dimensions + ("timeframe",)
            logger.info("persisted trades span %d timeframes -> added 'timeframe' to dimensions",
                        len(tfs_in_data))
        print(f"[score-from] loaded {len(all_trades):,} trades from {args.score_from} "
              f"(ref_balance={ref_balance:g}, ref_risk={ref_risk:g}%)")
    else:
        if args.start is None or args.end is None:
            raise SystemExit("--from and --to are required (unless using --score-from)")
        start_ms, end_ms = _date_to_ms(args.start), _date_to_ms(args.end)
        ref_balance, ref_risk = 100_000.0, args.risk
        data_start, data_end = start_ms, end_ms

        # Each instrument is independent -> replay them in parallel (CPU-bound
        # work, real processes). Each worker opens its own DB connection, loads
        # its instrument's candles, and returns tagged trades; the parent scores.
        payloads = [
            {"instrument": inst, "start_ms": start_ms, "end_ms": end_ms,
             "strategies": strategy_keys,
             "sessions": sessions, "timeframes": timeframes, "range_bars": args.range_bars,
             "buffer_frac": args.buffer_frac, "trend_ema": args.trend_ema, "risk": args.risk,
             "cost_model": args.cost_model, "intraday_only": not args.allow_overnight}
            for inst in instruments
        ]

        all_trades = []
        if args.workers > 1 and len(payloads) > 1:
            import multiprocessing as mp
            with mp.Pool(min(args.workers, len(payloads))) as pool:
                for trades in pool.imap_unordered(_replay_one_instrument, payloads):
                    all_trades.extend(trades)
        else:
            for payload in payloads:
                all_trades.extend(_replay_one_instrument(payload))

        if all_trades and args.persist:
            from app.cfd_research.trade_store import save_trades
            save_trades(
                args.persist, all_trades,
                ref_balance=ref_balance, ref_risk_pct=ref_risk,
                data_start_ms=start_ms, data_end_ms=end_ms,
                extra_meta={"strategies": strategy_keys,
                            "instruments": instruments, "sessions": sessions,
                            "timeframes": [tf.value for tf in timeframes],
                            "range_bars": args.range_bars, "buffer_frac": args.buffer_frac,
                            "trend_ema": args.trend_ema, "cost_model": args.cost_model,
                            "intraday_only": not args.allow_overnight,
                            "from": str(args.start), "to": str(args.end)},
            )
            print(f"[persist] saved {len(all_trades):,} trades -> {args.persist}")

    if not all_trades:
        print("No trades produced. Check instruments/date range/sessions (or the --score-from file).")
        return 1

    # Fallback data window from the trades themselves (needed for the consistency
    # gate when --score-from didn't carry a window).
    if data_start is None:
        data_start = min(t.entry_time_ms for t in all_trades)
    if data_end is None:
        data_end = max(t.entry_time_ms for t in all_trades)

    def do_score(trade_subset, ds, de, gated):
        """Score a set of trades with the run's dimensions/rules/gates."""
        if not trade_subset:
            return []
        return score_slices(
            trade_subset, dimensions, rules,
            ref_balance=ref_balance, ref_risk_pct=ref_risk, risk_levels=risk_levels,
            step_days=args.step_days, min_trades=args.min_trades,
            min_pass_rate=args.min_pass_rate, max_blowup_rate=args.max_blowup_rate,
            challenge_gated_only=gated,
            deploy_kwargs={
                "min_trades_per_month": args.min_trades_month,
                "min_active_months_per_year": args.min_active_months,
                "max_day_concentration": args.max_day_conc,
                "min_win_rate": args.min_wr,
                "data_start_ms": ds,   # G3: judge consistency over the window
                "data_end_ms": de,
            },
        )

    # ── Out-of-sample validation branch ──
    if args.oos_split is not None:
        from app.cfd_research.oos import format_oos, validate_oos
        split_ms = _date_to_ms(args.oos_split)
        rows, n_disc, n_conf = validate_oos(
            all_trades, split_ms,
            score_discover=lambda ts, ds, de: do_score(ts, ds, de, args.challenge_gated_only),
            score_confirm=lambda ts, ds, de: do_score(ts, ds, de, False),
            data_start_ms=data_start, data_end_ms=data_end,
        )
        print()
        print("=" * 100)
        print(f"CFD RESEARCH — OUT-OF-SAMPLE  split={args.oos_split}  | strategies: "
              f"{', '.join(strategy_keys)}")
        print(f"Rules: P1={args.p1}% P2={args.p2}% dailyDD={args.daily_dd}% maxDD={args.max_dd}% "
              f"({args.dd_mode}) | cost={args.cost_model} | sliced by: {', '.join(dimensions)}")
        print("-" * 100)
        print(format_oos(rows, n_disc, n_conf, top=args.top))
        print("=" * 100)
        return 0

    results = do_score(all_trades, data_start, data_end, args.challenge_gated_only)

    print()
    print("=" * 100)
    if args.score_from:
        print(f"CFD ORB RESEARCH  (scored from persisted trades: {args.score_from})")
        print(f"Instruments/sessions/timeframes: as generated in the file "
              f"| re-scored dimensions below")
    else:
        print(f"CFD RESEARCH  {args.start} .. {args.end}  | strategies: {', '.join(strategy_keys)}")
        print(f"Instruments: {', '.join(instruments)} | sessions: {', '.join(sessions)} "
              f"| timeframes: {', '.join(tf.value for tf in timeframes)} "
              f"| range={args.range_bars}b buffer={args.buffer_frac:g} "
              f"trendEMA={args.trend_ema or 'off'} | workers={args.workers}")
    print(f"Rules: P1={args.p1}% P2={args.p2}% dailyDD={args.daily_dd}% maxDD={args.max_dd}% "
          f"({args.dd_mode}) | cost={args.cost_model}")
    print(f"Total trades (entries x exits): {len(all_trades):,} | slices scored: {len(results)}")
    print(f"Sliced by: {', '.join(dimensions)}  (deployable first, then best pass-rate)")
    print(f"Deployability gates: >={args.min_trades_month:g}/mo, >={args.min_active_months} active "
          f"mo/full-yr, <={args.max_day_conc*100:g}% day-conc, WR>={args.min_wr*100:g}% or exp>0")
    print(f"Challenge-survival (for DEPLOYABLE): pass>={args.min_pass_rate*100:g}% and "
          f"account-loss (max-DD OR daily-DD breach) <={args.max_blowup_rate*100:g}%")
    print("-" * 100)
    print(format_slices(results, top=args.top, deployable_only=args.deployable_only))
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
