"""
Memory diagnostics for the long-running CFD runner.

Why: the ``cfd-ctrader`` process grows ~250 MB/day (observed Aug 2026) and the
cause is not attributable from code review alone — our per-tick paths and the
ctrader-api-client internals are statically clean. This module provides the
ground truth needed to attribute growth to real allocation sites:

* ``rss_mb()`` — reads VmRSS from /proc so the periodic STATS line carries a
  precise memory timeline (journald keeps it forever, correlatable against
  reconnects/backfills after the fact).
* ``start_probe()`` — optional tracemalloc tracing, enabled with
  ``CFD_TRACEMALLOC=1`` (sampling 10 frames: low overhead, exact attribution).
  Also installs SIGUSR2: requesting a dump from another shell does NOT restart
  the service and can be done mid-session safely.
* Dumps are written by the schedule-monitor thread (NOT inside the signal
  handler) so the asyncio loop thread is never interrupted mid-callback.

Usage (on the VM):
    # enable tracing at boot (small CPU cost), then trigger anytime:
    kill -USR2 $(pgrep -f main_ctrader)
    tail -f logs/tracemalloc_*.txt   # top allocation sites + RSS
"""

from __future__ import annotations

import os
import signal
import threading
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

_DUMPRequested = threading.Event()
_prev_snapshot = None  # last tracemalloc.Snapshot we compared against
_lock = threading.Lock()


def rss_mb() -> float | None:
    """Resident memory of this process in MB, or None where unavailable.

    Linux (/proc): authoritative live value the systemd unit reports.
    macOS/dev: /proc does not exist -> None (callers print without it).
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return None


def high_water_mb() -> float | None:
    """Peak resident memory (VmHWM) in MB, or None where unavailable."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return None


def _request_dump(signum, frame) -> None:  # noqa: ANN001 - signal signature
    """Signal handler: ONLY set a flag; heavy work happens on the monitor thread."""
    _DUMPRequested.set()


def request_dump() -> None:
    """Programmatic equivalent of SIGUSR2 (used by tests and ops tooling)."""
    _DUMPRequested.set()


def maybe_dump() -> str | None:
    """If a dump was requested, write the snapshot report and clear the flag.

    Called from the runner's schedule-monitor thread (~every 15s). Returns the
    report path when a dump was written.
    """
    if not _DUMPRequested.is_set():
        return None
    _DUMPRequested.clear()
    global _prev_snapshot
    with _lock:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_dir = Path(__file__).resolve().parent.parent.parent / "logs"
        out_dir.mkdir(exist_ok=True)
        path = out_dir / f"tracemalloc_{ts}.txt"
        lines: list[str] = [
            f"# tracemalloc report {ts} UTC",
            f"rss_mb={rss_mb()}",
            f"high_water_mb={high_water_mb()}",
        ]
        if tracemalloc.is_tracing():
            snap = tracemalloc.take_snapshot()
            lines.append(f"traced_current_mb={sum(s.size for s in snap.statistics('lineno')) / 1048576:.1f}")
            if _prev_snapshot is not None:
                lines.append("")
                lines.append("== TOP GROWTH SINCE LAST SNAPSHOT (by lineno) ==")
                for stat in snap.compare_to(_prev_snapshot, "lineno")[:30]:
                    lines.append(f"+{stat.size_diff / 1048576:.2f} MB ({stat.count_diff:+d} objs) {stat}")
            lines.append("")
            lines.append("== TOP ALLOCATIONS NOW (by lineno) ==")
            for stat in snap.statistics("lineno")[:30]:
                lines.append(f"{stat.size / 1048576:.2f} MB x{stat.count} {stat}")
            lines.append("")
            lines.append("== TOP 5 ALLOCATION BACKTRACES ==")
            for stat in snap.statistics("traceback")[:5]:
                tb = "".join(stat.traceback.format())
                lines.append(f"--- {stat.size / 1048576:.2f} MB ---")
                lines.append(tb.rstrip())
            _prev_snapshot = snap
        else:
            lines.append("tracemalloc NOT enabled — set CFD_TRACEMALLOC=1 before "
                         "(re)start for allocation-site attribution")
        text = "\n".join(lines) + "\n"
        path.write_text(text)
        logger.warning(
            "MEMORY PROBE dump written: %s (rss=%s MB)",
            path.name,
            f"{rss_mb():.0f}" if rss_mb() is not None else "?",
        )
        return str(path)


def start_probe() -> bool:
    """Install SIGUSR2 + optionally start tracemalloc when CFD_TRACEMALLOC=1.

    Must be called once from the main thread at startup. Returns True when
    tracemalloc tracing is active.
    """
    try:
        signal.signal(signal.SIGUSR2, _request_dump)
        logger.info("memory probe installed: SIGUSR2 triggers a heap report "
                    "(kill -USR2 <pid>)")
    except (ValueError, AttributeError):
        logger.warning("memory probe: could not install SIGUSR2 handler")

    enabled = os.getenv("CFD_TRACEMALLOC", "").strip().lower() in {"1", "true", "yes"}
    if enabled and not tracemalloc.is_tracing():
        tracemalloc.start(10)
        logger.info("tracemalloc STARTED (CFD_TRACEMALLOC=1, nframes=10) — "
                    "allocation sites will be attributed on SIGUSR2 dumps")
    return enabled
