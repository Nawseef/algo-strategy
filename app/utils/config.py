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
class MT5Config:
    """MetaTrader 5 (IC Markets CFD) feed configuration.

    The consumer talks to the mt5linux RPyC server on the x86 feed VM over an
    SSH tunnel (default localhost:8001). See MT5_FEED_SETUP.md / NEXT_CHAT_HANDOFF.md.
    """

    host: str = field(default_factory=lambda: os.getenv("MT5_HOST", "localhost"))
    port: int = field(default_factory=lambda: _parse_int("MT5_PORT", 8001))
    # The 10 IC Markets CFD symbols (exact broker names).
    symbols: list[str] = field(
        default_factory=lambda: _parse_list(
            "MT5_SYMBOLS",
            "XAUUSD,XAGUSD,EURUSD,GBPUSD,USDJPY,US30,US500,USTEC,DE40,XTIUSD",
        )
    )
    # How often to pull ticks per symbol (seconds). 1s batched pulls keep the
    # 1 GB feed VM safe (per load test in MT5_FEED_SETUP.md).
    poll_interval_s: float = field(default_factory=lambda: float(os.getenv("MT5_POLL_INTERVAL_S", "1.0")))
    # Overlap window added to each pull to guarantee gapless coverage across
    # poll jitter / reconnects. Ticks are de-duplicated by time_msc cursor.
    lookback_s: float = field(default_factory=lambda: float(os.getenv("MT5_LOOKBACK_S", "10.0")))
    # Broker server-time offset from UTC, in hours.
    # VERIFIED (30 Jul 2026): tick time_msc IS in server time (GMT+3), and
    # copy_ticks_range bounds must be given in server time too — querying with
    # UTC bounds returns ticks from `offset` hours ago (stale price!).
    # IC Markets servers also shift GMT+2/GMT+3 with DST, so leave this UNSET to
    # auto-detect the offset from the live tick on each connect (recommended).
    # Set to a number only to force a fixed offset (disables auto-detect).
    server_utc_offset_hours: float | None = field(
        default_factory=lambda: _parse_optional_float("MT5_SERVER_UTC_OFFSET_HOURS")
    )
    # Reconnect backoff bounds (seconds).
    reconnect_backoff_s: float = field(default_factory=lambda: float(os.getenv("MT5_RECONNECT_BACKOFF_S", "2.0")))
    reconnect_backoff_max_s: float = field(default_factory=lambda: float(os.getenv("MT5_RECONNECT_BACKOFF_MAX_S", "60.0")))
    # How often (seconds) to re-measure the server offset while running, so DST
    # flips (GMT+2<->+3) and weekend->market-open transitions are picked up
    # without a restart. Only applies when the offset is auto-detected.
    offset_redetect_s: float = field(default_factory=lambda: float(os.getenv("MT5_OFFSET_REDETECT_S", "300")))
    # Dedicated CFD Telegram bot (SEPARATE from the NSE TELEGRAM_* creds — CFD
    # alerts must never go to the NSE channel). Same bot as the feed VM uses.
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("MT5_TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_ids: list[str] = field(
        default_factory=lambda: _parse_list("MT5_TELEGRAM_CHAT_ID", "")
    )
    # Backfill: on startup and after a reconnect, replay ticks the consumer
    # missed from the feed's history (it retains ~30 days) so consumer-downtime
    # gaps get filled. Only helps when the FEED stayed up during the outage.
    backfill_enabled: bool = field(
        default_factory=lambda: os.getenv("MT5_BACKFILL_ENABLED", "true").lower() == "true"
    )
    # Cap how far back to backfill (older gaps are left for the Dukascopy history job).
    backfill_max_days: float = field(default_factory=lambda: float(os.getenv("MT5_BACKFILL_MAX_DAYS", "3.0")))
    # Pull the gap in chunks this many seconds wide (keeps each RPyC transfer small).
    backfill_chunk_s: float = field(default_factory=lambda: float(os.getenv("MT5_BACKFILL_CHUNK_S", "1800")))
    # Pause between backfill chunks so a big replay doesn't machine-gun the 1 GB feed VM.
    backfill_chunk_pause_s: float = field(default_factory=lambda: float(os.getenv("MT5_BACKFILL_CHUNK_PAUSE_S", "0.15")))

    @property
    def exchange(self) -> str:
        """Synthetic exchange label used in Tick/Candle for CFDs."""
        return os.getenv("MT5_EXCHANGE", "ICMARKETS")

    @property
    def segment(self) -> str:
        """Synthetic segment label used in Tick/Candle for CFDs."""
        return os.getenv("MT5_SEGMENT", "CFD")


@dataclass
class CTraderConfig:
    """cTrader Open API (IC Markets CFD) configuration.

    Replaces the MT5 feed VM entirely — connects directly to Spotware's
    cloud via WebSocket/TCP, receives push-based spot events (bid/ask on
    every price change), and can also place orders. Runs natively on ARM
    Linux (no Wine/Docker/RPyC).
    """

    client_id: str = field(default_factory=lambda: os.getenv("CTRADER_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: os.getenv("CTRADER_CLIENT_SECRET", ""))
    access_token: str = field(default_factory=lambda: os.getenv("CTRADER_ACCESS_TOKEN", ""))
    refresh_token: str = field(default_factory=lambda: os.getenv("CTRADER_REFRESH_TOKEN", ""))
    token_expires_at: int = field(default_factory=lambda: _parse_int("CTRADER_TOKEN_EXPIRES_AT", 0))
    # Your cTrader trader login number (visible in the platform header).
    account_login: int = field(default_factory=lambda: _parse_int("CTRADER_ACCOUNT_LOGIN", 0))
    # "demo" or "live"
    env: str = field(default_factory=lambda: os.getenv("CTRADER_ENV", "demo"))
    # The 10 IC Markets CFD symbols (string names — resolved to numeric IDs on connect).
    symbols: list[str] = field(
        default_factory=lambda: _parse_list(
            "CTRADER_SYMBOLS",
            "XAUUSD,XAGUSD,EURUSD,GBPUSD,USDJPY,US30,US500,USTEC,DE40,XTIUSD",
        )
    )
    # Dedicated CFD Telegram bot (same as MT5 consumer used).
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("CTRADER_TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_ids: list[str] = field(
        default_factory=lambda: _parse_list("CTRADER_TELEGRAM_CHAT_ID", "")
    )

    @property
    def host(self) -> str:
        """Protobuf API host based on environment."""
        if self.env == "live":
            return "live.ctraderapi.com"
        return "demo.ctraderapi.com"

    @property
    def port(self) -> int:
        """Protobuf API port (same for demo and live)."""
        return 5035

    @property
    def exchange(self) -> str:
        """Synthetic exchange label used in Tick/Candle for CFDs."""
        return "ICMARKETS"

    @property
    def segment(self) -> str:
        """Synthetic segment label used in Tick/Candle for CFDs."""
        return "CFD"


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
    chat_ids: list[str] = field(default_factory=lambda: _parse_list("TELEGRAM_CHAT_ID", ""))
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


def _parse_optional_float(env_key: str) -> float | None:
    """Return float(env) if the var is set to a non-empty value, else None."""
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_int(env_key: str, default: int) -> int:
    """Return int(env) if set to a valid value, else ``default``.

    Robust against an env var that is present but EMPTY (e.g. an
    unfilled ``CTRADER_ACCOUNT_LOGIN=`` in .env), which would otherwise crash
    ``int('')`` and take down config loading for every entrypoint.
    """
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_float(env_key: str, default: float) -> float:
    """Return float(env) if set to a valid value, else ``default`` (empty-safe)."""
    raw = os.getenv(env_key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_list(env_key: str, default: str) -> list[str]:
    raw = os.getenv(env_key, default)
    return [v.strip() for v in raw.split(",") if v.strip()]


@dataclass
class AppConfig:
    """Top-level application configuration."""

    groww: GrowwConfig = field(default_factory=GrowwConfig)
    mt5: MT5Config = field(default_factory=MT5Config)
    ctrader: CTraderConfig = field(default_factory=CTraderConfig)
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
