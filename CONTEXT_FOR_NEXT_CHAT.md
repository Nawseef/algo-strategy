# Context for Next Chat — 150K Variant Research Engine

## Current State (as of June 15, 2026)

### What Was Accomplished This Session

1. **Fetched 5.5 years of historical data** — all 11 instruments, 1,149,731 candles total
2. **Fixed India VIX symbol** — changed from `NSE-INDIA VIX` to `NSE-INDIAVIX` (103K candles from 2021)
3. **Fixed critical backtest bugs** (8 correctness fixes + 1 deadlock fix)
4. **Ran first successful backtest** — NIFTY + BANKNIFTY, Jan 2024 to Jun 2025
5. **Ran scoring** — found top 50 variants (all TREND strategy with 2 instruments)

### Backtest Results (NIFTY + BANKNIFTY only)

```
Run ID:          BT-211c4b73
Days processed:  370
Trades created:  135,208
Candles replayed: 52,586
Total time:      10.7 min
Trades/day avg:  365
Exit models:     57 per trade, 0 skipped
```

### Scoring Results (NIFTY + BANKNIFTY, --cost none)

- 1,946 unique variants produced trades (out of 150K)
- 728 candidates passed minimum filters (10+ trades, positive expectancy)
- Top 50 variants: ALL TREND strategy, 5m timeframe
- Best variant: 52.2/100 composite, 37.4 pts/trade expectancy, 1.89 PF, 4374 pts net
- Best exit model: ATR stop
- Best regime: volatility_regime=HIGH
- Worst regime: day_of_week=MON

### Infrastructure

- **Old VM:** Oracle E2.1.Micro (1 OCPU, 1GB) — IP: 144.24.154.233 — old paper trading
- **New VM:** Oracle A1 Flex (4 OCPU, 24GB) — IP: 140.245.222.14 — research engine
- **DB:** PostgreSQL 15, user=algo, db=research_db, password=algo_research_2026
- **Timezone:** Asia/Kolkata (IST) — critical, all timestamp logic assumes IST

### Historical Data in DB

| Instrument | Candles | Exchange Token |
|---|---|---|
| NIFTY | 105,180 | 26000 |
| BANKNIFTY | 105,083 | 26009 |
| RELIANCE | 104,690 | 2885 |
| HDFCBANK | 104,586 | 1333 |
| ICICIBANK | 104,387 | 4963 |
| SBIN | 104,647 | 3045 |
| AXISBANK | 104,084 | 5900 |
| INFY | 104,504 | 1594 |
| TCS | 104,486 | 11536 |
| BHARTIARTL | 104,093 | 10604 |
| INDIAVIX | 103,991 | 26017 |

---

## Bugs Fixed This Session

### Critical (would crash or deadlock):
1. **Reentrant lock deadlock in TradeRecorder** — `_flush()` called inside `with self._lock:` from `record_trade()`. Fix: moved `_flush()` outside the lock block.
2. **None OHLC from Groww API** — some candles have None values. Fix: skip those candles in fetch.

### Correctness (would produce wrong results):
3. **Indicator duplicate candle** — `on_candle` saw the same candle twice (once in history, once appended). Fix: timestamp dedup check.
4. **VWAP used `datetime.now()`** — wrong date in backtest. Fix: uses last candle's timestamp.
5. **Metadata session/day/month used `datetime.now()`** — always "today" in backtest. Fix: derives from candle timestamp.
6. **`inject_history` prepended** — history in reverse order. Fix: now appends.
7. **VIX query loaded ALL 104K rows per day** — Fix: uses day-specific timestamp range.
8. **15m/30m evaluation used M5 snapshot** — wrong indicator values. Fix: calls `on_candle()` for 15m/30m candles.
9. **prev_close used first candle's open** — gap always 0. Fix: queries previous day's last candle.

### Live engine fixes:
10. **Stale candle after feed reconnection** — CandleBuilder now discards candles with >2× interval gap.
11. **VIX default 0.0** — suppressed 40% of variants if VIX feed drops. Fix: defaults to 14.0.

---

## What To Do Next

### Immediate:

1. **Run full backtest with ALL 10 instruments**
   ```bash
   psql -U algo -d research_db -h localhost -c "TRUNCATE trades, exit_results, candle_cache, backtest_runs;"
   nohup python -m app.backtest.run --from 2024-01-01 --to 2025-06-01 > backtest_all.log 2>&1 &
   ```
   Takes ~50-60 minutes. Will produce trades from ORB/BB/VPA/MeanReversion strategies too.

2. **Score the full results**
   ```bash
   python -m app.scoring.run --days 520 --cost none
   python -m app.scoring.run --days 520 --cost equity_intraday
   ```

3. **Export results to CSV**
   ```bash
   psql -U algo -d research_db -h localhost -c "\COPY (SELECT v.variant_id, v.strategy, v.timeframe, v.filters, s.composite_score, s.trade_count, s.win_rate, s.expectancy, s.profit_factor, s.net_pnl, s.max_drawdown, s.stability_score FROM variant_scores s JOIN variant_definitions v ON s.variant_id = v.variant_id ORDER BY s.composite_score DESC LIMIT 100) TO '/home/ubuntu/algo-strategy/full_scores.csv' CSV HEADER"
   ```

4. **Start live research engine** (during market hours)
   ```bash
   sudo systemctl start algo-research
   sudo journalctl -u algo-research -f
   ```

### Later:

5. **Run backtest on full 5 years** (2021-2026) for more data
6. **Forward test** top 5-10 variants for 2-4 weeks
7. **Compare backtest vs forward** — validate consistency
8. **Pick final variants** for real capital deployment

---

## Key Technical Details for Next Agent

### Backtest command pattern:
```bash
# Always truncate first (unless resuming)
psql -U algo -d research_db -h localhost -c "TRUNCATE trades, exit_results, candle_cache, backtest_runs;"

# Run in background with nohup
nohup python -m app.backtest.run --from YYYY-MM-DD --to YYYY-MM-DD --instruments NIFTY,BANKNIFTY > backtest_output.log 2>&1 &

# Monitor
tail -f backtest_output.log
```

### Important code architecture notes:
- `TradeRecorder` in backtest: NO `start()` call, buffer=50K, `stop()` flushes all at once
- `IndicatorEngine.on_candle()` has timestamp dedup — safe for both live (candle in history before emit) and backtest (candle injected before call)
- `CandleBuilder.inject_history()` APPENDS (not prepends) — critical for correct indicator computation
- All datetime logic derives from candle timestamps (backtest-safe), NOT `datetime.now()`
- VIX defaults to 14.0 if no data available
- Holiday list only covers 2026 — older years rely on "< 10 candles" skip guard

### Performance characteristics:
- Backtest: ~0.5-0.6 days/sec for 2 instruments (~2 days/sec for evaluation + exit)
- ~365 trades/day for NIFTY+BANKNIFTY
- Exit simulation: ~2.5ms/trade
- Memory: ~125MB for 150K variants + processing

### Known accepted limitations:
- Corporate actions (stock splits/bonus) not adjusted — affects ~14 candles per event
- Muhurat trading (1 day/year) gets wrong session metadata
- First few trades of each day may have approximate swing stop (pre-entry candles not in cache)
- `opening_range_size` metadata is 0 in backtest (not populated)

### Files modified this session:
- `app/backtest/fetch.py` — None handling, VIX symbol fix
- `app/backtest/replay.py` — All backtest fixes
- `app/indicators/engine.py` — Dedup, VWAP, metadata timestamp fixes
- `app/core/candle_builder.py` — Append ordering, stale candle detection
- `app/execution/trade_recorder.py` — Deadlock fix (flush outside lock)
- `app/main_research.py` — VIX default 14.0
