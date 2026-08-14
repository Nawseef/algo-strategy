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
from app.cfd_research.timeframe import aggregate_with_index
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
    history_window: int = 400,
    intraday_only: bool = True,
) -> list[SimulatedTrade]:
    """Resolve every entry under every exit model; return tagged trades.

    ``history_window`` bounds how many trailing bars are passed to the strategy /
    regime classifier each step. This keeps the walk O(n * window) instead of
    O(n^2): building ``candles[:i+1]`` every bar is quadratic and made a 10-year
    run take hours. 400 bars comfortably covers the regime/volatility lookbacks
    (EMA50 / ATR100) and typical indicators; raise it for a strategy that needs
    more trailing history.
    """
    if not strategy.applies_to(instrument):
        return []
    models = exit_models or default_exit_models()
    cost_model = cost_model or COST_MODEL_INTRADAY
    # MULTI-TIMEFRAME: the strategy runs on its declared timeframe. We only store
    # 5m, so aggregate the base series up to the strategy's timeframe (identity if
    # it IS 5m, so the base path is unchanged). ``last_base_idx[i]`` is the index
    # of the last 5m bar composing HTF bar i — exits resolve on the 5m bars AFTER
    # it, so the signal bar's own sub-bars never leak in and stops keep 5m fidelity.
    htf_candles, last_base_idx = aggregate_with_index(candles, strategy.timeframe)

    window = max(history_window, strategy.min_history)

    trades: list[SimulatedTrade] = []
    tf = strategy.timeframe.value
    n = len(htf_candles)

    for i in range(n):
        if i + 1 < strategy.min_history:
            continue
        history = htf_candles[max(0, i + 1 - window): i + 1]

        ctx = EntryContext(
            instrument=instrument, timeframe=strategy.timeframe,
            candle=htf_candles[i], history=history,
        )
        try:
            intents = strategy.entries(ctx)
        except Exception as e:  # noqa: BLE001 - a bad strategy must not kill the run
            logger.error("entries() error %s on %s: %s", strategy.strategy_id, instrument, e)
            continue
        if not intents:
            continue

        entry_dt = datetime.fromtimestamp(htf_candles[i].timestamp_ms / 1000, timezone.utc)
        # Exit management runs on the BASE (5m) bars that come strictly after this
        # HTF bar has closed (its last constituent 5m bar is last_base_idx[i]).
        fut_start = last_base_idx[i] + 1
        future = candles[fut_start: fut_start + max_hold_bars]

        # G2 — INTRADAY: flatten by the end of the FX trading day the trade was
        # opened in (forex_hours.trading_day rolls at 17:00 New York, the broker
        # rollover). This prevents crediting free overnight/weekend trend moves
        # to the trailing/runner exits and keeps the strategy genuinely intraday
        # (no swap charged because nothing is held overnight). Any position still
        # open at the day boundary is force-flattened at the last in-day close by
        # the exit model's _flatten (EOD_FLATTEN).
        if intraday_only:
            entry_day = forex_hours.trading_day(entry_dt)
            capped: list[Candle] = []
            for c in future:
                c_dt = datetime.fromtimestamp(c.timestamp_ms / 1000, timezone.utc)
                if forex_hours.trading_day(c_dt) != entry_day:
                    break
                capped.append(c)
            future = capped
        if not future:
            continue

        # Entry-context tags (computed once per entry bar, shared by all exits).
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
                t.strategy_id = strategy.strategy_id   # clean attribution (G1)
                t.session = session
                t.regime = regime
                t.volatility = volatility
                t.timeframe = tf
                # t.exit_model already set by simulate_entry
                trades.append(t)

    return trades
