"""
CFD Backtest — replay stored 5m candles through the CFD strategy + exit engine.

Reads ``cfd_historical_candles`` (Dukascopy 5m bars) and runs each strategy
through the SAME decision path as live paper trading: candle-close evaluation
arms/opens signals, and intrabar SL/TP exits are resolved from each subsequent
candle's OHLC using a synthetic-tick path (the standard way to backtest
intrabar entries/exits on bar data without tick data).

Modules:
    exit_simulator — resolve a position's SL/TP over future candles (OHLC).
    replay         — day-by-day strategy replay + trade recording.
"""

from app.cfd_backtest.exit_simulator import (
    SimulatedTrade,
    simulate_exit,
    synthetic_tick_path,
)

__all__ = [
    "SimulatedTrade",
    "simulate_exit",
    "synthetic_tick_path",
]
