"""
Prop-firm challenge simulator — the scoreboard for CFD strategy research.

A good backtest (win rate, profit factor) does NOT tell you what matters for a
prop account: *would this pass the 2-step evaluation and survive the funded
account without ever breaching the daily or maximum drawdown?* This module
answers exactly that.

Design choices (why it's built this way):

  * ACCOUNT-SIZE AGNOSTIC. Everything is expressed as a percentage of the
    starting balance, so one simulation covers 10k / 25k / 100k / 200k accounts
    identically. Prop rules (targets, DD) are percentages anyway.

  * RISK-SWEEPABLE FOR FREE. Per-trade PnL scales linearly with risk-per-trade,
    so a single backtest at a reference risk can be re-scored at 0.5% or 1% (or
    anything) via ``risk_scale`` — no re-backtest needed.

  * MONEY-SAFE DRAWDOWN. Daily/max DD are checked against each trade's WORST
    intratrade equity (using its max adverse excursion + costs), not just the
    realized close. This never flatters the result — it catches a breach that
    happened *during* a trade even if it closed green.

  * MONTE-CARLO OVER HISTORY. The key metric isn't one run — it's: "if I started
    this challenge on any random day across 10 years, what fraction of the time
    do I pass Phase 1, then Phase 2, without blowing up?" That distribution
    (pass rate, blow-up rate, days-to-pass, worst DD) is what tells you a
    strategy is robust across market conditions, not curve-fit to one period.

Generic, configurable ruleset (no firm chosen yet): defaults approximate a
common 2-step evaluation (8% then 5% target, 5% daily / 10% max DD). Point the
numbers at FTMO / FundedNext / The5ers later — nothing else changes.

Phases are INDEPENDENT accounts: Phase 2 starts fresh (equity 0, DD floors
reset), exactly as a real evaluation resets balance between steps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_risk.instruments import get_instrument
from app.utils.logger import get_logger

logger = get_logger(__name__)


class Outcome(Enum):
    """Result of a single phase (or overall challenge)."""

    PASS = "PASS"
    FAIL_DAILY_DD = "FAIL_DAILY_DD"     # breached the daily drawdown limit
    FAIL_MAX_DD = "FAIL_MAX_DD"         # breached the overall/max drawdown limit
    TIMEOUT = "TIMEOUT"                 # ran out of allowed calendar days
    INCOMPLETE = "INCOMPLETE"           # ran out of trade data before target


@dataclass
class ChallengeRules:
    """A configurable, firm-agnostic 2-step evaluation ruleset (all % of start)."""

    phase1_target_pct: float = 8.0
    phase2_target_pct: float = 5.0     # set 0.0 for a 1-step challenge
    daily_dd_pct: float = 5.0
    max_dd_pct: float = 10.0
    dd_mode: str = "static"            # "static" (floor from initial) | "trailing" (from peak)
    min_trading_days: int = 0          # min distinct days with >=1 trade, per phase
    max_calendar_days: int | None = None   # per-phase time limit; None = unlimited (common now)
    reset_utc_offset_hours: float = 0.0    # daily-reset timezone (firm server tz)

    def __post_init__(self) -> None:
        if self.dd_mode not in ("static", "trailing"):
            raise ValueError(f"dd_mode must be 'static' or 'trailing', got {self.dd_mode!r}")


@dataclass
class TradeReturn:
    """One trade reduced to what the challenge cares about (% of balance)."""

    entry_ms: float
    exit_ms: float
    ret_pct: float          # realized net return, % of starting balance
    mae_ret_pct: float      # worst adverse excursion during the trade (>= 0), % + costs
    trading_day: date       # firm-local day the trade belongs to (by entry)


@dataclass
class PhaseResult:
    outcome: Outcome
    end_return_pct: float           # realized return % at phase end
    trading_days: int               # distinct days traded in this phase
    trades_used: int
    worst_overall_dd_pct: float     # deepest equity dip below initial (>= 0)
    next_index: int                 # index into the returns list to resume from

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS


@dataclass
class ChallengeResult:
    outcome: Outcome                # PASS only if every required phase passed
    phase1: PhaseResult
    phase2: PhaseResult | None      # None if 1-step (phase2_target_pct == 0)
    total_trading_days: int
    start_ms: float

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS

    @property
    def blew_up(self) -> bool:
        for p in (self.phase1, self.phase2):
            if p is not None and p.outcome is Outcome.FAIL_MAX_DD:
                return True
        return False


@dataclass
class MonteCarloResult:
    """Aggregate outcome of starting the challenge on many historical dates."""

    runs: int = 0
    passed: int = 0
    phase1_passed: int = 0
    failed_daily: int = 0
    failed_max: int = 0
    timeouts: int = 0
    incompletes: int = 0
    days_to_pass: list[int] = field(default_factory=list)
    worst_dd_pcts: list[float] = field(default_factory=list)

    # ── rates ──
    # G9: rates are over DECISIVE runs (excluding INCOMPLETE — a challenge that
    # ran out of trade history before reaching a verdict is "no decision", not a
    # fail). Counting incompletes in the denominator unfairly deflated rare
    # strategies. incomplete_rate is reported separately so it stays visible.
    @property
    def decisive_runs(self) -> int:
        return self.runs - self.incompletes

    @property
    def pass_rate(self) -> float:
        d = self.decisive_runs
        return self.passed / d if d else 0.0

    @property
    def phase1_pass_rate(self) -> float:
        d = self.decisive_runs
        return self.phase1_passed / d if d else 0.0

    @property
    def blowup_rate(self) -> float:
        """Fraction of DECISIVE runs that breached the MAX (overall) DD.

        Granular sub-metric. For the deployable survival gate use
        ``account_ending_rate``, which also counts daily-DD breaches — both end a
        real prop account/evaluation."""
        d = self.decisive_runs
        return self.failed_max / d if d else 0.0

    @property
    def daily_halt_rate(self) -> float:
        """Fraction of DECISIVE runs that breached the DAILY DD limit. In a real
        2-step evaluation this TERMINATES the account (a fail), same terminal
        outcome as a max-DD breach — it is not a soft halt."""
        d = self.decisive_runs
        return self.failed_daily / d if d else 0.0

    @property
    def account_ending_rate(self) -> float:
        """Fraction of DECISIVE runs that LOST THE ACCOUNT — breached EITHER the
        max-DD or the daily-DD limit. Both are hard failures that end a prop
        evaluation, so this (not blowup_rate alone) is the true survival metric
        the deployable gate checks. (Timeouts are excluded — running out of
        calendar time is 'didn't pass in time', not an account loss.)"""
        d = self.decisive_runs
        return (self.failed_max + self.failed_daily) / d if d else 0.0

    @property
    def incomplete_rate(self) -> float:
        """Fraction of start dates that ran out of history before a verdict."""
        return self.incompletes / self.runs if self.runs else 0.0

    @property
    def avg_days_to_pass(self) -> float:
        return sum(self.days_to_pass) / len(self.days_to_pass) if self.days_to_pass else 0.0

    @property
    def median_days_to_pass(self) -> float:
        if not self.days_to_pass:
            return 0.0
        s = sorted(self.days_to_pass)
        return float(s[len(s) // 2])

    @property
    def worst_dd_pct(self) -> float:
        return max(self.worst_dd_pcts) if self.worst_dd_pcts else 0.0

    def summary_text(self) -> str:
        return (
            f"runs={self.runs} (decisive={self.decisive_runs}) "
            f"pass={self.pass_rate*100:.1f}% "
            f"(P1={self.phase1_pass_rate*100:.1f}%) "
            f"accountEnding={self.account_ending_rate*100:.1f}% "
            f"(maxDD={self.blowup_rate*100:.1f}% daily={self.daily_halt_rate*100:.1f}%) "
            f"incomplete={self.incomplete_rate*100:.1f}% "
            f"avgDays={self.avg_days_to_pass:.0f} medDays={self.median_days_to_pass:.0f} "
            f"worstDD={self.worst_dd_pct:.1f}%"
        )


# ─── Building the % return stream ────────────────────────────────────────────


def _trading_day(entry_ms: float, reset_utc_offset_hours: float) -> date:
    dt = datetime.fromtimestamp(entry_ms / 1000, tz=timezone.utc) + timedelta(
        hours=reset_utc_offset_hours
    )
    return dt.date()


def from_simulated_trades(
    trades: list[SimulatedTrade],
    ref_balance: float,
    reset_utc_offset_hours: float = 0.0,
) -> list[TradeReturn]:
    """Reduce backtest trades to a chronological %-of-balance return stream.

    ``ref_balance`` is the balance the trades were sized against (the backtest's
    starting balance with fixed, non-compounding risk). Because risk is fixed,
    each trade's % return is stable and can be risk-scaled later.

    The adverse-excursion magnitude is expressed as a positive %; trade costs are
    added to it so the worst intratrade equity dip is modelled conservatively.
    """
    out: list[TradeReturn] = []
    for t in trades:
        inst = get_instrument(t.instrument)
        pv = inst.point_value_per_lot
        ret_pct = t.net_pnl_usd / ref_balance * 100.0
        mae_usd = t.mae_price * pv * t.lots + t.cost_usd
        mae_ret_pct = max(0.0, mae_usd / ref_balance * 100.0)
        out.append(TradeReturn(
            entry_ms=t.entry_time_ms,
            exit_ms=t.exit_time_ms,
            ret_pct=ret_pct,
            mae_ret_pct=mae_ret_pct,
            trading_day=_trading_day(t.entry_time_ms, reset_utc_offset_hours),
        ))
    out.sort(key=lambda r: (r.exit_ms, r.entry_ms))
    return out


def _has_overlap(returns: list[TradeReturn]) -> bool:
    """True if any two trades in the stream are open at the same time.

    When trades overlap, the sequential phase sim (one position at a time) would
    UNDERSTATE drawdown — two simultaneously-adverse trades stack. Detecting this
    routes the slice through the concurrency-aware phase sim instead.
    """
    ordered = sorted(returns, key=lambda r: r.entry_ms)
    max_exit = float("-inf")
    for r in ordered:
        if r.entry_ms < max_exit - 1e-6:
            return True
        max_exit = max(max_exit, r.exit_ms)
    return False


# ─── Phase / challenge simulation ────────────────────────────────────────────


def simulate_phase(
    returns: list[TradeReturn],
    start_index: int,
    rules: ChallengeRules,
    target_pct: float,
    risk_scale: float = 1.0,
) -> PhaseResult:
    """
    Simulate ONE evaluation phase from ``start_index`` until it passes, breaches
    a DD limit, times out, or runs out of trades. Fresh account state (equity 0).
    """
    equity = 0.0            # realized cumulative return %, phase-relative
    peak = 0.0
    worst_dd = 0.0          # deepest dip below 0 (initial), as positive %
    day: date | None = None
    sod_equity = 0.0        # start-of-day equity %
    trading_days: set[date] = set()

    n = len(returns)
    if start_index >= n:
        return PhaseResult(Outcome.INCOMPLETE, 0.0, 0, 0, 0.0, start_index)

    start_ms = returns[start_index].entry_ms
    i = start_index
    while i < n:
        tr = returns[i]

        # Time limit (per phase), if any.
        if rules.max_calendar_days is not None:
            if (tr.entry_ms - start_ms) / 86_400_000 > rules.max_calendar_days:
                return PhaseResult(
                    Outcome.TIMEOUT, equity, len(trading_days), i - start_index, worst_dd, i
                )

        # New firm-local day -> reset the daily baseline.
        if tr.trading_day != day:
            day = tr.trading_day
            sod_equity = equity
        trading_days.add(tr.trading_day)

        ret = tr.ret_pct * risk_scale
        mae = tr.mae_ret_pct * risk_scale

        # Worst intratrade equity (money-safe): floating dips by the adverse move.
        low = equity - mae
        worst_dd = max(worst_dd, -low if low < 0 else 0.0)

        # Max DD breach (account-ending). Static: floor from initial; trailing: from peak.
        max_floor = (peak - rules.max_dd_pct) if rules.dd_mode == "trailing" else -rules.max_dd_pct
        if low <= max_floor + 1e-9:
            return PhaseResult(
                Outcome.FAIL_MAX_DD, low, len(trading_days), i - start_index + 1, worst_dd, i + 1
            )

        # Daily DD breach (from start-of-day equity).
        if low <= (sod_equity - rules.daily_dd_pct) + 1e-9:
            return PhaseResult(
                Outcome.FAIL_DAILY_DD, low, len(trading_days), i - start_index + 1, worst_dd, i + 1
            )

        # Book the realized result.
        equity += ret
        peak = max(peak, equity)

        # Target reached (and enough trading days)?
        if equity >= target_pct - 1e-9 and len(trading_days) >= rules.min_trading_days:
            return PhaseResult(
                Outcome.PASS, equity, len(trading_days), i - start_index + 1, worst_dd, i + 1
            )
        i += 1

    return PhaseResult(Outcome.INCOMPLETE, equity, len(trading_days), n - start_index, worst_dd, n)


# Event kinds for the concurrency-aware walk (OPEN sorts before CLOSE at an equal
# timestamp, so a bar that opens one trade as another closes counts both — the
# conservative choice).
_OPEN, _CLOSE = 0, 1


def simulate_phase_concurrent(
    returns: list[TradeReturn],
    start_index: int,
    rules: ChallengeRules,
    target_pct: float,
    risk_scale: float = 1.0,
) -> PhaseResult:
    """Concurrency-aware phase sim (for slices whose trades OVERLAP in time).

    Identical rules to ``simulate_phase`` (targets, daily/max DD, money-safe check
    BEFORE booking), but the floating drawdown at any instant stacks the max
    adverse excursion of EVERY currently-open trade — so two simultaneously-
    adverse trades produce a combined dip (the honest worst case), instead of the
    sequential sim's one-at-a-time understatement.

    Realized PnL of each trade is unchanged (concurrency doesn't change a trade's
    own result), so the equity/target progression matches the sequential sim on
    non-overlapping input; only the floating-DD magnitude differs when trades
    actually overlap.
    """
    n = len(returns)
    if start_index >= n:
        return PhaseResult(Outcome.INCOMPLETE, 0.0, 0, 0, 0.0, start_index)

    trades = returns[start_index:]
    start_ms = trades[0].entry_ms

    # Build OPEN/CLOSE events; OPEN before CLOSE at equal time (max concurrency).
    events: list[tuple[float, int, int]] = []
    for k, t in enumerate(trades):
        events.append((t.entry_ms, _OPEN, k))
        events.append((t.exit_ms, _CLOSE, k))
    events.sort(key=lambda e: (e[0], e[1]))

    realized = 0.0
    peak = 0.0
    worst_dd = 0.0
    day: date | None = None
    sod_equity = 0.0
    trading_days: set[date] = set()
    open_mae: dict[int, float] = {}      # k -> mae%(scaled) of currently-open trades
    booked = 0
    last_booked_k = -1

    def _breach(low: float) -> Outcome | None:
        max_floor = (peak - rules.max_dd_pct) if rules.dd_mode == "trailing" else -rules.max_dd_pct
        if low <= max_floor + 1e-9:
            return Outcome.FAIL_MAX_DD
        if low <= (sod_equity - rules.daily_dd_pct) + 1e-9:
            return Outcome.FAIL_DAILY_DD
        return None

    for t_ms, kind, k in events:
        tr = trades[k]

        if rules.max_calendar_days is not None:
            if (t_ms - start_ms) / 86_400_000 > rules.max_calendar_days:
                return PhaseResult(Outcome.TIMEOUT, realized, len(trading_days),
                                   booked, worst_dd, start_index + last_booked_k + 1)

        # New firm-local day (by event time) -> reset the daily baseline.
        ev_day = _trading_day(t_ms, rules.reset_utc_offset_hours)
        if ev_day != day:
            day = ev_day
            sod_equity = realized

        if kind == _OPEN:
            trading_days.add(tr.trading_day)
            open_mae[k] = tr.mae_ret_pct * risk_scale
        # Worst-case simultaneous floating equity: all open trades at their MAE.
        low = realized - sum(open_mae.values())
        worst_dd = max(worst_dd, -low if low < 0 else 0.0)
        breach = _breach(low)
        if breach is not None:
            return PhaseResult(breach, low, len(trading_days),
                               booked + 1, worst_dd, start_index + last_booked_k + 1)

        if kind == _CLOSE:
            realized += tr.ret_pct * risk_scale
            peak = max(peak, realized)
            open_mae.pop(k, None)
            booked += 1
            last_booked_k = max(last_booked_k, k)
            if realized >= target_pct - 1e-9 and len(trading_days) >= rules.min_trading_days:
                return PhaseResult(Outcome.PASS, realized, len(trading_days),
                                   booked, worst_dd, start_index + last_booked_k + 1)

    return PhaseResult(Outcome.INCOMPLETE, realized, len(trading_days), booked, worst_dd, n)


def simulate_challenge(
    returns: list[TradeReturn],
    start_index: int,
    rules: ChallengeRules,
    risk_scale: float = 1.0,
    concurrent: bool = False,
) -> ChallengeResult:
    """Run Phase 1, then (if it passed and phase2_target_pct > 0) Phase 2.

    ``concurrent`` selects the concurrency-aware phase sim (for slices with
    overlapping trades); default False keeps the proven sequential path.
    """
    phase_fn = simulate_phase_concurrent if concurrent else simulate_phase
    start_ms = returns[start_index].entry_ms if start_index < len(returns) else 0.0
    p1 = phase_fn(returns, start_index, rules, rules.phase1_target_pct, risk_scale)

    if not p1.passed or rules.phase2_target_pct <= 0:
        outcome = p1.outcome if not p1.passed else Outcome.PASS
        return ChallengeResult(outcome, p1, None, p1.trading_days, start_ms)

    p2 = phase_fn(returns, p1.next_index, rules, rules.phase2_target_pct, risk_scale)
    outcome = Outcome.PASS if p2.passed else p2.outcome
    return ChallengeResult(outcome, p1, p2, p1.trading_days + p2.trading_days, start_ms)


# ─── Monte-Carlo over history ────────────────────────────────────────────────


def monte_carlo(
    returns: list[TradeReturn],
    rules: ChallengeRules,
    risk_scale: float = 1.0,
    step_days: int = 7,
    concurrent: bool | None = None,
) -> MonteCarloResult:
    """
    Start a fresh challenge every ``step_days`` calendar days across the whole
    trade history and aggregate the outcomes. This is the robustness metric:
    pass rate, blow-up rate, days-to-pass and worst DD across all market regimes.

    ``concurrent``: model overlapping trades' drawdown as worst-case simultaneous
    (stacked MAE). ``None`` = auto: on if the slice actually has time-overlapping
    trades, off otherwise (so a clean one-at-a-time slice keeps the proven
    sequential path and identical numbers).
    """
    res = MonteCarloResult()
    if not returns:
        return res

    if concurrent is None:
        concurrent = _has_overlap(returns)

    # Map each start date (every step_days) to the first trade index on/after it.
    first_day = _dt(returns[0].entry_ms)
    last_day = _dt(returns[-1].entry_ms)
    seen_indices: set[int] = set()

    cur = first_day
    j = 0
    while cur <= last_day:
        cur_ms = cur.timestamp() * 1000
        while j < len(returns) and returns[j].entry_ms < cur_ms:
            j += 1
        if j >= len(returns):
            break
        if j not in seen_indices:
            seen_indices.add(j)
            cr = simulate_challenge(returns, j, rules, risk_scale, concurrent=concurrent)
            _tally(res, cr)
        cur += timedelta(days=step_days)

    return res


def _tally(res: MonteCarloResult, cr: ChallengeResult) -> None:
    res.runs += 1
    if cr.phase1.passed:
        res.phase1_passed += 1
    worst = cr.phase1.worst_overall_dd_pct
    if cr.phase2 is not None:
        worst = max(worst, cr.phase2.worst_overall_dd_pct)
    res.worst_dd_pcts.append(worst)

    if cr.passed:
        res.passed += 1
        res.days_to_pass.append(cr.total_trading_days)
    elif cr.outcome is Outcome.FAIL_MAX_DD:
        res.failed_max += 1
    elif cr.outcome is Outcome.FAIL_DAILY_DD:
        res.failed_daily += 1
    elif cr.outcome is Outcome.TIMEOUT:
        res.timeouts += 1
    else:
        res.incompletes += 1


def _dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
