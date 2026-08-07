"""
Strategy/variant search harness — find the candidates that actually pass.

This is the engine that turns "here are some candidate strategies" into a ranked
shortlist of "(strategy, instrument, risk) combos that pass a prop challenge and
rarely blow up," measured over the full history via the challenge simulator.

Flow:
    for each (strategy, instrument):
        trades = backtest over [start, end]                     (once, at ref risk)
        returns = reduce to % stream
        for each risk level in the sweep:
            score = monte_carlo(returns, rules, risk_scale)      (pass %, blow-up %)
    rank all rows by (pass rate desc, blow-up asc, phase-1 pass desc)

Two design decisions that matter:
    * The backtest function is INJECTABLE. In production it wraps
      ``CFDBacktestReplay`` (needs the Postgres history); in tests we inject a
      fake that returns synthetic trades, so the harness is fully testable
      offline with no database.
    * Risk is swept by SCALING, not re-backtesting. Because per-trade PnL is
      linear in risk, one backtest at a reference risk (e.g. 1%) is re-scored at
      0.5% / 1% / etc. via ``risk_scale`` — fast, and keeps every risk level on
      the identical trade sequence.

Each (strategy, instrument) is scored with the SINGLE-STREAM ``challenge_sim``
(one instrument per strategy has no self-overlap). To evaluate a chosen SET as a
shared account, feed the winners to ``assemble_portfolio`` (uses the
concurrency-aware ``portfolio_sim``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_research.challenge_sim import (
    ChallengeRules,
    MonteCarloResult,
    from_simulated_trades,
    monte_carlo,
)
from app.cfd_research.portfolio_sim import (
    PortfolioConstraints,
    from_strategy_trades,
    monte_carlo_portfolio,
)
from app.cfd_strategy.base import CFDStrategy
from app.utils.logger import get_logger

logger = get_logger(__name__)

# (strategy, instrument, start, end, ref_risk_pct) -> trades for that combo.
BacktestFn = Callable[[CFDStrategy, str, date, date, float], list[SimulatedTrade]]


@dataclass
class SearchConfig:
    start_date: date
    end_date: date
    rules: ChallengeRules = field(default_factory=ChallengeRules)
    ref_risk_pct: float = 1.0                      # risk the backtest sizes each trade at
    risk_levels: tuple[float, ...] = (0.5, 1.0)    # per-trade risk % to score
    ref_balance: float = 100_000.0
    reset_utc_offset_hours: float = 0.0
    step_days: int = 7                             # Monte-Carlo start cadence
    min_trades: int = 30                           # ignore combos with too few trades


@dataclass
class SearchResult:
    strategy_id: str
    instrument: str
    risk_pct: float
    trade_count: int
    mc: MonteCarloResult

    @property
    def sort_key(self) -> tuple:
        # Best first: highest pass rate, then lowest blow-up, then highest P1 pass.
        return (-self.mc.pass_rate, self.mc.blowup_rate, -self.mc.phase1_pass_rate)

    def row(self) -> str:
        return (
            f"{self.strategy_id:20s} {self.instrument:8s} risk={self.risk_pct:>4.2f}% "
            f"| n={self.trade_count:<5d} pass={self.mc.pass_rate*100:5.1f}% "
            f"P1={self.mc.phase1_pass_rate*100:5.1f}% blowup={self.mc.blowup_rate*100:5.1f}% "
            f"medDays={self.mc.median_days_to_pass:>3.0f} worstDD={self.mc.worst_dd_pct:4.1f}%"
        )


def run_search(
    strategies: list[CFDStrategy],
    instruments: list[str],
    config: SearchConfig,
    backtest_fn: BacktestFn,
) -> tuple[list[SearchResult], dict[tuple[str, str], list[SimulatedTrade]]]:
    """
    Backtest + score every applicable (strategy, instrument) at each risk level.

    Returns (ranked results, trades_cache). ``trades_cache`` is keyed by
    ``(strategy_id, instrument)`` and reused by ``assemble_portfolio`` so we never
    re-backtest the winners.
    """
    results: list[SearchResult] = []
    trades_cache: dict[tuple[str, str], list[SimulatedTrade]] = {}

    for strat in strategies:
        for instrument in instruments:
            if not strat.applies_to(instrument):
                continue
            try:
                trades = backtest_fn(
                    strat, instrument, config.start_date, config.end_date, config.ref_risk_pct
                )
            except Exception as e:  # noqa: BLE001 - one bad combo must not kill the sweep
                logger.error("backtest failed for %s/%s: %s", strat.strategy_id, instrument, e)
                continue
            if len(trades) < config.min_trades:
                logger.info("  %s/%s: %d trades (< %d) — skipped",
                            strat.strategy_id, instrument, len(trades), config.min_trades)
                continue

            trades_cache[(strat.strategy_id, instrument)] = trades
            returns = from_simulated_trades(
                trades, config.ref_balance, config.reset_utc_offset_hours
            )
            for level in config.risk_levels:
                scale = level / config.ref_risk_pct if config.ref_risk_pct else 1.0
                mc = monte_carlo(returns, config.rules, risk_scale=scale, step_days=config.step_days)
                results.append(SearchResult(
                    strategy_id=strat.strategy_id, instrument=instrument,
                    risk_pct=level, trade_count=len(trades), mc=mc,
                ))

    results.sort(key=lambda r: r.sort_key)
    return results, trades_cache


def assemble_portfolio(
    selected: list[tuple[str, str]],
    trades_cache: dict[tuple[str, str], list[SimulatedTrade]],
    config: SearchConfig,
    constraints: PortfolioConstraints,
    risk_pct: float,
) -> MonteCarloResult:
    """
    Score a chosen SET of (strategy, instrument) combos as ONE shared prop
    account, using the concurrency-aware portfolio simulator.

    ``selected`` are keys into ``trades_cache`` (from ``run_search``). Each combo
    contributes its trades under a distinct label so overlaps across combos are
    modelled (the whole point — combined drawdown).
    """
    trades_by_source: dict[str, list[SimulatedTrade]] = {}
    for key in selected:
        if key not in trades_cache:
            logger.warning("assemble_portfolio: %s not in trades cache — skipped", key)
            continue
        trades_by_source[f"{key[0]}:{key[1]}"] = trades_cache[key]

    legs = from_strategy_trades(
        trades_by_source, config.ref_balance, per_trade_risk_pct=risk_pct,
        reset_utc_offset_hours=config.reset_utc_offset_hours,
    )
    return monte_carlo_portfolio(
        legs, config.rules, constraints, risk_scale=1.0, step_days=config.step_days
    )


def format_results(results: list[SearchResult], top: int | None = 25) -> str:
    """Render a ranked table for the console/log."""
    rows = results[:top] if top else results
    if not rows:
        return "(no qualifying results)"
    header = (
        f"{'strategy':20s} {'instr':8s} {'risk':>6s} | "
        f"{'trades':>7s} {'pass':>7s} {'P1':>7s} {'blowup':>8s} {'medDays':>8s} {'worstDD':>8s}"
    )
    lines = [header, "-" * len(header)]
    lines.extend(r.row() for r in rows)
    return "\n".join(lines)
