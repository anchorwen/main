"""Dynamic brain vote-weight service.

Maps brain performance data (tracker summaries or P&L metrics) to per-brain
vote weights so that ParliamentService._compute_consensus() naturally reduces
influence of underperforming brains and amplifies strong ones.

Phase 2 (2026-05-06): Added BrainPnLStore integration — when real P&L metrics
(Sharpe, win_rate, drawdown) are available they replace the synthetic
composite_score from BrainPerformanceTracker.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.feedback.brain_performance_tracker import BrainPerformanceTracker
    from core.feedback.brain_pnl_ledger import BrainPnLMetrics, BrainPnLStore


class DynamicBrainWeighter:
    """Reads performance data and computes vote weights for each brain.

    Priority: BrainPnLStore (real P&L metrics) > BrainPerformanceTracker (synthetic scores).

    Weights flow into BrainDecisionProposal.vote_weight, which
    ParliamentService._compute_consensus() already consumes via
    ``vote_weight * confidence * fallback_penalty``.

    Usage::

        weighter = DynamicBrainWeighter(tracker, pnl_store=pnl_ledger)
        weights = weighter.get_weights()        # {brain_id: float}
        weighter.apply_weights(proposals)       # sets vote_weight in-place
    """

    def __init__(
        self,
        performance_tracker: BrainPerformanceTracker,
        pnl_store: BrainPnLStore | None = None,
    ) -> None:
        self._tracker = performance_tracker
        self._pnl_store = pnl_store

    # ── public API ──

    def get_weights(self) -> dict[str, float]:
        """Return {brain_id: vote_weight} for every tracked brain.

        Uses P&L metrics when available, falling back to tracker summaries.
        """
        weights: dict[str, float] = {}

        # Collect brain IDs from both sources
        brain_ids: set[str] = set()
        if self._pnl_store is not None:
            brain_ids.update(self._pnl_store.brain_ids)
        brain_ids.update(self._tracker.get_brain_ids())

        for brain_id in brain_ids:
            weights[brain_id] = self._compute_weight_for_brain(brain_id)

        return weights

    def apply_weights(self, proposals: list) -> list:
        """Set vote_weight on each proposal in-place and return the list."""
        weights = self.get_weights()
        for p in proposals:
            brain_id = getattr(p, "brain_id", "")
            if brain_id in weights:
                p.vote_weight = weights[brain_id]
        return proposals

    # ── internal ──

    def _compute_weight_for_brain(self, brain_id: str) -> float:
        """Compute vote weight for a brain, preferring P&L metrics."""
        if self._pnl_store is not None:
            metrics = self._pnl_store.get_metrics(brain_id)
            if metrics.sample_count >= 5:
                return self._compute_weight_from_metrics(metrics)

        # Fall back to tracker summary
        summary = self._tracker.get_brain_summary(brain_id)
        return self._compute_weight(summary)

    def _compute_weight_from_metrics(self, m: BrainPnLMetrics) -> float:
        """Map real P&L metrics to vote weight in [0.0, 3.0].

        Rationale
        ---------
        - **zero-win** (≥8 samples, 0% WR) → 0.0  (silenced — toxic signal)
        - **insufficient_data** → 1.0  (neutral)
        - **critical**          → 0.1  (near-silent)
        - **degraded**          → 0.25 (heavily damped)
        - **warning**           → 0.5  (half weight)
        - **stable**            → 0.5 + tanh(sharpe/5) * 2.0  → [0.5, 2.5]
        - **healthy**           → 1.0 + tanh(sharpe/5) * 2.0  → [1.0, 3.0]

        Within healthy/stable tiers, Sharpe ratio drives continuous scaling.
        A Sharpe of 0 → lower bound; Sharpe ≥ 5 → upper bound.
        Win rate acts as a secondary modifier (±15%).
        """
        # Zero-win detection: brain with enough data but 0% WR is toxic
        if m.sample_count >= 8 and m.win_rate <= 0.0:
            return 0.0

        health = m.health_signal

        if health == "insufficient_data":
            return 1.0
        if health == "critical":
            return 0.0
        if health == "degraded":
            return 0.25
        if health == "warning":
            return 0.5

        # healthy or stable: scale by Sharpe
        sharpe = m.sharpe_ratio
        sharpe_factor = math.tanh(sharpe / 5.0)  # [0, ~1] for realistic Sharpe range
        sharpe_factor = max(0.0, sharpe_factor)

        if health == "healthy":
            weight = 1.0 + sharpe_factor * 2.0  # [1.0, 3.0]
        else:  # stable
            weight = 0.5 + sharpe_factor * 2.0  # [0.5, 2.5]

        # Win rate modifier: ±15% adjustment
        wr = m.win_rate
        if wr >= 0.55:
            weight *= 1.0 + min(wr - 0.55, 0.20) * 0.75  # boost up to +15%
        elif wr < 0.45:
            weight *= max(0.85, 1.0 - (0.45 - wr) * 0.75)  # penalty down to -15%

        # Drawdown penalty: heavy drawdowns reduce weight
        if m.max_drawdown > 3.0:
            weight *= 0.85

        return round(max(0.1, min(3.0, weight)), 2)

    def _compute_weight(self, summary: dict) -> float:
        """Map a brain performance tracker summary to a vote weight in [0.0, 3.0].

        Used as fallback when P&L metrics are not available.

        Rationale
        --------
        - **insufficient_data** → 1.0  (neutral; not enough signal to judge)
        - **healthy / stable**   → scaled by composite_mean, range [0.5, 3.0]
        - **warning**            → 0.5 (damped but still voting)
        - **degraded**           → 0.1 (near-silent)
        - **critical / zero-win** → 0.0 (silenced; toxic signal)
        """
        health = summary.get("health_signal", "insufficient_data")
        composite = float(summary.get("composite_mean", 0.0))

        if health == "insufficient_data":
            return 1.0
        if health == "critical":
            return 0.0
        if health == "degraded":
            return 0.1
        if health == "warning":
            return 0.5

        # healthy or stable: linearly scale composite_mean ~[0, 1] → [0.5, 3.0]
        weight = 0.5 + composite * 2.5
        return round(max(0.1, min(3.0, weight)), 2)
