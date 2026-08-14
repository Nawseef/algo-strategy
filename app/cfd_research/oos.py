"""
Out-of-sample (OOS) validation — the guard against data-mining false positives.

When you slice a strategy thousands of ways (instrument x session x timeframe x
regime x volatility x exit x risk), some slice WILL look deployable purely by
luck. The defence is to split history: DISCOVER deployable slices on one window,
then CONFIRM they're still deployable on a later, untouched window. A slice that
survives BOTH is "robust"; one that only shines in-sample is a mirage.

This works entirely on already-generated trades (filtered by entry time), so it
needs no re-walk — pair it with ``--score-from`` to try different split dates
instantly. The split is a single date: trades entered BEFORE it are discover
(in-sample), trades entered ON/AFTER it are confirm (out-of-sample). Both windows
are scored with the SAME dimensions/rules; the only difference is the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.cfd_backtest.exit_simulator import SimulatedTrade
from app.cfd_research.slice_scorer import SliceResult

# score_fn(trades, data_start_ms, data_end_ms) -> scored slices for that window.
ScoreFn = Callable[[list[SimulatedTrade], float | None, float | None], list[SliceResult]]


@dataclass
class OOSRow:
    """One discover slice paired with its confirm-window counterpart (if any)."""

    discover: SliceResult
    confirm: SliceResult | None      # None if that slice had no trades in confirm

    @property
    def robust(self) -> bool:
        """Deployable in BOTH windows — survived out-of-sample."""
        return bool(self.discover.qualifies and self.confirm is not None
                    and self.confirm.qualifies)


def _key(r: SliceResult) -> tuple:
    return (r.label(), r.risk_pct)


def validate_oos(
    trades: list[SimulatedTrade],
    split_ms: float,
    score_discover: ScoreFn,
    score_confirm: ScoreFn,
    *,
    data_start_ms: float | None = None,
    data_end_ms: float | None = None,
) -> tuple[list[OOSRow], int, int]:
    """Split ``trades`` at ``split_ms``, score each window, and join by slice key.

    Returns ``(rows, n_discover_trades, n_confirm_trades)``. ``rows`` are the
    DISCOVER slices (each carrying its confirm-window match, if the slice traded
    in confirm), sorted robust-first.
    """
    discover = [t for t in trades if t.entry_time_ms < split_ms]
    confirm = [t for t in trades if t.entry_time_ms >= split_ms]

    res_d = score_discover(discover, data_start_ms, split_ms)
    res_c = score_confirm(confirm, split_ms, data_end_ms)

    cmap: dict[tuple, SliceResult] = {_key(r): r for r in res_c}
    rows = [OOSRow(discover=rd, confirm=cmap.get(_key(rd))) for rd in res_d]

    # Robust first; then discover-deployable; then by discover pass-rate.
    rows.sort(key=lambda r: (
        0 if r.robust else 1,
        0 if r.discover.qualifies else 1,
        -r.discover.mc.pass_rate,
    ))
    return rows, len(discover), len(confirm)


def format_oos(
    rows: list[OOSRow],
    n_discover: int,
    n_confirm: int,
    *,
    top: int | None = 40,
    deployable_discover_only: bool = True,
) -> str:
    """Render the OOS comparison. By default shows only discover-deployable slices
    (the candidates) with their confirm-window outcome and a ROBUST verdict."""
    shown = [r for r in rows if r.discover.qualifies] if deployable_discover_only else rows
    n_disc_dep = sum(1 for r in rows if r.discover.qualifies)
    n_robust = sum(1 for r in rows if r.robust)

    header = (
        f"OUT-OF-SAMPLE VALIDATION  (discover trades={n_discover:,} | confirm trades={n_confirm:,})\n"
        f"Discover-deployable slices: {n_disc_dep}  |  ROBUST (deployable in confirm too): {n_robust}\n"
        f"Each row: DISCOVER pass%/blowup% [flags]  ->  CONFIRM pass%/blowup% [flags]  = verdict\n"
        + "-" * 100
    )
    if not shown:
        return header + "\n(no discover-deployable slices — nothing to confirm)"

    lines = []
    for r in (shown[:top] if top else shown):
        d = r.discover
        dcol = (f"D:{d.mc.pass_rate*100:5.1f}%/{d.mc.blowup_rate*100:4.1f}% "
                f"[{d.deploy.flags()} {'P' if d.passes_challenge else 'p'}]")
        if r.confirm is None:
            ccol = "C: (no trades in confirm window)"
            verdict = "NOT-CONFIRMED"
        else:
            c = r.confirm
            ccol = (f"C:{c.mc.pass_rate*100:5.1f}%/{c.mc.blowup_rate*100:4.1f}% "
                    f"[{c.deploy.flags()} {'P' if c.passes_challenge else 'p'}]")
            verdict = "ROBUST" if r.robust else "FAILED-OOS"
        lines.append(f"{d.label():44s} risk={d.risk_pct:>4.2f}% | {dcol} -> {ccol}  = {verdict}")

    return header + "\n" + "\n".join(lines)
