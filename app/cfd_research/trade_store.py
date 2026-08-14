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
