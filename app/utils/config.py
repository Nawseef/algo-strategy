"""
Configuration loader.
Reads .env and provides typed access to all config values.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(_env_path)


@dataclass
class GrowwConfig:
    """Groww broker configuration."""

    auth_method: str = field(default_factory=lambda: os.getenv("GROWW_AUTH_METHOD", "api_key"))
    api_key: str = field(default_factory=lambda: os.getenv("GROWW_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("GROWW_API_SECRET", ""))
    totp_token: str = field(default_factory=lambda: os.getenv("GROWW_TOTP_TOKEN", ""))
    totp_secret: str = field(default_factory=lambda: os.getenv("GROWW_TOTP_SECRET", ""))


@dataclass
class InstrumentConfig:
    """Instruments to subscribe."""

    exchange_tokens: list[str] = field(default_factory=lambda: _parse_instruments())


@dataclass
class StrategyConfig:
    """Strategy engine configuration."""

    # Candle timeframes to build (comma-separated: 1m,5m,15m)
    timeframes: list[str] = field(default_factory=lambda: _parse_list("CANDLE_TIMEFRAMES", "1m,5m"))
    # SMA crossover parameters
    sma_fast_period: int = field(default_factory=lambda: int(os.getenv("SMA_FAST_PERIOD", "5")))
    sma_slow_period: int = field(default_factory=lambda: int(os.getenv("SMA_SLOW_PERIOD", "20")))
    # ORB parameters
    orb_rr_ratio: float = field(default_factory=lambda: float(os.getenv("ORB_RR_RATIO", "1.5")))
    orb_max_range_pct: float = field(default_factory=lambda: float(os.getenv("ORB_MAX_RANGE_PCT", "1.5")))
    # EMA Crossover parameters
    ema_fast_period: int = field(default_factory=lambda: int(os.getenv("EMA_FAST_PERIOD", "9")))
    ema_slow_period: int = field(default_factory=lambda: int(os.getenv("EMA_SLOW_PERIOD", "21")))
    ema_adx_threshold: float = field(default_factory=lambda: float(os.getenv("EMA_ADX_THRESHOLD", "25")))
    # SuperTrend parameters
    supertrend_atr_period: int = field(default_factory=lambda: int(os.getenv("SUPERTREND_ATR_PERIOD", "10")))
    supertrend_multiplier: float = field(default_factory=lambda: float(os.getenv("SUPERTREND_MULTIPLIER", "3.0")))


@dataclass
class PaperTradingConfig:
    """Paper trading configuration."""

    default_quantity: int = field(default_factory=lambda: int(os.getenv("PAPER_QUANTITY", "1")))
    starting_balance: float = field(default_factory=lambda: float(os.getenv("PAPER_STARTING_BALANCE", "100000")))
    max_open_positions: int = field(default_factory=lambda: int(os.getenv("PAPER_MAX_POSITIONS", "5")))
    # Position sizing: percentage of balance to allocate per trade (e.g., 10 = 10%)
    position_size_pct: float = field(default_factory=lambda: float(os.getenv("PAPER_POSITION_SIZE_PCT", "10")))


@dataclass
class TelegramConfig:
    """Telegram notification configuration."""

    bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    summary_interval_minutes: int = field(default_factory=lambda: int(os.getenv("TELEGRAM_SUMMARY_INTERVAL", "30")))
    notify_signals: bool = field(default_factory=lambda: os.getenv("TELEGRAM_NOTIFY_SIGNALS", "true").lower() == "true")
    notify_positions: bool = field(default_factory=lambda: os.getenv("TELEGRAM_NOTIFY_POSITIONS", "true").lower() == "true")
    notify_reconnects: bool = field(default_factory=lambda: os.getenv("TELEGRAM_NOTIFY_RECONNECTS", "true").lower() == "true")
    notify_errors: bool = field(default_factory=lambda: os.getenv("TELEGRAM_NOTIFY_ERRORS", "true").lower() == "true")


@dataclass
class ReconnectConfig:
    """Reconnection configuration."""

    max_retries: int = field(default_factory=lambda: int(os.getenv("RECONNECT_MAX_RETRIES", "0")))


@dataclass
class WarmupConfig:
    """Historical data warmup configuration."""

    enabled: bool = field(default_factory=lambda: os.getenv("WARMUP_ENABLED", "true").lower() == "true")
    # Max concurrent API requests during warmup
    concurrency: int = field(default_factory=lambda: int(os.getenv("WARMUP_CONCURRENCY", "3")))
    # Delay between requests in milliseconds (rate limit protection)
    delay_ms: int = field(default_factory=lambda: int(os.getenv("WARMUP_DELAY_MS", "200")))
    # Max retries per failed request
    max_retries: int = field(default_factory=lambda: int(os.getenv("WARMUP_MAX_RETRIES", "3")))
    # Base backoff in seconds for retries (doubles each attempt)
    retry_backoff_base: float = field(default_factory=lambda: float(os.getenv("WARMUP_RETRY_BACKOFF", "1.0")))


def _parse_instruments() -> list[str]:
    raw = os.getenv("SUBSCRIBE_INSTRUMENTS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _parse_list(env_key: str, default: str) -> list[str]:
    raw = os.getenv(env_key, default)
    return [v.strip() for v in raw.split(",") if v.strip()]


@dataclass
class AppConfig:
    """Top-level application configuration."""

    groww: GrowwConfig = field(default_factory=GrowwConfig)
    instruments: InstrumentConfig = field(default_factory=InstrumentConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    paper_trading: PaperTradingConfig = field(default_factory=PaperTradingConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    warmup: WarmupConfig = field(default_factory=WarmupConfig)
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))


def load_config() -> AppConfig:
    """Load and return the application configuration."""
    return AppConfig()
