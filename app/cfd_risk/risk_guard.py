"""
Prop-Firm Risk Guard — Daily DD / Max DD tracking + auto-halt.

Enforces two independent drawdown limits (percentage-based, account-size-agnostic):

1. Daily Drawdown (default 5%):
   - Measured from the START-OF-DAY equity (higher of balance or equity at reset time)
   - Includes floating (unrealized) P&L
   - Resets at configurable time (default: midnight GMT+3, matching most prop firms)
   - Breach = no new trades until next reset

2. Maximum Drawdown (default 10%):
   - Measured from INITIAL balance (static floor — never trails up)
   - Includes floating P&L
   - Breach = account blown, all trading stops permanently (until manual reset)

Equity tracking:
   equity = balance + unrealized_pnl
   balance updates only when a trade closes (realized P&L added)
   unrealized_pnl updates on every tick/candle (sum of all open positions' floating P&L)

Usage:
    from app.cfd_risk.risk_guard import RiskGuard, RiskGuardConfig

    config = RiskGuardConfig(
        initial_balance=100_000.0,
        daily_dd_pct=5.0,
        max_dd_pct=10.0,
    )
    guard = RiskGuard(config)

    # On each trade close:
    guard.add_realized_pnl(150.0)

    # On each tick (update floating P&L across all open trades):
    guard.update_unrealized_pnl(-320.0)

    # Before opening a new trade:
    if guard.can_trade():
        # proceed
    else:
        print(guard.status)  # "DAILY_HALT" or "MAX_DD_BREACH"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum

from app.utils.logger import get_logger

logger = get_logger(__name__)


class RiskStatus(Enum):
    """Current risk guard status."""
    ACTIVE = "ACTIVE"               # Trading allowed
    DAILY_HALT = "DAILY_HALT"       # Daily DD breached — halted until reset
    MAX_DD_BREACH = "MAX_DD_BREACH" # Max DD breached — account blown


@dataclass
class RiskGuardConfig:
    """
    Configuration for the risk guard.
    All percentages are relative to initial_balance.
    """

    # ─── Account ─────────────────────────────────────────────────
    initial_balance: float = 100_000.0

    # ─── Drawdown Limits (percentage) ────────────────────────────
    daily_dd_pct: float = 5.0       # Max daily loss as % of start-of-day equity
    max_dd_pct: float = 10.0        # Max overall loss as % of initial balance

    # ─── Daily Reset ─────────────────────────────────────────────
    # Most prop firms reset daily DD at midnight server time (GMT+2/+3)
    reset_hour: int = 0             # Hour of day to reset (0-23)
    reset_minute: int = 0           # Minute of hour to reset
    reset_utc_offset_hours: float = 3.0  # UTC offset for reset timezone (GMT+3 default)

    # ─── Risk Per Trade (optional enforcement) ───────────────────
    max_risk_per_trade_pct: float = 1.0   # Max risk per single trade (% of balance)
    max_open_risk_pct: float = 3.0        # Max aggregate open risk (% of balance)

    # ─── Flatten Guards (per-strategy toggles, enforced externally) ─
    flatten_before_weekend: bool = False   # Force close before weekend
    flatten_before_daily_reset: bool = False  # Force close before DD reset


@dataclass
class DailyState:
    """Internal state for daily tracking."""
    start_of_day_equity: float = 0.0    # Equity at reset (higher of balance/equity)
    daily_realized_pnl: float = 0.0     # Closed P&L since last reset
    daily_lowest_equity: float = 0.0    # Lowest equity hit today (for reporting)
    reset_timestamp: float = 0.0        # When the last reset occurred (epoch ms)
    trades_today: int = 0               # Number of trades opened today


class RiskGuard:
    """
    Real-time drawdown tracking and trade gating.

    Thread-safe: designed to be called from a single consumer thread
    (the main strategy loop). If needed for multi-thread, add a lock.
    """

    def __init__(self, config: RiskGuardConfig) -> None:
        self._config = config

        # ─── Account State ───────────────────────────────────────
        self._balance: float = config.initial_balance
        self._unrealized_pnl: float = 0.0
        self._peak_balance: float = config.initial_balance  # For trailing DD (future)

        # ─── Drawdown Tracking ───────────────────────────────────
        self._status: RiskStatus = RiskStatus.ACTIVE
        self._daily = DailyState(
            start_of_day_equity=config.initial_balance,
            daily_lowest_equity=config.initial_balance,
        )

        # ─── Max DD Floor (static from initial balance) ──────────
        self._max_dd_floor: float = config.initial_balance * (1 - config.max_dd_pct / 100.0)

        # ─── History (for reporting / Telegram) ──────────────────
        self._total_realized_pnl: float = 0.0
        self._total_trades: int = 0
        self._daily_halts: int = 0

        logger.info(
            "RiskGuard initialized: balance=$%.2f, daily_dd=%.1f%%, max_dd=%.1f%%, "
            "floor=$%.2f",
            config.initial_balance, config.daily_dd_pct, config.max_dd_pct,
            self._max_dd_floor,
        )

    # ─── Public Properties ───────────────────────────────────────────────────

    @property
    def balance(self) -> float:
        """Current account balance (realized only)."""
        return self._balance

    @property
    def equity(self) -> float:
        """Current equity (balance + unrealized P&L)."""
        return self._balance + self._unrealized_pnl

    @property
    def unrealized_pnl(self) -> float:
        """Current floating P&L across all open positions."""
        return self._unrealized_pnl

    @property
    def status(self) -> RiskStatus:
        """Current risk status."""
        return self._status

    @property
    def config(self) -> RiskGuardConfig:
        """Current configuration."""
        return self._config

    @property
    def daily_pnl(self) -> float:
        """Today's total P&L (realized + unrealized)."""
        return (self.equity - self._daily.start_of_day_equity)

    @property
    def daily_dd_used_pct(self) -> float:
        """How much of the daily DD budget has been used (%)."""
        if self._daily.start_of_day_equity <= 0:
            return 0.0
        loss = max(0.0, self._daily.start_of_day_equity - self.equity)
        return (loss / self._daily.start_of_day_equity) * 100.0

    @property
    def daily_dd_remaining_usd(self) -> float:
        """USD remaining before daily DD breach."""
        allowed_loss = self._daily.start_of_day_equity * (self._config.daily_dd_pct / 100.0)
        current_loss = max(0.0, self._daily.start_of_day_equity - self.equity)
        return max(0.0, allowed_loss - current_loss)

    @property
    def max_dd_used_pct(self) -> float:
        """How much of the max DD budget has been used (%)."""
        loss = max(0.0, self._config.initial_balance - self.equity)
        return (loss / self._config.initial_balance) * 100.0

    @property
    def max_dd_remaining_usd(self) -> float:
        """USD remaining before max DD breach."""
        return max(0.0, self.equity - self._max_dd_floor)

    @property
    def total_pnl(self) -> float:
        """Total realized P&L since inception."""
        return self._total_realized_pnl

    @property
    def total_pnl_pct(self) -> float:
        """Total realized P&L as % of initial balance."""
        return (self._total_realized_pnl / self._config.initial_balance) * 100.0

    # ─── Core Actions ────────────────────────────────────────────────────────

    def can_trade(self) -> bool:
        """Check if new trades are allowed."""
        self._check_dd_breach()
        return self._status == RiskStatus.ACTIVE

    def check_trade_risk(self, risk_usd: float) -> tuple[bool, str]:
        """
        Check if a specific trade's risk is within limits.

        Args:
            risk_usd: Dollar risk of the proposed trade.

        Returns:
            (allowed, reason) tuple.
        """
        if not self.can_trade():
            return False, f"Trading halted: {self._status.value}"

        # Check per-trade risk limit
        max_per_trade = self._config.initial_balance * (self._config.max_risk_per_trade_pct / 100.0)
        if risk_usd > max_per_trade:
            return False, (
                f"Trade risk ${risk_usd:.2f} exceeds per-trade limit "
                f"${max_per_trade:.2f} ({self._config.max_risk_per_trade_pct}%)"
            )

        # Check if this trade would exceed daily DD if it went to full loss
        if risk_usd > self.daily_dd_remaining_usd:
            return False, (
                f"Trade risk ${risk_usd:.2f} exceeds remaining daily DD budget "
                f"${self.daily_dd_remaining_usd:.2f}"
            )

        return True, ""

    def add_realized_pnl(self, pnl: float) -> None:
        """
        Record a closed trade's P&L.

        Args:
            pnl: Realized profit/loss in USD (positive = profit).
        """
        self._balance += pnl
        self._total_realized_pnl += pnl
        self._total_trades += 1
        self._daily.daily_realized_pnl += pnl
        self._daily.trades_today += 1

        # Update peak balance
        if self._balance > self._peak_balance:
            self._peak_balance = self._balance

        # Check for DD breach after P&L update
        self._check_dd_breach()

        logger.debug(
            "Realized P&L: $%.2f | Balance: $%.2f | Daily P&L: $%.2f | DD used: %.2f%%",
            pnl, self._balance, self.daily_pnl, self.daily_dd_used_pct,
        )

    def update_unrealized_pnl(self, total_floating_pnl: float) -> None:
        """
        Update the total unrealized P&L across all open positions.

        Call this on every tick or candle with the SUM of all open position P&Ls.

        Args:
            total_floating_pnl: Sum of all open positions' floating P&L in USD.
        """
        self._unrealized_pnl = total_floating_pnl

        # Track lowest equity today
        current_equity = self.equity
        if current_equity < self._daily.daily_lowest_equity:
            self._daily.daily_lowest_equity = current_equity

        # Check for DD breach
        self._check_dd_breach()

    def check_daily_reset(self, current_time_ms: float | None = None) -> bool:
        """
        Check if it's time to reset the daily drawdown.

        Call this periodically (e.g. on every candle). If reset is due,
        performs the reset and returns True.

        Args:
            current_time_ms: Current time in epoch milliseconds.
                             If None, uses system time.

        Returns:
            True if a reset was performed.
        """
        now_ms = current_time_ms or (datetime.now(timezone.utc).timestamp() * 1000)
        now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)

        # Calculate reset time in the configured timezone
        offset = timedelta(hours=self._config.reset_utc_offset_hours)
        reset_tz = timezone(offset)
        now_local = now.astimezone(reset_tz)

        # Build today's reset datetime
        today_reset = now_local.replace(
            hour=self._config.reset_hour,
            minute=self._config.reset_minute,
            second=0,
            microsecond=0,
        )
        today_reset_ms = today_reset.timestamp() * 1000

        # Check if we've crossed the reset boundary since last reset
        if today_reset_ms > self._daily.reset_timestamp and now_ms >= today_reset_ms:
            self._perform_daily_reset(now_ms)
            return True

        return False

    def force_daily_reset(self) -> None:
        """Manually force a daily reset (for testing or manual intervention)."""
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        self._perform_daily_reset(now_ms)

    def reset_account(self, new_balance: float | None = None) -> None:
        """
        Full account reset (e.g. starting a new challenge).

        Args:
            new_balance: New initial balance. If None, uses config default.
        """
        balance = new_balance or self._config.initial_balance
        self._config = RiskGuardConfig(
            initial_balance=balance,
            daily_dd_pct=self._config.daily_dd_pct,
            max_dd_pct=self._config.max_dd_pct,
            reset_hour=self._config.reset_hour,
            reset_minute=self._config.reset_minute,
            reset_utc_offset_hours=self._config.reset_utc_offset_hours,
            max_risk_per_trade_pct=self._config.max_risk_per_trade_pct,
            max_open_risk_pct=self._config.max_open_risk_pct,
            flatten_before_weekend=self._config.flatten_before_weekend,
            flatten_before_daily_reset=self._config.flatten_before_daily_reset,
        )
        self._balance = balance
        self._unrealized_pnl = 0.0
        self._peak_balance = balance
        self._status = RiskStatus.ACTIVE
        self._max_dd_floor = balance * (1 - self._config.max_dd_pct / 100.0)
        self._daily = DailyState(
            start_of_day_equity=balance,
            daily_lowest_equity=balance,
        )
        self._total_realized_pnl = 0.0
        self._total_trades = 0
        self._daily_halts = 0

        logger.info("Account reset: balance=$%.2f", balance)

    def _seed_balance(self, real_balance: float) -> None:
        """
        Seed the running balance from a real broker account for DD tracking,
        WITHOUT overwriting config.initial_balance (which is the sizing base).

        Use this when the broker's real balance differs from the configured
        initial (e.g. demo account with $8k, but sizing is off a $10k config).
        DD limits are enforced against the real balance; per-trade risk limit
        uses config.initial_balance (the sizing base).
        """
        self._balance = real_balance
        self._unrealized_pnl = 0.0
        self._peak_balance = real_balance
        self._max_dd_floor = real_balance * (1 - self._config.max_dd_pct / 100.0)
        self._daily = DailyState(
            start_of_day_equity=real_balance,
            daily_lowest_equity=real_balance,
        )
        self._status = RiskStatus.ACTIVE
        self._total_realized_pnl = 0.0
        self._total_trades = 0
        self._daily_halts = 0

        logger.info(
            "_seed_balance: real=$%.2f, config.initial=$%.2f (sizing base unchanged)",
            real_balance, self._config.initial_balance,
        )

    # ─── Reporting ───────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Get a summary dict for logging/Telegram/display."""
        return {
            "status": self._status.value,
            "balance": round(self._balance, 2),
            "equity": round(self.equity, 2),
            "unrealized_pnl": round(self._unrealized_pnl, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_dd_used_pct": round(self.daily_dd_used_pct, 2),
            "daily_dd_remaining_usd": round(self.daily_dd_remaining_usd, 2),
            "max_dd_used_pct": round(self.max_dd_used_pct, 2),
            "max_dd_remaining_usd": round(self.max_dd_remaining_usd, 2),
            "total_pnl": round(self._total_realized_pnl, 2),
            "total_pnl_pct": round(self.total_pnl_pct, 2),
            "total_trades": self._total_trades,
            "trades_today": self._daily.trades_today,
            "daily_halts": self._daily_halts,
            "initial_balance": self._config.initial_balance,
        }

    # ─── Private Methods ─────────────────────────────────────────────────────

    def _check_dd_breach(self) -> None:
        """Check if either DD limit has been breached."""
        # Already blown — don't re-check
        if self._status == RiskStatus.MAX_DD_BREACH:
            return

        current_equity = self.equity

        # Check max DD first (more severe)
        if current_equity <= self._max_dd_floor:
            self._status = RiskStatus.MAX_DD_BREACH
            logger.critical(
                "MAX DRAWDOWN BREACH! Equity $%.2f <= floor $%.2f (%.1f%% from $%.2f)",
                current_equity, self._max_dd_floor,
                self.max_dd_used_pct, self._config.initial_balance,
            )
            return

        # Check daily DD
        if self._status == RiskStatus.DAILY_HALT:
            return  # Already halted today

        daily_dd_limit = self._daily.start_of_day_equity * (self._config.daily_dd_pct / 100.0)
        daily_loss = self._daily.start_of_day_equity - current_equity

        if daily_loss >= daily_dd_limit:
            self._status = RiskStatus.DAILY_HALT
            self._daily_halts += 1
            logger.warning(
                "DAILY DD HALT! Loss $%.2f >= limit $%.2f (%.1f%% of SOD equity $%.2f)",
                daily_loss, daily_dd_limit,
                self.daily_dd_used_pct, self._daily.start_of_day_equity,
            )

    def _perform_daily_reset(self, timestamp_ms: float) -> None:
        """Reset daily state for a new trading day."""
        # Start-of-day equity = higher of current balance or equity
        # (this is how most firms calculate it — prevents gaming)
        sod_equity = max(self._balance, self.equity)

        prev_status = self._status

        self._daily = DailyState(
            start_of_day_equity=sod_equity,
            daily_lowest_equity=sod_equity,
            reset_timestamp=timestamp_ms,
        )

        # Only un-halt if it was a daily halt (not max DD breach)
        if self._status == RiskStatus.DAILY_HALT:
            self._status = RiskStatus.ACTIVE
            logger.info(
                "Daily reset: HALT cleared. SOD equity=$%.2f", sod_equity
            )
        elif self._status == RiskStatus.ACTIVE:
            logger.info(
                "Daily reset: SOD equity=$%.2f, balance=$%.2f",
                sod_equity, self._balance,
            )
        # MAX_DD_BREACH is permanent — daily reset doesn't clear it
