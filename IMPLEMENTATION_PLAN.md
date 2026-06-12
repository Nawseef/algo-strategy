# 150K Variant Trading Engine — Implementation (COMPLETED)

---

## Status: ALL 10 PHASES COMPLETE ✅

Built and tested. Ready for deployment to Oracle A1 (4 OCPU, 24GB RAM).

---

## What Was Built

### Architecture

```
Live Feed → CandleBuilder → IndicatorEngine (shared, compute once)
  → VariantEvaluator (150K eval per candle close)
  → ArmedState → GroupingEngine → TickTriggerEngine
  → TradeRecorder → PostgreSQL

Post-Market:
  → ExitEngine (57 exit models per trade)
  → Scoring + Stability + Regime Analysis
  → Telegram Reports
```

### Key Numbers (verified by stress tests)

| Metric | Value |
|--------|-------|
| Total variants | 150,000 |
| Eval time (5 instruments) | 116ms |
| Tick throughput | 1.28M ticks/sec |
| CPU budget per 5m candle | 0.52% |
| Memory growth over 20 cycles | 0 MB |
| Exit models per trade | 57 |
| Exit processing per trade | ~12ms |
| DB writes in tick loop | 0 (verified) |

---

## Database: 9 Tables (PostgreSQL production, SQLite local tests)

| # | Table | Type | Purpose |
|---|-------|------|---------|
| 1 | `trades` | Permanent | Every entry recorded (~750/day) |
| 2 | `exit_results` | Permanent | 57 exit model PnLs per trade |
| 3 | `variant_scores` | Permanent | Scoring results per variant per period |
| 4 | `variant_definitions` | Permanent | Maps variant_id → full setup (JSON filters) |
| 5 | `variant_regime_scores` | Permanent | Regime breakdown per variant per period |
| 6 | `historical_candles` | Permanent | 5m candle data for backtesting (multi-year) |
| 7 | `backtest_runs` | Permanent | Backtest execution metadata |
| 8 | `fetch_progress` | Permanent | Tracks which dates have been fetched |
| 9 | `candle_cache` | Temporary | Today's live candles for exit sim (7-day retention) |

Dual-mode: `DATABASE_URL=postgresql://...` → PostgreSQL. Unset → SQLite.

---

## Directory Structure (Final)

```
app/
├── variants/
│   ├── __init__.py
│   ├── generator.py              # Generate 150K variant definitions
│   ├── models.py                 # Variant, FilterSet, IndicatorSnapshot, MetadataSnapshot, etc.
│   ├── evaluator.py              # Batch evaluation at candle close
│   ├── filter_engine.py          # Filter set evaluation (pure function, short-circuit)
│   ├── config.py                 # Research engine configuration
│   ├── benchmark.py              # Performance benchmarking CLI
│   └── strategies/
│       ├── __init__.py
│       ├── base_template.py      # Abstract template → CandidateSignal
│       ├── orb_template.py       # Opening Range Breakout
│       ├── bb_template.py        # Bollinger Band Squeeze
│       ├── vpa_template.py       # Volume Price Action patterns
│       ├── trend_template.py     # EMA Pullback trend following
│       └── mean_reversion_template.py  # RSI + VWAP mean reversion
├── indicators/
│   ├── __init__.py
│   └── engine.py                 # Shared indicator computation + metadata
├── execution/
│   ├── __init__.py
│   ├── armed_state.py            # Armed variant state machine (bounded, per-instrument)
│   ├── grouping.py               # Dynamic trigger grouping (price levels)
│   ├── tick_engine.py            # Tick-level trigger checking (O(groups) not O(variants))
│   ├── trade_recorder.py         # Batch trade writes (timer + buffer, safe flush)
│   └── candle_cache.py           # Temporary candle storage for exit sim
├── exit_engine/
│   ├── __init__.py
│   ├── engine.py                 # Post-market exit simulator (all 57 models)
│   ├── scheduler.py              # After-close trigger
│   ├── run.py                    # CLI entry point
│   └── models/
│       ├── __init__.py
│       ├── rr_exit.py            # 7 RR exits (RR1 through RR10)
│       ├── stop_loss_models.py   # 3 stops (ATR, swing, fixed)
│       ├── trailing_models.py    # 3 trails (ATR, EMA, swing)
│       ├── partial_exit_models.py # 3 partial (A, B, C)
│       ├── time_exits.py         # 12 time/session/dead exits
│       ├── breakeven_trail.py    # 7 BE + trail combos
│       ├── chandelier_exit.py    # 12 chandelier/pct/step/delayed
│       └── indicator_exits.py    # 10 indicator-based exits
├── scoring/
│   ├── __init__.py
│   ├── metrics.py                # Core performance metrics
│   ├── stability.py              # Temporal stability analysis (0-100)
│   ├── regime.py                 # Regime-specific analysis (7 dimensions)
│   ├── costs.py                  # Transaction cost model (equity/futures/options)
│   ├── ranker.py                 # Composite ranking + cost-adjusted metrics
│   └── run.py                    # CLI entry point
├── backtest/
│   ├── __init__.py
│   ├── fetch.py                  # Historical data fetcher (Groww API, 11 instruments)
│   ├── replay.py                 # Day-by-day replay through pipeline
│   ├── run.py                    # CLI: replay only
│   └── full.py                   # CLI: fetch + replay + exit
├── telegram/
│   ├── __init__.py
│   ├── notifier.py               # Existing paper trading notifier (kept)
│   └── research_notifier.py      # Research engine notifications
├── db/
│   ├── __init__.py
│   ├── store.py                  # Existing paper trading DB (kept)
│   └── research_store.py         # Dual-mode PostgreSQL/SQLite (9 tables)
├── main.py                       # Existing paper trade mode (kept, unchanged)
├── main_research.py              # Research engine entry point (150K pipeline)
└── ...existing modules (broker, core, strategy, utils)...
```

---

## Differences from Original Plan

### Added (not in original plan):

| Addition | Why |
|----------|-----|
| 57 exit models (plan said ~20) | More comprehensive research — time exits, BE+trail, chandelier, indicator exits |
| PostgreSQL dual-mode DB | Production-grade for multi-year data |
| `variant_definitions` table (JSON filters) | Know what each variant_id means without running Python |
| `variant_regime_scores` table | Persist regime breakdown per scoring period |
| Transaction cost model (`scoring/costs.py`) | Show profitability AFTER real charges |
| Variant ID future-proofing (non-NONE hash) | Adding new filters doesn't change existing IDs |
| Automatic daily reset from tick timestamps | System runs 24/7 without corruption |
| Pre-market tick filtering (before 9:15) | Prevents auction price pollution |
| `historical_candles` + `fetch_progress` tables | 5-year backtest data storage |
| `backtest_runs` table | Track/resume backtest executions |
| Auto variant registration + retirement | DB always reflects current variant set |
| 11 instruments (10 + VIX) for backtest | Broader research coverage |

### Changed from plan:

| Original | Actual |
|----------|--------|
| `research.db` SQLite only | Dual-mode: PostgreSQL (production) + SQLite (tests) |
| Variant ID = md5[:12] (48 bits) | md5[:16] (64 bits) — handles 300K+ variants |
| Variant ID hashes all fields including NONE | Only hashes non-NONE filters (future-proof) |
| `candle_entry.py` separate file | Integrated into evaluator (CANDLE_CLOSE handled inline) |
| `indicators/snapshot.py` + `indicators/metadata.py` | Both in `variants/models.py` (simpler) |
| NumPy vectorization for filters | Pure Python with short-circuit (fast enough: 116ms for 5×150K) |
| Exit models in 4 files | Split into 8 files (57 models total) |
| Scoring shows only top N | Shows raw AND after-cost metrics |

### Not implemented (decided against):

| Skipped | Why |
|---------|-----|
| NumPy vectorized filter evaluation | Pure Python with short-circuit already achieves 116ms — under budget |
| Separate `candle_entry.py` | Candle-close mode handled directly in evaluator (cleaner) |
| Real-time regime-aware filtering of live variants | Deferred to deployment phase — needs actual scoring data first |

---

## CLI Commands

```bash
# Live research engine
python -m app.main_research

# Variant generator (standalone test)
python -m app.variants.generator

# Performance benchmark
python -m app.variants.benchmark

# Exit engine (after market close)
python -m app.exit_engine.run
python -m app.exit_engine.run 2026-06-11
python -m app.exit_engine.run --last 3

# Scoring
python -m app.scoring.run --days 30
python -m app.scoring.run --days 365 --top 50 --cost futures
python -m app.scoring.run --days 30 --cost none  # raw, no costs

# Backtest: fetch historical data
python -m app.backtest.fetch --from 2021-01-01 --to 2026-06-12
python -m app.backtest.fetch --instruments NIFTY,BANKNIFTY --from 2024-01-01 --to 2024-12-31

# Backtest: replay through pipeline
python -m app.backtest.run --from 2024-01-01 --to 2024-12-31
python -m app.backtest.run --from 2024-01-01 --to 2024-12-31 --instruments NIFTY,BANKNIFTY

# Backtest: full pipeline (fetch + replay + exit)
python -m app.backtest.full --from 2021-01-01 --to 2026-06-01
python -m app.backtest.full --from 2024-01-01 --to 2024-12-31 --skip-fetch
```

---

## Test Suites

| Test | What it verifies |
|------|-----------------|
| `tests/test_full_pipeline.py` | Phase 1-5 end-to-end (generation → eval → arm → trigger → DB) |
| `tests/test_armed_grouping_isolation.py` | Multi-instrument, multi-timeframe isolation |
| `tests/test_exit_engine.py` | All exit models + DB integration + determinism |
| `tests/test_scoring_engine.py` | Metrics + stability + regime + ranking |
| `tests/test_telegram_research.py` | All notification types + formatting |
| `tests/test_stress_safety.py` | Performance + memory + safety checklist (File 6) |

Run all: `python -m tests.test_full_pipeline && python -m tests.test_armed_grouping_isolation && python -m tests.test_exit_engine && python -m tests.test_scoring_engine && python -m tests.test_telegram_research && python -m tests.test_stress_safety`

---

## Production Safety (File 6 Checklist) — ALL GREEN ✅

- ✅ Candle evaluation bounded (116ms for 5 instruments)
- ✅ Tick engine uses O(groups) not O(variants) (0.8µs/tick)
- ✅ ARMED state bounded per instrument (configurable max)
- ✅ No DB writes inside tick loop (verified with 10K tick test)
- ✅ Memory stable over long runs (0MB growth over 20 cycles)
- ✅ Daily reset auto-triggers on new day (from tick timestamp)
- ✅ Pre-market ticks (before 9:15) filtered out
- ✅ Crash recovery: fresh eval on restart re-arms variants
- ✅ Deduplication prevents duplicate trades
- ✅ Armed variants expire after validity window

---

## Deployment (Oracle A1 — 4 OCPU, 24GB RAM)

### Requirements:
- Python 3.11+
- PostgreSQL 15+
- `growwapi` SDK (Groww Trade API)
- `psycopg2-binary`
- `python-dotenv`

### .env (production):
```
DATABASE_URL=postgresql://algo:password@localhost:5432/research_db
GROWW_AUTH_METHOD=api_key
GROWW_API_KEY=xxx
GROWW_API_SECRET=xxx
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
RESEARCH_INSTRUMENTS=NIFTY,BANKNIFTY,2885,1333,4963,3045,5900,1594,11536,10604
RESEARCH_MAX_ARMED=500000
```

### Systemd service:
```
[Service]
ExecStart=/usr/bin/python3 -m app.main_research
Restart=always
RestartSec=10
```

### Cron (exit engine + candle cleanup):
```
35 15 * * 1-5  cd /path && python -m app.exit_engine.run
0  16 * * 1-5  cd /path && python -c "from app.execution.candle_cache import CandleCache; from app.db.research_store import ResearchStore; s=ResearchStore(); s.start(); CandleCache(s).cleanup(7); s.stop()"
```

---

## Transaction Cost Models

| Model | NIFTY | BANKNIFTY | Stocks | Use case |
|-------|-------|-----------|--------|----------|
| `none` | 0 pts | 0 pts | 0 pts | Raw comparison |
| `equity_intraday` | 4 pts | 6 pts | 3 pts | Cash equity intraday |
| `futures` | 17 pts | 28 pts | 5 pts | Futures (post Budget 2026 STT) |
| `options` | 8 pts | 12 pts | 4 pts | Options |
| `conservative` | 8 pts | 12 pts | 6 pts | Stress test (2× costs) |

---

## What Happens Next (Post-Deployment)

1. **Fetch 5 years of historical data** → `python -m app.backtest.fetch`
2. **Run backtest** → `python -m app.backtest.full --from 2021-01-01`
3. **Score results** → `python -m app.scoring.run --days 1800 --cost equity_intraday`
4. **Pick top 5-10 variants** → configure in paper trading
5. **Forward test 2-4 weeks** → validate live performance matches backtest
6. **Go live** with capital allocation on proven variants
7. **Continuously run research engine** → discover new candidates
8. **Weekly scoring** → promote/demote variants based on stability
