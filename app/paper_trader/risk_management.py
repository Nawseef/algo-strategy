"""
Advanced Risk Management Module.

Contains 4 exit enhancement tools that can be enabled per-strategy:
1. Trailing Stop-Loss — moves SL up as price moves in your favor
2. Breakeven Stop — moves SL to entry after 1x risk profit
3. Partial Profit Booking — closes 50% at 1:1 RR, lets rest run
4. Time-based Exit — closes if trade hasn't hit TP within X candles

These are NOT active by default. To enable, pass a RiskConfig to the
PaperTradingEngine or call the functions manually.

Usage (future):
    from app.paper_trader.risk_management import RiskManager, RiskConfig

    config = RiskConfig(
        trailing_stop=True,
        trailing_atr_multiplier=1.0,
        breakeven_after_1r=True,
        partial_profit_at_1r=True,
        partial_close_pct=50,
        time_exit_candles=20,
    )
    risk_mgr = RiskManager(config)
    risk_mgr.update(position, current_price, candles_elapsed)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.models import OrderSide, Position
from app.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RiskConfig:
    """
    Configuration for advanced risk management.
    All features are disabled by default.
    """

    # ─── Trailing Stop-Loss ──────────────────────────────────────
    # When enabled, SL moves up (for longs) or down (for shorts)
    # as price moves in your favor. Never moves against you.
    trailing_stop: bool = False
    # How much to trail behind the highest price (as ATR multiplier)
    # Example: 1.0 means SL trails 1x ATR behind the peak price
    trailing_atr_multiplier: float = 1.0
    # Alternative: trail as percentage of price (used if > 0)
    trailing_pct: float = 0.0

    # ─── Breakeven Stop ──────────────────────────────────────────
    # When enabled, moves SL to entry price once the trade is
    # in profit by 1x the original risk (entry - original_sl).
    # This eliminates losing trades that were once winning.
    breakeven_after_1r: bool = False
    # How much profit (in R multiples) before moving to breakeven
    # 1.0 = move to breakeven after 1R profit
    breakeven_trigger_r: float = 1.0
    # Small buffer above entry to cover slippage/brokerage
    breakeven_buffer_pct: float = 0.05  # 0.05% above entry

    # ─── Partial Profit Booking ──────────────────────────────────
    # When enabled, closes a portion of the position at 1:1 RR
    # and lets the remainder run to the full TP.
    partial_profit_at_1r: bool = False
    # Percentage of position to close at 1R (default 50%)
    partial_close_pct: float = 50.0
    # R-multiple at which to take partial profit
    partial_trigger_r: float = 1.0

    # ─── Time-based Exit ─────────────────────────────────────────
    # When enabled, closes the position if it hasn't hit TP
    # within X candles. Frees up capital from dead trades.
    time_exit: bool = False
    # Number of 5-min candles before force-closing (default 20 = 100 min)
    time_exit_candles: int = 20


class RiskManager:
    """
    Advanced risk management engine.

    Call `update()` on every tick or candle to apply active risk rules.
    Modifies the position's stop_loss in-place and returns exit signals.

    This class does NOT execute trades — it only computes new SL levels
    and signals when to close. The PaperTradingEngine handles execution.
    """

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        # Track per-position state
        self._highest_price: dict[str, float] = {}  # position_id -> highest favorable price
        self._lowest_price: dict[str, float] = {}   # position_id -> lowest favorable price
        self._partial_taken: dict[str, bool] = {}   # position_id -> partial profit taken?
        self._breakeven_set: dict[str, bool] = {}   # position_id -> breakeven activated?
        self._candles_elapsed: dict[str, int] = {}  # position_id -> candles since entry
        self._original_sl: dict[str, float] = {}    # position_id -> original stop-loss at entry
        self._original_risk: dict[str, float] = {}  # position_id -> original risk (entry - sl)

    def update_on_tick(self, position: Position, current_price: float) -> PositionAction:
        """
        Update risk management on every tick.
        Returns an action: HOLD, CLOSE, or PARTIAL_CLOSE.

        Args:
            position: The open position to manage.
            current_price: Current market price (tick LTP).

        Returns:
            PositionAction indicating what to do.
        """
        if not position.is_open:
            return PositionAction.HOLD

        pos_id = position.position_id
        entry = position.entry_price
        original_sl = position.stop_loss

        # Store original SL and risk on first encounter
        if pos_id not in self._original_sl:
            self._original_sl[pos_id] = position.stop_loss
            if position.side == OrderSide.BUY:
                self._original_risk[pos_id] = entry - position.stop_loss if position.stop_loss > 0 else entry * 0.01
            else:
                self._original_risk[pos_id] = position.stop_loss - entry if position.stop_loss > 0 else entry * 0.01

        original_risk = self._original_risk[pos_id]

        # Track highest/lowest price for trailing
        if position.side == OrderSide.BUY:
            prev_high = self._highest_price.get(pos_id, entry)
            self._highest_price[pos_id] = max(prev_high, current_price)
        else:
            prev_low = self._lowest_price.get(pos_id, entry)
            self._lowest_price[pos_id] = min(prev_low, current_price)

        # ─── 1. Breakeven Stop ───────────────────────────────────
        if self._config.breakeven_after_1r and original_risk > 0:
            if not self._breakeven_set.get(pos_id, False):
                profit = self._current_profit(position, current_price)
                trigger = original_risk * self._config.breakeven_trigger_r

                if profit >= trigger:
                    # Move SL to entry + small buffer
                    buffer = entry * (self._config.breakeven_buffer_pct / 100)
                    if position.side == OrderSide.BUY:
                        new_sl = entry + buffer
                        if new_sl > position.stop_loss:
                            position.stop_loss = new_sl
                            self._breakeven_set[pos_id] = True
                            logger.debug(
                                "Breakeven activated for %s: SL moved to %.2f",
                                pos_id, new_sl,
                            )
                    else:
                        new_sl = entry - buffer
                        if new_sl < position.stop_loss:
                            position.stop_loss = new_sl
                            self._breakeven_set[pos_id] = True
                            logger.debug(
                                "Breakeven activated for %s: SL moved to %.2f",
                                pos_id, new_sl,
                            )

        # ─── 2. Trailing Stop-Loss ───────────────────────────────
        if self._config.trailing_stop:
            new_sl = self._calculate_trailing_sl(position, current_price)
            if new_sl is not None:
                # Only move SL in favorable direction (never widen the stop)
                if position.side == OrderSide.BUY and new_sl > position.stop_loss:
                    position.stop_loss = new_sl
                    logger.debug(
                        "Trailing SL updated for %s: %.2f", pos_id, new_sl
                    )
                elif position.side == OrderSide.SELL and new_sl < position.stop_loss:
                    position.stop_loss = new_sl
                    logger.debug(
                        "Trailing SL updated for %s: %.2f", pos_id, new_sl
                    )

        # ─── 3. Partial Profit Booking ───────────────────────────
        if self._config.partial_profit_at_1r and original_risk > 0:
            if not self._partial_taken.get(pos_id, False):
                profit = self._current_profit(position, current_price)
                trigger = original_risk * self._config.partial_trigger_r

                if profit >= trigger:
                    self._partial_taken[pos_id] = True
                    logger.debug(
                        "Partial profit trigger for %s at price %.2f",
                        pos_id, current_price,
                    )
                    return PositionAction.PARTIAL_CLOSE

        return PositionAction.HOLD

    def update_on_candle(self, position: Position) -> PositionAction:
        """
        Update risk management on every candle close.
        Used for time-based exit counting.

        Args:
            position: The open position to manage.

        Returns:
            PositionAction indicating what to do.
        """
        if not position.is_open:
            return PositionAction.HOLD

        pos_id = position.position_id

        # ─── 4. Time-based Exit ──────────────────────────────────
        if self._config.time_exit:
            elapsed = self._candles_elapsed.get(pos_id, 0) + 1
            self._candles_elapsed[pos_id] = elapsed

            if elapsed >= self._config.time_exit_candles:
                logger.debug(
                    "Time exit for %s: %d candles elapsed (limit: %d)",
                    pos_id, elapsed, self._config.time_exit_candles,
                )
                return PositionAction.CLOSE

        return PositionAction.HOLD

    def cleanup(self, position_id: str) -> None:
        """Remove tracking state for a closed position."""
        self._highest_price.pop(position_id, None)
        self._lowest_price.pop(position_id, None)
        self._partial_taken.pop(position_id, None)
        self._breakeven_set.pop(position_id, None)
        self._candles_elapsed.pop(position_id, None)
        self._original_sl.pop(position_id, None)
        self._original_risk.pop(position_id, None)

    def reset_daily(self) -> None:
        """Clear all state at start of new day."""
        self._highest_price.clear()
        self._lowest_price.clear()
        self._partial_taken.clear()
        self._breakeven_set.clear()
        self._candles_elapsed.clear()
        self._original_sl.clear()
        self._original_risk.clear()

    # ─── Private Helpers ─────────────────────────────────────────

    def _current_profit(self, position: Position, current_price: float) -> float:
        """Calculate current unrealized profit in price units."""
        if position.side == OrderSide.BUY:
            return current_price - position.entry_price
        else:
            return position.entry_price - current_price

    def _calculate_trailing_sl(
        self, position: Position, current_price: float
    ) -> float | None:
        """Calculate the new trailing stop-loss level."""
        pos_id = position.position_id
        original_risk = self._original_risk.get(pos_id, 0)

        if self._config.trailing_pct > 0:
            # Percentage-based trailing
            if position.side == OrderSide.BUY:
                peak = self._highest_price.get(pos_id, current_price)
                return peak * (1 - self._config.trailing_pct / 100)
            else:
                trough = self._lowest_price.get(pos_id, current_price)
                return trough * (1 + self._config.trailing_pct / 100)

        elif self._config.trailing_atr_multiplier > 0 and original_risk > 0:
            # Trail using original risk as the distance
            trail_distance = original_risk * self._config.trailing_atr_multiplier

            if position.side == OrderSide.BUY:
                peak = self._highest_price.get(pos_id, current_price)
                return peak - trail_distance
            else:
                trough = self._lowest_price.get(pos_id, current_price)
                return trough + trail_distance

        return None


class PositionAction:
    """Actions the risk manager can recommend."""

    HOLD = "HOLD"                    # Do nothing
    CLOSE = "CLOSE"                  # Close entire position
    PARTIAL_CLOSE = "PARTIAL_CLOSE"  # Close partial_close_pct% of position
