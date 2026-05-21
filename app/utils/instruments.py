"""
Instrument name resolver.
Maps exchange tokens to human-readable names for Telegram alerts.
Reads from data/instruments.json.

Returns detailed format for beginners:
  Stocks:  "RELIANCE | NSE Stock (Reliance Industries)"
  Indices: "NIFTY 50 | NSE Index (not F&O)"

This makes it crystal clear WHAT you're looking at and WHERE to find it.
"""

import json
from pathlib import Path

_INSTRUMENTS_FILE = Path(__file__).parent.parent.parent / "data" / "instruments.json"
_TOKEN_DATA: dict[str, dict] = {}


def _load_names() -> None:
    """Load token-to-name mapping from JSON file."""
    global _TOKEN_DATA
    if _INSTRUMENTS_FILE.exists():
        with open(_INSTRUMENTS_FILE) as f:
            _TOKEN_DATA = json.load(f)


_load_names()


def get_instrument_name(exchange_token: str) -> str:
    """
    Get a detailed, beginner-friendly name for an exchange token.

    Returns format like:
      "RELIANCE | NSE Stock (Reliance Industries)"
      "NIFTY 50 | NSE Index (not F&O)"

    This tells you:
      1. The symbol to search for in your broker app
      2. Which exchange (NSE or BSE)
      3. What type (Stock or Index)
      4. The full company/index name

    Falls back to the token itself if not found.
    """
    data = _TOKEN_DATA.get(exchange_token)
    if data and isinstance(data, dict):
        symbol = data.get("symbol", exchange_token)
        name = data.get("name", "")
        exchange = data.get("exchange", "NSE")
        inst_type = data.get("type", "")

        # Build a clear, descriptive label
        if inst_type == "Index":
            # For indices, make it very clear this is NOT options/futures
            return f"{symbol} | {exchange} Index (not F&O)"
        elif inst_type == "Stock":
            return f"{symbol} | {exchange} Stock ({name})"
        else:
            # Fallback for unknown types
            if name and name != symbol:
                return f"{symbol} ({name})"
            return symbol

    return exchange_token


def get_instrument_short_name(exchange_token: str) -> str:
    """
    Get a shorter name for compact displays (summaries, tables).
    Returns: "RELIANCE (NSE)" or "NIFTY 50 (Index)"
    """
    data = _TOKEN_DATA.get(exchange_token)
    if data and isinstance(data, dict):
        symbol = data.get("symbol", exchange_token)
        inst_type = data.get("type", "")
        exchange = data.get("exchange", "NSE")

        if inst_type == "Index":
            return f"{symbol} (Index)"
        return f"{symbol} ({exchange})"

    return exchange_token


def get_instrument_map() -> dict[str, dict]:
    """
    Get the full instrument map: token → {"symbol": "...", "name": "...", ...}.
    Used by the warmup DataManager to resolve exchange tokens to trading symbols.
    Excludes comment keys (starting with _).
    """
    if not _TOKEN_DATA:
        _load_names()
    return {k: v for k, v in _TOKEN_DATA.items() if not k.startswith("_")}


def get_trading_symbol(exchange_token: str) -> str:
    """
    Get the trading symbol for an exchange token.
    Returns the symbol string (e.g., "RELIANCE", "NIFTY 50").
    Falls back to the token itself if not found.
    """
    data = _TOKEN_DATA.get(exchange_token)
    if data and isinstance(data, dict):
        return data.get("symbol", exchange_token)
    return exchange_token
