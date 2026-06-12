"""
Armed State Manager — manages the lifecycle of armed variants.

Lifecycle: IDLE → ARMED → TRIGGERED / DISARMED

Rules (from File 6 - Safety Checklist):
- ARMED list is bounded per candle cycle
- Expired variants are removed automatically
- Triggered variants are immediately removed from watch list
- No variant remains ARMED indefinitely

Each instrument has its own armed state (independent pipelines).
"""

from __future__ import annotations

from collections import defaultdict

from app.utils.logger import get_logger
from app.variants.models import ArmedVariant, Direction, TriggerType

logger = get_logger(__name__)


class ArmedStateManager:
    """
    Manages armed variants per instrument.

    Operations:
        - arm(): Add newly armed variants from evaluator
        - disarm(): Remove variants that triggered or expired
        - cleanup_expired(): Remove stale variants on candle close
        - get_armed(): Get all armed for an instrument
        - get_all_armed(): Get all armed across all instruments

    Safety:
        - Max armed per instrument (configurable, default 10000)
        - Automatic expiry based on candle count
        - Full clear on daily reset
    """

    def __init__(self, max_armed_per_instrument: int = 10000) -> None:
        self._max_per_instrument = max_armed_per_instrument

        # Storage: instrument → list of ArmedVariant
        self._armed: dict[str, list[ArmedVariant]] = defaultdict(list)

        # Track triggered variant IDs to prevent re-arming within same session
        self._triggered_today: set[tuple[str, str]] = set()  # (variant_id, instrument)

    def arm(self, variants: list[ArmedVariant]) -> int:
        """
        Add newly armed variants.
        Returns the number actually added (some may be rejected if at limit).
        """
        added = 0
        for av in variants:
            instrument = av.instrument

            # Safety: don't exceed max per instrument
            if len(self._armed[instrument]) >= self._max_per_instrument:
                logger.warning(
                    "Armed state FULL for %s (%d). Rejecting new arms.",
                    instrument, self._max_per_instrument,
                )
                break

            # Don't re-arm a variant that already triggered today
            key = (av.variant_id, instrument)
            if key in self._triggered_today:
                continue

            self._armed[instrument].append(av)
            added += 1

        return added

    def disarm_triggered(self, instrument: str, variant_ids: list[str]) -> None:
        """
        Remove variants that have been triggered (trade created).
        Mark them so they don't get re-armed today.
        """
        id_set = set(variant_ids)

        # Mark as triggered
        for vid in variant_ids:
            self._triggered_today.add((vid, instrument))

        # Remove from armed list
        self._armed[instrument] = [
            av for av in self._armed[instrument]
            if av.variant_id not in id_set
        ]

    def cleanup_expired(self, instrument: str, current_candle: int, timeframe: str | None = None) -> int:
        """
        Remove expired armed variants for an instrument.
        Called on every candle close.

        Args:
            instrument: Exchange token
            current_candle: The candle counter for the SPECIFIC timeframe that just closed
            timeframe: If provided, only expire variants of this timeframe.
                       This prevents a 5m counter from expiring 30m variants.

        Returns the number of variants disarmed.
        """
        before = len(self._armed[instrument])

        if timeframe:
            # Only expire variants matching this timeframe
            self._armed[instrument] = [
                av for av in self._armed[instrument]
                if not (av.variant.timeframe.value == timeframe and av.is_expired(current_candle))
            ]
        else:
            # Expire all (used for daily reset or simple cases)
            self._armed[instrument] = [
                av for av in self._armed[instrument]
                if not av.is_expired(current_candle)
            ]

        expired_count = before - len(self._armed[instrument])

        if expired_count > 0:
            logger.debug(
                "Cleaned %d expired armed variants for %s (tf=%s)",
                expired_count, instrument, timeframe or "all",
            )

        return expired_count

    def get_armed(self, instrument: str) -> list[ArmedVariant]:
        """Get all armed variants for an instrument."""
        return self._armed.get(instrument, [])

    def get_all_armed(self) -> list[ArmedVariant]:
        """Get all armed variants across all instruments."""
        all_armed = []
        for variants in self._armed.values():
            all_armed.extend(variants)
        return all_armed

    def get_armed_count(self, instrument: str | None = None) -> int:
        """Get count of armed variants (optionally for one instrument)."""
        if instrument:
            return len(self._armed.get(instrument, []))
        return sum(len(v) for v in self._armed.values())

    def get_instruments_with_armed(self) -> list[str]:
        """Get list of instruments that have armed variants."""
        return [inst for inst, variants in self._armed.items() if variants]

    def reset_daily(self) -> None:
        """Full daily reset — clear everything."""
        total = self.get_armed_count()
        self._armed.clear()
        self._triggered_today.clear()
        if total > 0:
            logger.info("ArmedState daily reset (cleared %d armed)", total)

    def get_stats(self) -> dict[str, int]:
        """Get current state statistics."""
        return {
            "total_armed": self.get_armed_count(),
            "instruments_active": len(self.get_instruments_with_armed()),
            "triggered_today": len(self._triggered_today),
        }
