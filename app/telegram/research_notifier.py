"""
Telegram notifications for the 150K Variant Research Engine.

Reports:
1. Daily Summary — trades recorded, top triggering strategies, system health
2. Exit Engine Report — best/worst exit models, MFE/MAE stats
3. Weekly Scoring Report — top 10 promoted candidates with full detail
4. Promoted Variant Alert — if a top candidate triggers live, instant notify
5. Health Monitoring — armed state size, eval timing, memory

Uses the same Telegram Bot API as the paper trading notifier.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

from app.utils.logger import get_logger

logger = get_logger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class ResearchNotifier:
    """
    Telegram integration for the research engine.

    Sends formatted reports at key moments:
    - After market close: daily trade summary
    - After exit engine runs: exit analysis report
    - After scoring runs: top candidates report
    - Real-time: promoted variant triggers
    """

    def __init__(self, bot_token: str, chat_ids: list[str]) -> None:
        self._bot_token = bot_token
        self._chat_ids = [cid for cid in chat_ids if cid]
        self._enabled = bool(bot_token and self._chat_ids)

        if not self._enabled:
            logger.warning("ResearchNotifier disabled (missing bot_token or chat_id)")

    # ─── 1. Daily Trade Summary ──────────────────────────────────────────────

    def send_daily_summary(
        self,
        date_str: str,
        total_trades: int,
        trades_by_strategy: dict[str, int],
        trades_by_instrument: dict[str, int],
        armed_state_stats: dict[str, int],
        eval_timing_ms: float = 0.0,
        candles_cached: int = 0,
    ) -> None:
        """Send end-of-day trade recording summary."""
        now = datetime.now()

        msg = (
            f"{'═'*30}\n"
            f"📊 RESEARCH ENGINE — DAILY SUMMARY\n"
            f"{date_str}\n"
            f"{'═'*30}\n\n"
            f"Trades Recorded: {total_trades}\n"
        )

        if trades_by_strategy:
            msg += "\n── By Strategy ──\n"
            for strategy, count in sorted(trades_by_strategy.items(), key=lambda x: -x[1]):
                msg += f"  {strategy}: {count}\n"

        if trades_by_instrument:
            msg += "\n── By Instrument ──\n"
            for instrument, count in sorted(trades_by_instrument.items(), key=lambda x: -x[1]):
                msg += f"  {instrument}: {count}\n"

        msg += "\n── System Health ──\n"
        msg += f"  Candles cached: {candles_cached}\n"
        if eval_timing_ms > 0:
            msg += f"  Avg eval time: {eval_timing_ms:.1f}ms\n"
        if armed_state_stats:
            msg += f"  Peak armed: {armed_state_stats.get('total_armed', 0)}\n"
            msg += f"  Triggered today: {armed_state_stats.get('triggered_today', 0)}\n"

        msg += f"\n⏱️ Report time: {now.strftime('%I:%M %p')}"

        self._send(msg)

    # ─── 2. Exit Engine Report ───────────────────────────────────────────────

    def send_exit_report(
        self,
        date_str: str,
        trades_processed: int,
        trades_skipped: int,
        processing_time_s: float,
        best_exit_summary: dict[str, float] | None = None,
        avg_mfe: float = 0.0,
        avg_mae: float = 0.0,
    ) -> None:
        """Send post-market exit simulation report."""
        msg = (
            f"{'═'*30}\n"
            f"🔬 EXIT SIMULATION COMPLETE\n"
            f"{date_str}\n"
            f"{'═'*30}\n\n"
            f"Trades processed: {trades_processed}\n"
            f"Trades skipped: {trades_skipped}\n"
            f"Processing time: {processing_time_s:.1f}s\n"
        )

        if trades_processed > 0:
            msg += f"Avg per trade: {(processing_time_s/trades_processed)*1000:.1f}ms\n"

        if avg_mfe != 0 or avg_mae != 0:
            msg += (
                f"\n── Excursion Analysis ──\n"
                f"  Avg MFE: {avg_mfe:+.1f} pts\n"
                f"  Avg MAE: {avg_mae:+.1f} pts\n"
                f"  Edge ratio: {avg_mfe/abs(avg_mae):.2f}\n" if avg_mae != 0 else ""
            )

        if best_exit_summary:
            msg += "\n── Top Exit Models (by avg PnL) ──\n"
            sorted_exits = sorted(best_exit_summary.items(), key=lambda x: -x[1])[:5]
            for model, avg_pnl in sorted_exits:
                msg += f"  {model}: {avg_pnl:+.1f} pts\n"

        self._send(msg)

    # ─── 3. Weekly Scoring Report ────────────────────────────────────────────

    def send_scoring_report(
        self,
        period_label: str,
        ranked_variants: list,
        total_variants_scored: int = 0,
        total_passed_filters: int = 0,
    ) -> None:
        """
        Send top candidate variants from scoring run.

        Args:
            ranked_variants: List of RankedVariant objects (from ranker).
        """
        msg = (
            f"{'═'*30}\n"
            f"🏆 VARIANT SCORING REPORT\n"
            f"Period: {period_label}\n"
            f"{'═'*30}\n\n"
            f"Variants analyzed: {total_variants_scored}\n"
            f"Passed all filters: {total_passed_filters}\n"
            f"Top candidates: {len(ranked_variants)}\n"
        )

        if not ranked_variants:
            msg += "\n⚠️ No variants met minimum criteria."
            self._send(msg)
            return

        msg += "\n── TOP CANDIDATES ──\n"

        for rv in ranked_variants[:10]:  # Top 10
            msg += (
                f"\n#{rv.rank} {rv.variant_id}\n"
                f"  {rv.strategy} | {rv.timeframe} | Exit: {rv.best_exit_model}\n"
                f"  Score: {rv.composite_score:.1f} | "
                f"WR: {rv.win_rate*100:.0f}% | "
                f"E: {rv.expectancy:.1f} | "
                f"PF: {rv.profit_factor:.2f}\n"
                f"  DD: {rv.max_drawdown:.0f} | "
                f"Sharpe: {rv.sharpe_ratio:.2f} | "
                f"Stab: {rv.stability_score:.0f}/100\n"
            )

            # Regime guidance: TURN ON / TURN OFF
            if hasattr(rv, 'regime_details') and rv.regime_details:
                on_conditions = []
                off_conditions = []
                for dim, values in rv.regime_details.items():
                    for val, exp in sorted(values.items(), key=lambda x: -x[1]):
                        label = f"{dim}={val}"
                        if exp > 5:
                            on_conditions.append((label, exp))
                        elif exp < -5:
                            off_conditions.append((label, exp))

                if on_conditions:
                    top_on = on_conditions[:3]
                    msg += "  ✅ ON: " + ", ".join(f"{c[0]}(+{c[1]:.0f})" for c in top_on) + "\n"
                if off_conditions:
                    top_off = off_conditions[:3]
                    msg += "  ❌ OFF: " + ", ".join(f"{c[0]}({c[1]:.0f})" for c in top_off) + "\n"
            else:
                if rv.best_regime:
                    msg += f"  ✅ Best in: {rv.best_regime}\n"
                if rv.worst_regime:
                    msg += f"  ❌ Avoid: {rv.worst_regime}\n"

        self._send(msg)

    # ─── 4. Promoted Variant Alert ───────────────────────────────────────────

    def send_variant_trigger_alert(
        self,
        variant_id: str,
        strategy: str,
        timeframe: str,
        instrument: str,
        direction: str,
        entry_price: float,
        best_exit_model: str,
        expectancy: float,
        regime_match: str = "",
    ) -> None:
        """
        Alert when a promoted (top-scored) variant triggers a trade.
        This is the "hot alert" — a variant the scoring engine identified
        as a candidate just fired in live market.
        """
        arrow = "🟢" if direction == "LONG" else "🔴"

        msg = (
            f"{'═'*30}\n"
            f"{arrow} PROMOTED VARIANT TRIGGERED\n"
            f"{'═'*30}\n\n"
            f"Variant: {variant_id}\n"
            f"Strategy: {strategy} | TF: {timeframe}\n"
            f"Instrument: {instrument}\n"
            f"Direction: {direction}\n"
            f"Entry Price: {entry_price:,.2f}\n\n"
            f"Recommended Exit: {best_exit_model}\n"
            f"Historical Expectancy: {expectancy:.1f} pts/trade\n"
        )

        if regime_match:
            msg += f"Regime Match: {regime_match}\n"

        msg += f"\n⏱️ {datetime.now().strftime('%I:%M:%S %p')}"

        self._send(msg)

    # ─── 5. Health / System Alerts ───────────────────────────────────────────

    def send_health_alert(
        self,
        alert_type: str,
        message: str,
        stats: dict | None = None,
    ) -> None:
        """
        System health notifications.
        alert_type: "WARNING", "ERROR", "INFO"
        """
        emoji = {"WARNING": "⚠️", "ERROR": "❌", "INFO": "ℹ️"}.get(alert_type, "📋")

        msg = f"{emoji} RESEARCH ENGINE: {alert_type}\n\n{message}\n"

        if stats:
            msg += "\n── Stats ──\n"
            for k, v in stats.items():
                msg += f"  {k}: {v}\n"

        msg += f"\n{datetime.now().strftime('%I:%M %p')}"

        self._send(msg)

    def send_startup(self, variant_count: int, instruments: list[str]) -> None:
        """Send startup notification."""
        msg = (
            f"{'═'*30}\n"
            f"🚀 RESEARCH ENGINE STARTED\n"
            f"{'═'*30}\n\n"
            f"Time: {datetime.now().strftime('%I:%M %p, %d %b %Y')}\n"
            f"Variants: {variant_count:,}\n"
            f"Instruments: {', '.join(instruments)}\n"
            f"Exit models: 57\n\n"
            f"Pipeline: Feed → Candles → Indicators\n"
            f"  → 150K Eval → Armed → Groups\n"
            f"  → Tick Triggers → Trade DB\n\n"
            f"Waiting for market data..."
        )
        self._send(msg)

    def send_shutdown(self, stats: dict) -> None:
        """Send shutdown notification with session stats."""
        msg = (
            f"{'═'*30}\n"
            f"🛑 RESEARCH ENGINE STOPPED\n"
            f"{'═'*30}\n\n"
            f"Time: {datetime.now().strftime('%I:%M %p')}\n"
        )

        if stats:
            msg += "\n── Session Stats ──\n"
            for k, v in stats.items():
                msg += f"  {k}: {v}\n"

        self._send(msg)

    # ─── Internal ────────────────────────────────────────────────────────────

    def _send(self, text: str) -> None:
        """Send message via Telegram Bot API."""
        if not self._enabled:
            logger.debug("ResearchNotifier (disabled): %s", text[:80])
            return

        url = TELEGRAM_API_URL.format(token=self._bot_token)

        for chat_id in self._chat_ids:
            payload = json.dumps({
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }).encode("utf-8")

            try:
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status != 200:
                        logger.warning("Telegram API returned %d", resp.status)
            except Exception as e:
                logger.error("Telegram send failed: %s", e)

    @property
    def enabled(self) -> bool:
        return self._enabled
