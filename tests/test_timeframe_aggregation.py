"""
Invariant tests for multi-timeframe aggregation (app/cfd_research/timeframe.py).

These pin the money-critical property that HTF candles are a FAITHFUL, non-leaking
aggregation of the 5m base — the guard against the old NSE "15m built wrong ->
fantasy win rate" class of bug. If any of these fail, no strategy should run on
higher timeframes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.cfd_research.timeframe import (
    INTERVAL_MS,
    aggregate_htf,
    aggregate_with_index,
)
from app.core.models import Candle, Timeframe

_M5_MS = 300_000
# 08:00 UTC on a Monday — aligned to 15m/30m/1h boundaries.
_BASE = datetime(2024, 6, 3, 8, 0, tzinfo=timezone.utc)


def _c(i: int, o: float, h: float, l: float, cl: float, vol: int = 0,
       base: datetime = _BASE) -> Candle:
    return Candle(
        exchange="ICMARKETS", segment="CFD", exchange_token="XAUUSD",
        timeframe=Timeframe.M5,
        timestamp_ms=(base + timedelta(minutes=5 * i)).timestamp() * 1000,
        open=o, high=h, low=l, close=cl, volume=vol,
    )


def _hour_of_5m() -> list[Candle]:
    """12 sequential 5m bars (08:00..08:55). Bar i: distinct O/H/L/C, vol=i+1."""
    return [_c(i, o=100 + i, h=100 + i + 2, l=100 + i - 2, cl=100 + i + 0.5, vol=i + 1)
            for i in range(12)]


# ─── Identity: base timeframe is untouched ───────────────────────────────────


def test_identity_returns_input_for_base_tf():
    candles = _hour_of_5m()
    out, idx = aggregate_with_index(candles, Timeframe.M5)
    assert out == candles                       # same values
    assert idx == list(range(len(candles)))     # each bar maps to itself


def test_empty_input():
    assert aggregate_with_index([], Timeframe.M15) == ([], [])
    assert aggregate_htf([], Timeframe.H1) == []


# ─── Reconstruction: OHLCV built correctly from constituents ─────────────────


def test_15m_reconstruction():
    candles = _hour_of_5m()
    htf = aggregate_htf(candles, Timeframe.M15)
    assert len(htf) == 4                         # 12 x 5m -> 4 x 15m

    # First 15m bar = 5m bars 0,1,2.
    b0 = htf[0]
    assert b0.timeframe is Timeframe.M15
    assert b0.open == candles[0].open            # first
    assert b0.close == candles[2].close          # last
    assert b0.high == max(c.high for c in candles[0:3])
    assert b0.low == min(c.low for c in candles[0:3])
    assert b0.volume == sum(c.volume for c in candles[0:3])
    assert b0.timestamp_ms == candles[0].timestamp_ms  # bucket open = first bar's open

    # Last 15m bar = 5m bars 9,10,11.
    b3 = htf[3]
    assert b3.open == candles[9].open
    assert b3.close == candles[11].close
    assert b3.high == max(c.high for c in candles[9:12])
    assert b3.low == min(c.low for c in candles[9:12])


def test_30m_and_1h_counts_and_close():
    candles = _hour_of_5m()
    h30 = aggregate_htf(candles, Timeframe.M30)
    h1 = aggregate_htf(candles, Timeframe.H1)
    assert len(h30) == 2                         # 12 x 5m -> 2 x 30m
    assert len(h1) == 1                          # -> 1 x 1h
    # The single 1h bar spans the whole set.
    assert h1[0].open == candles[0].open
    assert h1[0].close == candles[-1].close
    assert h1[0].high == max(c.high for c in candles)
    assert h1[0].low == min(c.low for c in candles)


# ─── Boundary alignment: wall-clock aligned, never index-drifting ────────────


@pytest.mark.parametrize("tf", [Timeframe.M15, Timeframe.M30, Timeframe.H1])
def test_boundary_alignment(tf):
    htf = aggregate_htf(_hour_of_5m(), tf)
    interval = INTERVAL_MS[tf]
    for bar in htf:
        assert int(bar.timestamp_ms) % interval == 0


# ─── Volume conservation: no double count, no loss ───────────────────────────


@pytest.mark.parametrize("tf", [Timeframe.M15, Timeframe.M30, Timeframe.H1])
def test_volume_conserved(tf):
    candles = _hour_of_5m()
    htf = aggregate_htf(candles, tf)
    assert sum(b.volume for b in htf) == sum(c.volume for c in candles)


# ─── Gap safety: a missing 5m bar must NOT shift subsequent buckets ──────────


def test_gap_does_not_shift_buckets():
    candles = _hour_of_5m()
    # Drop the 08:05 bar (index 1) — a market micro-gap inside the first bucket.
    with_gap = [c for j, c in enumerate(candles) if j != 1]

    full = aggregate_htf(candles, Timeframe.M15)
    gapped = aggregate_htf(with_gap, Timeframe.M15)

    # Same number of 15m bars, same bucket-open timestamps (NOT shifted).
    assert [b.timestamp_ms for b in gapped] == [b.timestamp_ms for b in full]

    # The affected bucket is rebuilt from the bars that remain (08:00, 08:10).
    assert gapped[0].open == candles[0].open           # 08:00 still first
    assert gapped[0].close == candles[2].close         # 08:10 still last in-bucket
    assert gapped[0].high == max(candles[0].high, candles[2].high)
    assert gapped[0].low == min(candles[0].low, candles[2].low)
    assert gapped[0].volume == candles[0].volume + candles[2].volume
    # Later buckets identical to the no-gap case.
    assert gapped[1].timestamp_ms == full[1].timestamp_ms


# ─── Index mapping: exits resolve strictly AFTER the HTF bar closes ──────────


def test_last_index_mapping_no_overlap():
    candles = _hour_of_5m()
    htf, idx = aggregate_with_index(candles, Timeframe.M15)
    assert len(htf) == len(idx) == 4

    # last_base_index points at the final 5m bar of each 15m bar: 2,5,8,11.
    assert idx == [2, 5, 8, 11]

    # Strictly increasing -> exit windows never overlap the signal bar's own bars.
    assert all(idx[k] < idx[k + 1] for k in range(len(idx) - 1))

    # The bar AFTER a HTF bar's last constituent belongs to a LATER bucket
    # (no look-ahead: the signal's own sub-bars can't be in the exit window).
    interval = INTERVAL_MS[Timeframe.M15]
    for k, htf_bar in enumerate(htf):
        last_i = idx[k]
        nxt = last_i + 1
        if nxt < len(candles):
            nxt_bucket = (int(candles[nxt].timestamp_ms) // interval) * interval
            assert nxt_bucket > int(htf_bar.timestamp_ms)


def test_partial_last_bucket_is_complete_relative_to_data():
    # 4 bars = one full 15m bucket (0,1,2) + a partial next bucket (bar 3 @ 08:15).
    candles = _hour_of_5m()[:4]
    htf, idx = aggregate_with_index(candles, Timeframe.M15)
    assert len(htf) == 2
    # Second bucket has only the one bar we have; it's faithful to available data.
    assert htf[1].open == candles[3].open
    assert htf[1].close == candles[3].close
    assert idx == [2, 3]
