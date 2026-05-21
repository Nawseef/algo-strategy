"""
Multi-Trader Manager — Runs isolated paper traders per strategy + confluence traders.

Architecture:
    - 1 isolated PaperTradingEngine per strategy (no limits, no cooldown)
    - 1 confluence trader for 2+ agreement
    - 1 confluence trader for 3+ agreement
    - All share the same tick feed for SL/TP execution
    - Each has its own balance and position tracking

This allows fair comparison of individual strategy performance
AND testing whether confluence produces better results.
"""

from __future__ import annotations

from app.broker.base import Tick
from app.core.events import EventBus
from app.core.models import Signal, SignalType
from app.paper_trader.confluence import ConfluenceResult, ConfluenceTracker
from app.paper_trader.engine import PaperTradingEngine
from app.utils.logger import get_logger

logger = get_logger(__name__)


class MultiTraderManager:
    """
    Manages multiple isolated paper traders + confluence traders.

    Each strategy gets its own paper trader with:
        - No signal cooldown
        - No max position limit (generous per-strategy limit)
        - No opposing-signal close (only SL/TP/square-off closes)
        - Allow multiple positions per instrument
        - Own starting balance

    Plus confluence traders that only trade on agreement.
    """

    def __init__(
        self,
        event_bus: EventBus,
        strategy_names: list[str],
        starting_balance: float = 100000.0,
        position_size_pct: float = 10.0,
    ) -> None:
        self._event_bus = event_bus
        self._strategy_names = strategy_names
        self._starting_balance = starting_balance
        self._position_size_pct = position_size_pct

        # Isolated traders: one per strategy
        self._isolated_traders: dict[str, PaperTradingEngine] = {}

        # Confluence traders
        self._confluence_2_trader: PaperTradingEngine | None = None
        self._confluence_3_trader: PaperTradingEngine | None = None
        self._confluence_tracker: ConfluenceTracker | None = None

        self._running = False

    def setup(self) -> None:
        """Create all paper traders."""
        # Create isolated trader for each strategy
        for name in self._strategy_names:
            trader = PaperTradingEngine(
                event_bus=self._event_bus,
                max_open_positions=0,  # unlimited
                allow_multiple_positions=True,
                starting_balance=self._starting_balance,
                position_size_pct=self._position_size_pct,
                slippage_pct=0.05,
                brokerage_pct=0.05,
                signal_cooldown_seconds=0,  # no cooldown
            )
            self._isolated_traders[name] = trader

        # Create confluence traders
        self._confluence_2_trader = PaperTradingEngine(
            event_bus=self._event_bus,
            max_open_positions=0,
            allow_multiple_positions=True,
            starting_balance=self._starting_balance,
            position_size_pct=self._position_size_pct,
            slippage_pct=0.05,
            brokerage_pct=0.05,
            signal_cooldown_seconds=0,
        )

        self._confluence_3_trader = PaperTradingEngine(
            event_bus=self._event_bus,
            max_open_positions=0,
            allow_multiple_positions=True,
            starting_balance=self._starting_balance,
            position_size_pct=self._position_size_pct,
            slippage_pct=0.05,
            brokerage_pct=0.05,
            signal_cooldown_seconds=0,
        )

        # Create confluence tracker
        self._confluence_tracker = ConfluenceTracker(self._event_bus)

        logger.info(
            "MultiTraderManager setup: %d isolated + 2 confluence traders",
            len(self._isolated_traders),
        )

    def start(self) -> None:
        """Start all traders and subscribe to events."""
        self._running = True

        # Subscribe to ticks for ALL traders (SL/TP execution)
        self._event_bus.subscribe("tick", self._on_tick)

        # Subscribe to signals — route to correct isolated trader
        self._event_bus.subscribe("signal", self._on_signal)

        # Subscribe to confluence events
        self._event_bus.subscribe("confluence_2", self._on_confluence_2)
        self._event_bus.subscribe("confluence_3", self._on_confluence_3)

        # Start square-off timers for all traders
        for trader in self._isolated_traders.values():
            trader._running = True
            trader._start_square_off_timer()

        self._confluence_2_trader._running = True
        self._confluence_2_trader._start_square_off_timer()
        self._confluence_3_trader._running = True
        self._confluence_3_trader._start_square_off_timer()

        logger.info("MultiTraderManager started")

    def stop(self) -> None:
        """Stop all traders."""
        self._running = False

        self._event_bus.unsubscribe("tick", self._on_tick)
        self._event_bus.unsubscribe("signal", self._on_signal)
        self._event_bus.unsubscribe("confluence_2", self._on_confluence_2)
        self._event_bus.unsubscribe("confluence_3", self._on_confluence_3)

        for trader in self._isolated_traders.values():
            trader._running = False
            if hasattr(trader, '_square_off_timer') and trader._square_off_timer:
                trader._square_off_timer.cancel()

        self._confluence_2_trader._running = False
        if hasattr(self._confluence_2_trader, '_square_off_timer') and self._confluence_2_trader._square_off_timer:
            self._confluence_2_trader._square_off_timer.cancel()

        self._confluence_3_trader._running = False
        if hasattr(self._confluence_3_trader, '_square_off_timer') and self._confluence_3_trader._square_off_timer:
            self._confluence_3_trader._square_off_timer.cancel()

        logger.info("MultiTraderManager stopped")

    def _on_tick(self, tick: Tick) -> None:
        """Forward tick to all traders for SL/TP checking."""
        for trader in self._isolated_traders.values():
            trader.on_tick(tick)
        self._confluence_2_trader.on_tick(tick)
        self._confluence_3_trader.on_tick(tick)

    def _on_signal(self, signal: Signal) -> None:
        """Route signal to the correct isolated trader + confluence tracker."""
        if not self._running:
            return

        # Route to isolated trader for this strategy
        trader = self._isolated_traders.get(signal.strategy_name)
        if trader:
            trader.on_signal(signal)

        # Also feed to confluence tracker
        self._confluence_tracker.on_signal(signal)

    def _on_confluence_2(self, result: ConfluenceResult) -> None:
        """Handle 2+ confluence signal."""
        if not self._running:
            return

        # Convert ConfluenceResult to Signal for the paper trader
        signal = Signal(
            signal_type=result.signal_type,
            exchange=result.exchange,
            segment=result.segment,
            exchange_token=result.exchange_token,
            price=result.price,
            timestamp_ms=result.timestamp_ms,
            strategy_name=f"Confluence_2+({','.join(result.strategies[:3])})",
            reason=f"{result.count} strategies agree: {', '.join(result.strategies)}",
            stop_loss=result.stop_loss,
            take_profit=result.take_profit,
            metadata={"confluence_count": result.count, "strategies": result.strategies},
        )
        self._confluence_2_trader.on_signal(signal)

        # Emit for telegram notifications
        self._event_bus.emit("confluence_signal", signal)

    def _on_confluence_3(self, result: ConfluenceResult) -> None:
        """Handle 3+ confluence signal."""
        if not self._running:
            return

        signal = Signal(
            signal_type=result.signal_type,
            exchange=result.exchange,
            segment=result.segment,
            exchange_token=result.exchange_token,
            price=result.price,
            timestamp_ms=result.timestamp_ms,
            strategy_name=f"Confluence_3+({','.join(result.strategies[:3])})",
            reason=f"{result.count} strategies agree: {', '.join(result.strategies)}",
            stop_loss=result.stop_loss,
            take_profit=result.take_profit,
            metadata={"confluence_count": result.count, "strategies": result.strategies},
        )
        self._confluence_3_trader.on_signal(signal)

        # Emit for telegram notifications
        self._event_bus.emit("confluence_signal", signal)

    # ─── Public Accessors ────────────────────────────────────────

    def get_trader(self, strategy_name: str) -> PaperTradingEngine | None:
        """Get the isolated trader for a specific strategy."""
        return self._isolated_traders.get(strategy_name)

    @property
    def confluence_2_trader(self) -> PaperTradingEngine:
        return self._confluence_2_trader

    @property
    def confluence_3_trader(self) -> PaperTradingEngine:
        return self._confluence_3_trader

    @property
    def all_traders(self) -> dict[str, PaperTradingEngine]:
        """All traders including confluence ones."""
        result = dict(self._isolated_traders)
        result["Confluence_2+"] = self._confluence_2_trader
        result["Confluence_3+"] = self._confluence_3_trader
        return result

    @property
    def total_pnl_by_strategy(self) -> dict[str, float]:
        """PnL breakdown by strategy."""
        return {name: trader.total_pnl for name, trader in self.all_traders.items()}

    def get_summary(self) -> str:
        """Generate a comprehensive multi-strategy summary."""
        lines = []
        lines.append("=" * 40)
        lines.append("STRATEGY COMPARISON")
        lines.append("=" * 40)
        lines.append("")
        lines.append(f"{'Strategy':<25} {'Trades':<7} {'Win%':<6} {'PnL':<12}")
        lines.append("-" * 55)

        for name, trader in self.all_traders.items():
            closed = trader.closed_positions
            total = len(closed)
            wins = len([p for p in closed if p.pnl > 0])
            win_pct = (wins / total * 100) if total > 0 else 0
            pnl = trader.total_pnl
            pnl_str = f"+Rs.{pnl:,.0f}" if pnl >= 0 else f"-Rs.{abs(pnl):,.0f}"

            # Highlight confluence and best performer
            marker = ""
            if "Confluence" in name:
                marker = " *"

            lines.append(f"{name:<25} {total:<7} {win_pct:<5.0f}% {pnl_str:<12}{marker}")

        lines.append("-" * 55)

        # Find best performer
        best_name = max(self.all_traders.keys(), key=lambda n: self.all_traders[n].total_pnl)
        best_pnl = self.all_traders[best_name].total_pnl
        lines.append(f"BEST: {best_name} ({'+' if best_pnl >= 0 else ''}Rs.{best_pnl:,.0f})")
        lines.append("")

        return "\n".join(lines)
