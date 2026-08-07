"""
Account + prop-firm rule configuration (pluggable placeholder).

Right now you trade one account (the IC Markets cTrader demo). Later you will
run several prop-firm accounts (FTMO, FundedNext, The5ers, ...), each with its
own balance, risk-per-trade, and drawdown rules. This module models that
without hard-coding any firm: you describe a firm's rules in a ``PropFirmRules``
object and attach it to an ``AccountConfig``.

The ``RiskGuard`` (app.cfd_risk.risk_guard) already enforces daily/max DD given
these numbers — this module just carries the per-account configuration and
builds the matching RiskGuardConfig.

When you sign up with a firm, fill in a PropFirmRules with their published
limits and you are done — no code changes needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.cfd_risk.risk_guard import RiskGuardConfig


@dataclass(frozen=True)
class PropFirmRules:
    """
    A prop firm's risk rules. All percentages are of the initial balance
    (or start-of-day equity for daily DD), matching how firms publish them.

    These are placeholders with neutral defaults. Populate per firm, e.g.:

        FTMO = PropFirmRules(
            firm_name="FTMO",
            daily_dd_pct=5.0, max_dd_pct=10.0,
            max_dd_is_trailing=False,           # FTMO max DD is static from initial
            profit_target_pct=10.0,
            min_trading_days=4,
            reset_hour=0, reset_tz_offset_hours=2.0,   # CE(S)T server
        )

    Nothing here is firm-specific until you fill it in.
    """

    firm_name: str = "generic"

    # Drawdown limits (percent).
    daily_dd_pct: float = 5.0
    max_dd_pct: float = 10.0
    # Some firms trail the max-DD floor up with equity/balance; others keep it
    # static from the initial balance. RiskGuard currently uses a static floor;
    # trailing is a future extension (flagged here so it isn't forgotten).
    max_dd_is_trailing: bool = False

    # Evaluation targets (informational for now; used by challenge tracking later).
    profit_target_pct: float = 0.0        # 0 = no target (funded phase).
    min_trading_days: int = 0
    max_trading_days: int = 0             # 0 = unlimited.

    # Daily-DD reset schedule (firm server timezone).
    reset_hour: int = 0
    reset_minute: int = 0
    reset_tz_offset_hours: float = 3.0    # GMT+3 default (common MT/cTrader server).

    # Per-trade / aggregate risk caps.
    max_risk_per_trade_pct: float = 1.0
    max_open_risk_pct: float = 3.0

    # Behavioural guards.
    flatten_before_weekend: bool = True   # Most firms penalise weekend holds.
    flatten_before_daily_reset: bool = False
    # Some firms forbid holding trades over high-impact news; enforced later.
    news_trading_allowed: bool = True


# A neutral default so single-account development works out of the box.
GENERIC_RULES = PropFirmRules()


@dataclass
class AccountConfig:
    """
    One tradable account: its broker identity, balance, and the firm rules
    that govern it.

    ``ctrader_account_id`` is the numeric ctidTraderAccountId used by the
    Open API to target orders at this specific account. For paper-only accounts
    it can be left as 0.
    """

    account_id: str                       # Our internal label, e.g. "ftmo_100k_1".
    initial_balance: float
    rules: PropFirmRules = field(default_factory=lambda: GENERIC_RULES)
    ctrader_account_id: int = 0           # ctidTraderAccountId for live routing.
    enabled: bool = True
    # Risk-per-trade this account uses for position sizing. Defaults to the
    # firm's per-trade cap but can be set lower (more conservative).
    risk_per_trade_pct: float | None = None

    def effective_risk_per_trade_pct(self) -> float:
        """Risk % used for sizing (falls back to the firm's per-trade cap)."""
        if self.risk_per_trade_pct is not None:
            return self.risk_per_trade_pct
        return self.rules.max_risk_per_trade_pct

    def to_risk_guard_config(self) -> RiskGuardConfig:
        """Build the RiskGuardConfig that enforces this account's rules."""
        return RiskGuardConfig(
            initial_balance=self.initial_balance,
            daily_dd_pct=self.rules.daily_dd_pct,
            max_dd_pct=self.rules.max_dd_pct,
            reset_hour=self.rules.reset_hour,
            reset_minute=self.rules.reset_minute,
            reset_utc_offset_hours=self.rules.reset_tz_offset_hours,
            max_risk_per_trade_pct=self.rules.max_risk_per_trade_pct,
            max_open_risk_pct=self.rules.max_open_risk_pct,
            flatten_before_weekend=self.rules.flatten_before_weekend,
            flatten_before_daily_reset=self.rules.flatten_before_daily_reset,
        )
