"""
Slice scorer — "which slice actually passes the prop challenge?"

Takes tagged trades (from the entry replay) and groups them by any combination of
tag dimensions — instrument, session, regime, volatility, exit_model, timeframe —
then runs the challenge simulator on each slice and ranks by pass-rate / blow-up.
This is what turns raw trades into the answer you want:

    "ORB on XAUUSD, London, trend_up, breakeven exit  -> pass 71%, blowup 2%"

Each slice is scored with the SINGLE-STREAM challenge sim (a slice is a filtered
set of that strategy's own trades). To evaluate a chosen SET of slices as one
shared account, feed the winners to the portfolio simulator separately.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_research.challenge_sim import (
    ChallengeRules,
    MonteCarloResult,
    from_simulated_trades,
    monte_carlo,
)
from app.cfd_research.deployability import DeployabilityMetrics, compute_deployability
from app.utils.logger import get_logger

logger = get_logger(__name__)

# The dimensions you can slice by (attributes on SimulatedTrade).
# ``strategy_id`` (e.g. orb_london_6b) is the CONFIGURED variant — the clean
# attribution axis. ``session`` is the REAL FX session at entry (can differ from
# the configured one during overlaps), useful for regime analysis but NOT for
# "which session variant has the edge" — use strategy_id for that.
TAG_DIMENSIONS = ("instrument", "strategy_id", "session", "regime", "volatility", "exit_model", "timeframe")


def _slice_has_overlap(trades: list[SimulatedTrade]) -> bool:
    """True if any two trades in the slice are open at the same time.

    The single-stream challenge sim processes trades sequentially and can't model
    two positions open at once, so it UNDERSTATES the combined floating drawdown
    of a slice that contains overlapping trades (G8). With clean per-variant
    slicing (strategy_id) and one-entry-per-session strategies this should be
    empty — this is a safety net that flags when it isn't."""
    ordered = sorted(trades, key=lambda t: t.entry_time_ms)
    max_exit = float("-inf")
    for t in ordered:
        if t.entry_time_ms < max_exit - 1e-6:
            return True
        max_exit = max(max_exit, t.exit_time_ms)
    return False


@dataclass
class SliceResult:
    key: dict[str, str]        # dimension -> value for this slice
    trade_count: int
    risk_pct: float
    mc: MonteCarloResult
    deploy: DeployabilityMetrics | None = None   # frequency/consistency/concentration/quality gates
    has_overlap: bool = False                    # trades overlap in time (single-stream DD understated)

    @property
    def sort_key(self) -> tuple:
        # Deployable slices first, then by pass-rate / blow-up.
        deployable = 0 if (self.deploy and self.deploy.deployable) else 1
        return (deployable, -self.mc.pass_rate, self.mc.blowup_rate, -self.mc.phase1_pass_rate)

    @property
    def qualifies(self) -> bool:
        """Passes the challenge convincingly AND all four deployability gates."""
        return bool(self.deploy and self.deploy.deployable)

    def label(self) -> str:
        return " ".join(f"{k}={v}" for k, v in self.key.items())

    def row(self) -> str:
        d = self.deploy
        deploy_cols = ""
        if d is not None:
            actmo = "-" if d.min_full_year_active_months is None else f"{d.min_full_year_active_months:>2d}"
            deploy_cols = (
                f" | t/mo={d.trades_per_month:4.1f} actMo={actmo} "
                f"dayC={d.worst_month_day_share*100:3.0f}% WR={d.win_rate*100:4.1f}% "
                f"[{d.flags()}] {'DEPLOY' if d.deployable else '  -   '}"
            )
        overlap_mark = " !OVERLAP" if self.has_overlap else ""
        return (
            f"{self.label():48s} risk={self.risk_pct:>4.2f}% | n={self.trade_count:<5d} "
            f"pass={self.mc.pass_rate*100:5.1f}%(d{self.mc.decisive_runs}) "
            f"blowup={self.mc.blowup_rate*100:5.1f}% inc={self.mc.incomplete_rate*100:3.0f}% "
            f"medDays={self.mc.median_days_to_pass:>3.0f} worstDD={self.mc.worst_dd_pct:4.1f}%"
            f"{deploy_cols}{overlap_mark}"
        )


def score_slices(
    trades: list[SimulatedTrade],
    dimensions: tuple[str, ...],
    rules: ChallengeRules,
    *,
    ref_balance: float = 100_000.0,
    ref_risk_pct: float = 1.0,               # risk the trades were sized at (in replay)
    risk_levels: tuple[float, ...] = (0.5, 1.0),
    step_days: int = 7,
    min_trades: int = 30,
    reset_utc_offset_hours: float = 0.0,
    deploy_kwargs: dict | None = None,
) -> list[SliceResult]:
    """Group trades by ``dimensions``, challenge-score each slice at each risk, and
    compute the deployability gates (frequency / consistency / concentration /
    quality) per slice."""
    for d in dimensions:
        if d not in TAG_DIMENSIONS:
            raise ValueError(f"unknown slice dimension {d!r}; valid: {TAG_DIMENSIONS}")
    # G7 guard: every entry is replayed under EVERY exit model, so a slice that
    # doesn't separate by exit_model would pool 5 correlated variants of each
    # entry and the challenge sim would treat them as independent trades —
    # inflating counts 5x and producing a meaningless equity curve.
    if "exit_model" not in dimensions:
        raise ValueError(
            "slice dimensions MUST include 'exit_model' — otherwise the 5 exit "
            "variants of each entry are pooled as if independent (5x correlated "
            f"duplicates). Got dimensions={dimensions}."
        )

    groups: dict[tuple, list[SimulatedTrade]] = defaultdict(list)
    for t in trades:
        groups[tuple(getattr(t, d) for d in dimensions)].append(t)

    deploy_kwargs = deploy_kwargs or {}
    results: list[SliceResult] = []
    for key_vals, group in groups.items():
        if len(group) < min_trades:
            continue
        returns = from_simulated_trades(group, ref_balance, reset_utc_offset_hours)
        deploy = compute_deployability(group, **deploy_kwargs)
        overlap = _slice_has_overlap(group)
        key = {d: v for d, v in zip(dimensions, key_vals)}
        for level in risk_levels:
            scale = level / ref_risk_pct if ref_risk_pct else 1.0
            mc = monte_carlo(returns, rules, risk_scale=scale, step_days=step_days)
            results.append(SliceResult(key=key, trade_count=len(group), risk_pct=level,
                                       mc=mc, deploy=deploy, has_overlap=overlap))

    n_overlap = len({tuple(r.key.items()) for r in results if r.has_overlap})
    if n_overlap:
        logger.warning(
            "%d slice(s) contain time-overlapping trades — their single-stream "
            "pass-rate/DD is OPTIMISTIC (concurrency not modelled). Score those "
            "as a portfolio for a correct combined drawdown.", n_overlap,
        )

    results.sort(key=lambda r: r.sort_key)
    return results


def format_slices(
    results: list[SliceResult], top: int | None = 30, deployable_only: bool = False
) -> str:
    shown = [r for r in results if r.qualifies] if deployable_only else results
    rows = shown[:top] if top else shown
    if not rows:
        return "(no deployable slices)" if deployable_only else "(no slices with enough trades)"
    n_deploy = sum(1 for r in results if r.qualifies)
    header = (
        "flags: F=frequency(>=5/mo) C=consistency(>=10 active mo/yr) "
        "D=dayConc(<=30%) Q=quality(WR>=40% or exp>0); UPPER=pass\n"
        f"DEPLOYABLE slices (pass challenge + all 4 gates): {n_deploy} of {len(results)}\n"
    )
    return header + "\n".join(r.row() for r in rows)
