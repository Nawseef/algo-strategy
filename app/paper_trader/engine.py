"""
Paper Trading Engine.

Simulates order execution and position management without placing real orders.
Listens for 'signal' events and manages the full trade lifecycle:
    Signal → Order → Position Open → Position Close → PnL

All trades are tracked internally. No broker orders are placed.
"""

import random
import threading
import uuid

from app.broker.base import Tick
from app.core.events import EventBus
from app.core.models import (
    OrderSide,
    PaperOrder,
    Position,
    Signal,
    SignalType,
)
from app.utils.logger import get_logger
from app.utils.market_hours import can_open_new_position, should_square_off

logger = get_logger(__name__)


class PaperTradingEngine:
    """
    Simulated trading engine for paper trading.

    Behavior:
        - On BUY signal: opens a long position (or closes an existing short).
        - On SELL signal: opens a short position (or closes an existing long).
        - Tracks all orders, positions, and PnL.
        - Emits events: 'order', 'position_open', 'position_close'.

    Configuration:
        - default_quantity: shares per trade (default 1).
        - max_open_positions: limit concurrent positions (0 = unlimited).
        - allow_multiple_positions: allow multiple positions per instrument.

    Usage:
        trader = PaperTradingEngine(event_bus, default_quantity=10)
        event_bus.subscribe("signal", trader.on_signal)
        event_bus.subscribe("tick", trader.on_tick)
        trader.start()
    """

    def __init__(
        self,
        event_bus: EventBus,
        default_quantity: int = 1,
        max_open_positions: int = 0,
        allow_multiple_positions: bool = False,
        starting_balance: float = 100000.0,
        position_size_pct: float = 10.0,
        slippage_pct: float = 0.05,
        brokerage_pct: float = 0.05,
        signal_cooldown_seconds: float = 120.0,
    ) -> None:
        self._event_bus = event_bus
        self._default_quantity = default_quantity
        self._max_open_positions = max_open_positions
        self._allow_multiple = allow_multiple_positions
        self._starting_balance = starting_balance
        self._position_size_pct = position_size_pct
        self._slippage_pct = slippage_pct  # ±0.05% random slippage
        self._brokerage_pct = brokerage_pct  # 0.05% per trade (entry + exit)
        self._signal_cooldown = signal_cooldown_seconds  # min seconds between signals per instrument

        # State
        self._orders: list[PaperOrder] = []
        self._positions: list[Position] = []
        self._last_signal_time: dict[str, float] = {}  # token -> last signal timestamp
        self._latest_prices: dict[str, float] = {}  # token -> last price
        self._running = False

    def start(self) -> None:
        """Start the paper trading engine."""
        self._running = True
        self._event_bus.subscribe("signal", self.on_signal)
        self._event_bus.subscribe("tick", self.on_tick)
        self._start_square_off_timer()
        logger.info(
            "PaperTradingEngine started (qty=%d, max_positions=%s)",
            self._default_quantity,
            self._max_open_positions or "unlimited",
        )

    def stop(self) -> None:
        """Stop the paper trading engine."""
        self._running = False
        if hasattr(self, '_square_off_timer') and self._square_off_timer:
            self._square_off_timer.cancel()
        self._event_bus.unsubscribe("signal", self.on_signal)
        self._event_bus.unsubscribe("tick", self.on_tick)
        logger.info("PaperTradingEngine stopped")
        self._log_summary()

    def on_tick(self, tick: Tick) -> None:
        """Track latest prices for position valuation."""
        self._latest_prices[tick.exchange_token] = tick.ltp

    def on_signal(self, signal: Signal) -> None:
        """
        Process a trading signal.
        Respects market hours, cooldown, and position limits.
        """
        if not self._running:
            return

        # Duplicate signal suppression (cooldown)
        import time as _time
        now = _time.time()
        last = self._last_signal_time.get(signal.exchange_token, 0)
        if now - last < self._signal_cooldown:
            logger.debug("Signal cooldown active for %s, skipping", signal.exchange_token)
            return
        self._last_signal_time[signal.exchange_token] = now

        logger.info("Processing signal: %s", signal)

        # Apply slippage to signal price
        execution_price = self._apply_slippage(signal.price, signal.signal_type)

        # Check if we have an opposing open position to close
        # (closing is always allowed, even near market close)
        existing = self._find_opposing_position(signal)
        if existing:
            self._close_position(existing, execution_price, signal.timestamp_ms)
            return

        # No new positions after 3:15 PM
        if not can_open_new_position():
            logger.info("Market closing soon — not opening new position")
            return

        # Check position limits
        if not self._can_open_position(signal):
            logger.warning(
                "Cannot open position: limit reached (%d open)",
                len(self.open_positions),
            )
            return

        # Open new position at slipped price
        self._open_position(signal, execution_price)

    def _find_opposing_position(self, signal: Signal) -> Position | None:
        """Find an open position that opposes this signal (to close it)."""
        for pos in self._positions:
            if not pos.is_open:
                continue
            if pos.exchange_token != signal.exchange_token:
                continue

            # BUY signal closes a SHORT position
            if signal.signal_type == SignalType.BUY and pos.side == OrderSide.SELL:
                return pos
            # SELL signal closes a LONG position
            if signal.signal_type == SignalType.SELL and pos.side == OrderSide.BUY:
                return pos

        return None

    def _can_open_position(self, signal: Signal) -> bool:
        """Check if we're allowed to open a new position."""
        # Check max positions limit
        if self._max_open_positions > 0:
            if len(self.open_positions) >= self._max_open_positions:
                return False

        # Check if we already have a same-direction position for this instrument
        if not self._allow_multiple:
            for pos in self._positions:
                if not pos.is_open:
                    continue
                if pos.exchange_token != signal.exchange_token:
                    continue
                # Same direction already open
                side = OrderSide.BUY if signal.signal_type == SignalType.BUY else OrderSide.SELL
                if pos.side == side:
                    logger.debug(
                        "Already have %s position for %s, skipping",
                        side.value,
                        signal.exchange_token,
                    )
                    return False

        return True

    def _open_position(self, signal: Signal, execution_price: float) -> None:
        """Open a new paper position with slippage and brokerage."""
        side = OrderSide.BUY if signal.signal_type == SignalType.BUY else OrderSide.SELL
        order_id = self._generate_id("ORD")
        position_id = self._generate_id("POS")

        # Calculate quantity based on position sizing
        quantity = self._calculate_quantity(execution_price)
        if quantity <= 0:
            logger.warning("Cannot open position: insufficient balance for %s @ %.2f", signal.exchange_token, execution_price)
            return

        # Create order
        order = PaperOrder(
            order_id=order_id,
            side=side,
            exchange=signal.exchange,
            segment=signal.segment,
            exchange_token=signal.exchange_token,
            quantity=quantity,
            price=execution_price,
            timestamp_ms=signal.timestamp_ms,
            strategy_name=signal.strategy_name,
            signal_reason=signal.reason,
        )
        self._orders.append(order)

        # Create position
        position = Position(
            position_id=position_id,
            exchange=signal.exchange,
            segment=signal.segment,
            exchange_token=signal.exchange_token,
            side=side,
            quantity=quantity,
            entry_price=execution_price,
            entry_time_ms=signal.timestamp_ms,
            strategy_name=signal.strategy_name,
        )
        self._positions.append(position)

        logger.info(
            "POSITION OPENED | %s %s qty=%d @%.2f | strategy=%s | reason=%s",
            side.value,
            signal.exchange_token,
            quantity,
            execution_price,
            signal.strategy_name,
            signal.reason,
        )

        # Emit events
        self._event_bus.emit("order", order)
        self._event_bus.emit("position_open", position)

    def _close_position(
        self, position: Position, exit_price: float, exit_time_ms: float
    ) -> None:
        """Close an existing position with brokerage deduction."""
        position.close(exit_price, exit_time_ms)

        # Deduct brokerage from PnL
        position.pnl = self._apply_brokerage(
            position.pnl, position.entry_price, exit_price, position.quantity
        )
        if position.entry_price > 0:
            position.pnl_pct = (position.pnl / (position.entry_price * position.quantity)) * 100

        logger.info(
            "POSITION CLOSED | %s %s qty=%d | entry=%.2f exit=%.2f | PnL=%.2f (%.2f%%)",
            position.side.value,
            position.exchange_token,
            position.quantity,
            position.entry_price,
            position.exit_price,
            position.pnl,
            position.pnl_pct,
        )

        # Emit event
        self._event_bus.emit("position_close", position)

    def _log_summary(self) -> None:
        """Log a summary of trading activity."""
        total_trades = len(self.closed_positions)
        if total_trades == 0:
            logger.info("No completed trades")
            return

        total_pnl = sum(p.pnl for p in self.closed_positions)
        winners = [p for p in self.closed_positions if p.pnl > 0]
        losers = [p for p in self.closed_positions if p.pnl < 0]
        win_rate = (len(winners) / total_trades) * 100 if total_trades > 0 else 0

        logger.info("=" * 50)
        logger.info("PAPER TRADING SUMMARY")
        logger.info("=" * 50)
        logger.info("Total trades: %d", total_trades)
        logger.info("Winners: %d | Losers: %d", len(winners), len(losers))
        logger.info("Win rate: %.1f%%", win_rate)
        logger.info("Total PnL: %.2f", total_pnl)
        logger.info("Open positions: %d", len(self.open_positions))
        logger.info("=" * 50)

    @staticmethod
    def _generate_id(prefix: str) -> str:
        """Generate a unique ID with prefix."""
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    # --- Public accessors ---

    @property
    def open_positions(self) -> list[Position]:
        """All currently open positions."""
        return [p for p in self._positions if p.is_open]

    @property
    def closed_positions(self) -> list[Position]:
        """All closed positions."""
        return [p for p in self._positions if not p.is_open]

    @property
    def all_positions(self) -> list[Position]:
        """All positions (open and closed)."""
        return list(self._positions)

    @property
    def all_orders(self) -> list[PaperOrder]:
        """All paper orders."""
        return list(self._orders)

    @property
    def total_pnl(self) -> float:
        """Total realized PnL from closed positions."""
        return sum(p.pnl for p in self.closed_positions)

    @property
    def unrealized_pnl(self) -> float:
        """Unrealized PnL from open positions based on latest prices."""
        pnl = 0.0
        for pos in self.open_positions:
            current_price = self._latest_prices.get(pos.exchange_token)
            if current_price is None:
                continue
            if pos.side == OrderSide.BUY:
                pnl += (current_price - pos.entry_price) * pos.quantity
            else:
                pnl += (pos.entry_price - current_price) * pos.quantity
        return pnl

    def get_position_for_instrument(self, exchange_token: str) -> Position | None:
        """Get the open position for an instrument, if any."""
        for pos in self.open_positions:
            if pos.exchange_token == exchange_token:
                return pos
        return None

    # ─── Slippage & Brokerage ────────────────────────────────────

    def _apply_slippage(self, price: float, signal_type: SignalType) -> float:
        """
        Apply random slippage to simulate real execution.
        BUY: price goes slightly UP (you pay more)
        SELL: price goes slightly DOWN (you receive less)
        """
        slippage = price * (self._slippage_pct / 100) * random.uniform(0, 1)
        if signal_type == SignalType.BUY:
            return price + slippage
        else:
            return price - slippage

    def _apply_brokerage(self, pnl: float, entry_price: float, exit_price: float, quantity: int) -> float:
        """
        Deduct brokerage charges from PnL.
        Charges apply on both entry and exit (total turnover).
        """
        turnover = (entry_price * quantity) + (exit_price * quantity)
        charges = turnover * (self._brokerage_pct / 100)
        return pnl - charges

    # ─── Position Sizing ────────────────────────────────────────

    def _calculate_quantity(self, price: float) -> int:
        """
        Calculate how many shares to buy based on position sizing rules.
        Allocates position_size_pct% of current balance per trade.
        
        Example: balance=100000, position_size_pct=10, price=1350
        → allocate Rs.10,000 → buy 7 shares
        """
        if price <= 0:
            return 0

        current_balance = self._starting_balance + self.total_pnl
        allocation = current_balance * (self._position_size_pct / 100)
        quantity = int(allocation / price)
        return max(quantity, 0)

    # ─── Auto Square-Off ─────────────────────────────────────────

    def _start_square_off_timer(self) -> None:
        """Check every 30 seconds if it's time to square off."""
        if not self._running:
            return
        self._square_off_timer = threading.Timer(30, self._check_square_off)
        self._square_off_timer.daemon = True
        self._square_off_timer.start()

    def _check_square_off(self) -> None:
        """Auto-close all open positions at 3:20 PM."""
        if not self._running:
            return

        if should_square_off() and self.open_positions:
            logger.info("SQUARE OFF: Auto-closing %d positions (market closing)", len(self.open_positions))
            import time as _time
            now_ms = _time.time() * 1000

            for pos in list(self.open_positions):
                current_price = self._latest_prices.get(pos.exchange_token)
                if current_price:
                    self._close_position(pos, current_price, now_ms)
                else:
                    # Use entry price if no current price (shouldn't happen)
                    self._close_position(pos, pos.entry_price, now_ms)

        self._start_square_off_timer()
