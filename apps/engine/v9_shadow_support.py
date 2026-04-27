from datetime import datetime

from core.contracts.domain.decision_candidate import DecisionCandidate
from core.parliament.schema_versions import SCHEMA_DECISION_CANDIDATE
from core.contracts.ids import new_candidate_id, new_snapshot_id


class StubFeatureService:
    def build_snapshot(self, trigger):
        from apps.engine.runtime_loop import SimpleFeatureSnapshot

        return SimpleFeatureSnapshot(
            snapshot_id=new_snapshot_id(),
            event_time=datetime.utcnow(),
            symbol=trigger.get("symbol", "XAUUSD"),
            venue="MT5",
        )


class V9ParliamentAdapter:
    def build_candidate(self, feature_snapshot, proposals, control_snapshot) -> DecisionCandidate:
        if not proposals:
            up_probability = 0.5
            down_probability = 0.5
            supporting_brains = []
        else:
            up_probability = sum(p.prediction.get("up_probability", 0.5) for p in proposals) / len(proposals)
            down_probability = sum(p.prediction.get("down_probability", 0.5) for p in proposals) / len(proposals)
            supporting_brains = [p.brain_id for p in proposals if p.prediction.get("direction_bias") != "neutral"]

        return DecisionCandidate(
            schema_version=SCHEMA_DECISION_CANDIDATE,
            candidate_id=new_candidate_id(),
            snapshot_id=feature_snapshot.snapshot_id,
            event_time=feature_snapshot.event_time,
            generated_at=datetime.utcnow(),
            regime_state={
                "primary_regime": "trend",
                "regime_confidence": 0.70,
            },
            consensus={
                "aggregated_bias": "long" if up_probability >= down_probability else "short",
                "consensus_score": max(up_probability, down_probability),
                "disagreement_score": abs(up_probability - down_probability),
            },
            supporting_brains=supporting_brains,
            opposing_brains=[],
            execution_feasibility={"is_feasible": True},
            risk_comments={"risk_bias": "acceptable"},
            candidate_summary={
                "symbol": feature_snapshot.symbol,
                "venue": feature_snapshot.venue,
                "up_probability": up_probability,
                "down_probability": down_probability,
                "expected_edge_bps": None,
                "expected_hold_seconds": None,
                "suggested_risk_fraction": 0.002,
            },
            trace={"parliament": "v9_shadow_stub"},
        )


