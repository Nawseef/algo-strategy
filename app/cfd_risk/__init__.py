"""
CFD Risk Infrastructure — prop-firm-style risk management for CFD trading.

Modules:
    instruments      — Instrument specifications (pip, contract size, spread, commission)
    position_sizing  — Risk-based lot sizing (balance × risk% / SL pips × pip value)
    risk_guard       — Daily DD / Max DD tracking + auto-halt
    costs            — CFD cost model (spread + commission + slippage)
"""

from app.cfd_risk.instruments import get_instrument, get_all_instruments, CFDInstrument
from app.cfd_risk.position_sizing import calculate_lot_size, PositionSizeResult
from app.cfd_risk.risk_guard import RiskGuard, RiskGuardConfig, RiskStatus
from app.cfd_risk.costs import CFDCostModel, calculate_trade_cost, TradeCost

__all__ = [
    "get_instrument",
    "get_all_instruments",
    "CFDInstrument",
    "calculate_lot_size",
    "PositionSizeResult",
    "RiskGuard",
    "RiskGuardConfig",
    "RiskStatus",
    "CFDCostModel",
    "calculate_trade_cost",
    "TradeCost",
]
