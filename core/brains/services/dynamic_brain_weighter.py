"""Dynamic brain vote-weight service.

Maps BrainPerformanceTracker summaries to per-brain vote weights so that
ParliamentService._compute_consensus() naturally reduces influence of
underperforming brains and amplifies strong ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.feedback.brain_performance_tracker import BrainPerformanceTracker


class DynamicBrainWeighter:
    """Reads performance summaries and computes vote weights for each brain.

    Weights flow into BrainDecisionProposal.vote_weight, which
    ParliamentService._compute_consensus() already consumes via
    ``vote_weight * confidence * fallback_penalty``.

    Usage::

        weighter = DynamicBrainWeighter(tracker)
        weights = weighter.get_weights()        # {brain_id: float}
        weighter.apply_weights(proposals)       # sets vote_weight in-place
    """

    def __init__(self, performance_tracker: BrainPerformanceTracker) -> None:
        self._tracker = performance_tracker

    # ── public API ──

    def get_weights(self) -> dict[str, float]:
        """Return {brain_id: vote_weight} for every tracked brain."""
        summaries = self._tracker.get_all_summaries()
        weights: dict[str, float] = {}
        for s in summaries:
            weights[s["brain_id"]] = self._compute_weight(s)
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

    def _compute_weight(self, summary: dict) -> float:
        """Map a brain performance summary to a vote weight in [0.1, 3.0].

        Rationale
        --------
        - **insufficient_data** → 1.0  (neutral; not enough signal to judge)
        - **healthy / stable**   → scaled by composite_mean, range [0.5, 3.0]
        - **warning**            → 0.5 (damped but still voting)
        - **degraded / critical** → 0.1 (near-silent; governance should freeze)
        """
        health = summary.get("health_signal", "insufficient_data")
        composite = float(summary.get("composite_mean", 0.0))

        if health == "insufficient_data":
            return 1.0
        if health in ("critical", "degraded"):
            return 0.1
        if health == "warning":
            return 0.5

        # healthy or stable: linearly scale composite_mean ~[0, 1] → [0.5, 3.0]
        weight = 0.5 + composite * 2.5
        return round(max(0.1, min(3.0, weight)), 2)
