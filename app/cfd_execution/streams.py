"""
Trading-stream configuration — run many parallel accounts off one signal feed.

A "stream" is one independent trading destination that consumes the SAME strategy
signals as every other stream, but executes + reports on its own:

  * kind = "paper" — simulated fills (no broker orders), for a cost-free baseline.
  * kind = "demo"  — REAL orders on a cTrader DEMO account.
  * kind = "live"  — REAL orders on a funded / prop-firm account.

You can run any combination at once (paper + demo + several live firms), turn any
of them on/off independently, and give each its own Telegram channel so a firm's
alerts stay separate. Each stream keeps its own balance, risk %, RiskGuard, and
trade log; the alert is titled by ``kind`` (PAPER / DEMO / LIVE ENTRY …).

Two config sources (checked in order):
  1. A JSON file at ``$CFD_STREAMS_CONFIG`` (default ``data/cfd_streams.json`` if
     present) — the way to describe many streams / prop firms with per-stream
     Telegram channels. Schema (all fields optional except id + kind):

        {
          "streams": [
            {"id": "paper",         "kind": "paper", "enabled": true,
             "balance": 100000, "risk_pct": 0.5, "cost_model": "raw"},
            {"id": "ctrader_demo",  "kind": "demo",  "enabled": true,
             "balance": 100000, "risk_pct": 0.5},
            {"id": "ftmo_100k",     "kind": "live",  "enabled": false,
             "balance": 100000, "risk_pct": 0.5, "ctrader_account_id": 12345678,
             "telegram_bot_token": "...", "telegram_chat_id": "..."}
          ]
        }

  2. Otherwise, the legacy flat env vars (``CFD_PAPER_EXECUTION_MODE`` = paper |
     live | both, plus ``CFD_PAPER_*`` / ``CFD_DEMO_*``) — kept so existing
     single-/dual-stream setups keep working with no JSON file.

NOTE on hosts: a single cTrader Open API connection targets ONE environment
(demo OR live host). So a demo stream and a live-firm stream that live on
different hosts need separate broker connections — supported later via
``ctrader_account_id`` routing. Today all order-placing streams share the one
authenticated broker (``CTRADER_ENV``), which is correct for "paper + demo" and
for multiple accounts under the same host.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

_VALID_KINDS = ("paper", "demo", "live")


@dataclass
class StreamConfig:
    """One trading stream (paper / demo / live)."""

    stream_id: str
    kind: str                          # paper | demo | live
    enabled: bool = True
    balance: float = 100_000.0
    risk_pct: float = 1.0
    # Paper: the cost model applied to simulated fills. demo/live: only a
    # FALLBACK — real commission/swap are read from cTrader's close deal.
    cost_model: str = "intraday"
    # cTrader account routing (0 = the default authenticated account). Used to
    # target a specific ctidTraderAccountId once multi-account routing lands.
    ctrader_account_id: int = 0
    # Optional dedicated Telegram channel for THIS stream (a prop firm gets its
    # own). Empty = fall back to the shared default CFD channel.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    def __post_init__(self) -> None:
        self.kind = (self.kind or "").lower().strip()
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"stream '{self.stream_id}': kind must be one of {_VALID_KINDS}, "
                f"got '{self.kind}'"
            )

    @property
    def is_paper(self) -> bool:
        return self.kind == "paper"

    @property
    def places_orders(self) -> bool:
        """demo + live both place REAL cTrader orders."""
        return self.kind in ("demo", "live")

    @property
    def has_own_channel(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_streams() -> list[StreamConfig]:
    """Load the enabled trading streams from JSON (if configured) or env vars."""
    path = _env("CFD_STREAMS_CONFIG")
    # Default location, used automatically if it exists.
    if not path and Path("data/cfd_streams.json").exists():
        path = "data/cfd_streams.json"
    if path and Path(path).exists():
        streams = _load_from_json(path)
        logger.info("Loaded %d stream(s) from %s", len(streams), path)
    else:
        streams = _load_from_env()
        logger.info("Loaded %d stream(s) from env (CFD_PAPER_EXECUTION_MODE)", len(streams))

    enabled = [s for s in streams if s.enabled]
    if not enabled:
        logger.warning("No ENABLED streams — nothing will trade.")
    # Guard: stream ids must be unique (they key accounts, tallies, trade rows).
    ids = [s.stream_id for s in enabled]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate stream ids: {sorted(dupes)}")
    return enabled


def _load_from_json(path: str) -> list[StreamConfig]:
    data = json.loads(Path(path).read_text())
    raw_streams = data.get("streams", data if isinstance(data, list) else [])
    out: list[StreamConfig] = []
    for item in raw_streams:
        out.append(StreamConfig(
            stream_id=str(item["id"]),
            kind=str(item["kind"]),
            enabled=bool(item.get("enabled", True)),
            balance=float(item.get("balance", 100_000.0)),
            risk_pct=float(item.get("risk_pct", 1.0)),
            cost_model=str(item.get("cost_model", "intraday")),
            ctrader_account_id=int(item.get("ctrader_account_id", 0)),
            telegram_bot_token=str(item.get("telegram_bot_token", "")),
            telegram_chat_id=str(item.get("telegram_chat_id", "")),
        ))
    return out


def _load_from_env() -> list[StreamConfig]:
    """Backward-compatible streams from the legacy flat env vars.

    CFD_PAPER_EXECUTION_MODE = paper | live | both:
      paper -> one paper stream (id CFD_PAPER_ACCOUNT_ID, default cfd_demo)
      live  -> one demo/live stream (id CFD_DEMO_ACCOUNT_ID)
      both  -> a paper stream (cfd_paper) + a demo stream (cfd_ctrader_demo)
    """
    mode = _env("CFD_PAPER_EXECUTION_MODE", "paper").lower() or "paper"
    balance = _env_float("CFD_PAPER_BALANCE", 100_000.0)
    risk_pct = _env_float("CFD_PAPER_RISK_PCT", 1.0)
    paper_cost = _env("CFD_PAPER_COST_MODEL", "intraday") or "intraday"
    demo_cost = _env("CFD_DEMO_COST_MODEL", "zero") or "zero"
    # For a demo/live account, "kind" mirrors CTRADER_ENV so a live host is
    # tagged LIVE and a demo host DEMO (both still place real orders).
    live_kind = "live" if _env("CTRADER_ENV", "demo").lower() == "live" else "demo"

    run_paper = mode in ("paper", "both")
    run_live = mode in ("live", "both")

    streams: list[StreamConfig] = []
    if run_paper:
        pid = _env("CFD_PAPER_ACCOUNT_ID", "cfd_paper" if mode == "both" else "cfd_demo")
        streams.append(StreamConfig(
            stream_id=pid, kind="paper", balance=balance,
            risk_pct=risk_pct, cost_model=paper_cost,
        ))
    if run_live:
        did = _env("CFD_DEMO_ACCOUNT_ID", "cfd_ctrader_demo")
        streams.append(StreamConfig(
            stream_id=did, kind=live_kind, balance=balance,
            risk_pct=risk_pct, cost_model=demo_cost,
        ))
    return streams
