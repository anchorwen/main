from datetime import datetime

from core.contracts.domain.decision_intent import DecisionIntent
from core.protocol.schema_versions import SCHEMA_DECISION_COMPILER, SCHEMA_DECISION_INTENT
from core.contracts.enums import DecisionAction, DecisionSide, SystemMode
from core.contracts.ids import new_intent_id


class DecisionCompiler:
    def __init__(self, base_policy: dict, intent_explainer):
        self._base_policy = base_policy
        self._intent_explainer = intent_explainer

    def compile_intent(self, candidate, mode_state, active_overrides):
        signal = self._extract_candidate_signal(candidate)
        effective_policy = self._build_effective_policy(active_overrides, mode_state)
        action, side, conviction = self._materialize_action(signal, effective_policy, mode_state)

        reason_tags = self._intent_explainer.build_reason_tags(
            candidate=candidate,
            action=action.value,
            side=side.value,
        )

        current_mode = mode_state.current_mode.value if hasattr(mode_state.current_mode, "value") else mode_state.current_mode

        return DecisionIntent(
            schema_version=SCHEMA_DECISION_INTENT,
            intent_id=new_intent_id(),
            candidate_id=candidate.candidate_id,
            snapshot_id=candidate.snapshot_id,
            event_time=candidate.event_time,
            compiled_at=datetime.utcnow(),
            symbol=signal["symbol"],
            venue=signal["venue"],
            action=action,
            side=side,
            conviction=conviction,
            priority="normal",
            suggested_risk_fraction=signal.get("suggested_risk_fraction"),
            expected_edge_bps=signal.get("expected_edge_bps"),
            expected_hold_seconds=signal.get("expected_hold_seconds"),
            reason_tags=reason_tags,
            trace={
                "compiler_version": SCHEMA_DECISION_COMPILER,
                "mode": current_mode,
                "applied_overrides": [getattr(item, "override_id", "unknown") for item in active_overrides],
                "effective_policy": effective_policy,
            },
            extensions={},
        )

    def _extract_candidate_signal(self, candidate) -> dict:
        summary = candidate.candidate_summary or {}
        return {
            "symbol": summary.get("symbol", "unknown"),
            "venue": summary.get("venue", "MT5"),
            "up_probability": float(summary.get("up_probability", 0.5)),
            "down_probability": float(summary.get("down_probability", 0.5)),
            "expected_edge_bps": summary.get("expected_edge_bps"),
            "expected_hold_seconds": summary.get("expected_hold_seconds"),
            "suggested_risk_fraction": summary.get("suggested_risk_fraction"),
        }

    def _build_effective_policy(self, active_overrides, mode_state) -> dict:
        policy = dict(self._base_policy)

        for item in active_overrides:
            adjustments = getattr(item, "adjustments", {}) or {}
            for key, value in adjustments.items():
                policy[key] = value

        current_mode = mode_state.current_mode
        if current_mode == SystemMode.CAUTIOUS:
            policy["entry_long_threshold"] = max(policy["entry_long_threshold"], 0.74)
            policy["entry_short_threshold"] = max(policy["entry_short_threshold"], 0.74)
            policy["probability_scale"] = min(policy["probability_scale"], 0.95)
        elif current_mode == SystemMode.DEGRADED:
            policy["entry_long_threshold"] = max(policy["entry_long_threshold"], 0.78)
            policy["entry_short_threshold"] = max(policy["entry_short_threshold"], 0.78)
            policy["probability_scale"] = min(policy["probability_scale"], 0.90)
        elif current_mode in {SystemMode.OBSERVE_ONLY, SystemMode.LIQUIDATION_ONLY, SystemMode.HALTED}:
            policy["force_passive"] = True

        return policy

    def _materialize_action(self, signal: dict, policy: dict, mode_state):
        if policy.get("force_passive", False):
            return DecisionAction.OBSERVE, DecisionSide.FLAT, 0.0

        up_probability = signal["up_probability"]
        down_probability = signal["down_probability"]

        shift = float(policy.get("probability_shift", 0.0))
        scale = float(policy.get("probability_scale", 1.0))
        long_threshold = float(policy.get("entry_long_threshold", 0.70))
        short_threshold = float(policy.get("entry_short_threshold", 0.70))

        adjusted_up = max(0.0, min(1.0, (up_probability + shift) * scale))
        adjusted_down = max(0.0, min(1.0, (down_probability - shift) * scale))

        if adjusted_up >= long_threshold:
            return DecisionAction.OPEN, DecisionSide.LONG, adjusted_up
        if adjusted_down >= short_threshold:
            return DecisionAction.OPEN, DecisionSide.SHORT, adjusted_down
        if mode_state.current_mode == SystemMode.OBSERVE_ONLY:
            return DecisionAction.OBSERVE, DecisionSide.FLAT, max(adjusted_up, adjusted_down)
        return DecisionAction.ABSTAIN, DecisionSide.FLAT, max(adjusted_up, adjusted_down)
