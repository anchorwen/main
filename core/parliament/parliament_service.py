import warnings
from datetime import UTC, datetime

from core.contracts.domain.decision_candidate import DecisionCandidate
from core.contracts.ids import new_candidate_id
from core.parliament.schema_versions import SCHEMA_DECISION_CANDIDATE


class ParliamentService:
    """Multi-brain deliberation service (legacy — DEPRECATED for live trading).

    **For live multi-brain trading**, use the contract-group architecture:
      - ``core.parliament.contract_groups.compute_all_group_signals()``
      - ``core.execution.capital_allocator.resolve_conflicts()``

    This class is retained for:
      - Shadow mode (apps/engine/v9_shadow_support.py)
      - Backward-compatible testing
      - Single-brain mode (trivially compatible with both paths)

    The key problem with the legacy approach: it mixed incommensurate
    confidence values (softmax vs tanh vs sigmoid) across models trained
    on different contracts into a single weighted average.  The new
    contract-group approach fixes this by voting within homogeneous
    groups and resolving cross-group conflicts at the capital-allocation
    level.
    """

    _DEPRECATION_SHOWN = False

    def __init__(self, governance_service=None, regime_detector=None):
        self._governance = governance_service
        self._regime_detector = regime_detector

    def build_candidate(
        self,
        feature_snapshot,
        proposals: list,
        control_snapshot,
    ) -> DecisionCandidate:
        """Build a DecisionCandidate via cross-contract mixed voting.

        **Deprecated for live trading.**  Use the contract-group
        architecture instead: ``compute_all_group_signals()`` →
        ``resolve_conflicts()``.

        Still used by the shadow runtime (apps/engine/).
        """
        if not ParliamentService._DEPRECATION_SHOWN:
            warnings.warn(
                "ParliamentService.build_candidate() is deprecated for live "
                "trading. Use core.parliament.contract_groups.compute_all_group_signals() "
                "and core.execution.capital_allocator.resolve_conflicts() instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            ParliamentService._DEPRECATION_SHOWN = True

        active_proposals = self._filter_active_proposals(proposals)

        regime_state = self._detect_regime(feature_snapshot)
        consensus = self._compute_consensus(active_proposals)
        supporting, opposing = self._classify_brains(active_proposals, consensus)
        feasibility = self._assess_feasibility(active_proposals, control_snapshot)

        return DecisionCandidate(
            schema_version=SCHEMA_DECISION_CANDIDATE,
            candidate_id=new_candidate_id(),
            snapshot_id=feature_snapshot.snapshot_id,
            event_time=feature_snapshot.event_time,
            generated_at=datetime.now(UTC).replace(tzinfo=None),
            regime_state=regime_state,
            consensus=consensus,
            supporting_brains=supporting,
            opposing_brains=opposing,
            execution_feasibility=feasibility,
            risk_comments=self._build_risk_comments(active_proposals),
            candidate_summary=self._build_summary(
                feature_snapshot,
                consensus,
                active_proposals,
            ),
            trace={"parliament_version": "v1", "proposal_count": len(active_proposals)},
        )

    def _filter_active_proposals(self, proposals: list) -> list:
        if self._governance is None:
            return list(proposals)
        active_ids = set(self._governance.get_active_brain_ids())
        return [p for p in proposals if p.brain_id in active_ids or not active_ids]

    def _detect_regime(self, feature_snapshot) -> dict:
        if self._regime_detector is not None:
            return self._regime_detector.detect(feature_snapshot)
        return {"primary_regime": "trend", "regime_confidence": 0.70}

    def _compute_consensus(self, proposals: list) -> dict:
        if not proposals:
            return {
                "aggregated_bias": "neutral",
                "consensus_score": 0.5,
                "disagreement_score": 0.0,
                "voter_count": 0,
            }

        up_scores = []
        down_scores = []
        weights = []

        for p in proposals:
            pred = p.prediction or {}
            up = pred.get("up_probability", 0.5)
            down = pred.get("down_probability", 0.5)
            confidence = pred.get("confidence", 0.5)
            health = p.health or {}
            runtime_ok = not health.get("fallback_used", False)
            vote_weight = getattr(p, "vote_weight", 1.0) or 1.0
            weight = vote_weight * confidence * (1.0 if runtime_ok else 0.5)
            up_scores.append(up * weight)
            down_scores.append(down * weight)
            weights.append(weight)

        total_weight = sum(weights) or 1.0
        weighted_up = sum(up_scores) / total_weight
        weighted_down = sum(down_scores) / total_weight

        biases = [p.prediction.get("direction_bias", "neutral") for p in proposals]
        long_count = biases.count("long")
        short_count = biases.count("short")
        neutral_count = biases.count("neutral")
        total = len(biases)
        majority_ratio = max(long_count, short_count) / total if total else 0

        # Use weighted scores to determine bias direction.
        # When neutral votes dominate (e.g. 2 neutral, 1 long, 1 short),
        # the weighted scores still express a directional preference —
        # honour that preference instead of forcing a neutral deadlock.
        if weighted_up >= weighted_down:
            bias = "long"
            raw_score = weighted_up
        else:
            bias = "short"
            raw_score = weighted_down

        # Apply neutral-uncertainty penalty when neutral votes are the
        # largest bloc.  The penalty reduces conviction but preserves
        # direction so the downstream gate can still decide.
        if neutral_count > long_count and neutral_count > short_count:
            neutral_ratio = neutral_count / total if total else 0
            # Scale penalty by how dominant the neutral bloc is.
            # 2/4 neutral → 0.85; 3/4 neutral → 0.70; 4/4 neutral → 0.55
            neutral_penalty = max(0.50, 1.0 - neutral_ratio * 0.30)
            raw_score *= neutral_penalty

        # Blend raw probability score with majority agreement strength.
        # A 2v2 deadlock (majority_ratio=0.5) stays near ~0.5; 3/4 agreement
        # (majority_ratio=0.75) boosts the score into actionable territory.
        majority_weight = 0.35
        score = raw_score * (1.0 - majority_weight) + majority_ratio * majority_weight

        return {
            "aggregated_bias": bias,
            "consensus_score": round(score, 4),
            "disagreement_score": round(abs(weighted_up - weighted_down), 4),
            "voter_count": total,
            "majority_ratio": round(majority_ratio, 4),
            "long_count": long_count,
            "short_count": short_count,
            "neutral_count": neutral_count,
        }

    def _classify_brains(self, proposals: list, consensus: dict) -> tuple[list[str], list[str]]:
        bias = consensus.get("aggregated_bias", "neutral")
        supporting = []
        opposing = []
        for p in proposals:
            direction = p.prediction.get("direction_bias", "neutral")
            if direction == bias:
                supporting.append(p.brain_id)
            elif direction != "neutral":
                opposing.append(p.brain_id)
        return supporting, opposing

    def _assess_feasibility(self, proposals: list, control_snapshot) -> dict:
        if not proposals:
            return {"is_feasible": False, "reason": "no_proposals"}

        mode = control_snapshot.mode_state.current_mode
        mode_val = mode.value if hasattr(mode, "value") else str(mode)
        if mode_val in {"halted", "observe_only"}:
            return {"is_feasible": False, "reason": f"mode_{mode_val}"}

        return {"is_feasible": True, "reason": "ok"}

    def _build_risk_comments(self, proposals: list) -> dict:
        risk_scores = []
        for p in proposals:
            h = p.health or {}
            r = h.get("risk_score")
            if r is not None:
                risk_scores.append(r)
        avg_risk = sum(risk_scores) / len(risk_scores) if risk_scores else None
        return {
            "risk_bias": "acceptable" if avg_risk is None or avg_risk < 0.6 else "elevated",
            "avg_risk_score": round(avg_risk, 4) if avg_risk is not None else None,
        }

    # ── Bridge to new contract-group architecture ──────────────────────

    def group_consensus(self, group_definition: dict, proposals: list, dynamic_weighter=None):
        """Compute consensus for a single contract-homogeneous group.

        Convenience method that delegates to ``ContractGroupConsensus``.
        Use this when you have proposals that are already grouped by
        training contract.

        Returns a ``GroupSignal`` or ``None``.
        """
        from core.parliament.contract_groups import ContractGroupConsensus

        cgc = ContractGroupConsensus(group_definition)
        return cgc.compute(proposals, dynamic_weighter)

    # ── Internal helpers ───────────────────────────────────────────────

    def _build_summary(self, feature_snapshot, consensus: dict, proposals: list) -> dict:
        bias = consensus["aggregated_bias"]
        score = consensus.get("consensus_score", 0.5)

        if bias == "neutral":
            up_prob = down_prob = score
        else:
            # Clamp the directional probability to [0.5, 1.0] so a
            # near-zero consensus score doesn't produce a spurious
            # near-certain opposite-direction signal (e.g. 1 - 0.02 = 0.98
            # short when the only brain voted neutral).
            directional_prob = max(score, 0.5)
            if bias == "long":
                up_prob = directional_prob
                down_prob = 1.0 - directional_prob
            else:  # short
                down_prob = directional_prob
                up_prob = 1.0 - directional_prob

        return {
            "symbol": feature_snapshot.symbol,
            "venue": getattr(feature_snapshot, "venue", "unknown"),
            "up_probability": up_prob,
            "down_probability": down_prob,
            "suggested_risk_fraction": 0.002,
            "proposal_count": len(proposals),
        }
