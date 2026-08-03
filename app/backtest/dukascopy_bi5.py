"""
Direct Dukascopy .bi5 tick fetcher — reliable path for the index/oil CFDs
(and for all instruments when --all-bi5 is used).

dukascopy-node's pre-aggregated minute-candle endpoint returns empty/patchy for
the CFDs, and its jetta tick JSON path intermittently drops files. The classic
binary tick feed (datafeed.dukascopy.com/.../HHh_ticks.bi5) serves the same
instruments completely and is trivially decodable, so we use it directly.

bi5 format: LZMA-compressed; each tick is 20 bytes big-endian:
    uint32 ms_offset_within_hour, uint32 ask, uint32 bid, float32 askVol, float32 bidVol
Real price = integer * point (e.g. 0.001 indices, 0.00001 EURUSD). Timestamps GMT/UTC.

Performance:
- HTTP keep-alive via a shared pooled requests.Session (no TLS handshake per file).
- numpy-vectorized decode + 5m aggregation (fast even for FX's 100k+ ticks/hour).
- Concurrent hourly fetches (default 16 workers) — the fetch is network-bound and
  the VM's CPU is idle, so concurrency is the main lever (bounded to stay under
  Dukascopy's rate limiter).
"""

from __future__ import annotations

import concurrent.futures as cf
import lzma
import time
from datetime import date, datetime, timezone

import numpy as np
import requests
from requests.adapters import HTTPAdapter

BI5_ROOT = "https://datafeed.dukascopy.com/datafeed"
_UA = {"User-Agent": "Mozilla/5.0"}
_M5_MS = 5 * 60 * 1000
_DEFAULT_WORKERS = 16

# bi5 record layout (big-endian): ms, ask, bid, askVol, bidVol
_BI5_DTYPE = np.dtype([("ms", ">u4"), ("ask", ">u4"), ("bid", ">u4"), ("av", ">f4"), ("bv", ">f4")])

# Shared keep-alive session (thread-safe for GETs; pool sized for our workers).
_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=8, pool_maxsize=_DEFAULT_WORKERS * 2, max_retries=0)
_session.mount("https://", _adapter)
_session.headers.update(_UA)


def _hour_url(bi5_name: str, dt: datetime) -> str:
    # NB: month is 0-indexed in the Dukascopy path.
    return f"{BI5_ROOT}/{bi5_name}/{dt.year:04d}/{dt.month - 1:02d}/{dt.day:02d}/{dt.hour:02d}h_ticks.bi5"


def _fetch_hour(bi5_name: str, dt: datetime, point: float, retries: int = 5):
    """Return (ts_ms_int64_array, bid_float64_array) for one UTC hour.

    Empty arrays for a legitimately absent hour (404/empty = closed/holiday).
    None only on persistent fetch failure, so the caller can distinguish
    "no data" from "couldn't get it" and avoid marking an incomplete day done.
    """
    url = _hour_url(bi5_name, dt)
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=30)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue

        if r.status_code == 404:
            return _EMPTY
        if r.status_code != 200:
            time.sleep(1.5 * (attempt + 1))
            continue

        raw = r.content
        if not raw:
            return _EMPTY
        try:
            data = lzma.decompress(raw)
        except lzma.LZMAError:
            return _EMPTY

        arr = np.frombuffer(data, dtype=_BI5_DTYPE)
        base_ms = int(datetime(dt.year, dt.month, dt.day, dt.hour, tzinfo=timezone.utc).timestamp()) * 1000
        ts = base_ms + arr["ms"].astype(np.int64)
        bid = arr["bid"].astype(np.float64) * point
        return ts, bid
    return None  # persistent failure


_EMPTY = (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64))


def _aggregate_m5(ts: np.ndarray, bid: np.ndarray) -> list[tuple]:
    """Vectorized aggregation of (ts_ms, bid) into 5m bid OHLCV bars.

    volume = tick count in the bar (matches our live candle semantics).
    """
    if ts.size == 0:
        return []
    order = np.argsort(ts, kind="stable")
    ts = ts[order]
    bid = bid[order]

    buckets = (ts // _M5_MS) * _M5_MS
    # group boundaries (buckets is non-decreasing after sort)
    start_idx = np.concatenate(([0], np.nonzero(np.diff(buckets))[0] + 1))
    end_idx = np.concatenate((start_idx[1:], [len(bid)]))  # exclusive ends

    bar_ts = buckets[start_idx]
    opens = bid[start_idx]
    closes = bid[end_idx - 1]
    highs = np.maximum.reduceat(bid, start_idx)
    lows = np.minimum.reduceat(bid, start_idx)
    counts = end_idx - start_idx

    return [
        (int(bar_ts[i]), float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i]), int(counts[i]))
        for i in range(len(bar_ts))
    ]


def fetch_day_m5(bi5_name: str, day: date, point: float, max_workers: int = _DEFAULT_WORKERS) -> tuple[list[tuple], int]:
    """Fetch one UTC day of ticks (24 hourly bi5 files, concurrently) and
    aggregate to 5m bid candles.

    Returns (candles, failed_hours). candles = [(ts,o,h,l,c,vol)]; failed_hours
    is the count of hours that could not be fetched. A day with failed_hours > 0
    is incomplete and should NOT be marked done, so it gets retried.
    """
    hours = [datetime(day.year, day.month, day.day, h, tzinfo=timezone.utc) for h in range(24)]
    results: list = [None] * 24
    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut_to_idx = {ex.submit(_fetch_hour, bi5_name, dt, point): i for i, dt in enumerate(hours)}
        for fut in cf.as_completed(fut_to_idx):
            results[fut_to_idx[fut]] = fut.result()

    ts_parts, bid_parts = [], []
    failed = 0
    for r in results:
        if r is None:
            failed += 1
        else:
            ts_parts.append(r[0])
            bid_parts.append(r[1])

    if ts_parts:
        ts_all = np.concatenate(ts_parts)
        bid_all = np.concatenate(bid_parts)
    else:
        ts_all = np.empty(0, dtype=np.int64)
        bid_all = np.empty(0, dtype=np.float64)

    return _aggregate_m5(ts_all, bid_all), failed
