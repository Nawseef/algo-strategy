"""
Variant generation and evaluation engine.

This module handles the 150K variant combinatorial system:
- Variant definition (strategy + timeframe + filter set)
- Variant generation (cartesian product of all filter dimensions)
- Batch evaluation at candle close
"""
