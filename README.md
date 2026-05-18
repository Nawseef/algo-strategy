# algo-strategy

Solo retail algorithmic trading experimentation platform for Indian markets.

## Quick Start

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your Groww API credentials

# Run (full pipeline with auto-reconnect)
python -m app.main

# Run (polling mode — good for debugging)
python -m app.main_polling
```

## Configuration

Edit `.env` with your Groww credentials. Two auth methods are supported:

**Method 1: API Key + Secret** (requires daily approval)
```
GROWW_AUTH_METHOD=api_key
GROWW_API_KEY=your_key
GROWW_API_SECRET=your_secret
```

**Method 2: TOTP** (no expiry, recommended)
```
GROWW_AUTH_METHOD=totp
GROWW_TOTP_TOKEN=your_totp_token
GROWW_TOTP_SECRET=your_totp_secret
```

### Instruments

Set `SUBSCRIBE_INSTRUMENTS` to comma-separated exchange tokens from the
[Groww instruments CSV](https://growwapi-assets.groww.in/instruments/instrument.csv).

Example: `SUBSCRIBE_INSTRUMENTS=2885,26000,26009`

### Strategy & Paper Trading

```env
CANDLE_TIMEFRAMES=1m,5m       # Timeframes to build
SMA_FAST_PERIOD=5             # Fast SMA window
SMA_SLOW_PERIOD=20            # Slow SMA window
PAPER_QUANTITY=1              # Shares per trade
PAPER_MAX_POSITIONS=5         # Max concurrent positions
RECONNECT_MAX_RETRIES=0       # 0 = unlimited retries
```

### Telegram Alerts

```env
TELEGRAM_BOT_TOKEN=your_bot_token    # From @BotFather
TELEGRAM_CHAT_ID=your_chat_id        # From @userinfobot
TELEGRAM_NOTIFY_SIGNALS=true
TELEGRAM_NOTIFY_POSITIONS=true
TELEGRAM_NOTIFY_RECONNECTS=true
TELEGRAM_NOTIFY_ERRORS=true
```

## Architecture

```
Broker WebSocket Feed (Groww)
        ↓
ReconnectingFeed (exponential backoff + jitter)
        ↓
EventBus ─── tick events
        ↓
CandleBuilder (1m, 5m, 15m...)
        ↓
EventBus ─── candle events
        ↓
StrategyEngine (SMA Crossover, ...)
        ↓
EventBus ─── signal events
        ↓
PaperTradingEngine (positions, PnL)
        ↓
EventBus ─── order/position events
        ↓
┌───────────────────────────────────┐
│  TradeStore (SQLite)              │
│  TelegramNotifier (alerts)       │
│  AnalyticsEngine (on shutdown)   │
└───────────────────────────────────┘
```

## Project Structure

```
algo-strategy/
├── app/
│   ├── main.py                  # Full pipeline entry point
│   ├── main_polling.py          # Polling-based entry point
│   ├── core/
│   │   ├── events.py            # EventBus (pub/sub)
│   │   ├── models.py            # Domain models (Signal, Position, Candle, etc.)
│   │   └── candle_builder.py    # Tick → OHLCV candle aggregation
│   ├── broker/
│   │   ├── base.py              # Abstract broker interface
│   │   ├── groww.py             # Groww SDK implementation
│   │   └── reconnect.py         # Auto-reconnect with backoff
│   ├── strategy/
│   │   ├── base.py              # Abstract strategy interface
│   │   ├── engine.py            # Strategy orchestrator
│   │   └── sma_crossover.py     # SMA crossover strategy
│   ├── paper_trader/
│   │   └── engine.py            # Paper trading engine
│   ├── db/
│   │   └── store.py             # SQLite persistence layer
│   ├── telegram/
│   │   └── notifier.py          # Telegram Bot API alerts
│   ├── analytics/
│   │   └── engine.py            # Performance analytics
│   └── utils/
│       ├── config.py            # Configuration loader
│       └── logger.py            # Centralized logging
├── logs/                        # Daily log files (auto-created)
├── data/                        # SQLite database (auto-created)
├── requirements.txt
├── .env.example
└── .gitignore
```

## Development Phases

- [x] Phase 1: Broker auth + websocket + live data
- [x] Phase 2: Reconnect handling + tick/candle processing
- [x] Phase 3: Strategy engine
- [x] Phase 4: Paper trading engine
- [x] Phase 5: SQLite logging
- [x] Phase 6: Telegram alerts
- [x] Phase 7: Analytics/statistics

## Analytics

On shutdown, the platform prints a performance report:

```
══════════════════════════════════════════════════════
PERFORMANCE REPORT
══════════════════════════════════════════════════════
Total trades:      8
Winners:           5 | Losers: 3 | Breakeven: 0
Win rate:          62.5%

Total PnL:         ₹900.00
Avg PnL/trade:     ₹112.50
Avg win:           ₹280.00
Avg loss:          ₹-166.67
Largest win:       ₹500.00
Largest loss:      ₹-200.00

Profit factor:     2.80
Expectancy:        ₹112.50
Reward/Risk:       1.68

Max drawdown:      ₹500.00 (35.7%)
Avg hold time:     0.5 min

Max win streak:    5
Max loss streak:   3
══════════════════════════════════════════════════════
```

You can also query the SQLite database directly for custom analysis:
```bash
sqlite3 data/trades.db "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_at DESC"
```

## Writing Custom Strategies

Create a new file in `app/strategy/` and implement `BaseStrategy`:

```python
from app.strategy.base import BaseStrategy
from app.core.models import Candle, Signal, SignalType

class MyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "MyStrategy"

    def on_candle(self, candle: Candle, history: list[Candle]) -> Signal | None:
        if some_condition:
            return Signal(
                signal_type=SignalType.BUY,
                exchange=candle.exchange,
                segment=candle.segment,
                exchange_token=candle.exchange_token,
                price=candle.close,
                timestamp_ms=candle.timestamp_ms,
                strategy_name=self.name,
                reason="My reason",
            )
        return None
```

Register it in `main.py`:
```python
strategy_engine.register(MyStrategy())
```

## Design Principles

- **Event-driven** — modules communicate via EventBus, not direct calls
- **Modular** — broker, strategy, trading, storage, alerts are fully decoupled
- **Resilient** — auto-reconnect with exponential backoff
- **Observable** — every event logged to console, file, SQLite, and Telegram
- **Extensible** — add strategies by implementing one interface
- **Paper-first** — no real orders, all simulation
- **Measurable** — full analytics with win rate, drawdown, profit factor
