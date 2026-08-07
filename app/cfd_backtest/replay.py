"""
CFD backtest replay — run strategies over stored 5m candles.

For each instrument, loads ``cfd_historical_candles`` for the date range and
walks the candle series chronologically. On each candle close it calls every
applicable strategy's ``evaluate``; the resulting signals are entered (at close
for CANDLE_CLOSE, or at the trigger price for INTRABAR once a later bar touches
it), and each trade's SL/TP is resolved by the exit simulator over the following
bars.

Because we have the full future at backtest time, each trade is resolved
immediately on entry (no tick loop needed) — the exit simulator walks forward
until SL/TP hits. This is accurate for the SL/TP-only exit design and much
faster than a tick replay.

Entry/exit conventions (kept consistent and conservative):
  * Exit simulation starts on the bar AFTER the entry bar. The entry bar's
    post-fill movement is not used to resolve exits (avoids attributing a bar's
    pre-entry extreme to the trade).
  * INTRABAR arms are valid for ``signal.expiry_candles`` bars after the signal;
    the fill price is the trigger price (no favourable slippage).
  * One open position per (strategy, variant, instrument) at a time: new signals
    for a key are ignored until the current trade for that key has closed.

Position sizing uses a fixed starting balance and per-trade risk% by default
(no compounding), so per-trade dollar risk is constant and RR statistics are
clean. Set ``compound=True`` to size off the running balance instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.cfd_backtest.exit_simulator import SimulatedTrade, simulate_exit
from app.cfd_research.regime import classify_regime, classify_volatility
from app.cfd_risk.costs import COST_MODEL_INTRADAY, CFDCostModel
from app.cfd_risk.instruments import get_instrument
from app.cfd_risk.position_sizing import calculate_lot_size
from app.cfd_strategy.base import CFDSignal, CFDStrategy, Direction, EntryMode, StrategyContext
from app.core.models import Candle, Timeframe
from app.db.research_store import ResearchStore
from app.utils import forex_hours
from app.utils.logger import get_logger

logger = get_logger("cfd_backtest.replay")


@dataclass
class BacktestConfig:
    """Parameters for a backtest run."""

    starting_balance: float = 100_000.0
    risk_pct: float = 1.0
    compound: bool = False
    cost_model: CFDCostModel = field(default_factory=lambda: COST_MODEL_INTRADAY)
    # Max bars to look forward when resolving an exit (safety cap).
    max_hold_bars: int = 2000
    # Persist each trade to cfd_paper_trades (mode='BACKTEST')?
    persist: bool = False
    # Research default: resolve EVERY signal independently (no stacking guard),
    # so scoring sees each entry's true outcome. Concurrency/stacking limits are
    # applied later at the scoring layer (portfolio_sim), NOT here. Set False to
    # restore "one open position per (strategy, variant, direction) at a time".
    # (Requires edge-triggered strategies — fire on the event, not every bar —
    #  and is intended with compound=False so sizing stays constant.)
    record_all_signals: bool = True


@dataclass
class BacktestResult:
    """Aggregate outcome of a backtest run."""

    trades: list[SimulatedTrade] = field(default_factory=list)
    starting_balance: float = 0.0
    ending_balance: float = 0.0
    candles_processed: int = 0
    instruments: list[str] = field(default_factory=list)

    # Computed stats.
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_profit_usd: float = 0.0
    gross_loss_usd: float = 0.0
    net_pnl_usd: float = 0.0
    profit_factor: float = 0.0
    avg_rr: float = 0.0
    expectancy_usd: float = 0.0
    max_drawdown_usd: float = 0.0

    def summary_text(self) -> str:
        return (
            f"Trades={self.total_trades} WR={self.win_rate:.1f}% "
            f"netPnL=${self.net_pnl_usd:,.2f} PF={self.profit_factor:.2f} "
            f"avgRR={self.avg_rr:.2f} exp=${self.expectancy_usd:.2f} "
            f"maxDD=${self.max_drawdown_usd:,.2f} "
            f"bal ${self.starting_balance:,.0f}->${self.ending_balance:,.0f}"
        )


@dataclass
class _OpenTrade:
    """Bookkeeping for a key that is currently in a trade (busy until exit)."""

    exit_time_ms: float


class CFDBacktestReplay:
    """Replays strategies over stored CFD 5m candles."""

    def __init__(
        self,
        strategies: list[CFDStrategy],
        store: ResearchStore,
        config: BacktestConfig | None = None,
    ) -> None:
        self._strategies = strategies
        self._store = store
        self._config = config or BacktestConfig()

    # ─── Public entry point ──────────────────────────────────────

    def run(
        self,
        instruments: list[str],
        start_date: date,
        end_date: date,
        timeframe: Timeframe = Timeframe.M5,
    ) -> BacktestResult:
        t0 = time.time()
        cfg = self._config
        balance = cfg.starting_balance
        all_trades: list[SimulatedTrade] = []
        candles_processed = 0

        logger.info(
            "CFD backtest: %s..%s | %d instruments | %d strategies | risk=%.2f%% compound=%s",
            start_date, end_date, len(instruments), len(self._strategies),
            cfg.risk_pct, cfg.compound,
        )

        start_ms = _date_to_ms(start_date)
        end_ms = _date_to_ms(end_date + timedelta(days=1))  # inclusive end

        for instrument in instruments:
            candles = self._load_candles(instrument, timeframe, start_ms, end_ms)
            if len(candles) < 10:
                logger.warning("  %s: only %d candles — skipping", instrument, len(candles))
                continue
            candles_processed += len(candles)

            strategies = [s for s in self._strategies if s.applies_to(instrument)
                          and s.timeframe == timeframe]
            if not strategies:
                continue

            trades, balance = self._replay_instrument(
                instrument, candles, strategies, balance,
            )
            all_trades.extend(trades)
            logger.info("  %s: %d candles -> %d trades", instrument, len(candles), len(trades))

        result = self._build_result(all_trades, cfg.starting_balance, balance, instruments, candles_processed)
        logger.info("CFD backtest complete in %.1fs | %s", time.time() - t0, result.summary_text())
        return result

    # ─── Per-instrument replay ───────────────────────────────────

    def _replay_instrument(
        self,
        instrument: str,
        candles: list[Candle],
        strategies: list[CFDStrategy],
        balance: float,
    ) -> tuple[list[SimulatedTrade], float]:
        cfg = self._config
        trades: list[SimulatedTrade] = []

        # Busy-until tracking per (strategy_id, variant_id, direction) key so a
        # strategy doesn't stack overlapping trades on the same instrument/side.
        busy_until: dict[tuple[str, str, str], float] = {}

        n = len(candles)
        for i in range(n):
            candle = candles[i]
            # History includes the just-closed candle (index 0..i).
            history = candles[: i + 1]

            for strat in strategies:
                if len(history) < strat.min_history:
                    continue
                ctx = StrategyContext(
                    instrument=instrument,
                    timeframe=strat.timeframe,
                    candle=candle,
                    history=history,
                )
                try:
                    signals = strat.evaluate(ctx)
                except Exception as e:  # noqa: BLE001 - a bad strategy must not kill the run
                    logger.error("strategy %s.evaluate error on %s: %s",
                                 strat.strategy_id, instrument, e)
                    continue

                for sig in signals:
                    trade = self._handle_signal(
                        sig, candles, i, balance, busy_until,
                    )
                    if trade is not None:
                        self._tag_trade(trade, sig, history, strat)
                        trades.append(trade)
                        if cfg.compound:
                            balance += trade.net_pnl_usd
                        if cfg.persist:
                            self._persist(trade, sig)

        return trades, balance

    def _handle_signal(
        self,
        sig: CFDSignal,
        candles: list[Candle],
        signal_idx: int,
        balance: float,
        busy_until: dict[tuple[str, str, str], float],
    ) -> SimulatedTrade | None:
        """Enter (and immediately resolve) a trade for a signal, or skip it."""
        cfg = self._config
        key = (sig.strategy_id, sig.variant_id, sig.direction.value)

        # Stacking guard (research default OFF via record_all_signals). When off,
        # every signal is resolved independently so the true outcome of each
        # entry is recorded; when on, a key holds one position at a time.
        if not cfg.record_all_signals and sig.timestamp_ms < busy_until.get(key, 0.0):
            return None

        fill_idx, fill_price, fill_time = self._resolve_fill(sig, candles, signal_idx)
        if fill_idx is None:
            return None  # arm expired unfilled (INTRABAR) — no trade

        # Exit simulation starts on the bar AFTER the fill bar.
        future = candles[fill_idx + 1: fill_idx + 1 + cfg.max_hold_bars]
        if not future:
            return None  # no bars left to manage the trade

        # Position sizing.
        sizing = calculate_lot_size(
            symbol=sig.instrument,
            account_balance=balance,
            risk_pct=cfg.risk_pct,
            sl_distance_price=abs(fill_price - sig.stop_loss),
        )
        if sizing.rejected:
            return None
        lots = sizing.lot_size

        trade = simulate_exit(
            instrument=sig.instrument,
            direction=sig.direction,
            entry_price=fill_price,
            entry_time_ms=fill_time,
            lots=lots,
            exit_plan=sig.exit_plan,
            future_candles=future,
            cost_model=cfg.cost_model,
        )

        # Mark the key busy until the trade closes (only when the guard is on).
        if not cfg.record_all_signals:
            busy_until[key] = trade.exit_time_ms
        return trade

    def _resolve_fill(
        self, sig: CFDSignal, candles: list[Candle], signal_idx: int,
    ) -> tuple[int | None, float, float]:
        """
        Determine the fill bar index, price, and time for a signal.

        CANDLE_CLOSE: fills at the signal bar's close (index == signal_idx).
        INTRABAR: scans up to expiry_candles following bars for a trigger touch;
                  fills at the trigger price on the first bar that touches it.
        """
        if sig.entry_mode is EntryMode.CANDLE_CLOSE:
            c = candles[signal_idx]
            return signal_idx, sig.entry_price, c.timestamp_ms

        # INTRABAR: look forward up to expiry_candles bars.
        trigger = sig.entry_price
        last = min(signal_idx + sig.expiry_candles, len(candles) - 1)
        for j in range(signal_idx + 1, last + 1):
            c = candles[j]
            if sig.direction is Direction.LONG:
                touched = c.high >= trigger
            else:
                touched = c.low <= trigger
            if touched:
                return j, trigger, c.timestamp_ms
        return None, 0.0, 0.0

    # ─── Research tagging ────────────────────────────────────────

    def _tag_trade(self, trade: SimulatedTrade, sig: CFDSignal,
                   history: list[Candle], strat: CFDStrategy) -> None:
        """Stamp the trade with session / regime / volatility / exit-model / TF.

        These tags are what the slice scorer groups by to answer "which session /
        which market condition / which exit model actually passes." ``history`` is
        the candle series up to and including the signal bar (what the strategy
        saw), so regime/volatility reflect the entry context.
        """
        entry_dt = datetime.fromtimestamp(trade.entry_time_ms / 1000, timezone.utc)
        trade.session = forex_hours.session_tag(entry_dt)
        trade.regime = classify_regime(history)
        trade.volatility = classify_volatility(history)
        trade.exit_model = getattr(sig.exit_plan, "exit_model", "") or "default"
        trade.timeframe = strat.timeframe.value

    # ─── Persistence ─────────────────────────────────────────────

    def _persist(self, trade: SimulatedTrade, sig: CFDSignal) -> None:
        open_dt = datetime.fromtimestamp(trade.entry_time_ms / 1000, timezone.utc)
        try:
            self._store.write_cfd_paper_trade({
                "position_id": f"BT-{sig.strategy_id}-{sig.variant_id}-{int(trade.entry_time_ms)}",
                "account_id": "backtest",
                "mode": "BACKTEST",
                "strategy_id": sig.strategy_id,
                "variant_id": sig.variant_id,
                "instrument": trade.instrument,
                "direction": trade.direction.value,
                "entry_mode": sig.entry_mode.value,
                "entry_price": trade.entry_price,
                "entry_time_ms": int(trade.entry_time_ms),
                "exit_price": trade.exit_price,
                "exit_time_ms": int(trade.exit_time_ms),
                "stop_loss": sig.exit_plan.stop_loss,
                "take_profits": ",".join(f"{p:.5g}" for p in sig.exit_plan.take_profit_prices),
                "planned_rr": round(trade.planned_rr, 4),
                "lots": trade.lots,
                "exit_reason": trade.exit_reason.value,
                "realized_rr": round(trade.realized_rr, 4),
                "pnl_price": round(trade.pnl_price, 6),
                "pnl_usd": round(trade.pnl_usd, 2),
                "cost_usd": round(trade.cost_usd, 2),
                "net_pnl_usd": round(trade.net_pnl_usd, 2),
                "mfe_price": round(trade.mfe_price, 6),
                "mae_price": round(trade.mae_price, 6),
                "session": forex_hours.session_tag(open_dt),
                "session_date": str(forex_hours.trading_day(open_dt)),
                "reason": sig.reason,
            })
        except Exception as e:  # noqa: BLE001
            logger.error("backtest persist failed: %s", e)

    # ─── Loading + stats ─────────────────────────────────────────

    def _load_candles(
        self, instrument: str, timeframe: Timeframe, start_ms: float, end_ms: float,
    ) -> list[Candle]:
        rows = self._store.get_cfd_historical_candles(
            instrument, timeframe.value, start_ms, end_ms,
        )
        out: list[Candle] = []
        for r in rows:
            out.append(Candle(
                exchange="ICMARKETS", segment="CFD", exchange_token=instrument,
                timeframe=timeframe, timestamp_ms=r["timestamp_ms"],
                open=r["open"], high=r["high"], low=r["low"], close=r["close"],
                volume=r.get("volume", 0),
            ))
        return out

    def _build_result(
        self, trades: list[SimulatedTrade], starting_balance: float,
        ending_balance: float, instruments: list[str], candles_processed: int,
    ) -> BacktestResult:
        res = BacktestResult(
            trades=trades,
            starting_balance=starting_balance,
            ending_balance=ending_balance,
            candles_processed=candles_processed,
            instruments=instruments,
        )
        res.total_trades = len(trades)
        if not trades:
            return res

        wins = [t for t in trades if t.net_pnl_usd > 0]
        losses = [t for t in trades if t.net_pnl_usd <= 0]
        res.wins = len(wins)
        res.losses = len(losses)
        res.win_rate = len(wins) / len(trades) * 100.0
        res.gross_profit_usd = sum(t.net_pnl_usd for t in wins)
        res.gross_loss_usd = sum(t.net_pnl_usd for t in losses)  # negative
        res.net_pnl_usd = sum(t.net_pnl_usd for t in trades)
        res.avg_rr = sum(t.realized_rr for t in trades) / len(trades)
        res.expectancy_usd = res.net_pnl_usd / len(trades)
        if res.gross_loss_usd != 0:
            res.profit_factor = res.gross_profit_usd / abs(res.gross_loss_usd)
        else:
            res.profit_factor = float("inf") if res.gross_profit_usd > 0 else 0.0

        # Max drawdown on the equity curve (chronological by exit time).
        ordered = sorted(trades, key=lambda t: t.exit_time_ms)
        equity = starting_balance
        peak = starting_balance
        max_dd = 0.0
        for t in ordered:
            equity += t.net_pnl_usd
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        res.max_drawdown_usd = max_dd
        return res


def _date_to_ms(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000
