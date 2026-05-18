"""
Paper Trading Engine.

Simulates order execution and position management without placing real orders.
Listens for 'signal' events and manages the full trade lifecycle:
    Signal → Order → Position Open → Position Close → PnL

All trades are tracked internally. No broker orders are placed.
"""

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
    ) -> None:
        self._event_bus = event_bus
        self._default_quantity = default_quantity
        self._max_open_positions = max_open_positions
        self._allow_multiple = allow_multiple_positions

        # State
        self._orders: list[PaperOrder] = []
        self._positions: list[Position] = []
        self._latest_prices: dict[str, float] = {}  # token -> last price
        self._running = False

    def start(self) -> None:
        """Start the paper trading engine."""
        self._running = True
        self._event_bus.subscribe("signal", self.on_signal)
        self._event_bus.subscribe("tick", self.on_tick)
        logger.info(
            "PaperTradingEngine started (qty=%d, max_positions=%s)",
            self._default_quantity,
            self._max_open_positions or "unlimited",
        )

    def stop(self) -> None:
        """Stop the paper trading engine."""
        self._running = False
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
        Decides whether to open a new position or close an existing one.
        """
        if not self._running:
            return

        logger.info("Processing signal: %s", signal)

        # Check if we have an opposing open position to close
        existing = self._find_opposing_position(signal)
        if existing:
            self._close_position(existing, signal.price, signal.timestamp_ms)
            return

        # Check position limits
        if not self._can_open_position(signal):
            logger.warning(
                "Cannot open position: limit reached (%d open)",
                len(self.open_positions),
            )
            return

        # Open new position
        self._open_position(signal)

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

    def _open_position(self, signal: Signal) -> None:
        """Open a new paper position from a signal."""
        side = OrderSide.BUY if signal.signal_type == SignalType.BUY else OrderSide.SELL
        order_id = self._generate_id("ORD")
        position_id = self._generate_id("POS")

        # Create order
        order = PaperOrder(
            order_id=order_id,
            side=side,
            exchange=signal.exchange,
            segment=signal.segment,
            exchange_token=signal.exchange_token,
            quantity=self._default_quantity,
            price=signal.price,
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
            quantity=self._default_quantity,
            entry_price=signal.price,
            entry_time_ms=signal.timestamp_ms,
            strategy_name=signal.strategy_name,
        )
        self._positions.append(position)

        logger.info(
            "POSITION OPENED | %s %s qty=%d @%.2f | strategy=%s | reason=%s",
            side.value,
            signal.exchange_token,
            self._default_quantity,
            signal.price,
            signal.strategy_name,
            signal.reason,
        )

        # Emit events
        self._event_bus.emit("order", order)
        self._event_bus.emit("position_open", position)

    def _close_position(
        self, position: Position, exit_price: float, exit_time_ms: float
    ) -> None:
        """Close an existing position."""
        position.close(exit_price, exit_time_ms)

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
