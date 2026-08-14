"""
Persist research trades so slicing/scoring can be replayed WITHOUT re-walking
the 10 years of candles.

Generation (walk candles -> entries x exit sweep -> tagged trades) is the
expensive step (~20-40 min for a full TF sweep). Slicing, the gates, and the
challenge sim are cheap and are what you iterate on: different ``--dimensions``
(regime / volatility / session / TF cuts), different prop rulesets, risk levels,
gate thresholds. This module freezes the generated trades to a file so that
iteration is instant (seconds), and you only re-generate when something that
changes WHICH trades exist changes (entry logic/params, exit models, cost model,
timeframe set, intraday flatten).

Format: JSON Lines (stdlib ``json`` — no new deps, streamable line-by-line so it
never holds the whole file in memory as one string, and human-inspectable). The
FIRST line is a metadata header; every subsequent line is one trade. If the path
ends in ``.gz`` it is transparently gzip-compressed.

What is (and isn't) stored:
  * ALL scalar fields of ``SimulatedTrade`` plus its tags (instrument /
    strategy_id / session / regime / volatility / exit_model / timeframe) —
    everything the scorer reads (``from_simulated_trades`` + ``compute_deployability``
    + ``_slice_has_overlap`` + the grouping dimensions).
  * ``partials`` (the per-leg close breakdown) is NOT stored — the scoring layer
    never reads it (only detailed trade reports do). Loaded trades have
    ``partials == []``. Re-generate if you need leg detail.
  * The header carries ``ref_balance`` and ``ref_risk_pct`` (what the trades were
    SIZED at) and ``data_start_ms`` / ``data_end_ms`` (the generation window).
    The scorer needs these to compute %-returns, risk-scaling, and the
    consistency gate correctly.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from typing import IO

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_execution.base import ExitReason
from app.cfd_risk.costs import CFDCostModel, calculate_trade_cost
from app.cfd_risk.instruments import get_instrument
from app.cfd_strategy.base import Direction
from app.utils.logger import get_logger

logger = get_logger(__name__)

SCHEMA = "cfd_research_trades_v1"

# Scalar SimulatedTrade fields stored verbatim (enums handled separately below).
_SCALAR_FIELDS = (
    "instrument", "entry_price", "entry_time_ms", "exit_price", "exit_time_ms",
    "lots", "planned_rr", "realized_rr", "pnl_price", "pnl_usd", "cost_usd",
    "net_pnl_usd", "mfe_price", "mae_price", "bars_held", "closed",
    # research tags
    "strategy_id", "session", "regime", "volatility", "exit_model", "timeframe",
)


def _open(path: str, mode: str) -> IO:
    """Open plain or gzip based on extension. mode is 'r' or 'w' (text)."""
    if str(path).endswith(".gz"):
        return gzip.open(path, mode + "t", encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def _trade_to_dict(t: SimulatedTrade) -> dict:
    d = {f: getattr(t, f) for f in _SCALAR_FIELDS}
    # Enums stored by NAME (robust regardless of the enum's value type).
    d["direction"] = t.direction.name
    d["exit_reason"] = t.exit_reason.name
    return d


def _trade_from_dict(d: dict) -> SimulatedTrade:
    return SimulatedTrade(
        instrument=d["instrument"],
        direction=Direction[d["direction"]],
        entry_price=d["entry_price"],
        entry_time_ms=d["entry_time_ms"],
        exit_price=d["exit_price"],
        exit_time_ms=d["exit_time_ms"],
        exit_reason=ExitReason[d["exit_reason"]],
        lots=d["lots"],
        planned_rr=d["planned_rr"],
        realized_rr=d["realized_rr"],
        pnl_price=d["pnl_price"],
        pnl_usd=d["pnl_usd"],
        cost_usd=d["cost_usd"],
        net_pnl_usd=d["net_pnl_usd"],
        mfe_price=d["mfe_price"],
        mae_price=d["mae_price"],
        bars_held=d["bars_held"],
        closed=d.get("closed", True),
        # partials intentionally dropped (not used by scoring) -> default [].
        strategy_id=d.get("strategy_id", ""),
        session=d.get("session", ""),
        regime=d.get("regime", ""),
        volatility=d.get("volatility", ""),
        exit_model=d.get("exit_model", ""),
        timeframe=d.get("timeframe", ""),
    )


def save_trades(
    path: str,
    trades: list[SimulatedTrade],
    *,
    ref_balance: float,
    ref_risk_pct: float,
    data_start_ms: float | None = None,
    data_end_ms: float | None = None,
    extra_meta: dict | None = None,
) -> dict:
    """Write ``trades`` to ``path`` as JSONL (header line + one trade per line).

    Streams to disk (never builds one giant string), so it is safe for the
    millions of trades a full TF sweep produces. Returns the metadata header.
    """
    meta = {
        "_schema": SCHEMA,
        "count": len(trades),
        "ref_balance": ref_balance,
        "ref_risk_pct": ref_risk_pct,
        "data_start_ms": data_start_ms,
        "data_end_ms": data_end_ms,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra_meta:
        meta["gen"] = extra_meta

    with _open(path, "w") as fh:
        fh.write(json.dumps(meta) + "\n")
        for t in trades:
            fh.write(json.dumps(_trade_to_dict(t), separators=(",", ":")) + "\n")

    logger.info("persisted %d trades -> %s (ref_balance=%s, ref_risk=%s%%)",
                len(trades), path, ref_balance, ref_risk_pct)
    return meta


def load_trades(path: str) -> tuple[list[SimulatedTrade], dict]:
    """Load trades + metadata from a file written by :func:`save_trades`.

    Streams line-by-line. Raises ``ValueError`` on an empty file or a schema it
    doesn't recognize (fail loud rather than silently mis-score).
    """
    with _open(path, "r") as fh:
        header = fh.readline()
        if not header.strip():
            raise ValueError(f"empty or headerless trade file: {path}")
        meta = json.loads(header)
        if meta.get("_schema") != SCHEMA:
            raise ValueError(
                f"unrecognized trade-file schema {meta.get('_schema')!r} in {path} "
                f"(expected {SCHEMA!r})"
            )
        trades = [_trade_from_dict(json.loads(line)) for line in fh if line.strip()]

    logger.info("loaded %d trades <- %s (ref_balance=%s, ref_risk=%s%%)",
                len(trades), path, meta.get("ref_balance"), meta.get("ref_risk_pct"))
    return trades, meta


def recost_trades(trades: list[SimulatedTrade], cost_model: CFDCostModel) -> list[SimulatedTrade]:
    """Re-apply a DIFFERENT cost model to already-generated trades, IN PLACE.

    This is exact — NOT an approximation — because cost never affects the trade
    path or sizing:
      * lot size is a function of (risk%, stop distance) only, not cost;
      * the exit models trigger on PRICE levels / time, never on cost, so the
        entry, exit, MAE/MFE and gross PnL are identical under any cost model;
      * cost is a pure function of (instrument, lots, model) applied at the end.

    So re-costing = recompute the per-trade fee from the persisted GROSS PnL
    (``pnl_usd``, which is cost-free) and the stored lots/instrument, then
    re-derive net. This reproduces exactly what a fresh backtest with that cost
    model would have booked. The equivalence is pinned by a test
    (``tests/test_trade_store.py::test_recost_equals_fresh_generation``); if that
    test ever fails, this shortcut is NOT valid and you must re-run instead.

    Only ``cost_usd`` and ``net_pnl_usd`` change; ``pnl_usd`` (gross),
    ``mae_price``, ``realized_rr`` etc. are cost-independent and untouched. The
    scorer's MAE %, which adds ``cost_usd``, therefore updates correctly.
    """
    for t in trades:
        inst = get_instrument(t.instrument)
        cost = calculate_trade_cost(symbol=t.instrument, lot_size=t.lots,
                                    cost_model=cost_model, instrument=inst)
        t.cost_usd = cost.total_usd
        t.net_pnl_usd = t.pnl_usd - cost.total_usd
    logger.info("re-costed %d trades under cost model '%s'", len(trades), cost_model.name)
    return trades
