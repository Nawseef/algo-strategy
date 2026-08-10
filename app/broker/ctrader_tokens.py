"""
Persistence for cTrader OAuth tokens across restarts.

Why this exists
---------------
The ``ctrader-api-client`` refreshes the access token before it expires and, on
every refresh, issues a NEW access+refresh pair and *invalidates the old pair
immediately*. That rotation lives only in memory. So for a long-running feed:

  * While the process stays up, the library handles refresh transparently.
  * But once tokens rotate (near the ~30-day access-token expiry) and the
    process later restarts, the ``CTRADER_REFRESH_TOKEN`` in ``.env`` is the
    OLD, now-invalid token -> authentication fails.

The library exposes a write-only ``TokenStore`` hook (``save`` is called on each
rotation); reading back is the app's job. This module provides a simple
file-based store (JSON at ``data/ctrader_tokens.json``, gitignored, chmod 600)
plus a loader the broker uses on startup to prefer the freshest tokens.

Design choices:
  * File-based (not Postgres): works identically on the VM and locally, no
    schema, single account. Easy to reason about.
  * On startup the broker prefers persisted tokens over the ``.env`` seed ONLY
    when the persisted ``expires_at`` is >= the ``.env`` value. That way, if you
    paste a fresh token pair into ``.env`` (later expiry), it correctly wins over
    a stale persisted file.
  * Atomic write (temp file + os.replace) so a crash mid-write can't corrupt the
    stored tokens.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)


def default_token_path() -> Path:
    """Default persisted-token location: <repo>/data/ctrader_tokens.json."""
    return Path(__file__).resolve().parent.parent.parent / "data" / "ctrader_tokens.json"


def load_persisted_tokens(path: str | os.PathLike | None = None) -> dict[str, Any] | None:
    """Return the persisted token dict, or None if absent/unreadable."""
    p = Path(path) if path else default_token_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        logger.warning("could not read persisted cTrader tokens (%s): %s", p, e)
        return None
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return None
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)  # tokens are secrets
        except OSError:
            pass
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def make_token_store(path: str | os.PathLike | None = None):
    """Build a library ``TokenStore`` that persists rotated tokens to a JSON file.

    Returns ``None`` if ``ctrader-api-client`` is not importable (so callers can
    degrade gracefully). The returned object is passed as
    ``CTraderClient(config, token_store=...)``.
    """
    try:
        from ctrader_api_client import TokenStore
    except Exception:  # noqa: BLE001 - library missing / older version without TokenStore
        logger.warning("ctrader-api-client TokenStore unavailable — rotated tokens "
                       "will NOT be persisted (restarts may fail after ~30 days)")
        return None

    p = Path(path) if path else default_token_path()

    class FileTokenStore(TokenStore):  # type: ignore[misc, valid-type]
        async def save(self, credentials) -> None:
            _atomic_write(p, {
                "account_id": getattr(credentials, "account_id", None),
                "access_token": credentials.access_token,
                "refresh_token": credentials.refresh_token,
                "expires_at": credentials.expires_at,
                "saved_at": int(time.time()),
            })
            logger.info(
                "cTrader tokens rotated -> persisted to %s (expires_at=%s)",
                p, credentials.expires_at,
            )

    return FileTokenStore()
