from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.contracts.domain.risk_verdict import RiskVerdict
from core.contracts.enums import RiskDecisionStatus
from core.contracts.ids import new_verdict_id
from core.risk.schema_versions import SCHEMA_RISK_VERDICT


@dataclass
class SimpleFeatureSnapshot:
    snapshot_id: str
    event_time: datetime
    symbol: str
    venue: str
    feature_vector: np.ndarray | None = None


@dataclass
class DecisionCycleResult:
    feature_snapshot: Any
    proposals: list[Any]
    candidate: Any
    intent: Any
    verdict: Any
    record: Any
    dispatch_result: Any
    ledger_path: Path
    communication_record: Any | None = None
    communication_ledger_path: Path | None = None
    communication_operations: dict | None = None


class RuntimeLoop:
    def __init__(
        self,
        control_snapshot_service,
        feature_service,
        brain_run_service,
        parliament_adapter,
        override_resolver,
        decision_compiler,
        decision_record_writer,
        intent_message_builder=None,
        communication_dispatcher=None,
        communication_record_writer=None,
        communication_operations_service=None,
        risk_evaluation_service=None,
    ):
        self._control_snapshot_service = control_snapshot_service
        self._feature_service = feature_service
        self._brain_run_service = brain_run_service
        self._parliament_adapter = parliament_adapter
        self._override_resolver = override_resolver
        self._decision_compiler = decision_compiler
        self._decision_record_writer = decision_record_writer
        self._intent_message_builder = intent_message_builder
        self._communication_dispatcher = communication_dispatcher
        self._communication_record_writer = communication_record_writer
        self._communication_operations_service = communication_operations_service
        self._risk_evaluation_service = risk_evaluation_service

    def run_decision_cycle(
        self, trigger, feature_source: dict | None = None
    ) -> DecisionCycleResult:
        feature_snapshot = self._feature_service.build_snapshot(trigger=trigger)

        control_snapshot = self._control_snapshot_service.freeze(
            symbol=feature_snapshot.symbol,
            regime=None,
        )

        proposals = self._brain_run_service.run_active_brains(
            feature_snapshot=feature_snapshot,
            control_snapshot=control_snapshot,
            feature_vector=getattr(feature_snapshot, "feature_vector", None),
            feature_source=feature_source,
        )

        candidate = self._parliament_adapter.build_candidate(
            feature_snapshot=feature_snapshot,
            proposals=proposals,
            control_snapshot=control_snapshot,
        )

        regime_name = candidate.regime_state.get("primary_regime")
        control_snapshot = self._control_snapshot_service.freeze(
            symbol=feature_snapshot.symbol,
            regime=regime_name,
        )

        current_mode = (
            control_snapshot.mode_state.current_mode.value
            if hasattr(control_snapshot.mode_state.current_mode, "value")
            else control_snapshot.mode_state.current_mode
        )
        active_overrides = self._override_resolver.resolve(
            symbol=feature_snapshot.symbol,
            regime=regime_name,
            mode=current_mode,
            active_overrides=control_snapshot.active_overrides,
        )

        intent = self._decision_compiler.compile_intent(
            candidate=candidate,
            mode_state=control_snapshot.mode_state,
            active_overrides=active_overrides,
        )

        if self._risk_evaluation_service is not None:
            verdict = self._risk_evaluation_service.evaluate(
                intent,
                control_snapshot,
            )
        elif intent.is_actionable():
            verdict = RiskVerdict(
                schema_version=SCHEMA_RISK_VERDICT,
                verdict_id=new_verdict_id(),
                intent_id=intent.intent_id,
                evaluated_at=datetime.now(UTC).replace(tzinfo=None),
                status=RiskDecisionStatus.ALLOW,
                mode=control_snapshot.mode_state.current_mode,
                risk_tier="standard",
            )
        else:
            verdict = RiskVerdict(
                schema_version=SCHEMA_RISK_VERDICT,
                verdict_id=new_verdict_id(),
                intent_id=intent.intent_id,
                evaluated_at=datetime.now(UTC).replace(tzinfo=None),
                status=RiskDecisionStatus.DENY,
                mode=control_snapshot.mode_state.current_mode,
                risk_tier="minimal",
                blocking_reasons=["passive_intent"],
            )

        record, ledger_path = self._decision_record_writer.seed_record(
            feature_snapshot=feature_snapshot,
            proposals=proposals,
            candidate=candidate,
            intent=intent,
            verdict=verdict,
        )

        dispatch_result = {
            "status": "submitted" if verdict.is_allowed() else "skipped",
            "reason": None if verdict.is_allowed() else "risk_blocked",
        }
        communication_record = None
        communication_ledger_path = None
        communication_operations = None

        if (
            verdict.is_allowed()
            and intent.is_actionable()
            and self._intent_message_builder
            and self._communication_dispatcher
        ):
            envelope = self._intent_message_builder.build(
                intent,
                correlation_id=record.record_id,
                causation_id=intent.intent_id,
            )
            dispatch_result = self._communication_dispatcher.dispatch(
                envelope,
                governance={"verdict_id": verdict.verdict_id},
            )
            if self._communication_record_writer:
                communication_record, communication_ledger_path = (
                    self._communication_record_writer.write_record(
                        envelope,
                        dispatch_result,
                    )
                )
            if self._communication_operations_service is not None:
                communication_operations = (
                    self._communication_operations_service.get_message_operations_view(
                        date_key=envelope.event_time.strftime("%Y-%m-%d"),
                        target=envelope.target,
                        message_id=envelope.message_id,
                    )
                )

        return DecisionCycleResult(
            feature_snapshot=feature_snapshot,
            proposals=proposals,
            candidate=candidate,
            intent=intent,
            verdict=verdict,
            record=record,
            dispatch_result=dispatch_result,
            ledger_path=ledger_path,
            communication_record=communication_record,
            communication_ledger_path=communication_ledger_path,
            communication_operations=communication_operations,
        )
