"""
Compare cTrader staging candles vs MT5 live candles — per-candle verification.

After running cTrader as a parallel candle archiver (writing to
``ctrader_staging_candles``) alongside the MT5 feed (``live_candles``), run this
to prove the two feeds produce the SAME 5m bars before cutting over.

It joins on the natural key ``(instrument, timeframe, timestamp_ms)`` and, per
instrument, reports:
  * how many 5m bars each feed produced and how many are shared (matched),
  * bars present in only ONE feed (missing on the other side),
  * among the matched bars, how many differ by MORE than the tolerance,
  * the worst-offending candles (with UTC time) and a sample of missing bars.

Tolerance is a PERCENTAGE (``--tol-pct``, default 0.5%), so it scales across
instruments — a raw 0.6 delta is nothing on US30 (~40000) but huge on EURUSD
(~1.08). A small delta is expected and fine (two brokers, slightly different
quotes); a bar that is MISSING on one side, or a large delta, is what we care
about.

Usage (on the VM where Postgres lives):
    venv/bin/python -m app.tools.compare_candles                      # last 3 days, all symbols
    venv/bin/python -m app.tools.compare_candles --days 2
    venv/bin/python -m app.tools.compare_candles --from 2026-08-12 --to 2026-08-14
    venv/bin/python -m app.tools.compare_candles --instrument XAUUSD --tol-pct 0.3
    venv/bin/python -m app.tools.compare_candles --show 20           # list more offenders
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _fmt_ts(ms: float) -> str:
    """Epoch ms (real UTC) -> readable UTC timestamp."""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def run(
    instrument: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    days: int = 3,
    tol_pct: float = 0.5,
    show: int = 10,
) -> None:
    store = ResearchStore()
    store.start()
    ph = "%s" if store._use_postgres else "?"

    # Default window = last `days` days by session_date (unless --from given).
    if from_date is None:
        from_date = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()

    # ── Shared WHERE builder (aliased for staging `s` / live `l`) ────────────
    def where_for(alias: str) -> tuple[str, list]:
        clauses = [f"{alias}.timeframe = '5m'", f"{alias}.session_date >= {ph}"]
        params: list = [from_date]
        if to_date:
            clauses.append(f"{alias}.session_date <= {ph}")
            params.append(to_date)
        if instrument:
            clauses.append(f"{alias}.instrument = {ph}")
            params.append(instrument)
        return " AND ".join(clauses), params

    s_where, s_params = where_for("s")
    l_where, l_params = where_for("l")
    tol = float(tol_pct)

    # ── Per-instrument aggregate (staging side + matched + close breaches) ───
    agg_sql = f"""
    SELECT
        s.instrument,
        COUNT(*)                                             AS staging_bars,
        COUNT(l.timestamp_ms)                                AS matched_bars,
        SUM(CASE WHEN l.timestamp_ms IS NULL THEN 1 ELSE 0 END) AS only_in_ctrader,
        SUM(CASE WHEN l.timestamp_ms IS NOT NULL AND l.close <> 0
                  AND ABS(s.close - l.close) / ABS(l.close) * 100.0 > {tol}
                 THEN 1 ELSE 0 END)                          AS close_breaches,
        MAX(CASE WHEN l.close <> 0
                  THEN ABS(s.close - l.close) / ABS(l.close) * 100.0 END) AS max_close_pct,
        MAX(CASE WHEN l.high <> 0
                  THEN ABS(s.high - l.high) / ABS(l.high) * 100.0 END)    AS max_high_pct,
        MAX(CASE WHEN l.low <> 0
                  THEN ABS(s.low  - l.low)  / ABS(l.low)  * 100.0 END)    AS max_low_pct
    FROM ctrader_staging_candles s
    LEFT JOIN live_candles l
        ON l.instrument = s.instrument
       AND l.timeframe  = s.timeframe
       AND l.timestamp_ms = s.timestamp_ms
    WHERE {s_where}
    GROUP BY s.instrument
    ORDER BY s.instrument
    """
    rows = store._query(agg_sql, tuple(s_params))

    # ── Bars only in MT5 (live) but missing from staging ─────────────────────
    mt5_only_sql = f"""
    SELECT l.instrument, COUNT(*) AS only_in_mt5
    FROM live_candles l
    LEFT JOIN ctrader_staging_candles s
        ON s.instrument = l.instrument
       AND s.timeframe  = l.timeframe
       AND s.timestamp_ms = l.timestamp_ms
    WHERE {l_where} AND s.timestamp_ms IS NULL
    GROUP BY l.instrument
    """
    mt5_only = {r["instrument"]: r["only_in_mt5"]
                for r in store._query(mt5_only_sql, tuple(l_params))}

    # ── Report: summary table ────────────────────────────────────────────────
    print("=" * 96)
    print("cTrader (staging) vs MT5 (live) — 5m candle comparison")
    print(f"Window: from={from_date}  to={to_date or 'now'}  "
          f"instrument={instrument or 'ALL'}  tolerance={tol:.3f}%")
    print("=" * 96)
    print(f"{'Symbol':<9} {'cTrader':>8} {'MT5only':>8} {'Matched':>8} "
          f"{'CTonly':>7} {'MT5only':>8} {'>tol':>6} {'maxΔC%':>8} {'maxΔH%':>8} {'maxΔL%':>8}")
    print("-" * 96)

    issues: list[str] = []
    total_matched = total_breaches = total_ct_only = total_mt5_only = 0

    for r in rows:
        sym = r["instrument"]
        mt5o = mt5_only.get(sym, 0)
        matched = r["matched_bars"] or 0
        breaches = r["close_breaches"] or 0
        ct_only = r["only_in_ctrader"] or 0
        # "MT5 bars" isn't directly in this row; matched + mt5-only = MT5 total.
        mt5_total = matched + mt5o

        total_matched += matched
        total_breaches += breaches
        total_ct_only += ct_only
        total_mt5_only += mt5o

        print(f"{sym:<9} {r['staging_bars']:>8} {mt5_total:>8} {matched:>8} "
              f"{ct_only:>7} {mt5o:>8} {breaches:>6} "
              f"{(r['max_close_pct'] or 0):>8.4f} {(r['max_high_pct'] or 0):>8.4f} "
              f"{(r['max_low_pct'] or 0):>8.4f}")

        if ct_only or mt5o or breaches:
            issues.append(sym)

    if not rows:
        print("  (no staging candles found — is cfd-ctrader running with CFD_CTRADER_STAGING=true?)")
        store.stop()
        return

    print("-" * 96)
    print(f"{'TOTAL':<9} {'':>8} {'':>8} {total_matched:>8} "
          f"{total_ct_only:>7} {total_mt5_only:>8} {total_breaches:>6}")
    print()
    print("Legend: cTrader/MT5only=bars each feed produced; Matched=shared bars; "
          "CTonly/MT5only=bar present on ONE side only;")
    print("        >tol=matched bars whose close differs by more than the tolerance; "
          "maxΔC/H/L%=largest close/high/low pct delta.")

    # ── Detail: worst offenders + missing bars for the flagged symbols ───────
    if issues and show > 0:
        print()
        print("=" * 96)
        print("DETAIL (flagged symbols only)")
        print("=" * 96)
        for sym in issues:
            print(f"\n### {sym}")
            _detail_worst(store, ph, sym, from_date, to_date, tol, show)
            _detail_missing(store, ph, sym, from_date, to_date, show)

    # ── Verdict ──────────────────────────────────────────────────────────────
    print()
    print("=" * 96)
    if total_ct_only == 0 and total_mt5_only == 0 and total_breaches == 0:
        print("VERDICT: ✅ every 5m candle matches on both sides within "
              f"{tol:.3f}% — safe to cut over.")
    else:
        print(f"VERDICT: ⚠️  {total_ct_only} bars only in cTrader, "
              f"{total_mt5_only} only in MT5, {total_breaches} matched bars over "
              f"{tol:.3f}% — investigate the DETAIL above before cutting over.")
    print("=" * 96)
    store.stop()


def _detail_worst(store, ph, sym, from_date, to_date, tol, show) -> None:
    """List the worst-differing matched candles for one symbol."""
    clauses = [
        "s.timeframe = '5m'", "s.instrument = " + ph,
        "s.session_date >= " + ph, "l.close <> 0",
        f"ABS(s.close - l.close) / ABS(l.close) * 100.0 > {tol}",
    ]
    params: list = [sym, from_date]
    if to_date:
        clauses.insert(3, "s.session_date <= " + ph)
        params.insert(2, to_date)
    sql = f"""
    SELECT s.timestamp_ms, s.close AS ct_close, l.close AS mt5_close,
           ABS(s.close - l.close) / ABS(l.close) * 100.0 AS dpct
    FROM ctrader_staging_candles s
    JOIN live_candles l
        ON l.instrument = s.instrument AND l.timeframe = s.timeframe
       AND l.timestamp_ms = s.timestamp_ms
    WHERE {' AND '.join(clauses)}
    ORDER BY dpct DESC
    LIMIT {int(show)}
    """
    rows = store._query(sql, tuple(params))
    if not rows:
        print("  close deltas: all within tolerance")
        return
    print(f"  worst close deltas (> {tol:.3f}%):")
    for r in rows:
        print(f"    {_fmt_ts(r['timestamp_ms'])}  cTrader={r['ct_close']:<12} "
              f"MT5={r['mt5_close']:<12} Δ={r['dpct']:.4f}%")


def _detail_missing(store, ph, sym, from_date, to_date, show) -> None:
    """Sample bars present on only one side for one symbol."""
    def missing(src_alias, src_tbl, other_tbl, label):
        clauses = [
            f"{src_alias}.timeframe = '5m'", f"{src_alias}.instrument = " + ph,
            f"{src_alias}.session_date >= " + ph, "o.timestamp_ms IS NULL",
        ]
        params: list = [sym, from_date]
        if to_date:
            clauses.insert(3, f"{src_alias}.session_date <= " + ph)
            params.insert(2, to_date)
        sql = f"""
        SELECT {src_alias}.timestamp_ms AS ts
        FROM {src_tbl} {src_alias}
        LEFT JOIN {other_tbl} o
            ON o.instrument = {src_alias}.instrument
           AND o.timeframe  = {src_alias}.timeframe
           AND o.timestamp_ms = {src_alias}.timestamp_ms
        WHERE {' AND '.join(clauses)}
        ORDER BY {src_alias}.timestamp_ms
        LIMIT {int(show)}
        """
        rows = store._query(sql, tuple(params))
        if rows:
            times = ", ".join(_fmt_ts(r["ts"]) for r in rows)
            print(f"  {label} ({len(rows)} shown): {times}")

    missing("s", "ctrader_staging_candles", "live_candles", "bars only in cTrader")
    missing("l", "live_candles", "ctrader_staging_candles", "bars only in MT5")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare cTrader staging candles vs MT5 live candles (per-candle)")
    parser.add_argument("--instrument", type=str, default=None)
    parser.add_argument("--from", dest="from_date", type=str, default=None,
                        help="session_date start (YYYY-MM-DD); overrides --days")
    parser.add_argument("--to", dest="to_date", type=str, default=None,
                        help="session_date end (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=3,
                        help="compare the last N days if --from not given (default 3)")
    parser.add_argument("--tol-pct", dest="tol_pct", type=float, default=0.5,
                        help="max acceptable close delta as %% of price (default 0.5)")
    parser.add_argument("--show", type=int, default=10,
                        help="rows of detail to list per flagged symbol (default 10)")
    args = parser.parse_args()
    run(instrument=args.instrument, from_date=args.from_date, to_date=args.to_date,
        days=args.days, tol_pct=args.tol_pct, show=args.show)


if __name__ == "__main__":
    main()
