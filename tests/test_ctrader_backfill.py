"""
Tests for the cTrader candle-archive backfill (app.broker.ctrader_backfill).

Verifies the window-scan gap-fill in isolation with a fake broker + fake store:
  * fills INTERIOR holes and trailing gaps (writes only bars not already stored),
  * skips the still-forming bar (only closed 5m bars are written),
  * writes through LiveCandleStore so bars are session-tagged + routed to the
    right table (live vs staging),
  * does nothing when the archive is already complete,
  * skips unresolved symbols, and caps the scan window to max_days.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from app.broker.ctrader_backfill import backfill_candles
from app.core.candle_builder import TIMEFRAME_MS
from app.core.models import Timeframe
from app.db.live_candle_store import LiveCandleStore

INTERVAL = TIMEFRAME_MS[Timeframe.M5]  # 300_000 ms


class FakeBar:
    """Mimics the ctrader-api-client Trendbar (timestamp=open time, Decimal OHLC)."""

    def __init__(self, open_ms: int, o, h, l, c, volume=100):
        self.timestamp = datetime.fromtimestamp(open_ms / 1000, timezone.utc)
        self.open = Decimal(str(o))
        self.high = Decimal(str(h))
        self.low = Decimal(str(l))
        self.close = Decimal(str(c))
        self.volume = volume


class FakeBroker:
    def __init__(self, bars_by_symbol: dict[str, list[FakeBar]], symbol_map: dict[str, int]):
        self._bars = bars_by_symbol
        self.symbol_map = symbol_map
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def fetch_trendbars(self, symbol, from_dt, to_dt, period=None):
        self.calls.append((symbol, from_dt, to_dt))
        lo = from_dt.timestamp() * 1000
        hi = to_dt.timestamp() * 1000
        return [b for b in self._bars.get(symbol, [])
                if lo <= b.timestamp.timestamp() * 1000 <= hi]


class FakeStore:
    def __init__(self, stored: dict[str, list[int]]):
        # stored: symbol -> list of candle open-times already in the archive.
        self._stored = {k: sorted(v) for k, v in stored.items()}
        self.written: list[dict] = []

    def get_last_candle_ms(self, instrument, timeframe, staging=False):
        v = self._stored.get(instrument) or []
        return v[-1] if v else None

    def get_candle_timestamps(self, instrument, timeframe, start_ms, end_ms, staging=False):
        return [t for t in self._stored.get(instrument, []) if start_ms <= t <= end_ms]

    def write_live_candle(self, **kw):
        self.written.append({"table": "live", **kw})

    def write_staging_candle(self, **kw):
        self.written.append({"table": "staging", **kw})


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_backfill_fills_interior_and_trailing_holes_skips_forming():
    base = 1_700_000_000_000
    base -= base % INTERVAL  # align to a 5m boundary
    # now is just past bar#5's OPEN -> bars 0..4 are closed, bar 5 is forming.
    now_ms = base + 5 * INTERVAL + 1_000
    bars = [FakeBar(base + i * INTERVAL, 10 + i, 11 + i, 9 + i, 10.5 + i) for i in range(6)]
    broker = FakeBroker({"XAUUSD": bars}, {"XAUUSD": 1})
    # Stored: bar 0 and bar 2 -> interior hole at bar 1, trailing holes at 3 & 4.
    store = FakeStore({"XAUUSD": [base + 0 * INTERVAL, base + 2 * INTERVAL]})
    cs = LiveCandleStore(store, use_staging=True)

    n = _run(backfill_candles(
        broker, store, cs, ["XAUUSD"], exchange="CFD", segment="CFD",
        use_staging=True, min_lookback_hours=1.0, request_pause_s=0.0, now_ms=now_ms,
    ))

    assert n == 3
    ts = sorted(w["timestamp_ms"] for w in store.written)
    assert ts == [base + 1 * INTERVAL, base + 3 * INTERVAL, base + 4 * INTERVAL]
    assert all(w["table"] == "staging" for w in store.written)
    assert all(w["session_date"] for w in store.written)  # session-tagged


def test_backfill_noop_when_archive_complete():
    base = 1_700_000_000_000
    base -= base % INTERVAL
    now_ms = base + 4 * INTERVAL + 1_000  # bars 0..3 closed
    bars = [FakeBar(base + i * INTERVAL, 10, 11, 9, 10.5) for i in range(4)]
    broker = FakeBroker({"XAUUSD": bars}, {"XAUUSD": 1})
    # Everything closed is already stored.
    store = FakeStore({"XAUUSD": [base + i * INTERVAL for i in range(4)]})
    cs = LiveCandleStore(store, use_staging=True)
    n = _run(backfill_candles(
        broker, store, cs, ["XAUUSD"], exchange="CFD", segment="CFD",
        use_staging=True, min_lookback_hours=1.0, request_pause_s=0.0, now_ms=now_ms,
    ))
    assert n == 0
    assert store.written == []


def test_backfill_skips_unresolved_symbol():
    broker = FakeBroker({}, {})  # nothing resolved
    store = FakeStore({"US30": [1_700_000_000_000]})
    cs = LiveCandleStore(store, use_staging=False)
    n = _run(backfill_candles(broker, store, cs, ["US30"], exchange="CFD",
                              segment="CFD", request_pause_s=0.0))
    assert n == 0
    assert broker.calls == []


def test_backfill_caps_window_to_max_days():
    base = 1_700_000_000_000
    base -= base % INTERVAL
    now_ms = base + 10 * 86_400_000  # last stored candle is 10 days old
    broker = FakeBroker({"XAUUSD": []}, {"XAUUSD": 1})
    store = FakeStore({"XAUUSD": [base]})
    cs = LiveCandleStore(store, use_staging=True)
    _run(backfill_candles(
        broker, store, cs, ["XAUUSD"], exchange="CFD", segment="CFD",
        use_staging=True, min_lookback_hours=6.0, max_days=3.0, chunk_days=1.0,
        request_pause_s=0.0, now_ms=now_ms,
    ))
    assert broker.calls, "expected at least one fetch"
    first_from = broker.calls[0][1].timestamp() * 1000
    assert first_from >= now_ms - 3 * 86_400_000 - 1  # capped at max_days
