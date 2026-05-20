"""
Instrument name resolver.
Maps exchange tokens to human-readable names for Telegram alerts.
Reads from data/instruments.json.

Returns format: "INFY (Infosys)" — ticker for searching + full name for clarity.
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
    Get human-readable name for an exchange token.
    Returns "SYMBOL (Full Name)" format.
    Falls back to the token itself if not found.
    """
    data = _TOKEN_DATA.get(exchange_token)
    if data and isinstance(data, dict):
        symbol = data.get("symbol", exchange_token)
        name = data.get("name", "")
        if name and name != symbol:
            return f"{symbol} ({name})"
        return symbol
    if data and isinstance(data, str):
        return data
    return exchange_token
