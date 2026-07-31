"""
Dedicated CFD/MT5 Telegram notifier.

Sends operational alerts for the MT5 consumer to the CFD Telegram bot ONLY.
This is intentionally separate from the NSE TelegramNotifier — CFD alerts must
never reach the NSE channel. Uses only MT5_TELEGRAM_* config.

Fire-and-forget: sends run in a daemon thread with a short timeout and swallow
errors, so a Telegram hiccup can never stall or crash the tick pipeline.
"""

from __future__ import annotations

import json
import threading
import urllib.request

from app.utils.logger import get_logger

logger = get_logger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


class MT5Notifier:
    """Lightweight sender for CFD ops alerts (started/stopped/sessions/etc.)."""

    def __init__(self, bot_token: str, chat_ids: list[str]) -> None:
        self._token = bot_token
        self._chat_ids = [c for c in chat_ids if c]
        self._enabled = bool(self._token and self._chat_ids)
        if not self._enabled:
            logger.warning("MT5Notifier disabled (missing MT5_TELEGRAM_BOT_TOKEN / MT5_TELEGRAM_CHAT_ID)")

    def send(self, text: str, block: bool = False) -> None:
        """Send a message. Non-blocking by default; block=True for shutdown."""
        if not self._enabled:
            return
        if block:
            self._deliver(text)
        else:
            t = threading.Thread(target=self._deliver, args=(text,), daemon=True)
            t.start()

    def _deliver(self, text: str) -> None:
        url = _API.format(token=self._token)
        for chat_id in self._chat_ids:
            payload = json.dumps({
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }).encode("utf-8")
            try:
                req = urllib.request.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status != 200:
                        logger.warning("CFD Telegram returned %d for chat %s", resp.status, chat_id)
            except Exception as e:  # noqa: BLE001
                logger.error("CFD Telegram send failed for chat %s: %s", chat_id, e)
