"""
Dynamic Grouping Engine — groups armed variants by trigger condition.

From File 4:
    "Armed variants are grouped dynamically based on trigger type.
     This is NOT pre-defined. This is created in runtime memory only."

Group types:
    A. Price Level Groups  → ORB breakout, BB band touch
    B. Structure Groups    → Trend pullback zones
    C. Indicator Events    → (future: RSI crossing threshold during candle)
    D. Pattern Groups      → (future: candle pattern detection mid-bar)

For now, all INTRABAR strategies use PRICE_LEVEL or STRUCTURE triggers,
which both resolve to "price crosses a level" — so we can treat them
uniformly as sorted price level groups.

Rebuilt every candle close (cheap — only operates on the armed set).
"""

from __future__ import annotations

from bisect import bisect_left, insort
from dataclasses import dataclass, field

from app.utils.logger import get_logger
from app.variants.models import ArmedVariant, Direction, TriggerType

logger = get_logger(__name__)


@dataclass
class PriceGroup:
    """
    A group of armed variants sharing a price trigger level.

    When the live price crosses this level:
    - LONG: price >= trigger_value → fire all members
    - SHORT: price <= trigger_value → fire all members
    """

    trigger_value: float
    direction: Direction
    members: list[ArmedVariant] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.members)


class GroupingEngine:
    """
    Groups armed variants by trigger level for efficient tick checking.

    Instead of checking 1200 individual variants per tick,
    the tick engine checks ~5 price groups.

    Storage per instrument:
        - long_levels: sorted list of price levels to watch for longs
        - short_levels: sorted list of price levels to watch for shorts
        - groups dict: (direction, level) → PriceGroup

    On each tick:
        - LONG groups: check if price >= any group level (ascending check)
        - SHORT groups: check if price <= any group level (descending check)
    """

    def __init__(self) -> None:
        # Per instrument storage
        # instrument → {direction → sorted list of trigger levels}
        self._long_levels: dict[str, list[float]] = {}
        self._short_levels: dict[str, list[float]] = {}

        # instrument → (direction, rounded_level) → PriceGroup
        self._groups: dict[str, dict[tuple[str, float], PriceGroup]] = {}

    def rebuild(self, instrument: str, armed_variants: list[ArmedVariant]) -> int:
        """
        Rebuild groups for an instrument from its armed variants.
        Called on every candle close after the evaluator adds new armed variants.

        Returns the number of unique groups created.
        """
        # Clear existing groups for this instrument
        self._long_levels[instrument] = []
        self._short_levels[instrument] = []
        self._groups[instrument] = {}

        for av in armed_variants:
            # Only group price-based triggers (PRICE_LEVEL and STRUCTURE)
            # Both resolve to "price crosses a numeric level"
            if av.trigger_type not in (TriggerType.PRICE_LEVEL, TriggerType.STRUCTURE):
                continue

            level = round(av.trigger_value, 2)
            direction = av.direction
            key = (direction.value, level)

            if key not in self._groups[instrument]:
                # New group
                group = PriceGroup(
                    trigger_value=level,
                    direction=direction,
                )
                self._groups[instrument][key] = group

                # Insert into sorted level list
                if direction == Direction.LONG:
                    insort(self._long_levels[instrument], level)
                else:
                    insort(self._short_levels[instrument], level)

            # Add variant to existing group
            self._groups[instrument][key].members.append(av)

        group_count = len(self._groups.get(instrument, {}))
        return group_count

    def check_triggers(self, instrument: str, price: float) -> list[PriceGroup]:
        """
        Check if the current price triggers any groups.

        LONG groups: fire if price >= trigger_value (breakout above)
        SHORT groups: fire if price <= trigger_value (breakdown below)

        Returns list of triggered PriceGroups (caller should fire all members).
        """
        triggered: list[PriceGroup] = []
        groups = self._groups.get(instrument, {})

        if not groups:
            return triggered

        # Check LONG triggers: price crossed above the level
        long_levels = self._long_levels.get(instrument, [])
        for level in long_levels:
            if price >= level:
                key = (Direction.LONG.value, level)
                group = groups.get(key)
                if group:
                    triggered.append(group)
            else:
                # Sorted ascending — if price < this level, won't cross higher ones
                # Actually no — we need to check all because price could gap above multiple
                # But for efficiency, once price < level, remaining levels are higher
                break

        # Check SHORT triggers: price crossed below the level
        short_levels = self._short_levels.get(instrument, [])
        for level in reversed(short_levels):  # Check from highest down
            if price <= level:
                key = (Direction.SHORT.value, level)
                group = groups.get(key)
                if group:
                    triggered.append(group)
            else:
                # Sorted ascending, iterating high→low. If price > this level,
                # price is also > all remaining (lower) levels, so none will trigger.
                break

        return triggered

    def remove_group(self, instrument: str, direction: Direction, level: float) -> None:
        """Remove a triggered group (after all its members fired)."""
        level = round(level, 2)
        key = (direction.value, level)

        if instrument in self._groups and key in self._groups[instrument]:
            del self._groups[instrument][key]

        # Remove from sorted levels
        if direction == Direction.LONG and instrument in self._long_levels:
            try:
                self._long_levels[instrument].remove(level)
            except ValueError:
                pass
        elif direction == Direction.SHORT and instrument in self._short_levels:
            try:
                self._short_levels[instrument].remove(level)
            except ValueError:
                pass

    def get_group_count(self, instrument: str | None = None) -> int:
        """Get number of active groups."""
        if instrument:
            return len(self._groups.get(instrument, {}))
        return sum(len(g) for g in self._groups.values())

    def get_all_groups(self, instrument: str) -> list[PriceGroup]:
        """Get all groups for an instrument."""
        return list(self._groups.get(instrument, {}).values())

    def clear(self, instrument: str | None = None) -> None:
        """Clear groups (optionally for one instrument only)."""
        if instrument:
            self._long_levels.pop(instrument, None)
            self._short_levels.pop(instrument, None)
            self._groups.pop(instrument, None)
        else:
            self._long_levels.clear()
            self._short_levels.clear()
            self._groups.clear()
