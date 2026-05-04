from datetime import datetime

from core.contracts.domain.decision_intent import DecisionIntent
from core.contracts.enums import DecisionAction, DecisionSide, RiskDecisionStatus
from core.protocol.schema_versions import SCHEMA_DECISION_INTENT
from core.risk.risk_evaluation_service import RiskEvaluationService
from core.risk.risk_policies import (
    ConcentrationPolicy,
    DrawdownPolicy,
    ExposurePolicy,
    ModePolicy,
    PositionLimitPolicy,
)


def _intent(action=DecisionAction.OPEN, symbol="XAUUSD", conviction=0.8):
    side = (
        DecisionSide.FLAT
        if action in {DecisionAction.ABSTAIN, DecisionAction.OBSERVE}
        else DecisionSide.LONG
    )
    return DecisionIntent(
        schema_version=SCHEMA_DECISION_INTENT,
        intent_id="intent_001",
        candidate_id="candidate_001",
        snapshot_id="snapshot_001",
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        compiled_at=datetime(2026, 4, 24, 12, 0, 1),
        symbol=symbol,
        venue="MT5",
        action=action,
        side=side,
        conviction=conviction,
        priority="high",
    )


def _snapshot(mode="normal"):
    return type(
        "ControlSnapshot",
        (),
        {
            "mode_state": type(
                "ModeState",
                (),
                {
                    "current_mode": type("Mode", (), {"value": mode})(),
                },
            )(),
            "active_overrides": [],
        },
    )()


class TestRiskEvaluationServiceBasic:
    def test_passive_intent_abstain_denied(self):
        svc = RiskEvaluationService()
        v = svc.evaluate(_intent(DecisionAction.ABSTAIN), _snapshot())
        assert v.status == RiskDecisionStatus.DENY
        assert "passive_intent_abstain" in v.blocking_reasons

    def test_passive_intent_observe_allowed(self):
        svc = RiskEvaluationService()
        v = svc.evaluate(_intent(DecisionAction.OBSERVE), _snapshot())
        assert v.status == RiskDecisionStatus.ALLOW
        assert v.risk_tier == "minimal"
        assert "observe_intent_no_risk" in v.warning_reasons

    def test_no_policies_allows(self):
        svc = RiskEvaluationService()
        v = svc.evaluate(_intent(), _snapshot())
        assert v.status == RiskDecisionStatus.ALLOW
        assert v.risk_tier == "standard"

    def test_single_allow_policy(self):
        svc = RiskEvaluationService([PositionLimitPolicy(max_open_positions=10)])
        v = svc.evaluate(_intent(), _snapshot(), context={"open_position_count": 3})
        assert v.status == RiskDecisionStatus.ALLOW

    def test_verdict_contains_policy_trace(self):
        svc = RiskEvaluationService([PositionLimitPolicy()])
        v = svc.evaluate(_intent(), _snapshot(), context={"open_position_count": 0})
        assert "policy_results" in v.trace
        assert len(v.trace["policy_results"]) == 1
        assert v.trace["policy_results"][0]["policy"] == "position_limit"


class TestPositionLimitPolicy:
    def test_allows_when_under_limit(self):
        svc = RiskEvaluationService([PositionLimitPolicy(max_open_positions=5)])
        v = svc.evaluate(_intent(), _snapshot(), context={"open_position_count": 4})
        assert v.status == RiskDecisionStatus.ALLOW

    def test_denies_when_at_limit(self):
        svc = RiskEvaluationService([PositionLimitPolicy(max_open_positions=5)])
        v = svc.evaluate(_intent(), _snapshot(), context={"open_position_count": 5})
        assert v.status == RiskDecisionStatus.DENY
        assert any("position_limit" in r for r in v.blocking_reasons)

    def test_allows_close_even_at_limit(self):
        svc = RiskEvaluationService([PositionLimitPolicy(max_open_positions=5)])
        v = svc.evaluate(
            _intent(DecisionAction.CLOSE), _snapshot(), context={"open_position_count": 10}
        )
        assert v.is_allowed()


class TestDrawdownPolicy:
    def test_allows_under_threshold(self):
        svc = RiskEvaluationService([DrawdownPolicy(max_drawdown_pct=5.0)])
        v = svc.evaluate(_intent(), _snapshot(), context={"current_drawdown_pct": 2.0})
        assert v.status == RiskDecisionStatus.ALLOW

    def test_limited_near_threshold(self):
        svc = RiskEvaluationService([DrawdownPolicy(max_drawdown_pct=5.0)])
        v = svc.evaluate(_intent(), _snapshot(), context={"current_drawdown_pct": 4.5})
        assert v.status == RiskDecisionStatus.ALLOW_LIMITED
        assert v.constraints.get("max_risk_fraction") == 0.5

    def test_denies_open_at_threshold(self):
        svc = RiskEvaluationService([DrawdownPolicy(max_drawdown_pct=5.0)])
        v = svc.evaluate(_intent(), _snapshot(), context={"current_drawdown_pct": 5.5})
        assert v.status == RiskDecisionStatus.DENY

    def test_force_reduce_close_at_threshold(self):
        svc = RiskEvaluationService([DrawdownPolicy(max_drawdown_pct=5.0)])
        v = svc.evaluate(
            _intent(DecisionAction.CLOSE), _snapshot(), context={"current_drawdown_pct": 5.5}
        )
        assert v.status == RiskDecisionStatus.FORCE_REDUCE


class TestExposurePolicy:
    def test_allows_under_limit(self):
        svc = RiskEvaluationService([ExposurePolicy(max_notional=1_000_000)])
        v = svc.evaluate(_intent(), _snapshot(), context={"current_notional_exposure": 500_000})
        assert v.status == RiskDecisionStatus.ALLOW

    def test_denies_over_limit(self):
        svc = RiskEvaluationService([ExposurePolicy(max_notional=1_000_000)])
        v = svc.evaluate(_intent(), _snapshot(), context={"current_notional_exposure": 1_200_000})
        assert v.status == RiskDecisionStatus.DENY


class TestConcentrationPolicy:
    def test_allows_under_limit(self):
        svc = RiskEvaluationService([ConcentrationPolicy(max_per_symbol=3)])
        v = svc.evaluate(_intent(), _snapshot(), context={"positions_per_symbol": {"XAUUSD": 1}})
        assert v.status == RiskDecisionStatus.ALLOW

    def test_denies_at_limit(self):
        svc = RiskEvaluationService([ConcentrationPolicy(max_per_symbol=3)])
        v = svc.evaluate(_intent(), _snapshot(), context={"positions_per_symbol": {"XAUUSD": 3}})
        assert v.status == RiskDecisionStatus.DENY


class TestModePolicy:
    def test_normal_mode_allows(self):
        svc = RiskEvaluationService([ModePolicy()])
        v = svc.evaluate(_intent(), _snapshot("normal"))
        assert v.status == RiskDecisionStatus.ALLOW

    def test_halted_mode_denies(self):
        svc = RiskEvaluationService([ModePolicy()])
        v = svc.evaluate(_intent(), _snapshot("halted"))
        assert v.status == RiskDecisionStatus.DENY
        assert "system_halted" in v.blocking_reasons

    def test_observe_only_defers(self):
        svc = RiskEvaluationService([ModePolicy()])
        v = svc.evaluate(_intent(), _snapshot("observe_only"))
        assert v.status == RiskDecisionStatus.DEFER

    def test_liquidation_only_denies_open(self):
        svc = RiskEvaluationService([ModePolicy()])
        v = svc.evaluate(_intent(DecisionAction.OPEN), _snapshot("liquidation_only"))
        assert v.status == RiskDecisionStatus.DENY

    def test_liquidation_only_allows_close(self):
        svc = RiskEvaluationService([ModePolicy()])
        v = svc.evaluate(_intent(DecisionAction.CLOSE), _snapshot("liquidation_only"))
        assert v.status == RiskDecisionStatus.LIQUIDATE_ONLY

    def test_cautious_mode_limits(self):
        svc = RiskEvaluationService([ModePolicy()])
        v = svc.evaluate(_intent(), _snapshot("cautious"))
        assert v.status == RiskDecisionStatus.ALLOW_LIMITED
        assert v.constraints.get("max_risk_fraction") == 0.5


class TestPolicyMerging:
    def test_most_restrictive_wins(self):
        svc = RiskEvaluationService(
            [
                PositionLimitPolicy(max_open_positions=100),
                ExposurePolicy(max_notional=1_000_000),
            ]
        )
        v = svc.evaluate(
            _intent(),
            _snapshot(),
            context={
                "open_position_count": 1,
                "current_notional_exposure": 2_000_000,
            },
        )
        assert v.status == RiskDecisionStatus.DENY

    def test_limited_plus_allow_gives_limited(self):
        svc = RiskEvaluationService(
            [
                DrawdownPolicy(max_drawdown_pct=5.0),
                PositionLimitPolicy(max_open_positions=100),
            ]
        )
        v = svc.evaluate(
            _intent(),
            _snapshot(),
            context={
                "current_drawdown_pct": 4.5,
                "open_position_count": 1,
            },
        )
        assert v.status == RiskDecisionStatus.ALLOW_LIMITED

    def test_multiple_denies_aggregate_reasons(self):
        svc = RiskEvaluationService(
            [
                PositionLimitPolicy(max_open_positions=2),
                ExposurePolicy(max_notional=100_000),
            ]
        )
        v = svc.evaluate(
            _intent(),
            _snapshot(),
            context={
                "open_position_count": 5,
                "current_notional_exposure": 200_000,
            },
        )
        assert v.status == RiskDecisionStatus.DENY
        assert len(v.blocking_reasons) == 2

    def test_all_policies_combined(self):
        svc = RiskEvaluationService(
            [
                ModePolicy(),
                PositionLimitPolicy(max_open_positions=10),
                DrawdownPolicy(max_drawdown_pct=5.0),
                ExposurePolicy(max_notional=1_000_000),
                ConcentrationPolicy(max_per_symbol=3),
            ]
        )
        v = svc.evaluate(
            _intent(),
            _snapshot(),
            context={
                "open_position_count": 2,
                "current_drawdown_pct": 1.0,
                "current_notional_exposure": 100_000,
                "positions_per_symbol": {"XAUUSD": 1},
            },
        )
        assert v.status == RiskDecisionStatus.ALLOW
        assert v.risk_tier == "standard"
        assert len(v.trace["policy_results"]) == 5


class TestRuntimeLoopWithRiskService:
    def test_runtime_loop_uses_risk_service_when_provided(self, tmp_path):
        from pathlib import Path

        from apps.engine.runtime_loop import RuntimeLoop

        event_time = datetime(2026, 4, 24, 12, 0, 0)
        feature_snapshot = type(
            "FS",
            (),
            {
                "snapshot_id": "s1",
                "event_time": event_time,
                "symbol": "XAUUSD",
                "venue": "MT5",
            },
        )()
        snap = _snapshot()
        candidate = type("C", (), {"regime_state": {"primary_regime": "trend"}})()
        record = type("R", (), {"record_id": "r1"})()

        risk_svc = RiskEvaluationService(
            [
                PositionLimitPolicy(max_open_positions=2),
            ]
        )

        loop = RuntimeLoop(
            control_snapshot_service=type(
                "CSS",
                (),
                {
                    "freeze": lambda self, symbol, regime: snap,
                },
            )(),
            feature_service=type(
                "FS",
                (),
                {
                    "build_snapshot": lambda self, trigger: feature_snapshot,
                },
            )(),
            brain_run_service=type(
                "BRS",
                (),
                {
                    "run_active_brains": lambda self, **kw: ["p"],
                },
            )(),
            parliament_adapter=type(
                "PA",
                (),
                {
                    "build_candidate": lambda self, **kw: candidate,
                },
            )(),
            override_resolver=type(
                "OR",
                (),
                {
                    "resolve": lambda self, **kw: [],
                },
            )(),
            decision_compiler=type(
                "DC",
                (),
                {
                    "compile_intent": lambda self, **kw: _intent(),
                },
            )(),
            decision_record_writer=type(
                "DRW",
                (),
                {
                    "seed_record": lambda self, **kw: (record, Path(tmp_path) / "x.jsonl"),
                },
            )(),
            risk_evaluation_service=risk_svc,
        )

        result = loop.run_decision_cycle(trigger={"symbol": "XAUUSD"}, feature_source={"f": 1})
        assert result.verdict.status == RiskDecisionStatus.ALLOW
        assert "policy_results" in result.verdict.trace

    def test_runtime_loop_risk_service_blocks_at_position_limit(self, tmp_path):
        from pathlib import Path

        from apps.engine.runtime_loop import RuntimeLoop

        event_time = datetime(2026, 4, 24, 12, 0, 0)
        feature_snapshot = type(
            "FS",
            (),
            {
                "snapshot_id": "s1",
                "event_time": event_time,
                "symbol": "XAUUSD",
                "venue": "MT5",
            },
        )()
        snap = _snapshot()
        candidate = type("C", (), {"regime_state": {"primary_regime": "trend"}})()
        record = type("R", (), {"record_id": "r1"})()

        risk_svc = RiskEvaluationService(
            [
                PositionLimitPolicy(max_open_positions=0),
            ]
        )

        loop = RuntimeLoop(
            control_snapshot_service=type("CSS", (), {"freeze": lambda self, **kw: snap})(),
            feature_service=type(
                "FS", (), {"build_snapshot": lambda self, trigger: feature_snapshot}
            )(),
            brain_run_service=type("BRS", (), {"run_active_brains": lambda self, **kw: ["p"]})(),
            parliament_adapter=type("PA", (), {"build_candidate": lambda self, **kw: candidate})(),
            override_resolver=type("OR", (), {"resolve": lambda self, **kw: []})(),
            decision_compiler=type("DC", (), {"compile_intent": lambda self, **kw: _intent()})(),
            decision_record_writer=type(
                "DRW",
                (),
                {
                    "seed_record": lambda self, **kw: (record, Path(tmp_path) / "x.jsonl"),
                },
            )(),
            risk_evaluation_service=risk_svc,
        )

        result = loop.run_decision_cycle(trigger={"symbol": "XAUUSD"}, feature_source={"f": 1})
        assert result.verdict.status == RiskDecisionStatus.DENY
        assert result.dispatch_result["status"] == "skipped"
