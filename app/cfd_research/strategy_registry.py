"""
Research strategy registry — pick which entry strategy (or strategies) to run.

The research CLI (``run_research``) is strategy-agnostic: it walks candles, runs
whatever ``EntryStrategy`` variants it's given through the exit sweep, tags the
trades, and scores them. This registry is the small seam that maps a CLI key
(``--strategies orb``) to the concrete variants to walk.

Each strategy registers a ``build(cfg) -> list[EntryStrategy]`` that expands the
run config into the variants to backtest. This is where a strategy declares its
GENERATION axes (the things that create distinct trade sets and so must be walked
separately):

    * ORB is SESSION-TRIGGERED: it builds one variant per (session x timeframe),
      because the session open IS its entry trigger.
    * A fire-anytime strategy (e.g. a mean-reversion / VWAP fade) would build one
      variant per timeframe only — session/regime/volatility are free TAGS it
      slices by afterward, not generation axes. (When such a strategy is added,
      register it here; nothing else in the pipeline changes.)

``build_variants`` is called INSIDE each per-instrument worker process, so the
built strategy instances (which may hold un-picklable closures like session
functions) never cross the multiprocessing boundary — only the plain config dict
and the string keys do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.cfd_research.entries.session_orb import SessionORB
from app.cfd_research.entry_strategy import EntryStrategy


@dataclass(frozen=True)
class ResearchStrategySpec:
    key: str
    description: str
    build: Callable[[dict], list[EntryStrategy]]
    session_triggered: bool = True   # documents whether session is a generation axis


def _build_orb(cfg: dict) -> list[EntryStrategy]:
    """Session ORB: one variant per (session x timeframe)."""
    out: list[EntryStrategy] = []
    for tf in cfg["timeframes"]:
        for session in cfg["sessions"]:
            out.append(SessionORB(
                session=session,
                range_bars=cfg.get("range_bars", 6),
                buffer_frac=cfg.get("buffer_frac", 0.0),
                trend_ema=cfg.get("trend_ema") or None,
                timeframe=tf,
            ))
    return out


# The registry. Add new research entries here (one line) — the CLI, exit sweep,
# tagging, gates, challenge sim and OOS split all work unchanged.
REGISTRY: dict[str, ResearchStrategySpec] = {
    "orb": ResearchStrategySpec(
        key="orb",
        description="Session opening-range breakout (session-triggered)",
        build=_build_orb,
        session_triggered=True,
    ),
}


def available() -> list[str]:
    return sorted(REGISTRY)


def build_variants(keys: list[str], cfg: dict) -> list[EntryStrategy]:
    """Expand the requested strategy keys into the concrete variants to walk.

    Raises ``ValueError`` on an unknown key (fail loud rather than silently
    running nothing).
    """
    variants: list[EntryStrategy] = []
    for k in keys:
        spec = REGISTRY.get(k)
        if spec is None:
            raise ValueError(f"unknown strategy {k!r}; available: {available()}")
        variants.extend(spec.build(cfg))
    return variants
