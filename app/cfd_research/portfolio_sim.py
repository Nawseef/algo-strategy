"""
Portfolio challenge simulator — a SET of strategies on ONE prop account.

Why this exists (the money-critical bit):
    ``challenge_sim`` scores a single strategy's trades one at a time. But on a
    real prop account you run several strategies that SHARE one balance and one
    drawdown budget. If two positions are open at once and both move against you,
    the account feels the COMBINED floating loss — that is exactly how a set of
    individually-safe strategies can still blow a daily/max drawdown. This module
    models that shared account faithfully.

How it models the account (event-based):
    * Every trade becomes an OPEN event (at entry) and a CLOSE event (at exit).
      Events are processed in time order (closes before opens at the same
      instant, so freed capacity/risk is available again).
    * Realized equity changes only on CLOSE (you book the PnL then).
    * DRAWDOWN is checked against the WORST-CASE SIMULTANEOUS floating loss:
      while a set of positions is open, we assume they could ALL sit at their
      worst adverse excursion at the same time. That is deliberately
      conservative — it never understates drawdown, so it would rather reject a
      marginal combo than let it blow up. (Real timing rarely lines up that
      badly; with few concurrent trades and hard stops the overstatement is
      small.)

Entry constraints (a new position is SKIPPED — never opened — if it would break
a rule; its later close is skipped too, matching how the live runner behaves):
    * max_trades_per_day       — your "2-3 trades/day per account" cap
    * max_concurrent_positions — how many can be open at once
    * max_open_risk_pct        — cap on the SUM of open positions' risk
    * one_position_per_instrument

Everything is in % of the starting balance (account-size agnostic), and
``risk_scale`` sweeps per-trade risk (0.5% vs 1%) without re-backtesting.
Phases are independent accounts (Phase 2 starts fresh), as in a real evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_research.challenge_sim import (
    ChallengeRules,
    MonteCarloResult,
    Outcome,
    _trading_day,
)
from app.cfd_risk.instruments import get_instrument
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PortfolioConstraints:
    """Account-level caps shared across all strategies on one account."""

    max_trades_per_day: int = 3
    max_concurrent_positions: int = 3
    max_open_risk_pct: float = 3.0          # cap on SUM of open positions' risk (% of balance)
    one_position_per_instrument: bool = True


@dataclass
class PortfolioLeg:
    """One trade on the shared account (from any strategy)."""

    strategy_id: str
    instrument: str
    entry_ms: float
    exit_ms: float
    ret_pct: float          # realized net return, % of starting balance
    mae_ret_pct: float      # worst adverse excursion during the trade (>= 0), % + costs
    risk_pct: float         # per-trade risk used (% of balance), for the open-risk cap
    trading_day: date


@dataclass
class PortfolioPhaseResult:
    outcome: Outcome
    end_return_pct: float
    trading_days: int
    trades_taken: int
    trades_skipped: int         # entries rejected by the caps
    worst_overall_dd_pct: float
    end_ms: float               # time the phase resolved (to resume phase 2)

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS


@dataclass
class PortfolioChallengeResult:
    outcome: Outcome
    phase1: PortfolioPhaseResult
    phase2: PortfolioPhaseResult | None
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


# ─── Build legs from backtest trades ─────────────────────────────────────────


def from_strategy_trades(
    trades_by_strategy: dict[str, list[SimulatedTrade]],
    ref_balance: float,
    per_trade_risk_pct: float,
    reset_utc_offset_hours: float = 0.0,
) -> list[PortfolioLeg]:
    """Flatten many strategies' backtest trades into one time-sortable leg list.

    ``per_trade_risk_pct`` is the risk the backtest sized each trade at (constant,
    non-compounding), used for the open-risk cap. PnL and adverse-excursion are
    reduced to % of ``ref_balance`` (costs folded into the adverse move — the
    money-safe worst case).
    """
    legs: list[PortfolioLeg] = []
    for strategy_id, trades in trades_by_strategy.items():
        for t in trades:
            pv = get_instrument(t.instrument).point_value_per_lot
            ret_pct = t.net_pnl_usd / ref_balance * 100.0
            mae_usd = t.mae_price * pv * t.lots + t.cost_usd
            mae_ret_pct = max(0.0, mae_usd / ref_balance * 100.0)
            legs.append(PortfolioLeg(
                strategy_id=strategy_id,
                instrument=t.instrument,
                entry_ms=t.entry_time_ms,
                exit_ms=t.exit_time_ms,
                ret_pct=ret_pct,
                mae_ret_pct=mae_ret_pct,
                risk_pct=per_trade_risk_pct,
                trading_day=_trading_day(t.entry_time_ms, reset_utc_offset_hours),
            ))
    legs.sort(key=lambda l: (l.entry_ms, l.exit_ms))
    return legs


# ─── Event-based phase simulation ────────────────────────────────────────────

# Event kinds; CLOSE sorts before OPEN at the same timestamp (free capacity first).
_CLOSE, _OPEN = 0, 1


def simulate_portfolio_phase(
    legs: list[PortfolioLeg],
    start_ms: float,
    rules: ChallengeRules,
    constraints: PortfolioConstraints,
    target_pct: float,
    risk_scale: float = 1.0,
) -> PortfolioPhaseResult:
    """Simulate ONE phase on the shared account over legs with entry >= start_ms."""
    phase_legs = [l for l in legs if l.entry_ms >= start_ms]
    if not phase_legs:
        return PortfolioPhaseResult(Outcome.INCOMPLETE, 0.0, 0, 0, 0, 0.0, start_ms)

    # Build the event stream. Each accepted OPEN later triggers its CLOSE.
    events: list[tuple[float, int, PortfolioLeg]] = []
    for leg in phase_legs:
        events.append((leg.entry_ms, _OPEN, leg))
        events.append((leg.exit_ms, _CLOSE, leg))
    events.sort(key=lambda e: (e[0], e[1]))

    equity = 0.0
    peak = 0.0
    worst_dd = 0.0
    day: date | None = None
    sod_equity = 0.0
    trading_days: set[date] = set()
    trades_today = 0
    trades_taken = 0
    trades_skipped = 0

    open_legs: list[PortfolioLeg] = []
    open_instruments: set[str] = set()
    open_risk = 0.0
    accepted: set[int] = set()      # id(leg) of legs that were actually opened

    phase_start = phase_legs[0].entry_ms

    def worst_floating() -> float:
        # Worst-case simultaneous floating: all open positions at their MAE.
        return equity - sum(l.mae_ret_pct * risk_scale for l in open_legs)

    def dd_breach() -> Outcome | None:
        low = worst_floating()
        max_floor = (peak - rules.max_dd_pct) if rules.dd_mode == "trailing" else -rules.max_dd_pct
        if low <= max_floor + 1e-9:
            return Outcome.FAIL_MAX_DD
        if low <= (sod_equity - rules.daily_dd_pct) + 1e-9:
            return Outcome.FAIL_DAILY_DD
        return None

    def result(outcome: Outcome, end_ms: float) -> PortfolioPhaseResult:
        return PortfolioPhaseResult(
            outcome, equity, len(trading_days), trades_taken, trades_skipped, worst_dd, end_ms,
        )

    for ev_ms, kind, leg in events:
        # Time limit (per phase).
        if rules.max_calendar_days is not None:
            if (ev_ms - phase_start) / 86_400_000 > rules.max_calendar_days:
                return result(Outcome.TIMEOUT, ev_ms)

        # Day rollover (resets daily-loss baseline + per-day trade count).
        ev_day = _trading_day(ev_ms, rules.reset_utc_offset_hours)
        if ev_day != day:
            day = ev_day
            sod_equity = equity
            trades_today = 0

        if kind == _CLOSE:
            if id(leg) in accepted:
                open_legs.remove(leg)
                open_instruments.discard(leg.instrument)
                open_risk -= leg.risk_pct * risk_scale
                equity += leg.ret_pct * risk_scale
                peak = max(peak, equity)
        else:  # OPEN — apply entry constraints; skip the trade if any is violated.
            trading_days.add(ev_day)
            reject = (
                trades_today >= constraints.max_trades_per_day
                or len(open_legs) >= constraints.max_concurrent_positions
                or (constraints.one_position_per_instrument and leg.instrument in open_instruments)
                or (open_risk + leg.risk_pct * risk_scale) > constraints.max_open_risk_pct + 1e-9
            )
            if reject:
                trades_skipped += 1
            else:
                open_legs.append(leg)
                open_instruments.add(leg.instrument)
                open_risk += leg.risk_pct * risk_scale
                trades_today += 1
                trades_taken += 1
                accepted.add(id(leg))

        # Update worst DD and check breaches with the current open set.
        low = worst_floating()
        if low < 0:
            worst_dd = max(worst_dd, -low)
        breach = dd_breach()
        if breach is not None:
            return result(breach, ev_ms)

        # Target check (realized equity only meaningfully changes on closes).
        if kind == _CLOSE and equity >= target_pct - 1e-9 and len(trading_days) >= rules.min_trading_days:
            return result(Outcome.PASS, ev_ms)

    return result(Outcome.INCOMPLETE, events[-1][0])


def simulate_portfolio_challenge(
    legs: list[PortfolioLeg],
    start_ms: float,
    rules: ChallengeRules,
    constraints: PortfolioConstraints,
    risk_scale: float = 1.0,
) -> PortfolioChallengeResult:
    """Phase 1, then a fresh Phase 2 (if P1 passed and phase2 target > 0)."""
    p1 = simulate_portfolio_phase(
        legs, start_ms, rules, constraints, rules.phase1_target_pct, risk_scale
    )
    if not p1.passed or rules.phase2_target_pct <= 0:
        outcome = Outcome.PASS if p1.passed else p1.outcome
        return PortfolioChallengeResult(outcome, p1, None, p1.trading_days, start_ms)

    # Phase 2 begins strictly after Phase 1 resolved.
    p2 = simulate_portfolio_phase(
        legs, p1.end_ms + 1, rules, constraints, rules.phase2_target_pct, risk_scale
    )
    outcome = Outcome.PASS if p2.passed else p2.outcome
    return PortfolioChallengeResult(
        outcome, p1, p2, p1.trading_days + p2.trading_days, start_ms
    )


# ─── Monte-Carlo over history ────────────────────────────────────────────────


def monte_carlo_portfolio(
    legs: list[PortfolioLeg],
    rules: ChallengeRules,
    constraints: PortfolioConstraints,
    risk_scale: float = 1.0,
    step_days: int = 7,
) -> MonteCarloResult:
    """Start a fresh shared-account challenge every ``step_days`` across history."""
    res = MonteCarloResult()
    if not legs:
        return res

    first = datetime.fromtimestamp(legs[0].entry_ms / 1000, tz=timezone.utc)
    last = datetime.fromtimestamp(legs[-1].entry_ms / 1000, tz=timezone.utc)

    cur = first
    while cur <= last:
        cr = simulate_portfolio_challenge(
            legs, cur.timestamp() * 1000, rules, constraints, risk_scale
        )
        _tally_portfolio(res, cr)
        cur += timedelta(days=step_days)
    return res


def _tally_portfolio(res: MonteCarloResult, cr: PortfolioChallengeResult) -> None:
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
