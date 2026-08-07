"""
CFD strategy implementations.

Importing this package registers every strategy defined in its modules with the
process-wide strategy registry (via the ``@register_strategy`` decorator), so a
runner can simply do ``import app.cfd_strategy.strategies`` and then
``get_registry().all()``.

Add new strategy modules here and import them below so they self-register.

NOTE: ``sma_cross`` is a DEMONSTRATION strategy only — it exists to exercise the
paper-trading plumbing end-to-end (feed -> candles -> strategy -> executor). It
is NOT a researched, profitable strategy and must not be used for real trading.
Replace/augment it with real strategies as they are developed, and use the
``CFD_PAPER_STRATEGIES`` env var to select exactly which strategy ids run.
"""

from __future__ import annotations

# Import strategy modules so their @register_strategy decorators run.
from app.cfd_strategy.strategies import sma_cross  # noqa: F401

__all__ = ["sma_cross"]
