from core.governance.governance_service import GovernanceService


class TestGovernanceService:
    def test_register_brain(self):
        gs = GovernanceService()
        state = gs.register_brain("brain_001")
        assert state["status"] == "candidate"
        assert state["brain_id"] == "brain_001"

    def test_promote_candidate_to_live(self):
        gs = GovernanceService()
        gs.register_brain("brain_001", "candidate")
        result = gs.transition("brain_001", "live", "good_performance")
        assert result["action"] == "transitioned"
        assert result["to"] == "live"

    def test_demote_live_to_probation(self):
        gs = GovernanceService()
        gs.register_brain("brain_001", "live")
        result = gs.transition("brain_001", "probation", "degraded")
        assert result["action"] == "transitioned"
        assert result["to"] == "probation"

    def test_freeze_live(self):
        gs = GovernanceService()
        gs.register_brain("brain_001", "live")
        result = gs.transition("brain_001", "frozen", "critical")
        assert result["action"] == "transitioned"
        state = gs.get_brain_state("brain_001")
        assert state is not None
        assert state["freeze_count"] == 1

    def test_invalid_transition_rejected(self):
        gs = GovernanceService()
        gs.register_brain("brain_001", "retired")
        result = gs.transition("brain_001", "live", "attempt")
        assert result["action"] == "rejected"

    def test_apply_recommendation_freeze(self):
        gs = GovernanceService()
        gs.register_brain("brain_001", "live")
        result = gs.apply_recommendation("brain_001", "freeze", "critical_health")
        assert result["action"] == "transitioned"
        state = gs.get_brain_state("brain_001")
        assert state is not None
        assert state["status"] == "frozen"

    def test_apply_recommendation_promote(self):
        gs = GovernanceService()
        gs.register_brain("brain_001", "candidate")
        result = gs.apply_recommendation("brain_001", "eligible_for_promotion")
        assert result["to"] == "live"

    def test_active_brain_ids(self):
        gs = GovernanceService()
        gs.register_brain("a", "live")
        gs.register_brain("b", "frozen")
        gs.register_brain("c", "candidate")
        gs.register_brain("d", "retired")
        active = gs.get_active_brain_ids()
        assert set(active) == {"a", "c"}

    def test_process_feedback_signals(self):
        gs = GovernanceService()
        gs.register_brain("brain_bad", "live")
        gs.register_brain("brain_good", "candidate")
        signals = [
            {"brain_id": "brain_bad", "recommendation": "freeze", "health_signal": "critical"},
            {
                "brain_id": "brain_good",
                "recommendation": "eligible_for_promotion",
                "health_signal": "healthy",
            },
        ]
        results = gs.process_feedback_signals(signals)
        assert len(results) == 2
        state_bad = gs.get_brain_state("brain_bad")
        assert state_bad is not None
        assert state_bad["status"] == "frozen"
        state_good = gs.get_brain_state("brain_good")
        assert state_good is not None
        assert state_good["status"] == "live"

    def test_transition_log(self):
        gs = GovernanceService()
        gs.register_brain("a", "candidate")
        gs.transition("a", "live", "promote")
        gs.transition("a", "probation", "warning")
        log = gs.get_transition_log()
        # FIX-20260529-034: register_brain() now appends 1 entry + 2 transitions = 3
        assert len(log) == 3
        # Entry 0: registration
        assert log[0]["from"] is None
        assert log[0]["to"] == "candidate"
        # Entry 1: candidate → live
        assert log[1]["from_status"] == "candidate"
        assert log[1]["to_status"] == "live"
        # Entry 2: live → probation
        assert log[2]["from_status"] == "live"
        assert log[2]["to_status"] == "probation"
