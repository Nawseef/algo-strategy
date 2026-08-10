"""
cTrader CFD runner — ONE process: push feed -> 5m candles -> store -> strategies
-> paper executor -> Telegram.

This is the owner's single-process target for the cTrader migration. It is a thin
entry point over the feed-agnostic ``CFDPaperTradingApp`` (see app/main_cfd_paper.py),
selecting the cTrader Open API feed instead of the MT5 bridge. Everything else —
candle building, live_candles archiving, strategy evaluation, the multi-account
paper executor, risk guard, and rich Telegram alerts — is shared with the MT5
runner, so the two stay behaviourally identical apart from the data source.

Pipeline:
    cTrader Open API (SpotEvent push, bid/ask)
        -> Tick(bid) -> EventBus 'tick'
            -> CandleBuilder                 (5m candles)
            -> MultiAccountManager.on_tick   (fills armed entries, manages SL/TP)
        -> EventBus 'candle' (each completed 5m candle)
            -> LiveCandleStore               (archive to live_candles)
            -> strategy evaluation -> MultiAccountManager.on_signal

No feed VM, no SSH tunnel, no Wine/Docker/RPyC. Push-based, UTC-native, ARM Linux.

Prerequisites:
    * cTrader Open API app Active (openapi.ctrader.com) — DONE.
    * OAuth tokens + login in .env (CTRADER_ACCESS_TOKEN, CTRADER_REFRESH_TOKEN,
      CTRADER_TOKEN_EXPIRES_AT, CTRADER_ACCOUNT_LOGIN). See CFD_SYSTEM.md section 4.
    * pip install 'ctrader-api-client>=0.8.0'

Configuration (env, all optional — shared with app.main_cfd_paper):
    CFD_PAPER_STRATEGIES       csv of strategy ids to run (default: all registered)
    CFD_PAPER_ACCOUNT_ID       account label (default "cfd_demo")
    CFD_PAPER_BALANCE          starting balance USD (default 100000)
    CFD_PAPER_RISK_PCT         risk per trade % (default 1.0)
    CFD_PAPER_ARCHIVE_CANDLES  archive 5m candles to live_candles (default true)
    CFD_PAPER_COST_MODEL       intraday | conservative | zero (default intraday)
    CFD_PAPER_SUMMARY_MIN      periodic portfolio summary cadence (default 30; 0 off)
    CTRADER_SYMBOLS            csv of symbols to trade (default: the 10 CFDs)

To run as a candle archiver only (no paper trades), point CFD_PAPER_STRATEGIES
at a non-existent id (e.g. CFD_PAPER_STRATEGIES=none) so no strategy loads.

Usage:
    python -m app.main_ctrader
"""

from __future__ import annotations

from app.main_cfd_paper import CFDPaperTradingApp


def main() -> None:
    app = CFDPaperTradingApp(feed="ctrader")
    app.run()


if __name__ == "__main__":
    main()
