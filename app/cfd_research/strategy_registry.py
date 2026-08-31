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

from app.cfd_research.entries.liquidity_sweep import LiquiditySweep
from app.cfd_research.entries.mean_reversion import MeanReversion
from app.cfd_research.entries.session_orb import SessionORB
from app.cfd_research.entries.squeeze_breakout import SqueezeBreakout
from app.cfd_research.entries.trend_pullback import TrendPullback
from app.cfd_research.entries.volatility_breakout import VolatilityBreakout
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


def _build_mr(cfg: dict) -> list[EntryStrategy]:
    """Mean-Reversion: one variant per timeframe (fire-anytime, not session-triggered)."""
    out: list[EntryStrategy] = []
    for tf in cfg["timeframes"]:
        out.append(MeanReversion(
            bb_period=cfg.get("bb_period", 20),
            bb_std=cfg.get("bb_std", 2.0),
            adx_threshold=cfg.get("adx_threshold", 25.0),
            adx_period=cfg.get("adx_period", 14),
            atr_period=cfg.get("atr_period", 14),
            atr_stop_mult=cfg.get("atr_stop_mult", 1.0),
            min_vwap_dev=cfg.get("min_vwap_dev", 1.5),
            require_rejection=cfg.get("require_rejection", True),
            cooldown_bars=cfg.get("cooldown_bars", 6),
            session_vwap=cfg.get("session_vwap", True),
            timeframe=tf,
        ))
    return out


def _build_sweep(cfg: dict) -> list[EntryStrategy]:
    """Liquidity Sweep: variants per timeframe (fire-anytime, not session-triggered).

    IMPORTANT (CFD data reality): CFDs have NO real traded volume — the feed only
    carries TICK volume (quote-update count), so the VWAP confirmation is a proxy,
    not a true institutional VWAP. Rather than trust the proxy, we build BOTH a
    VWAP-on and a VWAP-off variant (distinct strategy_ids: ``..._novwap``) so the
    scorer directly answers "does the tick-VWAP filter actually add edge on CFD
    data?" — clean, evidence-based attribution. Override with
    ``sweep_require_vwap`` to force one side only.
    """
    out: list[EntryStrategy] = []
    # If the caller explicitly sets sweep_require_vwap, honour it (one side);
    # otherwise sweep BOTH so the data decides whether the proxy VWAP helps.
    if "sweep_require_vwap" in cfg:
        vwap_modes = [bool(cfg["sweep_require_vwap"])]
    else:
        vwap_modes = [True, False]
    for tf in cfg["timeframes"]:
        for require_vwap in vwap_modes:
            out.append(LiquiditySweep(
                lookback=cfg.get("sweep_lookback", 20),
                ema_len=cfg.get("sweep_ema", 9),
                sl_buffer_atr=cfg.get("sweep_sl_buffer_atr", 0.1),
                atr_period=cfg.get("atr_period", 14),
                require_ema=cfg.get("sweep_require_ema", True),
                require_vwap=require_vwap,
                cooldown_bars=cfg.get("sweep_cooldown_bars", 3),
                timeframe=tf,
            ))
    return out


def _build_pullback(cfg: dict) -> list[EntryStrategy]:
    """Trend Pullback: one variant per timeframe (fire-anytime, not session-triggered)."""
    out: list[EntryStrategy] = []
    for tf in cfg["timeframes"]:
        out.append(TrendPullback(
            ema_fast=cfg.get("pb_ema_fast", 20),
            ema_slow=cfg.get("pb_ema_slow", 50),
            adx_period=cfg.get("adx_period", 14),
            adx_min=cfg.get("pb_adx_min", 20.0),
            atr_period=cfg.get("atr_period", 14),
            sl_buffer_atr=cfg.get("pb_sl_buffer_atr", 0.2),
            rsi_period=cfg.get("pb_rsi_period", 14),
            require_momentum=cfg.get("pb_require_momentum", True),
            cooldown_bars=cfg.get("pb_cooldown_bars", 4),
            timeframe=tf,
        ))
    return out


def _build_squeeze(cfg: dict) -> list[EntryStrategy]:
    """TTM Squeeze breakout: one variant per timeframe (fire-anytime)."""
    out: list[EntryStrategy] = []
    for tf in cfg["timeframes"]:
        out.append(SqueezeBreakout(
            bb_period=cfg.get("sqz_bb_period", 20),
            bb_std=cfg.get("sqz_bb_std", 2.0),
            kc_ema=cfg.get("sqz_kc_ema", 20),
            kc_atr=cfg.get("sqz_kc_atr", 10),
            kc_mult=cfg.get("sqz_kc_mult", 1.5),
            mom_len=cfg.get("sqz_mom_len", 20),
            atr_period=cfg.get("atr_period", 14),
            sl_atr_mult=cfg.get("sqz_sl_atr_mult", 1.5),
            cooldown_bars=cfg.get("sqz_cooldown_bars", 6),
            timeframe=tf,
        ))
    return out


def _build_lwvb(cfg: dict) -> list[EntryStrategy]:
    """Larry Williams volatility breakout: one variant per timeframe (day-trade)."""
    out: list[EntryStrategy] = []
    for tf in cfg["timeframes"]:
        out.append(VolatilityBreakout(
            k=cfg.get("lwvb_k", 0.5),
            atr_period=cfg.get("atr_period", 14),
            require_vol_expansion=cfg.get("lwvb_vol_expansion", False),
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
    "mr": ResearchStrategySpec(
        key="mr",
        description="Mean-reversion VWAP/BB fade (fire-anytime, regime-filtered)",
        build=_build_mr,
        session_triggered=False,
    ),
    "sweep": ResearchStrategySpec(
        key="sweep",
        description="Liquidity-sweep (stop-hunt) reversal (fire-anytime, confirmation-filtered)",
        build=_build_sweep,
        session_triggered=False,
    ),
    "pullback": ResearchStrategySpec(
        key="pullback",
        description="Trend-continuation EMA-pullback bounce (fire-anytime, trend-regime-filtered)",
        build=_build_pullback,
        session_triggered=False,
    ),
    "lwvb": ResearchStrategySpec(
        key="lwvb",
        description="Larry Williams volatility breakout (prior-day range projected off the open; day-trade)",
        build=_build_lwvb,
        session_triggered=False,
    ),
    "squeeze": ResearchStrategySpec(
        key="squeeze",
        description="TTM Squeeze breakout (John Carter; BB-inside-KC compression releases into momentum)",
        build=_build_squeeze,
        session_triggered=False,
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
