"""
Entry replay + exit sweep — turn an entry strategy into tagged trades.

Walks the candle series for one instrument, asks the entry strategy for entries
on each just-closed bar, and for EACH entry resolves it under EACH exit model in
the sweep. Every resulting trade is tagged with the entry context (session,
regime, volatility, exit_model, timeframe) so the slice scorer can group and
score by any combination.

Conventions (money-safe, matching the rest of the backtest):
    * The entry is filled at the breakout bar's close; exit management starts on
      the NEXT bar (no same-bar entry+exit).
    * UNCONSTRAINED: every entry the strategy emits is recorded (no daily/stack
      caps here) — prop-firm limits are applied later at the scoring layer.
    * Sizing is constant (risk_pct off ref_balance), so % returns are stable and
      risk-scalable in the challenge sim.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_research.entry_strategy import EntryContext, EntryStrategy
from app.cfd_research.exit_models import ExitModel, default_exit_models, simulate_entry
from app.cfd_research.regime import classify_regime, classify_volatility
from app.cfd_risk.costs import COST_MODEL_INTRADAY, CFDCostModel
from app.core.models import Candle
from app.strategy.indicators import atr
from app.utils import forex_hours
from app.utils.logger import get_logger

logger = get_logger(__name__)


def replay_entries(
    instrument: str,
    candles: list[Candle],
    strategy: EntryStrategy,
    exit_models: list[ExitModel] | None = None,
    *,
    risk_pct: float = 1.0,
    ref_balance: float = 100_000.0,
    cost_model: CFDCostModel | None = None,
    atr_period: int = 14,
    max_hold_bars: int = 2000,
) -> list[SimulatedTrade]:
    """Resolve every entry under every exit model; return tagged trades."""
    if not strategy.applies_to(instrument):
        return []
    models = exit_models or default_exit_models()
    cost_model = cost_model or COST_MODEL_INTRADAY

    trades: list[SimulatedTrade] = []
    tf = strategy.timeframe.value
    n = len(candles)

    for i in range(n):
        history = candles[: i + 1]
        if len(history) < strategy.min_history:
            continue

        ctx = EntryContext(
            instrument=instrument, timeframe=strategy.timeframe,
            candle=candles[i], history=history,
        )
        try:
            intents = strategy.entries(ctx)
        except Exception as e:  # noqa: BLE001 - a bad strategy must not kill the run
            logger.error("entries() error %s on %s: %s", strategy.strategy_id, instrument, e)
            continue
        if not intents:
            continue

        future = candles[i + 1: i + 1 + max_hold_bars]
        if not future:
            continue

        # Entry-context tags (computed once per entry bar, shared by all exits).
        entry_dt = datetime.fromtimestamp(candles[i].timestamp_ms / 1000, timezone.utc)
        session = forex_hours.session_tag(entry_dt)
        regime = classify_regime(history)
        volatility = classify_volatility(history)
        atr_at_entry = atr(history, atr_period)

        for intent in intents:
            for model in models:
                t = simulate_entry(
                    intent, future, model,
                    risk_pct=risk_pct, ref_balance=ref_balance,
                    cost_model=cost_model, atr_at_entry=atr_at_entry,
                )
                if t is None:
                    continue
                t.session = session
                t.regime = regime
                t.volatility = volatility
                t.timeframe = tf
                # t.exit_model already set by simulate_entry
                trades.append(t)

    return trades
