# Context for Next Chat — 150K Variant Research Engine

## What Was Built (ALL COMPLETE)

A 150,000-variant algo trading research platform. All 10 phases implemented and tested.
Full details in `/Users/nawseefali/Proj/algo-strategy/IMPLEMENTATION_PLAN.md`

## Current State (as of June 12, 2026)

### Infrastructure
- **Old VM:** Oracle E2.1.Micro (1 OCPU, 1GB) — IP: 144.24.154.233 — runs old paper trading (`app/main.py`)
- **New VM:** Oracle A1 Flex (4 OCPU, 24GB) — IP: 140.245.222.14 — deploying research engine
- **Groww API:** Both IPs whitelisted (primary=new VM, secondary=old VM)
- **New VM has:** Python 3, PostgreSQL 15, venv with growwapi/pyotp/psycopg2/numpy/pandas, systemd service configured, cron configured

### What's Verified Working on New VM
- ✅ `python -m app.variants.generator` → 150,000 variants generated
- ✅ PostgreSQL connected (DATABASE_URL from .env, 9 tables created)
- ✅ Groww API authentication (TOTP method)
- ✅ Historical data fetch: 666 NIFTY 5m candles fetched for June 1-11, 2025
- ✅ systemd service `algo-research` created and enabled (not started yet)
- ✅ Cron: exit engine runs at 15:35 weekdays

### Code Location
- Laptop: `/Users/nawseefali/Proj/algo-strategy/`
- New VM: `/home/ubuntu/algo-strategy/` (cloned from git, venv at `./venv/`)
- Git repo has everything including .env (user chose to include it)

## What To Do Next

### Immediate (in order):

1. **Fetch ALL historical data (5 years, all instruments)**
   ```bash
   # On new VM:
   cd /home/ubuntu/algo-strategy && source venv/bin/activate
   python -m app.backtest.fetch --from 2021-01-01 --to 2025-06-12
   ```
   This fetches 11 instruments (NIFTY, BANKNIFTY, RELIANCE, HDFCBANK, ICICIBANK, SBIN, AXISBANK, INFY, TCS, BHARTIARTL, INDIAVIX) in 30-day chunks. Takes ~5-10 minutes.

2. **Run backtest replay**
   ```bash
   python -m app.backtest.run --from 2024-01-01 --to 2025-06-01
   ```
   Start with 1 year first to verify. This replays candles through the 150K pipeline and generates trades + exit results.

3. **Run scoring**
   ```bash
   python -m app.scoring.run --days 365 --cost equity_intraday
   ```

4. **Start live research engine** (during market hours)
   ```bash
   sudo systemctl start algo-research
   sudo journalctl -u algo-research -f  # watch logs
   ```

### Known Issues / Notes
- Volume=0 for NIFTY index candles (normal — indices don't have volume)
- First candle of each day may have wide OHLC range (includes pre-market auction) — our code filters ticks before 9:15
- Groww backtesting API limit: 30 days per request for 5m candles
- The `CANDLE_INTERVAL` must use SDK constant `self._api.CANDLE_INTERVAL_MIN_5` (not string "5")

### Design Files to Reference
- `IMPLEMENTATION_PLAN.md` — full system documentation
- `file1.md` through `file7.md` — original design documents
- `question.md` — reviewer Q&A (architecture validation)

### Key Architecture Points
- 150K variants = 5 strategies × 3 timeframes × 10,000 filter combos
- Entries recorded to `trades` table (no exit during market)
- Post-market: 57 exit models simulated per trade
- Scoring: composite score with confidence weighting (sqrt(trades/100))
- Transaction costs: configurable (equity_intraday=4pts NIFTY, futures=17pts)
- Daily reset auto-triggers from tick timestamps (backtest-safe)
- Variant ID: md5[:16] of non-NONE filters only (future-proof)
- DB: PostgreSQL (production) / SQLite (local tests) — dual mode

### VM Config
- Timezone: Asia/Kolkata (IST)
- PostgreSQL: user=algo, db=research_db, password=algo_research_2026
- Service: `/etc/systemd/system/algo-research.service`
- Cron: `35 15 * * 1-5` runs exit engine
- No swap needed (24GB RAM, system uses ~250MB)
