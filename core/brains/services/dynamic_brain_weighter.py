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
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

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

    # Two brains with sample_count within this fraction are candidates for redundancy
    _REDUNDANCY_COUNT_TOLERANCE = 0.05  # ±5%
    _REDUNDANCY_PNL_TOLERANCE = 0.15  # ±15%
    _REDUNDANCY_WR_TOLERANCE = 0.06  # ±6 percentage points
    _REDUNDANCY_MULTIPLIER_RANK2 = 0.65  # second brain in cluster
    _REDUNDANCY_MULTIPLIER_RANK3 = 0.45  # third+ brain in cluster

    def __init__(
        self,
        performance_tracker: BrainPerformanceTracker,
        pnl_store: BrainPnLStore | None = None,
        quality_engine: Any | None = None,
    ) -> None:
        self._tracker = performance_tracker
        self._pnl_store = pnl_store
        self._engine = quality_engine
        # Per-brain metadata (set by caller for redundancy detection)
        self._brain_meta: dict[str, dict[str, str]] = {}
        # Auto-wire singleton when no engine provided (production path)
        if self._engine is None and pnl_store is not None:
            try:
                from core.feedback.brain_quality_engine import BrainQualityEngine

                self._engine = BrainQualityEngine.instance()
            except Exception:  # BLE001:REVIEWED
                pass

    # ── public API ──

    def get_weights(self) -> dict[str, float]:
        """Return {brain_id: vote_weight} for every tracked brain.

        Uses P&L metrics when available, falling back to tracker summaries.
        Applies redundancy penalty so near-identical brains in the same
        contract group don't artificially inflate consensus.
        """
        weights: dict[str, float] = {}

        # Collect brain IDs from both sources
        brain_ids: set[str] = set()
        if self._pnl_store is not None:
            brain_ids.update(self._pnl_store.brain_ids)
        brain_ids.update(self._tracker.get_brain_ids())

        for brain_id in brain_ids:
            weights[brain_id] = self._compute_weight_for_brain(brain_id)

        # ── Redundancy penalty: discount near-identical brains ──
        if self._pnl_store is not None and self._brain_meta:
            metrics_map = {
                bid: self._pnl_store.get_metrics(bid)
                for bid in brain_ids
                if bid in self._brain_meta
            }
            weights = self._apply_redundancy_penalty(weights, metrics_map)

        return weights

    def apply_weights(self, proposals: list) -> list:
        """Set dynamic_scale on each proposal in-place and return the list.

        FIX-20260607-011: vote_weight is the config-level BASE permission
        (0.0=muted).  This method sets dynamic_scale — the PnL-based
        performance multiplier.  The final voting weight in contract_groups
        is base_weight × dynamic_scale.

        Frozen objects (e.g. BrainSignal) reject mutation — weights are
        still available via get_summary() / get_weights() for downstream
        consumers that accept a weighter reference.
        """
        weights = self.get_weights()
        for p in proposals:
            brain_id = getattr(p, "brain_id", "")
            if brain_id in weights:
                try:  # noqa: SIM105
                    p.dynamic_scale = weights[brain_id]
                except Exception:  # BLE001:REVIEWED
                    pass  # frozen object
        return proposals

    def get_summary(self, brain_id: str) -> dict:
        """Return a summary dict for the brain (used by contract_groups for weight lookups)."""
        if self._pnl_store is not None:
            metrics = self._pnl_store.get_metrics(brain_id)
            if metrics.sample_count >= 5:
                if self._engine is not None:
                    verdict = self._engine.assess(brain_id, metrics)
                    return {
                        "brain_id": brain_id,
                        "health_signal": verdict.quality_tier,
                        "composite_mean": verdict.score / 100.0,
                        "weight": verdict.vote_weight,
                    }
                return {
                    "brain_id": brain_id,
                    "health_signal": self.get_voting_tier(brain_id),
                    "composite_mean": min(max(metrics.sharpe_ratio, -5.0) / 5.0, 1.0),
                    "weight": self._compute_weight_from_metrics(metrics),
                }
        summary = self._tracker.get_brain_summary(brain_id)
        return {**summary, "weight": self._compute_weight(summary)}

    def get_weights_with_tiers(self) -> dict[str, dict[str, Any]]:
        """Return {brain_id: {weight, tier}} for governance monitoring."""
        result: dict[str, dict[str, Any]] = {}
        weights = self.get_weights()
        for brain_id in weights:
            tier = self.get_voting_tier(brain_id)
            if self._engine is not None and self._pnl_store is not None:
                metrics = self._pnl_store.get_metrics(brain_id)
                if metrics.sample_count >= 5:
                    tier = self._engine.assess(brain_id, metrics).quality_tier
            result[brain_id] = {
                "weight": weights[brain_id],
                "tier": tier,
            }
        return result

    def set_brain_metadata(self, brain_id: str, meta: dict[str, str]) -> None:
        """Register per-brain metadata for redundancy detection.

        ``meta`` should contain ``contract_group`` and optionally ``feature_schema``.
        Called once per brain during initialisation (before get_weights).
        """
        self._brain_meta[brain_id] = meta

    def _detect_redundant_clusters(self, metrics_map: Mapping[str, object]) -> list[list[str]]:
        """Group brains whose PnL profiles are near-identical → likely redundant.

        Two brains are considered redundant when they share the same contract_group
        AND their metrics agree within tight tolerances on sample_count, cumulative_pnl,
        and win_rate — meaning they were trained on the same data and produce the
        same directional votes.

        Returns a list of clusters, where each cluster is a list of brain_ids sorted
        by Sharpe ratio descending (best first).
        """
        brain_ids = list(metrics_map.keys())
        assigned: set[str] = set()
        clusters: list[list[str]] = []

        for i, bid_a in enumerate(brain_ids):
            if bid_a in assigned:
                continue
            meta_a = self._brain_meta.get(bid_a, {})
            group_a = meta_a.get("contract_group", "")
            if not group_a:
                continue
            m_a = metrics_map[bid_a]
            cluster = [bid_a]
            for j, bid_b in enumerate(brain_ids):
                if j <= i or bid_b in assigned:
                    continue
                meta_b = self._brain_meta.get(bid_b, {})
                group_b = meta_b.get("contract_group", "")
                if group_b != group_a:
                    continue
                m_b = metrics_map[bid_b]
                # Tolerance checks on PnL profile similarity
                if self._metrics_are_redundant(m_a, m_b):
                    cluster.append(bid_b)
                    assigned.add(bid_b)
            if len(cluster) >= 2:
                assigned.add(bid_a)
                # Sort by Sharpe descending (best first)
                cluster.sort(
                    key=lambda bid: getattr(metrics_map[bid], "sharpe_ratio", 0.0),
                    reverse=True,
                )
                clusters.append(cluster)

        return clusters

    def _metrics_are_redundant(self, m_a: object, m_b: object) -> bool:
        """Return True when two BrainPnLMetrics are near-identical in PnL profile."""
        count_a = getattr(m_a, "sample_count", 0)
        count_b = getattr(m_b, "sample_count", 0)
        pnl_a = getattr(m_a, "cumulative_pnl", 0.0)
        pnl_b = getattr(m_b, "cumulative_pnl", 0.0)
        wr_a = getattr(m_a, "win_rate", 0.0)
        wr_b = getattr(m_b, "win_rate", 0.0)

        if count_a == 0 or count_b == 0:
            return False
        count_diff = abs(count_a - count_b) / max(count_a, count_b)
        if count_diff > self._REDUNDANCY_COUNT_TOLERANCE:
            return False
        # Skip PnL check if both near zero
        if max(abs(pnl_a), abs(pnl_b)) > 0.5:
            pnl_diff = abs(pnl_a - pnl_b) / max(abs(pnl_a), abs(pnl_b), 0.01)
            if pnl_diff > self._REDUNDANCY_PNL_TOLERANCE:
                return False
        wr_diff = abs(wr_a - wr_b)
        if wr_diff > self._REDUNDANCY_WR_TOLERANCE:
            return False
        return True

    def _apply_redundancy_penalty(
        self, weights: dict[str, float], metrics_map: Mapping[str, object]
    ) -> dict[str, float]:
        """Discount weights of redundant brains within each PnL-profile cluster.

        The best brain (highest Sharpe) keeps full weight.  Subsequent brains in
        the same cluster are multiplied by _REDUNDANCY_MULTIPLIER_RANK2 (0.65)
        or _REDUNDANCY_MULTIPLIER_RANK3 (0.45).
        """
        clusters = self._detect_redundant_clusters(metrics_map)
        if not clusters:
            return weights

        penalized = dict(weights)
        for cluster in clusters:
            for rank, bid in enumerate(cluster):
                if rank == 0:
                    continue  # best brain → no penalty
                mult = (
                    self._REDUNDANCY_MULTIPLIER_RANK3
                    if rank >= 2
                    else self._REDUNDANCY_MULTIPLIER_RANK2
                )
                old_w = penalized.get(bid, 1.0)
                penalized[bid] = round(old_w * mult, 2)

        return penalized

    # ── internal ──

    def _compute_weight_for_brain(self, brain_id: str) -> float:
        """Compute vote weight for a brain, preferring P&L metrics."""
        if self._pnl_store is not None:
            metrics = self._pnl_store.get_metrics(brain_id)
            if metrics.sample_count >= 5:
                if self._engine is not None:
                    # ── Single source of truth: BrainQualityEngine ──
                    return self._engine.get_weight(brain_id, metrics)
                return self._compute_weight_from_metrics(metrics)

        # Fall back to tracker summary
        summary = self._tracker.get_brain_summary(brain_id)
        return self._compute_weight(summary)

    def _compute_weight_from_metrics(self, m: BrainPnLMetrics) -> float:
        """PnL-first vote weight with hard gates and health multipliers.

        Hard Gates (apply before multiplier):
          - trades >= 100, cumulative PnL < 0, PF < 0.60 or WR < 30%  → RETIRED (0)
          - trades >= 100, cumulative PnL < 0                         → PROBATION (×0.5)
          - trades < 30                                                → CANDIDATE (shadow only)

        Health Multiplier (applied after base_weight):
          - PnL > 0, WR >= 55%, PF >= 1.5  → HIGH_ALPHA  ×1.5
          - PnL > 0, WR >= 50%, PF >= 1.1  → POSITIVE    ×1.2
          - PnL ≈ 0 or trades < 30          → NEUTRAL     ×1.0
          - PnL < 0, trades < 100           → UNDER_REVIEW ×0.7
          - PnL < 0, trades >= 100          → NEGATIVE    ×0.5

        Returns weight in [0.0, 3.0].  Weight=0 means the brain cannot vote.
        """
        trades = m.sample_count
        pnl = m.cumulative_pnl
        wr = m.win_rate
        pf = m.profit_factor
        health = m.health_signal

        # ── Hard gate: auto-retirement (overrides all tiers) ──
        # Condition: trades >= 100, PnL < 0, AND (WR < 30% or PF < 0.60)
        if trades >= 100 and pnl < 0 and (wr < 0.30 or pf < 0.60):
            return 0.0

        # ── Health tier mapping (preserves original classification) ──
        if health == "insufficient_data":
            return 1.0
        if health == "critical":
            return 0.0
        if health == "degraded":
            return 0.25
        if health == "warning":
            return 0.5

        # ── PnL-first weighting for healthy/stable brains ──
        # Candidate gate: trades < 30 → shadow vote (too few samples to judge)
        if trades < 30:
            base_weight = 0.15
        elif trades >= 100 and pnl < 0:
            base_weight = 0.5  # probation
        else:
            base_weight = 0.5

        # Health multiplier (PnL-first additional scaling)
        if pnl > 0 and wr >= 0.55 and pf >= 1.5:
            health_mult = 1.5  # high_alpha
        elif pnl > 0 and wr >= 0.50 and pf >= 1.1:
            health_mult = 1.2  # positive
        elif pnl < 0 and trades >= 100:
            health_mult = 0.5  # negative_downgrade
        elif pnl < 0 and trades < 100:
            health_mult = 0.7  # under_review
        else:
            health_mult = 1.0  # neutral (no PnL signal)

        # Sharpe-driven continuous scaling (original formula core)
        sharpe = m.sharpe_ratio
        sharpe_factor = math.tanh(sharpe / 5.0)
        sharpe_factor = max(0.0, sharpe_factor)

        if health == "exceptional":
            weight = (base_weight * 2.5 + sharpe_factor * 2.5) * health_mult
        elif health == "healthy":
            weight = (base_weight * 2.0 + sharpe_factor * 2.0) * health_mult
        elif health == "marginal":
            weight = (base_weight * 0.5 + sharpe_factor * 1.0) * health_mult
        else:  # stable, warning, degraded — or unclassified
            weight = (base_weight + sharpe_factor * 2.0) * health_mult

        # Win rate modifier: ±15%
        if wr >= 0.55:
            weight *= 1.0 + min(wr - 0.55, 0.20) * 0.75
        elif wr < 0.45:
            weight *= max(0.85, 1.0 - (0.45 - wr) * 0.75)

        # Drawdown penalty
        if m.max_drawdown > 3.0:
            weight *= 0.85

        return round(max(0.0, min(3.0, weight)), 2)

    def get_voting_tier(self, brain_id: str) -> str:
        """Return the governance tier for a brain.

        Returns one of: "live" (full vote), "probation" (reduced),
        "candidate" (shadow only), "retired" (banned), "ungoverned" (no data).
        """
        metrics = None
        if self._pnl_store is not None:
            metrics = self._pnl_store.get_metrics(brain_id)

        if metrics is None or metrics.sample_count == 0:
            return "ungoverned"

        trades = metrics.sample_count
        pnl = metrics.cumulative_pnl
        wr = metrics.win_rate
        pf = metrics.profit_factor

        # Auto-retired
        if trades >= 100 and pnl < 0 and (wr < 0.30 or pf < 0.60):
            return "retired"
        # Probation
        if trades >= 100 and pnl < 0:
            return "probation"
        # Candidate
        if trades < 30:
            return "candidate"
        return "live"

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
