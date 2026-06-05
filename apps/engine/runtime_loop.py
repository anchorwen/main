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


def _apply_meta_filter(
    filter,
    candidate,
    proposals: list,
    feature_blackboard: dict,
    feature_snapshot,
) -> None:
    """Apply Stage 2 meta-signal filter to the candidate.

    Finds the Stage 1 Huber proposal, extracts its raw prediction, computes
    runtime meta-features from unified V9+Micro features (49-dim when micro
    data is available, 40-dim V9 fallback), and evaluates P(TP|signal).
    If the filter rejects, the candidate's consensus direction is set to
    "neutral" so that no order is dispatched (shadow-only record).
    """
    consensus = candidate.consensus
    direction_str = consensus.get("direction", "neutral")
    if direction_str not in ("long", "short"):
        return  # non-actionable, nothing to filter

    direction = 1 if direction_str == "long" else -1

    # Find the Stage 1 Huber proposal
    stage1_proposal = None
    for p in proposals:
        raw_outputs = getattr(p, "extensions", {}).get("raw_outputs", {})
        if "raw_score" in raw_outputs and getattr(p, "brain_role", "") == "alpha_brain":
            stage1_proposal = p
            break
    if stage1_proposal is None:
        candidate.risk_comments["meta_filter"] = "no_stage1_proposal"
        return

    raw_outputs = getattr(stage1_proposal, "extensions", {}).get("raw_outputs", {})
    s1_prediction = float(raw_outputs.get("raw_score", 0.0))

    # Prefer unified 49-dim features (V9 + micro) when available
    unified_features = feature_blackboard.get("v9_micro_49", {})
    if not unified_features:
        unified_features = feature_blackboard.get("v9_institutional_40", {})
    if not unified_features:
        candidate.risk_comments["meta_filter"] = "no_v9_features"
        return

    # Get timestamp from snapshot
    timestamp_utc = None
    if hasattr(feature_snapshot, "event_time"):
        import contextlib
        with contextlib.suppress(Exception):
            timestamp_utc = feature_snapshot.event_time.timestamp()

    # Apply the filter with unified 49-dim features
    result = filter.filter(
        direction=direction,
        s1_prediction=s1_prediction,
        v9_features=unified_features,
        timestamp_utc=timestamp_utc,
    )

    # Record the filter result (include micro data availability)
    micro_available = any(
        k in unified_features
        and unified_features.get(k) is not None
        and not (
            isinstance(unified_features.get(k), float)
            and unified_features.get(k) != unified_features.get(k)
        )
        for k in ("avg_spread", "OIM", "tick_velocity")
    )
    candidate.extensions["meta_filter_result"] = {
        "passed": result.passed,
        "p_win": result.p_win,
        "threshold": result.threshold,
        "reason": result.reason,
        "s1_prediction": s1_prediction,
        "filter_brain_id": getattr(stage1_proposal, "brain_id", "unknown"),
        "micro_data_available": micro_available,
        "feature_dim": len(unified_features),
    }

    if not result.passed:
        # Neutralize the direction — no order dispatch, shadow-only record
        candidate.consensus["direction"] = "neutral"
        candidate.risk_comments["meta_filter"] = result.reason
        candidate.extensions["meta_filter_blocked"] = True


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
        dynamic_brain_weighter=None,
        meta_signal_filter=None,
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
        self._dynamic_brain_weighter = dynamic_brain_weighter
        self._meta_signal_filter = meta_signal_filter

    def run_decision_cycle(
        self, trigger, feature_source: dict | None = None
    ) -> DecisionCycleResult:
        feature_snapshot = self._feature_service.build_snapshot(trigger=trigger)

        control_snapshot = self._control_snapshot_service.freeze(
            symbol=feature_snapshot.symbol,
            regime=None,
        )

        v9_dict = feature_source or {}
        # Build unified v9_micro_49 dict when micro features are available
        v9_micro_dict: dict[str, float] = {}
        if feature_source and any(
            k in feature_source for k in ("tick_return", "avg_spread", "OIM")
        ):
            v9_micro_dict = {**v9_dict}

        feature_blackboard = {
            "v9_institutional_40": v9_dict,
            "v9_micro_49": v9_micro_dict if v9_micro_dict else v9_dict,
            # swing_24: not computed yet — empty dict → safe neutral
        }
        proposals = self._brain_run_service.run_active_brains(
            feature_snapshot=feature_snapshot,
            control_snapshot=control_snapshot,
            feature_blackboard=feature_blackboard,
        )

        if self._dynamic_brain_weighter is not None:
            self._dynamic_brain_weighter.apply_weights(proposals)

        candidate = self._parliament_adapter.build_candidate(
            feature_snapshot=feature_snapshot,
            proposals=proposals,
            control_snapshot=control_snapshot,
        )

        # ── Meta-Signal Filter (Stage 2 two-stage meta-labeling) ──
        if self._meta_signal_filter is not None and self._meta_signal_filter.is_active():
            _apply_meta_filter(
                filter=self._meta_signal_filter,
                candidate=candidate,
                proposals=proposals,
                feature_blackboard=feature_blackboard,
                feature_snapshot=feature_snapshot,
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
