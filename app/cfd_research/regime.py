"""
Market-condition (regime) tagging — a deliberately SIMPLE classifier.

For research slicing we label each trade's entry with the market condition it
fired in, so the scorer can answer "this edge only works in trends" or "only in
range". We keep it to a small, defensible set — NOT a research project — because
the goal is to SLICE results, not to build a regime-prediction model.

Labels:
    "trend_up"    — directional up move (ADX >= threshold, fast EMA above slow)
    "trend_down"  — directional down move (ADX >= threshold, fast EMA below slow)
    "range"       — no clear direction (ADX below threshold)
    "unknown"     — not enough history to classify

Volatility bucket (separate tag) via ATR vs its own recent median:
    "loVol" | "normalVol" | "hiVol"

These two combine in the slice scorer (e.g. group by regime, or regime+vol).
"""

from __future__ import annotations

from app.core.models import Candle
from app.strategy.indicators import adx, atr, atr_series, ema


def classify_regime(
    candles: list[Candle],
    adx_period: int = 14,
    ema_fast: int = 20,
    ema_slow: int = 50,
    adx_trend_threshold: float = 22.0,
) -> str:
    """Trend/range label from the history UP TO (and including) the entry bar."""
    if len(candles) < ema_slow + 1:
        return "unknown"
    closes = [c.close for c in candles]
    ef = ema(closes, ema_fast)
    es = ema(closes, ema_slow)
    adx_val = adx(candles, adx_period)
    if ef is None or es is None or adx_val is None:
        return "unknown"
    if adx_val >= adx_trend_threshold:
        return "trend_up" if ef >= es else "trend_down"
    return "range"


def classify_volatility(
    candles: list[Candle],
    atr_period: int = 14,
    lookback: int = 100,
    lo: float = 0.8,
    hi: float = 1.25,
) -> str:
    """Volatility bucket: current ATR vs the median ATR over ``lookback`` bars."""
    if len(candles) < atr_period + 5:
        return "unknown"
    cur = atr(candles, atr_period)
    if cur is None or cur <= 0:
        return "unknown"
    series = [v for v in atr_series(candles[-lookback:], atr_period) if v and v > 0]
    if len(series) < 5:
        return "unknown"
    ordered = sorted(series)
    median = ordered[len(ordered) // 2]
    if median <= 0:
        return "unknown"
    ratio = cur / median
    if ratio <= lo:
        return "loVol"
    if ratio >= hi:
        return "hiVol"
    return "normalVol"
