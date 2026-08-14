"""
General multi-timeframe aggregation — build higher-TF candles from a base series.

We only STORE 5m candles (Dukascopy + live feed), but strategies may want to
trade on 15m / 30m / 1h. This module builds those higher-timeframe (HTF) candles
from the 5m base, correctly and generically, so ANY strategy can declare a
``timeframe`` and be backtested on it with no per-strategy code.

The cardinal rule (this is where the old NSE "MR 15m 96% WR" class of bug came
from): an HTF bar must be built ONLY from base bars that fall inside its window,
and a strategy may act on it ONLY after it has fully closed — never a forming
bar, and never a bar mislabeled by an index-drifting scheme. So:

    * Bucketing is by ABSOLUTE epoch time: bucket = (ts // interval) * interval.
      This aligns HTF bars to the wall clock (15m -> :00/:15/:30/:45), so a
      missing base bar (market break / holiday) can NEVER shift subsequent
      buckets. NEVER group "every N candles" — one gap would misalign everything.
    * OHLCV aggregation: open = first, high = max, low = min, close = last,
      volume = sum — over exactly the base bars in the bucket.
    * ``aggregate_with_index`` also returns, per HTF bar, the index of its LAST
      constituent base bar. The replay uses this to resolve exits on the base
      (5m) bars that come strictly AFTER the HTF bar closes — so the signal bar's
      own sub-bars can never leak into the trade (no look-ahead, no same-bar
      entry+exit), and stops keep 5m fidelity instead of coarse HTF fidelity.

A base->same-or-finer request is an identity (returns the input unchanged), so
the 5m path is byte-for-byte what it always was.
"""

from __future__ import annotations

from app.core.models import Candle, Timeframe

# Interval of each timeframe in milliseconds.
INTERVAL_MS: dict[Timeframe, int] = {
    Timeframe.M1: 60_000,
    Timeframe.M5: 300_000,
    Timeframe.M15: 900_000,
    Timeframe.M30: 1_800_000,
    Timeframe.H1: 3_600_000,
    Timeframe.D1: 86_400_000,
}


def _bucket_open(ts_ms: float, interval_ms: int) -> int:
    """Floor a timestamp to its HTF bucket-open (wall-clock aligned, UTC epoch)."""
    return (int(ts_ms) // interval_ms) * interval_ms


def aggregate_with_index(
    candles: list[Candle], timeframe: Timeframe
) -> tuple[list[Candle], list[int]]:
    """Aggregate a base candle series to ``timeframe``.

    Returns ``(htf_candles, last_base_index)`` where ``last_base_index[i]`` is the
    position in the input ``candles`` of the LAST base bar composing ``htf_candles[i]``.
    Exits should be resolved on ``candles[last_base_index[i] + 1:]`` (strictly
    after the HTF bar closes).

    * ``candles`` must be sorted ascending by ``timestamp_ms`` (as loaded from the
      DB) and share one base timeframe (read from ``candles[0].timeframe``).
    * If ``timeframe`` is the same as (or finer than) the base, this is an
      identity: the input is returned unchanged with ``last_base_index = range(n)``,
      so the base-TF path is completely unaffected.
    """
    if not candles:
        return [], []

    interval = INTERVAL_MS[timeframe]
    base = INTERVAL_MS[candles[0].timeframe]

    # Same-or-finer target => identity (no aggregation; base path untouched).
    if interval <= base:
        return list(candles), list(range(len(candles)))
    if interval % base != 0:
        raise ValueError(
            f"target timeframe {timeframe.value} ({interval}ms) is not a whole "
            f"multiple of the base {candles[0].timeframe.value} ({base}ms)"
        )

    out: list[Candle] = []
    last_idx: list[int] = []

    cur_bucket: int | None = None
    o = h = l = c = 0.0
    vol = 0
    li = -1
    ex = seg = tok = ""

    def _flush() -> None:
        out.append(Candle(
            exchange=ex, segment=seg, exchange_token=tok,
            timeframe=timeframe, timestamp_ms=cur_bucket,
            open=o, high=h, low=l, close=c, volume=vol,
        ))
        last_idx.append(li)

    for i, cd in enumerate(candles):
        b = _bucket_open(cd.timestamp_ms, interval)
        if cur_bucket is None or b != cur_bucket:
            if cur_bucket is not None:
                _flush()
            cur_bucket = b
            ex, seg, tok = cd.exchange, cd.segment, cd.exchange_token
            o, h, l, c = cd.open, cd.high, cd.low, cd.close
            vol = cd.volume
            li = i
        else:
            # Same bucket: extend the forming HTF bar.
            if cd.high > h:
                h = cd.high
            if cd.low < l:
                l = cd.low
            c = cd.close
            vol += cd.volume
            li = i

    if cur_bucket is not None:
        _flush()

    return out, last_idx


def aggregate_htf(candles: list[Candle], timeframe: Timeframe) -> list[Candle]:
    """Aggregate a base candle series to ``timeframe`` (candles only).

    Thin wrapper over :func:`aggregate_with_index` for callers that don't need
    the base-index mapping (e.g. plotting, indicators, tests).
    """
    return aggregate_with_index(candles, timeframe)[0]
