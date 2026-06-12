"""
CLI entry point for the exit simulation engine.

Usage:
    python -m app.exit_engine.run              # Run for today
    python -m app.exit_engine.run 2026-06-10   # Run for specific date
    python -m app.exit_engine.run --last 3     # Run for last 3 days
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from app.db.research_store import ResearchStore
from app.exit_engine.engine import ExitSimulationEngine
from app.utils.logger import get_logger

logger = get_logger("exit_engine")


def main() -> None:
    """CLI entry point."""
    print("=" * 60)
    print("  POST-MARKET EXIT SIMULATION ENGINE")
    print("=" * 60)

    store = ResearchStore()
    store.start()
    engine = ExitSimulationEngine(store)

    try:
        args = sys.argv[1:]

        if not args:
            # Default: run for today
            date_str = datetime.now().strftime("%Y-%m-%d")
            print(f"\n  Running for today: {date_str}")
            stats = engine.run_for_date(date_str)

        elif args[0] == "--last":
            # Run for last N days
            days = int(args[1]) if len(args) > 1 else 1
            print(f"\n  Running for last {days} day(s)...")
            for i in range(days):
                date_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                print(f"\n  ─── {date_str} ───")
                stats = engine.run_for_date(date_str)
                print(f"  Processed: {stats.trades_processed} | Skipped: {stats.trades_skipped}")
            return

        else:
            # Specific date
            date_str = args[0]
            print(f"\n  Running for: {date_str}")
            stats = engine.run_for_date(date_str)

        # Print summary
        print(f"\n{'─' * 60}")
        print(f"  Results:")
        print(f"    Trades processed:  {stats.trades_processed}")
        print(f"    Trades skipped:    {stats.trades_skipped}")
        print(f"    Total time:        {stats.total_time_seconds:.2f}s")
        if stats.trades_processed > 0:
            print(f"    Avg per trade:     {stats.avg_time_per_trade_ms:.1f}ms")
        print(f"{'─' * 60}")

        # Send to Telegram if configured
        _send_exit_report_to_telegram(date_str, stats)

    finally:
        store.stop()


def _send_exit_report_to_telegram(date_str: str, stats) -> None:
    """Send exit report to Telegram if configured."""
    try:
        from app.utils.config import load_config
        config = load_config()
        bot_token = config.telegram.bot_token if hasattr(config, 'telegram') else ""
        chat_ids = config.telegram.chat_ids if hasattr(config, 'telegram') else []

        if not bot_token or not chat_ids:
            return

        from app.telegram.research_notifier import ResearchNotifier
        notifier = ResearchNotifier(bot_token, chat_ids)
        notifier.send_exit_report(
            date_str=date_str,
            trades_processed=stats.trades_processed,
            trades_skipped=stats.trades_skipped,
            processing_time_s=stats.total_time_seconds,
        )
        print("  📱 Exit report sent to Telegram")
    except Exception as e:
        logger.debug("Telegram send skipped: %s", e)


if __name__ == "__main__":
    main()
