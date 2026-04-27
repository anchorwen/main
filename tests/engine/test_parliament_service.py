from datetime import datetime

from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
from core.governance.governance_service import GovernanceService
from core.parliament.parliament_service import ParliamentService


def _proposal(brain_id, direction="long", confidence=0.8, up=0.8, down=0.2):
    return BrainDecisionProposal(
        schema_version="v1", proposal_id=f"p_{brain_id}",
        snapshot_id="s1", brain_id=brain_id, brain_role="primary",
        brain_status="live", model_version="v1",
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        generated_at=datetime(2026, 4, 24, 12, 0, 1),
        prediction={
            "direction_bias": direction,
            "up_probability": up,
            "down_probability": down,
            "confidence": confidence,
        },
        health={"fallback_used": False, "risk_score": 0.3},
    )


def _snapshot(mode="normal"):
    return type("ControlSnapshot", (), {
        "mode_state": type("MS", (), {"current_mode": type("M", (), {"value": mode})()})(),
        "active_overrides": [],
    })()


def _feature():
    return type("FS", (), {
        "snapshot_id": "s1", "event_time": datetime(2026, 4, 24, 12, 0, 0),
        "symbol": "XAUUSD", "venue": "MT5",
    })()


class TestParliamentService:
    def test_build_candidate_basic(self):
        ps = ParliamentService()
        proposals = [_proposal("brain_a"), _proposal("brain_b")]
        c = ps.build_candidate(_feature(), proposals, _snapshot())
        assert c.candidate_id
        assert c.consensus["aggregated_bias"] in {"long", "short"}
        assert c.consensus["voter_count"] == 2
        assert len(c.supporting_brains) > 0

    def test_empty_proposals(self):
        ps = ParliamentService()
        c = ps.build_candidate(_feature(), [], _snapshot())
        assert c.consensus["aggregated_bias"] == "neutral"
        assert c.consensus["voter_count"] == 0
        assert c.execution_feasibility["is_feasible"] is False

    def test_consensus_with_disagreement(self):
        ps = ParliamentService()
        proposals = [
            _proposal("a", "long", 0.9, up=0.9, down=0.1),
            _proposal("b", "short", 0.7, up=0.3, down=0.7),
            _proposal("c", "long", 0.8, up=0.8, down=0.2),
        ]
        c = ps.build_candidate(_feature(), proposals, _snapshot())
        assert c.consensus["long_count"] == 2
        assert c.consensus["short_count"] == 1
        assert len(c.opposing_brains) >= 1

    def test_governance_filters_frozen(self):
        gs = GovernanceService()
        gs.register_brain("a", "live")
        gs.register_brain("b", "frozen")
        ps = ParliamentService(governance_service=gs)
        proposals = [_proposal("a"), _proposal("b")]
        c = ps.build_candidate(_feature(), proposals, _snapshot())
        assert c.trace["proposal_count"] == 1

    def test_halted_mode_not_feasible(self):
        ps = ParliamentService()
        c = ps.build_candidate(_feature(), [_proposal("a")], _snapshot("halted"))
        assert c.execution_feasibility["is_feasible"] is False

    def test_risk_comments_elevated(self):
        ps = ParliamentService()
        p = _proposal("a")
        p.health["risk_score"] = 0.8
        c = ps.build_candidate(_feature(), [p], _snapshot())
        assert c.risk_comments["risk_bias"] == "elevated"
