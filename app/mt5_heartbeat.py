"""
Daily heartbeat for the MT5 CFD consumer -> CFD Telegram.

Mirrors the feed VM's heartbeat: a once-a-day proof-of-life so that SILENCE
signals a problem. Reports market state, sessions, and how many live candles
have been stored today (a zero while the market is open is a red flag).

Run via a systemd timer (see deploy/mt5-heartbeat.timer). One-shot: it sends a
message and exits.

    python -m app.mt5_heartbeat
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db.research_store import ResearchStore
from app.telegram.mt5_notifier import MT5Notifier
from app.utils import forex_hours
from app.utils.config import load_config
from app.utils.logger import get_logger

logger = get_logger("mt5_heartbeat")

_SESSION_LABEL = {"sydney": "Sydney", "tokyo": "Tokyo", "london": "London", "new_york": "New York"}


def build_message() -> str:
    # "Today" = FX trading day (rolls at 17:00 NY), matching how candles are tagged.
    today = forex_hours.trading_day()

    store = ResearchStore()
    store.start()
    try:
        stats = store.get_live_candle_stats(today)
        by_inst = store.get_live_counts_by_instrument(today)
    finally:
        store.stop()

    st = forex_hours.status()
    open_ = bool(st["market_open"])
    sessions = ", ".join(_SESSION_LABEL.get(s, s) for s in st["active_sessions"]) or "none"

    # Health verdict: candles must accumulate while the market is open.
    if open_ and stats["today"] == 0:
        verdict = "⚠️ market OPEN but 0 candles stored today"
    else:
        verdict = "✅ OK"

    last_txt = "-"
    if stats["last_ms"]:
        last_txt = datetime.fromtimestamp(
            int(stats["last_ms"]) / 1000, timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

    # Per-instrument line (spot an instrument that's under-producing).
    if by_inst:
        inst_line = " ".join(f"{k}:{v}" for k, v in sorted(by_inst.items()))
    else:
        inst_line = "(none yet)"

    return (
        f"[MT5 consumer] daily heartbeat\n"
        f"{verdict}\n"
        f"trading day: {today} | market={'OPEN' if open_ else 'CLOSED'} | sessions: {sessions}\n"
        f"candles today: {stats['today']} across {stats['instruments_today']} instruments\n"
        f"per-instrument: {inst_line}\n"
        f"last candle: {last_txt}\n"
        f"stored total: {stats['total']:,}\n"
        f"store: {'postgres' if store.is_postgres else 'sqlite'}"
    )


def main() -> None:
    cfg = load_config()
    msg = build_message()
    logger.info("heartbeat: %s", msg.replace("\n", " | "))
    notifier = MT5Notifier(cfg.mt5.telegram_bot_token, cfg.mt5.telegram_chat_ids)
    notifier.send(msg, block=True)


if __name__ == "__main__":
    main()
