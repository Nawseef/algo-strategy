"""
Time-based exit models.

These test the hypothesis: "how long should a trade be held?"

Models:
- Fixed time exits: close after N candles (3, 6, 12, 24, 48 candles @ 5m = 15min to 4hrs)
- Session exits: force close at specific times (11:30, 13:00, 14:00, 15:15)
- Intraday decay: exit if not profitable after N candles (dead trade detection)

Each 5-minute candle = 5 minutes. So:
- 3 candles = 15 min
- 6 candles = 30 min  
- 12 candles = 1 hour
- 24 candles = 2 hours
- 48 candles = 4 hours
"""

from __future__ import annotations

from app.exit_engine.models.rr_exit import ExitResult


# ─── Fixed Time Exits ────────────────────────────────────────────────────────

TIME_CANDLE_EXITS = [3, 6, 12, 24, 48]  # candles to hold


def simulate_time_exit(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
    hold_candles: int,
) -> ExitResult:
    """
    Exit after exactly N candles (or at EOD if fewer candles available).
    """
    if not candle_path:
        return ExitResult(exit_price=entry_price, pnl_points=0.0, exit_reason="NO_PATH", exit_candle_index=-1)

    exit_idx = min(hold_candles - 1, len(candle_path) - 1)
    exit_price = candle_path[exit_idx]["close"]

    if direction == "LONG":
        pnl = exit_price - entry_price
    else:
        pnl = entry_price - exit_price

    return ExitResult(
        exit_price=exit_price,
        pnl_points=pnl,
        exit_reason=f"TIME_{hold_candles}_CANDLES",
        exit_candle_index=exit_idx,
    )


def simulate_all_time_exits(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
) -> dict[str, float]:
    """
    Run all time-based exits.

    Returns:
        {"time_15m": ..., "time_30m": ..., "time_1h": ..., "time_2h": ..., "time_4h": ...}
    """
    _KEY_MAP = {
        3: "time_15m",
        6: "time_30m",
        12: "time_1h",
        24: "time_2h",
        48: "time_4h",
    }

    results: dict[str, float] = {}
    for candles in TIME_CANDLE_EXITS:
        result = simulate_time_exit(entry_price, direction, candle_path, candles)
        results[_KEY_MAP[candles]] = result.pnl_points

    return results


# ─── Session-Based Exits ─────────────────────────────────────────────────────

# Session close times as candle offsets from 9:15 market open
# Each candle = 5 minutes from market open
SESSION_EXITS = {
    "session_morning": 27,    # 11:30 = 27 candles from 9:15
    "session_midday": 45,     # 13:00 = 45 candles from 9:15
    "session_afternoon": 57,  # 14:00 = 57 candles from 9:15
    "session_preclose": 72,   # 15:15 = 72 candles from 9:15
}


def simulate_session_exit(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
    entry_candle_from_open: int,
    session_close_candle: int,
) -> ExitResult:
    """
    Exit at a specific session time.
    entry_candle_from_open: which candle (from market open) the entry happened at.
    session_close_candle: which candle (from market open) to force exit.
    
    If entry is AFTER the session close, returns EOD close (inapplicable).
    """
    candles_to_hold = session_close_candle - entry_candle_from_open

    if candles_to_hold <= 0:
        # Entry was after this session point — just close at EOD
        if candle_path:
            last_close = candle_path[-1]["close"]
            pnl = (last_close - entry_price) if direction == "LONG" else (entry_price - last_close)
            return ExitResult(exit_price=last_close, pnl_points=pnl, exit_reason="SESSION_NA", exit_candle_index=-1)
        return ExitResult(exit_price=entry_price, pnl_points=0.0, exit_reason="SESSION_NA", exit_candle_index=-1)

    return simulate_time_exit(entry_price, direction, candle_path, candles_to_hold)


def simulate_all_session_exits(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
    entry_candle_from_open: int = 0,
) -> dict[str, float]:
    """
    Run all session-based exits.

    entry_candle_from_open: how many 5m candles after 9:15 the entry occurred.
    """
    results: dict[str, float] = {}
    for key, close_candle in SESSION_EXITS.items():
        result = simulate_session_exit(
            entry_price, direction, candle_path, entry_candle_from_open, close_candle
        )
        results[key] = result.pnl_points

    return results


# ─── Dead Trade Exit (not profitable after N candles) ────────────────────────

DEAD_TRADE_CANDLES = [6, 12, 24]  # check if losing after N candles


def simulate_dead_trade_exit(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
    check_after_candles: int,
) -> ExitResult:
    """
    Exit if trade is not profitable after N candles.
    If profitable, hold to EOD.
    Tests the "cut dead trades early" hypothesis.
    """
    if not candle_path or len(candle_path) < check_after_candles:
        # Not enough data — just close at EOD
        if candle_path:
            last = candle_path[-1]["close"]
            pnl = (last - entry_price) if direction == "LONG" else (entry_price - last)
            return ExitResult(exit_price=last, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)
        return ExitResult(exit_price=entry_price, pnl_points=0.0, exit_reason="NO_PATH", exit_candle_index=-1)

    # Check P&L at the checkpoint
    check_close = candle_path[check_after_candles - 1]["close"]
    if direction == "LONG":
        pnl_at_check = check_close - entry_price
    else:
        pnl_at_check = entry_price - check_close

    if pnl_at_check <= 0:
        # Losing — cut it
        return ExitResult(
            exit_price=check_close,
            pnl_points=pnl_at_check,
            exit_reason=f"DEAD_TRADE_{check_after_candles}",
            exit_candle_index=check_after_candles - 1,
        )

    # Profitable — hold to EOD
    last = candle_path[-1]["close"]
    pnl = (last - entry_price) if direction == "LONG" else (entry_price - last)
    return ExitResult(exit_price=last, pnl_points=pnl, exit_reason="CLOSE_AT_EOD", exit_candle_index=-1)


def simulate_all_dead_trade_exits(
    entry_price: float,
    direction: str,
    candle_path: list[dict],
) -> dict[str, float]:
    """
    Returns:
        {"dead_30m": ..., "dead_1h": ..., "dead_2h": ...}
    """
    _KEY_MAP = {6: "dead_30m", 12: "dead_1h", 24: "dead_2h"}
    results: dict[str, float] = {}
    for candles in DEAD_TRADE_CANDLES:
        result = simulate_dead_trade_exit(entry_price, direction, candle_path, candles)
        results[_KEY_MAP[candles]] = result.pnl_points
    return results
