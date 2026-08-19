"""
Tests for:
  1. The trading-stream config loader (app.cfd_execution.streams) — JSON +
     legacy-env sources, enable toggles, dedicated-channel detection.
  2. The cTrader executor reading REAL commission / swap / gross from the close
     deal's CloseDetail (app.cfd_execution.ctrader_executor._fetch_real_costs),
     rather than modeling costs.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass

import pytest

from app.cfd_execution.streams import StreamConfig, load_streams


# ─── Streams config ──────────────────────────────────────────────────────────

def test_stream_kind_validation():
    with pytest.raises(ValueError):
        StreamConfig(stream_id="x", kind="bogus")
    assert StreamConfig(stream_id="p", kind="paper").is_paper
    assert StreamConfig(stream_id="d", kind="demo").places_orders
    assert StreamConfig(stream_id="l", kind="live").places_orders
    assert not StreamConfig(stream_id="p", kind="paper").places_orders


def test_stream_own_channel_detection():
    shared = StreamConfig(stream_id="a", kind="demo")
    own = StreamConfig(stream_id="b", kind="live",
                       telegram_bot_token="BOT", telegram_chat_id="CHAT")
    assert not shared.has_own_channel
    assert own.has_own_channel


def test_load_streams_from_json_respects_enabled(monkeypatch):
    cfg = {"streams": [
        {"id": "paper", "kind": "paper", "enabled": True, "cost_model": "raw"},
        {"id": "demo", "kind": "demo", "enabled": True},
        {"id": "ftmo", "kind": "live", "enabled": False,
         "telegram_bot_token": "B", "telegram_chat_id": "C"},
    ]}
    path = tempfile.mktemp(suffix=".json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    monkeypatch.setenv("CFD_STREAMS_CONFIG", path)

    streams = load_streams()
    ids = [s.stream_id for s in streams]
    assert ids == ["paper", "demo"]           # ftmo disabled -> excluded
    assert streams[0].cost_model == "raw"


def test_load_streams_duplicate_ids_raise(monkeypatch):
    cfg = {"streams": [
        {"id": "dup", "kind": "paper"},
        {"id": "dup", "kind": "demo"},
    ]}
    path = tempfile.mktemp(suffix=".json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    monkeypatch.setenv("CFD_STREAMS_CONFIG", path)
    with pytest.raises(ValueError):
        load_streams()


def test_load_streams_env_both_mode(monkeypatch):
    # No JSON -> legacy env fallback. 'both' => a paper + a demo stream.
    monkeypatch.delenv("CFD_STREAMS_CONFIG", raising=False)
    monkeypatch.setenv("CFD_PAPER_EXECUTION_MODE", "both")
    monkeypatch.setenv("CFD_PAPER_ACCOUNT_ID", "cfd_paper")
    monkeypatch.setenv("CFD_DEMO_ACCOUNT_ID", "cfd_ctrader_demo")
    monkeypatch.setenv("CFD_PAPER_COST_MODEL", "raw")
    monkeypatch.setenv("CFD_DEMO_COST_MODEL", "zero")
    # Ensure the default-path file isn't picked up during the test.
    if os.path.exists("data/cfd_streams.json"):
        pytest.skip("data/cfd_streams.json present; env-fallback path not exercised")

    streams = load_streams()
    by_id = {s.stream_id: s for s in streams}
    assert set(by_id) == {"cfd_paper", "cfd_ctrader_demo"}
    assert by_id["cfd_paper"].kind == "paper"
    assert by_id["cfd_paper"].cost_model == "raw"
    assert by_id["cfd_ctrader_demo"].places_orders


# ─── Real-cost reading from the close deal ───────────────────────────────────

@dataclass
class _FakeCloseDetail:
    gross_profit: float
    swap: float
    commission: float
    pnl_conversion_fee: float
    balance: float


@dataclass
class _FakeDeal:
    commission: float = 0.0
    close_detail: object | None = None


class _FakeTrading:
    def __init__(self, deals):
        self._deals = deals
        self.calls = 0

    async def get_deals_by_position_id(self, account_id, position_id):
        self.calls += 1
        return self._deals


class _FakeClient:
    def __init__(self, trading):
        self.trading = trading


class _FakeBroker:
    def __init__(self, deals):
        self.client = _FakeClient(_FakeTrading(deals))
        self.account_id = 111


def _make_executor(deals):
    from app.cfd_execution.account import AccountConfig
    from app.cfd_execution.ctrader_executor import CTraderExecutor
    acct = AccountConfig(account_id="demo", initial_balance=100_000.0)
    return CTraderExecutor(acct, broker=_FakeBroker(deals), kind="demo")


def test_fetch_real_costs_aggregates_close_detail():
    # A win: gross +100, swap -2 (charged), commission -7 (charged), conv fee 0.5,
    # plus an opening-deal commission of -3.
    deals = [
        _FakeDeal(commission=-3.0, close_detail=None),                       # opening leg
        _FakeDeal(commission=-4.0, close_detail=_FakeCloseDetail(            # closing leg
            gross_profit=100.0, swap=-2.0, commission=-4.0,
            pnl_conversion_fee=0.5, balance=100_093.5)),
    ]
    ex = _make_executor(deals)
    real = asyncio.run(ex._fetch_real_costs(555))
    assert real is not None
    assert real["gross"] == 100.0
    assert real["swap"] == -2.0
    # commission = close (-4) + open (-3) = -7
    assert real["commission"] == -7.0
    # net = gross + swap + commission - conv = 100 - 2 - 7 - 0.5 = 90.5
    assert abs(real["net"] - 90.5) < 1e-9
    assert real["balance"] == 100_093.5


def test_finalize_books_net_from_real_balance_delta():
    """Net PnL for a demo/live close is the REAL account-balance delta (not a
    model, not the component sum) — authoritative and sign-agnostic."""
    from app.cfd_execution.base import ManagedPosition, PositionStatus, PartialClose, ExitReason
    from app.cfd_strategy.base import Direction, build_rr_exit_plan

    ex = _make_executor([])
    # start() seeds both the RiskGuard and _last_real_balance from the real account.
    ex._risk.reset_account(8_143.00)
    ex._last_real_balance = 8_143.00
    plan = build_rr_exit_plan(Direction.LONG, 155.20, 155.10, rr_targets=[2.0])
    pos = ManagedPosition(
        position_id="900", strategy_id="orb_usdjpy_tokyo_5m", variant_id="default",
        instrument="USDJPY", direction=Direction.LONG, entry_price=155.20,
        entry_time_ms=1_000_000, lots=0.02, exit_plan=plan, account_id="demo",
        status=PositionStatus.OPEN,
    )
    pos.partial_closes.append(PartialClose(
        price=155.40, fraction=1.0, reason=ExitReason.TAKE_PROFIT,
        timestamp_ms=1_900_000, rr=2.0, pnl_price=0.20))
    pos.remaining_fraction = 0.0
    ex._positions[900] = pos

    # Real close deal: account balance went 8143.00 -> 8158.63 (net +15.63 after
    # real commission/swap). commission/swap signs here are irrelevant to net.
    real = {"gross": 18.0, "swap": -0.63, "commission": -1.74,
            "conv_fee": 0.0, "net": 999.0, "balance": 8_158.63}
    ex._finalize(900, pos, real=real)

    # Net booked = balance delta = 8158.63 - 8143.00 = 15.63 (NOT real['net']).
    assert abs(ex._risk.balance - 8_158.63) < 1e-6
    assert ex._last_real_balance == 8_158.63


def test_fetch_real_costs_none_when_no_close_deal():
    # Only an opening deal so far -> no close_detail -> fall back to modeled cost.
    ex = _make_executor([_FakeDeal(commission=-3.0, close_detail=None)])
    # Speed: patch asyncio.sleep so the retry loop doesn't actually wait.
    import app.cfd_execution.ctrader_executor as mod

    async def _run():
        orig = asyncio.sleep

        async def _nosleep(_):
            return None
        asyncio.sleep = _nosleep  # type: ignore
        try:
            return await ex._fetch_real_costs(555)
        finally:
            asyncio.sleep = orig
    assert asyncio.run(_run()) is None
