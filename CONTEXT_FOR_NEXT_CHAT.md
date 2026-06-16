# Context for Next Chat — 150K Variant Research Engine

## Current State (as of June 16, 2026 morning)

### FULL 5.5-YEAR BACKTEST COMPLETED ✅

```
Days processed:  1,413
Trades created:  12,342,391
Exit results:    12,012,907 (329,484 skipped = 2.7% last-candle entries)
Instruments:     10 (all except VIX which is used as a filter only)
Date range:      2021-01-01 → 2026-06-12
Total time:      ~15 hours
Zero errors.
```

---

## What We're Trying to Achieve

Build a research platform that discovers **which trading strategy variants actually work** across multiple market regimes, then deploy the proven ones for live trading. The system evaluates 150,000 entry variants × 57 exit models = millions of combinations, scored over 5.5 years of data.

---

## Where We Are Right Now

### Backtest is done. Next step: FORWARD WALK VALIDATION.

The critical question: **Are the top-scoring variants genuinely profitable, or just overfitted to a specific regime?**

The preliminary scoring (on NIFTY+BANKNIFTY Jan 2024–Jun 2025 only) showed:
- Top variant: MR 15m, ATR>25 filter, `session_morning` exit
- 70% win rate, 99 pts/trade, PF 6.48
- Suspiciously good — likely inflated by bull market bias ("buy every dip" works when market goes up)

Now with 12M trades across 5.5 years and 10 instruments (including 2021 COVID recovery, 2022 correction, 2023 range, 2024 bull, 2025 mixed), we can properly validate.

---

## IMMEDIATE NEXT STEPS (do these in order)

### Step 1: Forward Walk Validation
```bash
# Clear old scores
PGPASSWORD=algo_research_2026 psql -U algo -d research_db -h localhost -c "TRUNCATE variant_scores, variant_regime_scores;"

# Score TRAIN period (2021-2024)
python -m app.scoring.run --from 2021-01-01 --to 2024-12-31 --cost none --top 10

# Score VALIDATE period (2025-2026, unseen data)
python -m app.scoring.run --from 2025-01-01 --to 2026-06-12 --cost none --top 10
```

**What to look for:**
- Do the SAME variant_ids appear in both top 10 lists?
- If yes → edge is real, survives regime change
- If no → overfitting to historical conditions

### Step 2: Score with Transaction Costs
```bash
python -m app.scoring.run --from 2021-01-01 --to 2026-06-12 --cost equity_intraday --top 50
python -m app.scoring.run --from 2021-01-01 --to 2026-06-12 --cost futures --top 50
```

### Step 3: Regime Analysis
```bash
# Score only during crash/correction periods
python -m app.scoring.run --from 2022-01-01 --to 2022-06-30 --cost none --top 10  # Fed tightening
python -m app.scoring.run --from 2024-10-01 --to 2025-03-31 --cost none --top 10  # Indian market correction
```

### Step 4: Start Live Forward Testing
```bash
sudo systemctl start algo-research
sudo journalctl -u algo-research -f
```
Run for 2-4 weeks. Compare live signals against backtest predictions.

### Step 5: Deploy Proven Variants
Pick top 3-5 variants that survive ALL validation steps. Paper trade, then real capital.

---

## Bugs Fixed This Session

| # | Bug | Fix | File |
|---|-----|-----|------|
| 1 | Backtest warmup ate morning candles (ORB=0 trades) | Load 50 candles from previous day | `app/backtest/replay.py` |
| 2 | MeanReversion used broken VWAP (volume=0 for indices) + inverted logic | Use EMA20, correct direction | `app/variants/strategies/mean_reversion_template.py` |
| 3 | Exit engine included pre-entry candle in path (false stop triggers) | Skip entry candle for CANDLE_CLOSE trades | `app/exit_engine/engine.py` |
| 4 | Scoring CLI only supported --days (hard to target specific periods) | Added --from/--to date range | `app/scoring/run.py` |

---

## Infrastructure

- **VM:** Oracle A1 Flex (4 OCPU, 24GB RAM, 45GB disk) — IP: 140.245.222.14
- **SSH:** `ssh -i ssh-key-2026-05-19.key ubuntu@140.245.222.14`
- **DB:** PostgreSQL 15, user=algo, db=research_db, password=algo_research_2026
- **DB size:** ~8-10GB after full backtest
- **Timezone:** Asia/Kolkata (IST) — all timestamp logic assumes IST
- **Oracle Free Tier** — no charges for compute

---

## Data in DB

### Historical Candles (for backtesting)
| Instrument | Token | Candles | Range |
|---|---|---|---|
| NIFTY | 26000 | 105,180 | 2021-01-01 → 2026-06-12 |
| BANKNIFTY | 26009 | 105,083 | 2021-01-01 → 2026-06-12 |
| RELIANCE | 2885 | 104,690 | 2021-01-01 → 2026-06-12 |
| HDFCBANK | 1333 | 104,586 | 2021-01-01 → 2026-06-12 |
| ICICIBANK | 4963 | 104,387 | 2021-01-01 → 2026-06-12 |
| SBIN | 3045 | 104,647 | 2021-01-01 → 2026-06-12 |
| AXISBANK | 5900 | 104,084 | 2021-01-01 → 2026-06-12 |
| INFY | 1594 | 104,504 | 2021-01-01 → 2026-06-12 |
| TCS | 11536 | 104,486 | 2021-01-01 → 2026-06-12 |
| BHARTIARTL | 10604 | 104,093 | 2021-01-01 → 2026-06-12 |
| INDIAVIX | 26017 | 103,991 | 2021-01-01 → 2026-06-12 |

### Backtest Results
- **trades:** 12,342,391 rows
- **exit_results:** 12,012,907 rows (57 exit model PnLs per trade)
- **candle_cache:** temporary intraday candles (used by exit engine)

---

## Key Architecture Notes

### Strategy Templates (5 strategies)
| Strategy | Entry Mode | How it works |
|----------|-----------|--------------|
| ORB | INTRABAR | Builds 9:15-9:30 range, arms breakout levels, tick trigger |
| BB | INTRABAR | Detects squeeze (5+ candles), arms band levels on release |
| VPA | CANDLE_CLOSE | Pattern detection (engulfing, hammer, shooting star) |
| TREND | INTRABAR | EMA9>EMA21 + pullback to EMA21, arms bounce level |
| MR | CANDLE_CLOSE | Price below/above EMA20 by 0.3+ATR + RSI crosses 35/65 |

### Variant System
- 150,000 total variants = 5 strategies × 3 timeframes × 10,000 filter combos
- Filters: ATR(5), ADX(5), VIX(4), Volume(4), RSI(5), VWAP(5) = 10,000 combinations
- Each variant has a deterministic ID (hash of strategy+timeframe+filters)

### Exit Models (57 per trade)
- RR: 1, 1.5, 2, 2.5, 3, 5, 10 (7 models)
- Stops: ATR, swing, fixed (3)
- Trails: ATR, EMA, swing (3)
- Partials: A, B, C (3)
- Time: 15m, 30m, 1h, 2h, 4h (5)
- Session: morning, midday, afternoon, preclose (4)
- Dead trade: 30m, 1h, 2h (3)
- Breakeven+trail: 7 combos
- Chandelier: 12 variants
- Indicator exits: 10 models

### Scoring Engine
- Computes: win rate, expectancy, profit factor, max drawdown, stability, regime analysis
- Stability: splits into weekly periods, checks consistency
- Regime: analyzes per-session, per-day, per-volatility performance
- Ranking: composite score 0-100 combining all metrics

---

## Known Issues / Concerns

1. **`time_1h` exit mislabeled for 15m/30m trades** — code holds for 12 candles regardless of timeframe. On 15m = 3 hour hold, on 30m = 6 hour hold. Valid but misleading name.

2. **Afternoon time exits degenerate to EOD** — if not enough candles remain, `time_1h`/`time_2h` just exits at last available candle (= EOD close).

3. **MR "too good to be true"** — 99 pts/trade on NIFTY+BANKNIFTY preliminary scoring. Likely inflated by bull market bias. The full 5.5-year backtest across multiple regimes will reveal the truth.

4. **Volume=0 for indices** — VWAP is useless for NIFTY/BANKNIFTY (no volume data from Groww). MR uses EMA20 instead. Stocks DO have volume.

5. **Corporate actions not adjusted** — stock splits/bonus in historical data (RELIANCE, INFY etc) may cause ~14 bad candles per event. Affects a tiny fraction of trades.

6. **`opening_range_size` metadata always 0 in backtest** — ORB template builds range internally but doesn't update the metadata field.

---

## Commands Cheat Sheet

```bash
# SSH to VM
ssh -i ssh-key-2026-05-19.key ubuntu@140.245.222.14

# Activate venv
source venv/bin/activate

# Check trade count
PGPASSWORD=algo_research_2026 psql -U algo -d research_db -h localhost -c "SELECT COUNT(*) FROM trades;"

# Score any period
python -m app.scoring.run --from 2021-01-01 --to 2024-12-31 --cost none --top 10
python -m app.scoring.run --from 2025-01-01 --to 2026-06-12 --cost none --top 10

# Score with costs
python -m app.scoring.run --from 2021-01-01 --to 2026-06-12 --cost equity_intraday --top 50

# Strategy distribution check
PGPASSWORD=algo_research_2026 psql -U algo -d research_db -h localhost -c "SELECT strategy, COUNT(*) FROM trades GROUP BY strategy ORDER BY COUNT(*) DESC;"

# DB size
PGPASSWORD=algo_research_2026 psql -U algo -d research_db -h localhost -c "SELECT pg_size_pretty(pg_database_size('research_db'));"

# Re-run exit engine only (if needed)
PGPASSWORD=algo_research_2026 psql -U algo -d research_db -h localhost -c "TRUNCATE exit_results;"
nohup python -m app.exit_engine.run --last 1500 > exit_rerun.log 2>&1 &

# Run new backtest (truncate first!)
PGPASSWORD=algo_research_2026 psql -U algo -d research_db -h localhost -c "TRUNCATE trades, exit_results, candle_cache, backtest_runs, variant_scores, variant_regime_scores;"
nohup python -m app.backtest.run --from 2021-01-01 --to 2026-06-12 > backtest_full.log 2>&1 &
```

---

## The Top Variant From Preliminary Scoring (needs re-validation with full data)

**Variant `fc5890e64f70a64e`:**
- Strategy: Mean Reversion, 15m timeframe
- Filter: ATR > 25 only (everything else = None)
- Entry: LONG when price < EMA20 by 0.3+ATR AND RSI crosses up from 35; SHORT when price > EMA20 by 0.3+ATR AND RSI crosses down from 65
- Best exit: `session_morning` (exit at end of morning session ~11:30)
- Performance (2024-2025 only, 2 instruments): 341 trades, 70% win, 99 pts/trade
- Best regime: HIGH volatility
- Worst regime: CLOSING session
- **Skepticism:** This is essentially "buy morning dips in a bull market." Need to see if it survives 2021-2022 correction and crash periods.
