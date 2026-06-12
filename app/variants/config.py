"""
Configuration for the 150K variant research engine.

Separate from the paper trading config — this controls the research pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(_env_path)


@dataclass
class ResearchConfig:
    """Configuration for the research engine."""

    # ─── Instruments to evaluate (smaller set for now) ───────────────────────
    # These are the instruments variants are evaluated on.
    # NIFTY + BANKNIFTY (indices) + 2-3 liquid stocks
    instruments: list[str] = field(default_factory=lambda: _parse_research_instruments())

    # India VIX token — subscribed separately for the VIX filter
    vix_token: str = "INDIAVIX"
    vix_exchange: str = "NSE"
    vix_segment: str = "CASH"

    # ─── Timeframes ──────────────────────────────────────────────────────────
    # Candle timeframes to build for the research engine
    timeframes: list[str] = field(default_factory=lambda: ["5m", "15m", "30m"])

    # ─── Performance ─────────────────────────────────────────────────────────
    # Max time (seconds) allowed for candle-close evaluation
    max_eval_time_seconds: float = field(
        default_factory=lambda: float(os.getenv("RESEARCH_MAX_EVAL_TIME", "10.0"))
    )

    # ─── Armed State ─────────────────────────────────────────────────────────
    # Maximum armed variants per instrument (safety bound)
    max_armed_per_instrument: int = field(
        default_factory=lambda: int(os.getenv("RESEARCH_MAX_ARMED", "10000"))
    )

    # ─── Trade Recording ─────────────────────────────────────────────────────
    # Batch flush interval for trade writes (seconds)
    trade_flush_interval: float = field(
        default_factory=lambda: float(os.getenv("RESEARCH_FLUSH_INTERVAL", "5.0"))
    )

    # ─── Warmup ──────────────────────────────────────────────────────────────
    warmup_enabled: bool = field(
        default_factory=lambda: os.getenv("RESEARCH_WARMUP_ENABLED", "true").lower() == "true"
    )

    # ─── Logging ─────────────────────────────────────────────────────────────
    # Only log trade-level events, never per-variant or per-tick
    log_level: str = field(default_factory=lambda: os.getenv("RESEARCH_LOG_LEVEL", "INFO"))


def _parse_research_instruments() -> list[str]:
    """
    Parse research instruments from env.
    Defaults to NIFTY, BANKNIFTY, RELIANCE, HDFCBANK, TCS.
    """
    raw = os.getenv(
        "RESEARCH_INSTRUMENTS",
        "NIFTY,BANKNIFTY,2885,1333,11536"
    )
    return [t.strip() for t in raw.split(",") if t.strip()]


def load_research_config() -> ResearchConfig:
    """Load research engine configuration."""
    return ResearchConfig()
