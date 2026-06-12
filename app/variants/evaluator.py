"""
Batch Variant Evaluator — the core of the candle-close evaluation pipeline.

On every candle close (per instrument, per timeframe):
1. Get the IndicatorSnapshot (already computed by IndicatorEngine)
2. Evaluate ALL strategy templates once → produce CandidateSignals
3. For each candidate, evaluate ALL filter sets that match that strategy+timeframe
4. Variants that pass both strategy + filters → become ARMED (or immediate trade)

Key optimization:
- Strategy templates are evaluated ONCE per strategy (not per variant)
- Only the filter evaluation runs 150K times
- Filters short-circuit on first failure

This is the "150K evaluation per candle close" from File 4.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.models import Candle
from app.utils.logger import get_logger
from app.variants.filter_engine import evaluate_filters
from app.variants.generator import STRATEGY_EXPIRY_CANDLES
from app.variants.models import (
    ArmedVariant,
    Direction,
    EntryMode,
    IndicatorSnapshot,
    MetadataSnapshot,
    ResearchTimeframe,
    StrategyType,
    Variant,
)
from app.variants.strategies.base_template import BaseStrategyTemplate, CandidateSignal

logger = get_logger(__name__)


@dataclass
class EvaluationResult:
    """Result of a single candle-close evaluation cycle."""

    instrument: str
    timeframe: ResearchTimeframe
    total_variants: int = 0
    filters_passed: int = 0
    candidates_produced: int = 0
    armed_variants: list[ArmedVariant] = field(default_factory=list)
    immediate_trades: list[tuple[Variant, CandidateSignal]] = field(default_factory=list)
    eval_time_ms: float = 0.0


class VariantEvaluator:
    """
    Batch evaluator for the 150K variant system.

    Architecture:
        1. Group variants by (strategy, timeframe) for efficient evaluation
        2. Evaluate strategy template ONCE → get CandidateSignals
        3. For each candidate direction, filter all matching variants
        4. Output: armed variants (INTRABAR) or immediate trades (CANDLE_CLOSE)

    Usage:
        evaluator = VariantEvaluator(variants, templates)
        result = evaluator.evaluate(
            instrument="2885",
            timeframe=ResearchTimeframe.M5,
            candle=candle,
            history=history,
            snapshot=snapshot,
            metadata=metadata,
            candle_index=42,
        )
    """

    def __init__(
        self,
        variants: list[Variant],
        templates: dict[StrategyType, BaseStrategyTemplate],
    ) -> None:
        self._templates = templates

        # Pre-group variants by (strategy, timeframe) for O(1) lookup
        # This avoids iterating all 150K every time — only the relevant subset
        self._variant_groups: dict[tuple[StrategyType, ResearchTimeframe], list[Variant]] = {}
        for v in variants:
            key = (v.strategy, v.timeframe)
            if key not in self._variant_groups:
                self._variant_groups[key] = []
            self._variant_groups[key].append(v)

        self._total_variants = len(variants)

        logger.info(
            "VariantEvaluator initialized: %d variants in %d groups",
            self._total_variants,
            len(self._variant_groups),
        )

    def evaluate(
        self,
        instrument: str,
        timeframe: ResearchTimeframe,
        candle: Candle,
        history: list[Candle],
        snapshot: IndicatorSnapshot,
        metadata: MetadataSnapshot,
        candle_index: int = 0,
    ) -> EvaluationResult:
        """
        Evaluate all variants for a given instrument + timeframe on candle close.

        Steps:
            1. For each strategy template, call evaluate() → CandidateSignals
            2. For each candidate, check all variants with matching strategy+timeframe
            3. Apply filter evaluation to each variant
            4. Separate into armed (INTRABAR) vs immediate (CANDLE_CLOSE)

        Args:
            instrument: Exchange token (e.g. "2885")
            timeframe: Which timeframe's candle just closed
            candle: The just-completed candle
            history: Candle history for this instrument/timeframe
            snapshot: Pre-computed indicator values
            metadata: Market context metadata
            candle_index: Running candle counter (for armed expiry tracking)

        Returns:
            EvaluationResult with armed variants and immediate trades
        """
        t0 = time.perf_counter()

        result = EvaluationResult(
            instrument=instrument,
            timeframe=timeframe,
        )

        # For each strategy, evaluate the template and then filter variants
        for strategy_type, template in self._templates.items():
            # Get the variant group for this (strategy, timeframe)
            group_key = (strategy_type, timeframe)
            variants_in_group = self._variant_groups.get(group_key, [])

            if not variants_in_group:
                continue

            result.total_variants += len(variants_in_group)

            # Step 1: Evaluate strategy template ONCE
            candidates = template.evaluate(
                timeframe=timeframe,
                candle=candle,
                history=history,
                snapshot=snapshot,
                metadata=metadata,
            )

            if not candidates:
                continue

            result.candidates_produced += len(candidates)

            # Step 2: For each candidate, filter all variants in this group
            for candidate in candidates:
                self._process_candidate(
                    candidate=candidate,
                    variants=variants_in_group,
                    snapshot=snapshot,
                    instrument=instrument,
                    candle_index=candle_index,
                    strategy_type=strategy_type,
                    result=result,
                )

        result.eval_time_ms = (time.perf_counter() - t0) * 1000
        return result

    def _process_candidate(
        self,
        candidate: CandidateSignal,
        variants: list[Variant],
        snapshot: IndicatorSnapshot,
        instrument: str,
        candle_index: int,
        strategy_type: StrategyType,
        result: EvaluationResult,
    ) -> None:
        """
        Process a single CandidateSignal against all variants in a group.

        For each variant that passes filter evaluation:
        - CANDLE_CLOSE mode → add to immediate_trades
        - INTRABAR mode → add to armed_variants
        """
        expiry = STRATEGY_EXPIRY_CANDLES.get(strategy_type, 3)

        for variant in variants:
            # Filter evaluation (the hot loop — must be fast)
            if not evaluate_filters(variant.filters, snapshot):
                continue

            result.filters_passed += 1

            if candidate.entry_mode == EntryMode.CANDLE_CLOSE:
                # Immediate trade — no tick watching needed
                result.immediate_trades.append((variant, candidate))
            else:
                # INTRABAR — create armed variant for tick monitoring
                armed = ArmedVariant(
                    variant=variant,
                    instrument=instrument,
                    direction=candidate.direction,
                    trigger_type=candidate.trigger_type,
                    trigger_value=candidate.trigger_value,
                    armed_at_candle=candle_index,
                    expiry_candles=expiry,
                    entry_price_hint=candidate.entry_price_hint,
                    metadata=candidate.metadata,
                )
                result.armed_variants.append(armed)

    def get_group_count(self, strategy: StrategyType, timeframe: ResearchTimeframe) -> int:
        """Get number of variants in a specific group."""
        return len(self._variant_groups.get((strategy, timeframe), []))
