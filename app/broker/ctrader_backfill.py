"""
cTrader candle-archive backfill — self-heal gaps after a feed disconnect.

The cTrader feed is push-only: when the connection drops, the spot events during
the outage are never delivered, so the 5m candles for that window are simply not
built (unlike the MT5 feed, which replays missed ticks via ``copy_ticks_range``).
This module fills those holes AFTER the fact by pulling finished OHLC bars from
the Open API's historical ``get_trendbars`` and writing them into the same
archive (``live_candles`` / ``ctrader_staging_candles``) via ``LiveCandleStore``.

It is deliberately ARCHIVE-ONLY: backfilled bars are written straight to the
store (idempotent, session-tagged) and are NOT emitted on the EventBus, so they
never trigger strategy evaluation or order management on stale history. Live
trade safety comes from broker-side SL/TP, not from this — see CFD_SYSTEM.md.

Design:
  * Resume per symbol from the last stored candle (``get_last_candle_ms``); a
    fresh symbol with no prior candle is skipped (warmup seeds it, and pulling
    all history here is not the job).
  * Cap the span to ``max_days`` (a long outage's older gap is a separate job).
  * Page the request by ``chunk_days`` (the API limits bars per request).
  * Skip the still-forming bar (open + interval > now); only closed bars land.

Must run on the broker's asyncio loop (startup: ``run_until_complete`` on the
idle loop; reconnect: awaited from inside the running loop).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from app.core.candle_builder import TIMEFRAME_MS
from app.core.models import Candle, Timeframe
from app.db.live_candle_store import LiveCandleStore
from app.db.research_store import ResearchStore
from app.utils.logger import get_logger

logger = get_logger(__name__)

_DAY_MS = 86_400_000


def _dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


async def backfill_candles(
    broker: Any,
    store: ResearchStore,
    candle_store: LiveCandleStore,
    symbols: list[str],
    *,
    exchange: str,
    segment: str,
    timeframe: Timeframe = Timeframe.M5,
    use_staging: bool = False,
    min_lookback_hours: float = 6.0,
    max_days: float = 3.0,
    chunk_days: float = 1.0,
    request_pause_s: float = 1.0,
    now_ms: float | None = None,
) -> int:
    """Fill any missing candles per symbol over a recent window (idempotent).

    For each resolved symbol we scan a window ending now: at least
    ``min_lookback_hours`` back (so recent INTERIOR holes get repaired, not just
    a trailing gap), and further back if the last stored candle is older — up to
    ``max_days``. We fetch the window's trendbars, diff against what's already
    stored, and write ONLY the missing, fully-closed bars. Writing only the gaps
    keeps this cheap on a healthy feed (nothing to do) yet complete after an
    outage or a partial (rate-limited) previous run.

    ``broker`` must expose ``fetch_trendbars(symbol, from_dt, to_dt)`` (async) and
    a ``symbol_map`` (resolved symbols). Returns the number of candles written.

    ``request_pause_s`` throttles the historical requests: cTrader rate-limits
    ``get_trendbars`` (a rapid 10-symbol burst returns "You are being rate
    limited"), so we sleep between calls to stay under the ceiling.
    """
    interval_ms = TIMEFRAME_MS[timeframe]
    tf_value = timeframe.value
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    max_span_ms = int(max_days * _DAY_MS)
    min_look_ms = int(min_lookback_hours * 3_600_000)
    chunk_ms = max(int(chunk_days * _DAY_MS), interval_ms)
    resolved = getattr(broker, "symbol_map", {}) or {}

    total = 0
    for sym in symbols:
        if sym not in resolved:
            continue
        # Window = at least min_lookback, extended to cover a longer outage,
        # capped at max_days.
        last = store.get_last_candle_ms(sym, tf_value, staging=use_staging)
        gap_ms = (now_ms - int(last)) if last is not None else min_look_ms
        span_ms = min(max(gap_ms, min_look_ms), max_span_ms)
        from_ms = now_ms - span_ms
        if last is not None and now_ms - int(last) > max_span_ms:
            logger.warning(
                "backfill %s: gap exceeds %.1fd — capping (older gap left for a history job)",
                sym, max_days,
            )

        # What's already stored in the window — so we write only the holes.
        existing = set(store.get_candle_timestamps(sym, tf_value, from_ms, now_ms,
                                                    staging=use_staging))

        sym_written = 0
        cur = from_ms
        while cur < now_ms - interval_ms:
            to = min(cur + chunk_ms, now_ms)
            try:
                bars = await broker.fetch_trendbars(sym, _dt(cur), _dt(to))
            except Exception as e:  # noqa: BLE001 - one symbol's failure must not abort the rest
                logger.error("backfill %s: fetch_trendbars failed: %s", sym, e)
                break
            # Throttle historical requests to avoid cTrader's rate limit.
            if request_pause_s > 0:
                await asyncio.sleep(request_pause_s)
            for bar in bars or []:
                open_ms = bar.timestamp.timestamp() * 1000
                if open_ms + interval_ms > now_ms:
                    continue  # the still-forming bar — leave it for the live feed
                if int(open_ms) in existing:
                    continue  # already stored — idempotent skip
                candle = Candle(
                    exchange=exchange,
                    segment=segment,
                    exchange_token=sym,
                    timeframe=timeframe,
                    timestamp_ms=open_ms,
                    open=float(bar.open),
                    high=float(bar.high),
                    low=float(bar.low),
                    close=float(bar.close),
                    volume=int(getattr(bar, "volume", 0) or 0),
                )
                candle_store.on_candle(candle)  # write + session tagging
                existing.add(int(open_ms))
                sym_written += 1
            cur = to
        if sym_written:
            total += sym_written
            logger.info("backfill %s: wrote %d missing candle(s)", sym, sym_written)

    if total:
        logger.info("cTrader candle backfill complete: %d candle(s) written", total)
    else:
        logger.info("cTrader candle backfill: archive already complete (no holes)")
    return total
