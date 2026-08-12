"""
CFD Instrument Specifications.

Defines pip value, contract size, pip value per lot (in USD), typical spread,
commission, and lot constraints for all 10 IC Markets CFD instruments.

Pip value calculation:
    For FX pairs: 1 pip = 0.0001 (except JPY = 0.01)
    For metals:   1 pip = 0.01 (gold), 0.001 (silver)
    For indices:  1 pip = 1.0 (point-based)
    For oil:      1 pip = 0.01

Pip value in USD (per standard lot):
    = pip_size × contract_size
    EURUSD: 0.0001 × 100,000 = $10/pip
    XAUUSD: 0.01 × 100 = $1/pip  (i.e. $1 per $0.01 move per lot)
    US30:   1.0 × 1 = $1/point

Note on Gold (XAUUSD):
    The "pip" convention for gold varies. We use 0.01 = 1 pip (industry standard
    for MT5 prop firms). So a move from 2400.00 to 2401.00 = 100 pips.
    Pip value = $1.00 per standard lot (100 oz × $0.01 = $1).
    BUT for position sizing, what matters is the DOLLAR MOVE per lot:
    A $1 price move on gold = $100 per standard lot (100 oz × $1).
    We provide both pip_value_per_lot AND point_value_per_lot for clarity.

Usage:
    from app.cfd_risk.instruments import get_instrument

    gold = get_instrument("XAUUSD")
    # How much does a $10 SL cost per lot?
    sl_cost = 10.0 * gold.point_value_per_lot  # $10 × $100/point = $1000
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import Enum

from app.utils.logger import get_logger

logger = get_logger(__name__)


class InstrumentCategory(Enum):
    """Instrument category for grouping."""
    METAL = "metal"
    FX = "fx"
    INDEX = "index"
    COMMODITY = "commodity"


@dataclass(frozen=True)
class CFDInstrument:
    """
    Complete specification for a CFD instrument.

    All values are for IC Markets Raw Spread account conditions.
    Commission on Raw account: $7 per standard lot round-trip for FX/metals,
    $0 for indices/commodities (spread-only).
    """

    symbol: str                     # IC Markets symbol name (e.g. "XAUUSD")
    name: str                       # Human-readable name
    category: InstrumentCategory    # metal, fx, index, commodity

    # ─── Pip / Point Definition ──────────────────────────────────
    pip_size: float                  # Price movement = 1 pip (0.0001, 0.01, 1.0, etc.)
    contract_size: float            # Units per standard lot (100oz, 100000, 1, etc.)
    pip_value_per_lot: float        # USD per pip per standard lot
    point_value_per_lot: float      # USD per $1 (or 1 point) price move per lot
    # NOTE (G6): for quote≠USD instruments (USDJPY, EUR-quoted DE40) this is a
    # STATIC approximation, not a live FX conversion. That's fine for the
    # backtest: position size = risk/(SL×point_value) and PnL = pnl_price×
    # point_value×lots, so point_value CANCELS in the risk-normalized % return —
    # R-multiples, pass-rate and drawdown are unaffected by a wrong point_value.
    # It only mis-scales the per-lot COMMISSION drag (and commission is $0 for
    # every index, incl. DE40), so the residual error is negligible. The LIVE
    # path still reads the exact value from the broker (apply_broker_spec).

    # ─── Costs ───────────────────────────────────────────────────
    typical_spread_pips: float      # Average spread in pips (Raw account)
    commission_per_lot: float       # USD per round-trip per standard lot ($7 or $0)

    # Realistic execution slippage in PRICE units (not pips) — instrument-native,
    # so it's meaningful for indices (where 0.5 "pips" ≈ nothing). This is the
    # BASE (normal-conditions) slippage; the session-open cost model multiplies
    # it. Used when a cost model sets slippage_price_multiplier (else the old
    # flat slippage_pips path applies). See G4/G5 in CFD_BACKTEST_PIPELINE.md.
    typical_slippage_price: float = 0.0

    # ─── Lot Constraints ─────────────────────────────────────────
    min_lot: float = 0.01           # Minimum trade size
    max_lot: float = 100.0          # Maximum trade size
    lot_step: float = 0.01          # Lot increment

    # ─── Margin ──────────────────────────────────────────────────
    margin_pct: float = 1.0         # Margin requirement % (1% = 1:100 leverage)

    @property
    def spread_cost_per_lot(self) -> float:
        """Spread cost in USD per standard lot (entry only)."""
        return self.typical_spread_pips * self.pip_value_per_lot

    @property
    def total_entry_cost_per_lot(self) -> float:
        """Total cost to enter + exit 1 standard lot (spread + commission)."""
        return self.spread_cost_per_lot + self.commission_per_lot

    def price_to_pips(self, price_distance: float) -> float:
        """Convert a price distance to pips."""
        return price_distance / self.pip_size

    def pips_to_price(self, pips: float) -> float:
        """Convert pips to price distance."""
        return pips * self.pip_size

    def sl_cost_per_lot(self, sl_distance_price: float) -> float:
        """
        USD risk per standard lot for a given SL distance in price units.

        This is the key calculation for position sizing:
            risk_per_lot = sl_distance × point_value_per_lot
        """
        return abs(sl_distance_price) * self.point_value_per_lot


# ─── Instrument Definitions ──────────────────────────────────────────────────

# Note on pip values:
# FX pairs (except JPY): pip = 0.0001, pip_value = $10/lot (contract 100,000)
# JPY pairs:             pip = 0.01,   pip_value = ~$6.50/lot (depends on USD/JPY rate)
#                        We use $6.50 as approximate (at USDJPY ~154)
# Gold (XAUUSD):         pip = 0.01,   pip_value = $1/lot (contract 100 oz)
#                        point_value = $100/lot ($1 price move × 100 oz)
# Silver (XAGUSD):       pip = 0.001,  pip_value = $1/lot (contract 1000 oz × 0.001)
#                        point_value = $1000/lot ($1 price move × 1000 oz) — CAREFUL!
#                        Actually: $0.01 move = $10 per lot (1000 × 0.01)
#                        Let's use pip=0.001, pip_value=$1 for clarity
# Indices:               pip = 1.0 (= 1 point), values vary by index
# Oil (XTIUSD):          pip = 0.01,   pip_value = $1/lot (contract 100 barrels)

_INSTRUMENTS: dict[str, CFDInstrument] = {}


def _register(inst: CFDInstrument) -> None:
    _INSTRUMENTS[inst.symbol] = inst


# ─── Metals ──────────────────────────────────────────────────────────────────

_register(CFDInstrument(
    symbol="XAUUSD",
    typical_slippage_price=0.08,    # ~8 cents base execution slippage
    name="Gold",
    category=InstrumentCategory.METAL,
    pip_size=0.01,
    contract_size=100.0,            # 100 troy ounces
    pip_value_per_lot=1.0,          # 100 oz × $0.01 = $1
    point_value_per_lot=100.0,      # 100 oz × $1.00 = $100 per $1 move
    typical_spread_pips=7.0,        # 0.07 in price = 7 pips (avg raw)
    commission_per_lot=7.0,         # $7 round-trip
    margin_pct=1.0,                 # 1:100 leverage (prop firm standard)
))

_register(CFDInstrument(
    symbol="XAGUSD",
    typical_slippage_price=0.008,
    name="Silver",
    category=InstrumentCategory.METAL,
    pip_size=0.001,
    contract_size=1000.0,           # 1000 troy oz — VERIFIED on our IC Markets feed
    pip_value_per_lot=1.0,          # 1000 oz × $0.001 = $1
    point_value_per_lot=1000.0,     # 1000 oz × $1.00 = $1000 per $1 move
    typical_spread_pips=2.0,        # ~0.002 in price = 2 pips (avg raw)
    commission_per_lot=7.0,         # $7 round-trip
    margin_pct=1.0,
    # ─── CONTRACT SIZE IS BROKER-SPECIFIC — this is only a FALLBACK. ───────────
    # The authoritative value comes from the broker at runtime (MT5 symbol_info
    # trade_contract_size / trade_tick_value; see MT5Broker.get_symbol_spec +
    # instruments.apply_broker_spec, called by the runner on connect).
    # VERIFIED values (queried live):
    #   * IC Markets (our feed):  1000 oz  -> $1000 per $1 move/lot
    #   * FundedNext / MT5 std:   5000 oz  -> $5000 per $1 move/lot
    #   * FTMO / The5ers:         confirm per account (auto-synced when live)
    # This default matches IC Markets (the venue we paper-trade on) so backtests
    # line up with paper. For a different venue, either let the live sync correct
    # it, or force it for offline/backtest:
    #   * env:      CFD_CONTRACT_SIZE_XAGUSD=5000
    #   * runtime:  instruments.set_contract_size("XAGUSD", 5000)
))

# ─── FX Pairs ────────────────────────────────────────────────────────────────

_register(CFDInstrument(
    symbol="EURUSD",
    typical_slippage_price=0.00003,   # ~0.3 pip
    name="Euro/USD",
    category=InstrumentCategory.FX,
    pip_size=0.0001,
    contract_size=100_000.0,        # Standard lot = 100,000 units
    pip_value_per_lot=10.0,         # 100,000 × 0.0001 = $10
    point_value_per_lot=100_000.0,  # 100,000 × $1 per 1.0000 move
    # SL in price: 0.0020 (20 pips) → risk = 0.0020 × 100,000 = $200/lot
    typical_spread_pips=0.1,        # IC Markets Raw avg: 0.01 pips (≈0.1 rounded up)
    commission_per_lot=7.0,
    margin_pct=1.0,
))

_register(CFDInstrument(
    symbol="GBPUSD",
    typical_slippage_price=0.00005,
    name="Pound/USD",
    category=InstrumentCategory.FX,
    pip_size=0.0001,
    contract_size=100_000.0,
    pip_value_per_lot=10.0,
    point_value_per_lot=100_000.0,
    typical_spread_pips=0.4,        # IC Markets Raw avg: 0.04 pips (use 0.4 conservative)
    commission_per_lot=7.0,
    margin_pct=1.0,
))

_register(CFDInstrument(
    symbol="USDJPY",
    typical_slippage_price=0.005,     # ~0.5 pip
    name="USD/Yen",
    category=InstrumentCategory.FX,
    pip_size=0.01,
    contract_size=100_000.0,
    # For USDJPY: pip value in USD = (0.01 / USDJPY rate) × 100,000
    # At USDJPY ~154: (0.01 / 154) × 100,000 ≈ $6.49
    # We use $6.50 as a reasonable approximation.
    # This should ideally be recalculated from live price, but for risk
    # sizing a fixed approximation is standard practice.
    pip_value_per_lot=6.50,
    point_value_per_lot=649.35,     # (1.0 / 154) × 100,000 ≈ $649 per 1 yen move
    typical_spread_pips=0.3,        # IC Markets Raw avg: 0.03 pips (use 0.3 conservative)
    commission_per_lot=7.0,
    margin_pct=1.0,
))

# ─── Indices ─────────────────────────────────────────────────────────────────

_register(CFDInstrument(
    symbol="US30",
    typical_slippage_price=1.5,       # ~1.5 index points
    name="Dow Jones",
    category=InstrumentCategory.INDEX,
    pip_size=1.0,                   # 1 point = 1 pip for indices
    contract_size=1.0,              # 1 contract = $1 per point
    pip_value_per_lot=1.0,          # $1 per point per lot
    point_value_per_lot=1.0,        # $1 per point per lot
    typical_spread_pips=1.4,        # IC Markets avg: 1.411 points
    commission_per_lot=0.0,         # No commission (spread-only)
    margin_pct=1.0,
))

_register(CFDInstrument(
    symbol="US500",
    typical_slippage_price=0.3,
    name="S&P 500",
    category=InstrumentCategory.INDEX,
    pip_size=0.1,                   # 0.1 point = 1 pip for S&P
    contract_size=1.0,
    pip_value_per_lot=0.1,          # $0.1 per pip per lot
    point_value_per_lot=1.0,        # $1 per full point per lot
    typical_spread_pips=5.0,        # IC Markets avg: 0.492 points = ~5 pips (at 0.1/pip)
    commission_per_lot=0.0,
    margin_pct=1.0,
))

_register(CFDInstrument(
    symbol="USTEC",
    typical_slippage_price=2.0,
    name="Nasdaq 100",
    category=InstrumentCategory.INDEX,
    pip_size=0.1,                   # 0.1 point = 1 pip
    contract_size=1.0,
    pip_value_per_lot=0.1,          # $0.1 per pip per lot
    point_value_per_lot=1.0,        # $1 per full point per lot
    typical_spread_pips=18.0,       # IC Markets avg: 1.807 points = ~18 pips (at 0.1/pip)
    commission_per_lot=0.0,
    margin_pct=1.0,
))

_register(CFDInstrument(
    symbol="DE40",
    typical_slippage_price=1.5,
    name="DAX",
    category=InstrumentCategory.INDEX,
    pip_size=0.1,                   # 0.1 point = 1 pip
    contract_size=1.0,
    pip_value_per_lot=0.1,          # €0.1 per pip ≈ $0.1 (simplified to USD)
    point_value_per_lot=1.0,        # $1 per full point per lot
    typical_spread_pips=13.0,       # IC Markets avg: 1.338 points = ~13 pips (at 0.1/pip)
    commission_per_lot=0.0,
    margin_pct=1.0,
))

# ─── Commodities ─────────────────────────────────────────────────────────────

_register(CFDInstrument(
    symbol="XTIUSD",
    typical_slippage_price=0.03,
    name="WTI Crude Oil",
    category=InstrumentCategory.COMMODITY,
    pip_size=0.01,
    contract_size=100.0,            # 100 barrels per lot
    pip_value_per_lot=1.0,          # 100 barrels × $0.01 = $1
    point_value_per_lot=100.0,      # 100 barrels × $1.00 = $100 per $1 move
    typical_spread_pips=3.4,        # IC Markets avg: 0.034 price = 3.4 pips
    commission_per_lot=0.0,         # Spread-only on IC Markets
    margin_pct=1.0,
))


# ─── Public API ──────────────────────────────────────────────────────────────

def get_instrument(symbol: str) -> CFDInstrument:
    """
    Get instrument specification by symbol.

    Args:
        symbol: IC Markets symbol name (e.g. "XAUUSD", "EURUSD", "US30")

    Returns:
        CFDInstrument with all specifications.

    Raises:
        KeyError: If symbol is not in the 10 supported instruments.
    """
    symbol_upper = symbol.upper()
    if symbol_upper not in _INSTRUMENTS:
        raise KeyError(
            f"Unknown instrument '{symbol}'. "
            f"Supported: {list(_INSTRUMENTS.keys())}"
        )
    return _INSTRUMENTS[symbol_upper]


def get_all_instruments() -> dict[str, CFDInstrument]:
    """Get all registered instrument specifications."""
    return dict(_INSTRUMENTS)


def get_instruments_by_category(category: InstrumentCategory) -> list[CFDInstrument]:
    """Get all instruments in a category."""
    return [inst for inst in _INSTRUMENTS.values() if inst.category == category]


# ─── Broker-specific contract-size overrides ─────────────────────────────────
#
# Contract size (units per standard lot) is BROKER/ENTITY-specific — the same
# symbol (e.g. XAGUSD) can be 5000 oz on one prop firm/broker and 1000 oz on
# another. That single number linearly drives the dollar value of a move, so the
# per-pip and per-point USD values MUST scale with it. Rather than hand-editing
# three fields (and risking them drifting out of sync), set the contract size in
# ONE place and let the dependent values scale.
#
# Two ways to override (both call set_contract_size under the hood):
#   1. Env, per symbol, at process start:   CFD_CONTRACT_SIZE_XAGUSD=1000
#   2. Runtime (e.g. from live broker symbol metadata):
#          from app.cfd_risk import instruments
#          instruments.set_contract_size("XAGUSD", 1000)
#
# Commission (fixed $/lot) and lot constraints are NOT scaled — only the
# contract-size-linear values (pip_value_per_lot, point_value_per_lot).


def set_contract_size(symbol: str, new_contract_size: float) -> CFDInstrument:
    """
    Override an instrument's contract size and rescale its USD-per-move values.

    ``pip_value_per_lot`` and ``point_value_per_lot`` are linear in contract size,
    so both are scaled by ``new_contract_size / old_contract_size``. This is the
    single, safe way to adapt a spec to a specific broker/prop-firm account.

    Returns the updated (re-registered) CFDInstrument.
    """
    key = symbol.upper()
    if key not in _INSTRUMENTS:
        raise KeyError(f"Unknown instrument '{symbol}'. Supported: {list(_INSTRUMENTS)}")
    inst = _INSTRUMENTS[key]
    if new_contract_size <= 0:
        raise ValueError(f"contract_size must be > 0, got {new_contract_size}")
    if inst.contract_size <= 0:
        raise ValueError(f"{key} has a non-positive base contract_size; cannot scale")

    if abs(new_contract_size - inst.contract_size) < 1e-9:
        return inst  # no change

    ratio = new_contract_size / inst.contract_size
    updated = replace(
        inst,
        contract_size=new_contract_size,
        pip_value_per_lot=inst.pip_value_per_lot * ratio,
        point_value_per_lot=inst.point_value_per_lot * ratio,
    )
    _INSTRUMENTS[key] = updated
    logger.warning(
        "Contract size override for %s: %g -> %g oz/units (x%.4g). "
        "pip_value $%.4g->$%.4g/lot, point_value $%.4g->$%.4g/lot.",
        key, inst.contract_size, new_contract_size, ratio,
        inst.pip_value_per_lot, updated.pip_value_per_lot,
        inst.point_value_per_lot, updated.point_value_per_lot,
    )
    return updated


def apply_broker_spec(
    symbol: str,
    contract_size: float,
    tick_value: float,
    tick_size: float,
) -> CFDInstrument:
    """
    Update an instrument spec from the broker's AUTHORITATIVE symbol info.

    This is the correct, venue-exact source of truth (call it on connect with
    values from ``MT5Broker.get_symbol_spec`` / the cTrader symbol). The USD
    value of a 1.0 price move per lot is ``tick_value / tick_size`` — already in
    the account currency, so cross-currency conversion is handled by the broker.
    We set:

        point_value_per_lot = tick_value / tick_size
        pip_value_per_lot   = point_value_per_lot * pip_size   (pip_size kept)
        contract_size       = contract_size

    Logs a WARNING if this differs from the current (static) spec — that warning
    is exactly what catches a wrong hardcoded contract size (e.g. silver 5000 vs
    the broker's real 1000). Returns the updated, re-registered instrument.
    """
    key = symbol.upper()
    if key not in _INSTRUMENTS:
        raise KeyError(f"Unknown instrument '{symbol}'. Supported: {list(_INSTRUMENTS)}")
    if not tick_size or tick_size <= 0 or not contract_size or contract_size <= 0:
        raise ValueError(
            f"{key}: invalid broker spec (contract_size={contract_size}, tick_size={tick_size})"
        )

    inst = _INSTRUMENTS[key]
    point_value = tick_value / tick_size
    pip_value = point_value * inst.pip_size
    updated = replace(
        inst,
        contract_size=contract_size,
        point_value_per_lot=point_value,
        pip_value_per_lot=pip_value,
    )
    _INSTRUMENTS[key] = updated

    changed = (
        abs(inst.point_value_per_lot - point_value) > 1e-6
        or abs(inst.contract_size - contract_size) > 1e-9
    )
    log = logger.warning if changed else logger.info
    log(
        "Broker spec for %s: contract_size %g->%g, point_value $%.4g->$%.4g/lot%s",
        key, inst.contract_size, contract_size,
        inst.point_value_per_lot, point_value,
        "  (CORRECTED static default)" if changed else "  (matches static)",
    )
    return updated


def _apply_env_overrides() -> None:
    """Apply any ``CFD_CONTRACT_SIZE_<SYMBOL>`` env overrides at import time."""
    for key in list(_INSTRUMENTS):
        raw = os.getenv(f"CFD_CONTRACT_SIZE_{key}", "").strip()
        if not raw:
            continue
        try:
            size = float(raw)
        except ValueError:
            logger.error("Ignoring invalid CFD_CONTRACT_SIZE_%s=%r (not a number)", key, raw)
            continue
        try:
            set_contract_size(key, size)
        except (KeyError, ValueError) as e:
            logger.error("Ignoring CFD_CONTRACT_SIZE_%s: %s", key, e)


_apply_env_overrides()
