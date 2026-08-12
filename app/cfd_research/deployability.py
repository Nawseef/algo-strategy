"""
Deployability filters — "is this slice frequent, consistent, and clean enough to
actually run in a prop challenge?"

Passing the challenge sim (pass-rate / low blow-up) is necessary but NOT
sufficient. A slice can pass the DD math yet be undeployable because it trades
too rarely (takes years to hit the profit target), only works a few months a
year (dead time during an eval), concentrates its trades on single event-days
(correlated risk that blows the daily DD in one shot), or has no real edge
(low win rate AND negative expectancy).

These four gates are computed from the trade LIST itself (independent of the
risk level), so they live here — separate from the DD-focused challenge sim.
They are applied AFTER the challenge sim, as a deployability filter.

Owner's conditions (defaults below):
  1. FREQUENCY    — a slice averages >= 5 trades / month.
  2. CONSISTENCY  — every fully-covered year has >= 10 "active" months
                    (a month with >= 5 trades).
  3. CONCENTRATION— in any month with enough trades, <= 30% of that month's
                    trades fall on a single day (anti event-day clustering).
  4. QUALITY      — win rate >= 40% OR positive expectancy after costs.

Portfolio-level (a SET on one account) adds:
  * >= 12 trades / month combined  (see ``portfolio_trades_per_month``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.cfd_backtest.exit_simulator import SimulatedTrade

# ── Default thresholds (the owner's deployability conditions) ──
MIN_TRADES_PER_MONTH = 5.0          # individual slice
MIN_ACTIVE_MONTHS_PER_YEAR = 10     # active = a month with >= MIN_TRADES_PER_MONTH trades
MAX_DAY_CONCENTRATION = 0.30        # <= 30% of a month's trades on one day
MIN_WIN_RATE = 0.40                 # OR positive expectancy
CONCENTRATION_MIN_MONTH_TRADES = 5  # only judge concentration on months with >= this many trades
MIN_PORTFOLIO_TRADES_PER_MONTH = 12.0

_AVG_DAYS_PER_MONTH = 30.437


@dataclass
class DeployabilityMetrics:
    n_trades: int
    trades_per_month: float
    win_rate: float
    expectancy_usd: float
    active_months_by_year: dict[int, int]
    full_years: list[int]
    min_full_year_active_months: int | None    # None if no fully-covered year in the data
    worst_month_day_share: float               # max single-day share across qualifying months
    worst_month: str

    pass_frequency: bool = False
    pass_consistency: bool = False
    pass_concentration: bool = False
    pass_quality: bool = False

    @property
    def deployable(self) -> bool:
        return (self.pass_frequency and self.pass_consistency
                and self.pass_concentration and self.pass_quality)

    def flags(self) -> str:
        """Compact per-filter marker, e.g. 'F C D q' (upper = pass, lower = fail)."""
        return " ".join([
            "F" if self.pass_frequency else "f",
            "C" if self.pass_consistency else "c",
            "D" if self.pass_concentration else "d",
            "Q" if self.pass_quality else "q",
        ])


def _full_years(first_ms: float, last_ms: float) -> list[int]:
    """Years whose Jan 1 .. Dec 31 are entirely within [first_ms, last_ms]."""
    fd = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc)
    ld = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)
    out: list[int] = []
    for y in range(fd.year, ld.year + 1):
        jan1 = datetime(y, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
        dec31 = datetime(y, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000
        if first_ms <= jan1 and last_ms >= dec31:
            out.append(y)
    return out


def compute_deployability(
    trades: list[SimulatedTrade],
    *,
    min_trades_per_month: float = MIN_TRADES_PER_MONTH,
    min_active_months_per_year: int = MIN_ACTIVE_MONTHS_PER_YEAR,
    max_day_concentration: float = MAX_DAY_CONCENTRATION,
    min_win_rate: float = MIN_WIN_RATE,
    concentration_min_month_trades: int = CONCENTRATION_MIN_MONTH_TRADES,
    data_start_ms: float | None = None,
    data_end_ms: float | None = None,
) -> DeployabilityMetrics:
    """Compute the four deployability gates for one slice's trade list."""
    n = len(trades)
    if n == 0:
        return DeployabilityMetrics(
            n_trades=0, trades_per_month=0.0, win_rate=0.0, expectancy_usd=0.0,
            active_months_by_year={}, full_years=[], min_full_year_active_months=None,
            worst_month_day_share=0.0, worst_month="",
        )

    entries = sorted(t.entry_time_ms for t in trades)
    first, last = entries[0], entries[-1]
    span_months = max((last - first) / 1000 / 86_400 / _AVG_DAYS_PER_MONTH, 1e-9)
    trades_per_month = n / span_months if span_months > 1e-6 else float(n)

    wins = sum(1 for t in trades if t.net_pnl_usd > 0)
    win_rate = wins / n
    expectancy = sum(t.net_pnl_usd for t in trades) / n

    # Group by (year, month).
    by_month: dict[tuple[int, int], list[SimulatedTrade]] = defaultdict(list)
    for t in trades:
        dt = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=timezone.utc)
        by_month[(dt.year, dt.month)].append(t)

    # Active months per year (a month with >= min_trades_per_month trades).
    active_by_year: dict[int, int] = defaultdict(int)
    for (y, _m), ts in by_month.items():
        if len(ts) >= min_trades_per_month:
            active_by_year[y] += 1

    # G3: judge consistency over the FULL requested data window, not just the
    # years this slice happened to trade. A year inside the window with zero (or
    # too few) active months = fail — this is what catches a DECAYED or seasonal
    # edge (one that worked early then died). Fall back to the trade span only if
    # the caller didn't pass the data window.
    fy_start = data_start_ms if data_start_ms is not None else first
    fy_end = data_end_ms if data_end_ms is not None else last
    full_years = _full_years(fy_start, fy_end)
    min_full = min((active_by_year.get(y, 0) for y in full_years), default=None) if full_years else None

    # Day concentration on months with enough trades to judge.
    worst_share = 0.0
    worst_month = ""
    for (y, m), ts in by_month.items():
        if len(ts) < concentration_min_month_trades:
            continue
        by_day: dict[object, int] = defaultdict(int)
        for t in ts:
            d = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=timezone.utc).date()
            by_day[d] += 1
        share = max(by_day.values()) / len(ts)
        if share > worst_share:
            worst_share = share
            worst_month = f"{y}-{m:02d}"

    m = DeployabilityMetrics(
        n_trades=n,
        trades_per_month=trades_per_month,
        win_rate=win_rate,
        expectancy_usd=expectancy,
        active_months_by_year=dict(sorted(active_by_year.items())),
        full_years=full_years,
        min_full_year_active_months=min_full,
        worst_month_day_share=worst_share,
        worst_month=worst_month,
    )
    m.pass_frequency = trades_per_month >= min_trades_per_month
    # If the data has no fully-covered year, we can't judge consistency -> don't fail on it.
    m.pass_consistency = (min_full is None) or (min_full >= min_active_months_per_year)
    # No month had enough trades to judge -> concentration is not a concern (share 0).
    m.pass_concentration = worst_share <= max_day_concentration + 1e-9
    m.pass_quality = (win_rate >= min_win_rate) or (expectancy > 0)
    return m


def portfolio_trades_per_month(legs) -> float:
    """Combined trades/month for a portfolio leg list (from portfolio_sim)."""
    if not legs:
        return 0.0
    entries = sorted(l.entry_ms for l in legs)
    span_months = max((entries[-1] - entries[0]) / 1000 / 86_400 / _AVG_DAYS_PER_MONTH, 1e-9)
    return len(legs) / span_months if span_months > 1e-6 else float(len(legs))
