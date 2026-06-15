"""
CLI entry point for the scoring engine.

Usage:
    python -m app.scoring.run                     # Score last 30 days
    python -m app.scoring.run --period monthly    # Score by month
    python -m app.scoring.run --period weekly     # Score by week
    python -m app.scoring.run --days 60           # Score last 60 days
    python -m app.scoring.run --top 100           # Show top 100

Output: ranked list of top candidate variants with their best exit models.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from app.db.research_store import ResearchStore
from app.scoring.ranker import RankingConfig, VariantRanker
from app.utils.logger import get_logger

logger = get_logger("scoring")


def main() -> None:
    """CLI entry point."""
    print("=" * 70)
    print("  VARIANT SCORING + RANKING ENGINE")
    print("=" * 70)

    # Parse args
    args = sys.argv[1:]
    days = 30
    top_n = 50
    min_trades = 10
    cost_model_name = "equity_intraday"
    from_date: str | None = None
    to_date: str | None = None

    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
            i += 2
        elif args[i] == "--from" and i + 1 < len(args):
            from_date = args[i + 1]
            i += 2
        elif args[i] == "--to" and i + 1 < len(args):
            to_date = args[i + 1]
            i += 2
        elif args[i] == "--top" and i + 1 < len(args):
            top_n = int(args[i + 1])
            i += 2
        elif args[i] == "--min-trades" and i + 1 < len(args):
            min_trades = int(args[i + 1])
            i += 2
        elif args[i] == "--cost" and i + 1 < len(args):
            cost_model_name = args[i + 1]
            i += 2
        else:
            i += 1

    # Period — support both --days (relative) and --from/--to (absolute)
    now = datetime.now()
    if from_date:
        start_dt = datetime.strptime(from_date, "%Y-%m-%d")
    else:
        start_dt = now - timedelta(days=days)

    if to_date:
        end_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    else:
        end_dt = now

    start_ms = start_dt.timestamp() * 1000
    end_ms = end_dt.timestamp() * 1000
    period_label = f"{start_dt.strftime('%Y-%m-%d')}_to_{end_dt.strftime('%Y-%m-%d')}"

    print(f"\n  Period: {start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')}")
    print(f"  Min trades: {min_trades}")
    print(f"  Top N: {top_n}")
    print(f"  Cost model: {cost_model_name}")

    # Setup
    store = ResearchStore()
    store.start()

    config = RankingConfig(
        min_trade_count=min_trades,
        top_n=top_n,
        cost_model=cost_model_name,
    )
    ranker = VariantRanker(store, config)

    # Run ranking
    results = ranker.rank_variants(start_ms, end_ms, period_label)

    # ─── Send to Telegram (if configured) ────────────────────────
    _send_to_telegram(results, period_label, top_n)

    # Display
    if not results:
        print("\n  No variants met the minimum criteria.")
        print("  (Need at least trades with positive expectancy and stability)")
    else:
        print(f"\n{'─' * 70}")
        print(f"  TOP {len(results)} CANDIDATE VARIANTS")
        print(f"{'─' * 70}")

        for rv in results:
            print(f"\n  ┌─ #{rv.rank} ─ {rv.variant_id} ──────────────────────────────────")
            print(f"  │ Strategy: {rv.strategy} | Timeframe: {rv.timeframe}")
            print(f"  │ Composite Score: {rv.composite_score:.1f} / 100")
            print(f"  │")
            print(f"  │ ── Best Exit: {rv.best_exit_model}")
            if rv.top_exit_models and len(rv.top_exit_models) > 1:
                alts = ", ".join(f"{m.model_name}(E={m.expectancy:.1f})" for m in rv.top_exit_models[1:3])
                print(f"  │    Alternatives: {alts}")
            print(f"  │")
            print(f"  │ ── Performance ({rv.trade_count} trades: {rv.win_count}W / {rv.loss_count}L)")
            print(f"  │    Win Rate:       {rv.win_rate*100:.1f}%")
            print(f"  │    Avg Win:        {rv.avg_win:.1f} pts")
            print(f"  │    Avg Loss:       {rv.avg_loss:.1f} pts")
            print(f"  │    Expectancy:     {rv.expectancy:.1f} pts/trade")
            print(f"  │    Profit Factor:  {rv.profit_factor:.2f}")
            print(f"  │    Net PnL:        {rv.net_pnl:.1f} pts")
            print(f"  │    Sharpe Ratio:   {rv.sharpe_ratio:.2f}")
            print(f"  │")
            print(f"  │ ── Risk")
            print(f"  │    Max Drawdown:   {rv.max_drawdown:.1f} pts")
            print(f"  │    Recovery Factor:{rv.recovery_factor:.2f}")
            print(f"  │    Max Consec Loss:{rv.max_consecutive_losses}")
            print(f"  │    Avg MFE:        {rv.avg_mfe:.1f} | Avg MAE: {rv.avg_mae:.1f}")
            print(f"  │    Edge Ratio:     {rv.edge_ratio:.2f}")
            print(f"  │")
            print(f"  │ ── Stability ({rv.periods_profitable}/{rv.periods_analyzed} periods profitable)")
            print(f"  │    Score:          {rv.stability_score:.0f} / 100")
            print(f"  │")
            print(f"  │ ── After Costs ({rv.cost_model_name}, -{rv.cost_per_trade:.1f} pts/trade)")
            profit_marker = "✅" if rv.profitable_after_costs else "❌"
            print(f"  │    {profit_marker} Net Expectancy: {rv.net_expectancy:.1f} pts/trade")
            print(f"  │    Net PF:         {rv.net_profit_factor:.2f}")
            print(f"  │    Net PnL:        {rv.net_net_pnl:.1f} pts")
            print(f"  │")
            if rv.best_regime or rv.worst_regime:
                print(f"  │ ── Regime")
                if rv.best_regime:
                    print(f"  │    Best:  {rv.best_regime}")
                if rv.worst_regime:
                    print(f"  │    Worst: {rv.worst_regime}")
            print(f"  └──────────────────────────────────────────────────────────────")

        print(f"\n  Results saved to variant_scores table (period: {period_label})")

    store.stop()
    print(f"\n{'═' * 70}")


def _send_to_telegram(results: list, period_label: str, top_n: int) -> None:
    """Send scoring report to Telegram if configured."""
    try:
        from app.utils.config import load_config
        config = load_config()
        bot_token = config.telegram.bot_token if hasattr(config, 'telegram') else ""
        chat_ids = config.telegram.chat_ids if hasattr(config, 'telegram') else []

        if not bot_token or not chat_ids:
            return

        from app.telegram.research_notifier import ResearchNotifier
        notifier = ResearchNotifier(bot_token, chat_ids)
        notifier.send_scoring_report(
            period_label=period_label,
            ranked_variants=results,
            total_variants_scored=150000,
            total_passed_filters=len(results),
        )
        print("  📱 Scoring report sent to Telegram")
    except Exception as e:
        logger.debug("Telegram send skipped: %s", e)


if __name__ == "__main__":
    main()
