"""
Transaction Cost Model — deducts realistic trading costs from PnL.

Costs are deducted per ROUND TRIP (entry + exit) in POINTS (not rupees).
This keeps it instrument-agnostic — the scoring engine works in points.

Components per round trip:
1. Brokerage (Groww: ₹20 flat per order × 2 = ₹40)
2. STT (Securities Transaction Tax)
3. Exchange transaction charges
4. SEBI turnover + IPFT
5. Stamp duty
6. GST (18% on brokerage + exchange charges)
7. Slippage (market impact — price moves against you on execution)

All converted to POINTS based on lot size and price level.

Usage:
    from app.scoring.costs import CostModel, COST_EQUITY_INTRADAY, COST_FUTURES

    model = COST_EQUITY_INTRADAY
    net_pnl = raw_pnl - model.cost_per_trade_points("NIFTY")

    # Or apply to a list of PnLs:
    adjusted_pnls = model.apply(raw_pnls, instrument="NIFTY")
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """
    Transaction cost model for a specific trading mode.

    Costs are expressed in POINTS per round trip (entry + exit).
    These are approximate and conservative (slightly over-estimate costs
    so that any variant passing the filter is genuinely profitable).

    The cost_per_trade_points varies by instrument because the same
    ₹ cost translates to different points depending on lot size and price.
    """

    name: str
    description: str

    # Fixed costs in POINTS per round trip (instrument-specific)
    # These include: brokerage + STT + exchange + SEBI + stamp + GST + slippage
    cost_points_nifty: float  # NIFTY (lot=25, price~25000, 1pt=₹25)
    cost_points_banknifty: float  # BANKNIFTY (lot=15, price~52000, 1pt=₹15)
    cost_points_stock: float  # Stocks (qty=1, varies, use ₹ equivalent in points)

    def cost_per_trade_points(self, instrument: str) -> float:
        """Get cost in points for a specific instrument."""
        instrument_upper = instrument.upper()

        if "NIFTY" in instrument_upper and "BANK" not in instrument_upper:
            return self.cost_points_nifty
        elif "BANKNIFTY" in instrument_upper or "BANK" in instrument_upper:
            return self.cost_points_banknifty
        else:
            # Stock — use generic stock cost
            return self.cost_points_stock

    def apply(self, pnl_values: list[float], instrument: str = "NIFTY") -> list[float]:
        """
        Apply cost model to a list of PnL values.
        Deducts cost per trade from each PnL.

        Returns new list with costs deducted (original list unchanged).
        """
        cost = self.cost_per_trade_points(instrument)
        return [pnl - cost for pnl in pnl_values]


# ─── Pre-built cost models ───────────────────────────────────────────────────

# Equity Intraday (Groww, 2026)
# NIFTY: brokerage ₹40 + STT 0.025%×sell + exchange + stamp + GST + slippage
# At NIFTY 25000, lot 25: ₹40 brokerage + ~₹15 STT + ~₹5 other + ~₹25 slippage = ~₹85
# In points: ₹85 / 25 = ~3.4 points → round up to 4
COST_EQUITY_INTRADAY = CostModel(
    name="equity_intraday",
    description="Equity intraday on Groww (₹20/order, STT 0.025% sell, ~2pt slippage)",
    cost_points_nifty=4.0,      # ~₹100 total / 25 qty
    cost_points_banknifty=6.0,  # ~₹90 total / 15 qty
    cost_points_stock=3.0,      # ~₹60 total, stocks vary but ~3pts average
)

# Futures (Groww, 2026 — post Budget STT hike)
# NIFTY Futures: brokerage ₹40 + STT 0.05%×sell + exchange + stamp + GST + slippage
# At NIFTY 25000, lot 25: ₹40 + ₹312 STT + ~₹10 other + ₹50 slippage = ~₹412
# In points: ₹412 / 25 = ~16.5 points
COST_FUTURES = CostModel(
    name="futures",
    description="Futures on Groww (₹20/order, STT 0.05% sell, ~2pt slippage)",
    cost_points_nifty=17.0,     # ~₹425 total / 25 qty
    cost_points_banknifty=28.0, # ~₹420 total / 15 qty
    cost_points_stock=5.0,      # Stock futures — varies
)

# Options (Groww, 2026 — for future use)
# Not relevant for current research (we trade underlying, not options)
# But included for completeness
COST_OPTIONS = CostModel(
    name="options",
    description="Options on Groww (₹20/order, STT 0.15% sell on premium)",
    cost_points_nifty=8.0,      # Options cost depends on premium, approximate
    cost_points_banknifty=12.0,
    cost_points_stock=4.0,
)

# Zero cost (for raw/unadjusted scoring)
COST_NONE = CostModel(
    name="none",
    description="No transaction costs (raw PnL)",
    cost_points_nifty=0.0,
    cost_points_banknifty=0.0,
    cost_points_stock=0.0,
)

# Conservative (double the equity intraday — for stress testing)
COST_CONSERVATIVE = CostModel(
    name="conservative",
    description="Conservative estimate (2× equity intraday costs + extra slippage)",
    cost_points_nifty=8.0,
    cost_points_banknifty=12.0,
    cost_points_stock=6.0,
)


# ─── Helper to get model by name ────────────────────────────────────────────

COST_MODELS: dict[str, CostModel] = {
    "none": COST_NONE,
    "equity_intraday": COST_EQUITY_INTRADAY,
    "futures": COST_FUTURES,
    "options": COST_OPTIONS,
    "conservative": COST_CONSERVATIVE,
}


def get_cost_model(name: str) -> CostModel:
    """Get a cost model by name. Defaults to equity_intraday if not found."""
    return COST_MODELS.get(name, COST_EQUITY_INTRADAY)
