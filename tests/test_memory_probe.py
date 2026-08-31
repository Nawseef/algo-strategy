"""Tests for the memory probe (SIGUSR2 heap reports + RSS in STATS)."""

import os
import sys
from pathlib import Path

import pytest

from app.utils import memory_probe


@pytest.fixture(autouse=True)
def _reset_state():
    """Isolate module-global flag/snapshot between tests."""
    memory_probe._DUMPRequested.clear()
    memory_probe._prev_snapshot = None
    yield
    memory_probe._DUMPRequested.clear()
    memory_probe._prev_snapshot = None


def test_rss_mb_platform_appropriate():
    rss = memory_probe.rss_mb()
    if sys.platform.startswith("linux"):
        assert rss is not None and rss > 0
    else:
        # /proc doesn't exist on macOS — must degrade to None, never raise.
        assert rss is None


def test_maybe_dump_is_noop_when_not_requested():
    assert memory_probe.maybe_dump() is None


def test_dump_writes_report(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_probe, "_DUMPRequested", type(memory_probe._DUMPRequested)())
    # Point log dir at tmp_path via a fake module __file__
    monkeypatch.setattr(
        memory_probe, "__file__", str(tmp_path / "app" / "utils" / "memory_probe.py")
    )
    memory_probe.request_dump()
    out = memory_probe.maybe_dump()
    assert out is not None
    text = Path(out).read_text()
    assert "# tracemalloc report" in text
    # CFD_TRACEMALLOC unset -> report says tracing is off
    assert "tracemalloc NOT enabled" in text


def test_start_probe_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CFD_TRACEMALLOC", raising=False)
    assert memory_probe.start_probe() is False


def test_traced_dump_has_top_allocations(monkeypatch):
    pytest.importorskip("tracemalloc")
    tracemalloc_mod = memory_probe.tracemalloc
    if not tracemalloc_mod.is_tracing():
        tracemalloc_mod.start(1)
    try:
        base = Path(temp_logs())
        base.mkdir(exist_ok=True)
        monkeypatch.setattr(
            memory_probe, "__file__", str(base.parent / "app" / "utils" / "memory_probe.py")
        )
        memory_probe.request_dump()
        first = memory_probe.maybe_dump()
        memory_probe.request_dump()
        second = memory_probe.maybe_dump()
        assert first and second
        txt2 = Path(second).read_text()
        assert "TOP ALLOCATIONS NOW" in txt2
        assert "TOP GROWTH SINCE LAST SNAPSHOT" in txt2  # prev snapshot existed
    finally:
        if tracemalloc_mod.is_tracing():
            tracemalloc_mod.stop()


def temp_logs():
    import tempfile
    return tempfile.mkdtemp(prefix="probe_test_")
