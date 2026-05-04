from datetime import UTC, datetime

from core.contracts.domain.decision_candidate import DecisionCandidate
from core.contracts.ids import new_candidate_id
from core.parliament.schema_versions import SCHEMA_DECISION_CANDIDATE


class ParliamentService:
    """Multi-brain deliberation service.

    Aggregates proposals from all active brains, applies weighting
    based on brain role/health, detects consensus/dissent, and
    produces a DecisionCandidate for the downstream compiler.
    """

    def __init__(self, governance_service=None, regime_detector=None):
        self._governance = governance_service
        self._regime_detector = regime_detector

    def build_candidate(
        self,
        feature_snapshot,
        proposals: list,
        control_snapshot,
    ) -> DecisionCandidate:
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
            weight = confidence * (1.0 if runtime_ok else 0.5)
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

        if neutral_count > long_count and neutral_count > short_count:
            bias = "neutral"
            score = max(weighted_up, weighted_down)
        elif weighted_up >= weighted_down:
            bias = "long"
            score = weighted_up
        else:
            bias = "short"
            score = weighted_down

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

    def _build_summary(self, feature_snapshot, consensus: dict, proposals: list) -> dict:
        bias = consensus["aggregated_bias"]
        score = consensus.get("consensus_score", 0.5)

        if bias == "neutral":
            up_prob = down_prob = score
        else:
            up_prob = score if bias == "long" else 1 - score
            down_prob = score if bias == "short" else 1 - score

        return {
            "symbol": feature_snapshot.symbol,
            "venue": getattr(feature_snapshot, "venue", "unknown"),
            "up_probability": up_prob,
            "down_probability": down_prob,
            "suggested_risk_fraction": 0.002,
            "proposal_count": len(proposals),
        }
