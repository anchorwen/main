"""Alpha promotion gate for lifecycle automation."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.alpha.contracts import AlphaLifecycleState, AlphaRecord
from core.alpha.lifecycle_service import AlphaLifecycleService
from core.alpha.performance_store import AlphaPerformanceStore
from core.alpha.schema_versions import SCHEMA_ALPHA_PROMOTION_DECISION


@dataclass(frozen=True)
class AlphaPromotionPolicy:
    min_signal_count: int = 1
    min_order_count: int = 1
    min_fill_ratio: float = 0.95
    max_denied_count: int = 0
    min_paper_cycles: int = 2
    max_slippage_bps: float = 5.0
    throttle_fill_ratio: float = 0.80
    throttle_denied_count: int = 2
    throttle_slippage_bps: float = 15.0
    retire_denied_count: int = 5
    # Live bridge metrics: defaults are conservative and effectively disabled.
    max_live_rejection_rate: float = 1.0
    max_live_consecutive_rejected: int = 10**9


@dataclass(frozen=True)
class AlphaPromotionDecision:
    alpha_id: str
    current_state: str
    action: str
    target_state: str | None
    approved: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_ALPHA_PROMOTION_DECISION,
            "alpha_id": self.alpha_id,
            "current_state": self.current_state,
            "action": self.action,
            "target_state": self.target_state,
            "approved": self.approved,
            "reasons": self.reasons,
            "metrics": self.metrics,
            "generated_at": self.generated_at.isoformat(),
        }


class AlphaPromotionGate:
    """Evaluates Alpha lifecycle decisions from latest performance metrics."""

    def __init__(
        self, performance_store: AlphaPerformanceStore, policy: AlphaPromotionPolicy | None = None
    ):
        self._store = performance_store
        self._policy = policy or AlphaPromotionPolicy()

    def evaluate(self, record: AlphaRecord) -> AlphaPromotionDecision:
        snapshot = self._store.latest(record.alpha_id)
        if snapshot is None:
            return self._decision(record, "hold", None, False, ["performance_snapshot_missing"], {})
        metrics = snapshot.metrics
        state = record.state_value
        if self._live_bridge_guards_apply(state) and metrics.get("live_bridge"):
            if self._live_bridge_should_retire(metrics):
                return self._decision(
                    record,
                    "retire",
                    AlphaLifecycleState.RETIRED.value,
                    True,
                    ["live_bridge_retire_threshold_breached"],
                    metrics,
                )
            if self._live_bridge_should_throttle(metrics):
                return self._decision(
                    record,
                    "throttle",
                    AlphaLifecycleState.THROTTLED.value,
                    True,
                    ["live_bridge_throttle_threshold_breached"],
                    metrics,
                )
        if self._should_retire(metrics):
            return self._decision(
                record,
                "retire",
                AlphaLifecycleState.RETIRED.value,
                True,
                ["retire_threshold_breached"],
                metrics,
            )
        if self._should_throttle(state, metrics):
            return self._decision(
                record,
                "throttle",
                AlphaLifecycleState.THROTTLED.value,
                True,
                ["throttle_threshold_breached"],
                metrics,
            )
        if state == AlphaLifecycleState.CANDIDATE.value:
            return self._candidate_decision(record, metrics)
        if state == AlphaLifecycleState.BACKTEST_PASSED.value:
            return self._backtest_passed_decision(record, metrics)
        if state == AlphaLifecycleState.PAPER_TRADING.value:
            return self._paper_trading_decision(record, metrics)
        if state == AlphaLifecycleState.PROBATION_LIVE.value:
            return self._probation_decision(record, metrics)
        return self._decision(
            record, "hold", None, False, [f"no_promotion_rule_for_state:{state}"], metrics
        )

    def _live_bridge_guards_apply(self, state: str) -> bool:
        return state in {AlphaLifecycleState.PROBATION_LIVE.value, AlphaLifecycleState.ACTIVE.value}

    def _live_bridge_should_retire(self, metrics: dict[str, Any]) -> bool:
        cons = int(self._metric(metrics, "live_consecutive_rejected", 0))
        return cons > self._policy.max_live_consecutive_rejected

    def _live_bridge_should_throttle(self, metrics: dict[str, Any]) -> bool:
        lr = float(self._metric(metrics, "live_rejection_rate", 0.0))
        return lr > self._policy.max_live_rejection_rate

    def apply(
        self, record: AlphaRecord, lifecycle: AlphaLifecycleService
    ) -> AlphaPromotionDecision:
        decision = self.evaluate(record)
        if decision.approved and decision.target_state:
            lifecycle.transition(
                record.alpha_id,
                decision.target_state,
                reason="alpha_promotion_gate:" + decision.action,
            )
        return decision

    def _candidate_decision(
        self, record: AlphaRecord, metrics: dict[str, Any]
    ) -> AlphaPromotionDecision:
        reasons = self._base_requirements(metrics)
        if reasons:
            return self._decision(record, "hold", None, False, reasons, metrics)
        return self._decision(
            record,
            "promote",
            AlphaLifecycleState.BACKTEST_PASSED.value,
            True,
            ["candidate_requirements_passed"],
            metrics,
        )

    def _backtest_passed_decision(
        self, record: AlphaRecord, metrics: dict[str, Any]
    ) -> AlphaPromotionDecision:
        reasons = self._base_requirements(metrics)
        if reasons:
            return self._decision(record, "hold", None, False, reasons, metrics)
        return self._decision(
            record,
            "promote",
            AlphaLifecycleState.PAPER_TRADING.value,
            True,
            ["paper_trading_requirements_passed"],
            metrics,
        )

    def _paper_trading_decision(
        self, record: AlphaRecord, metrics: dict[str, Any]
    ) -> AlphaPromotionDecision:
        reasons = self._base_requirements(metrics)
        if self._metric(metrics, "paper_cycles", 0) < self._policy.min_paper_cycles:
            reasons.append("paper_cycles_below_minimum")
        if self._metric(metrics, "average_slippage_bps", 0.0) > self._policy.max_slippage_bps:
            reasons.append("slippage_above_maximum")
        if reasons:
            return self._decision(record, "hold", None, False, reasons, metrics)
        return self._decision(
            record,
            "promote",
            AlphaLifecycleState.PROBATION_LIVE.value,
            True,
            ["probation_live_requirements_passed"],
            metrics,
        )

    def _probation_decision(
        self, record: AlphaRecord, metrics: dict[str, Any]
    ) -> AlphaPromotionDecision:
        reasons = self._base_requirements(metrics)
        if self._metric(metrics, "paper_cycles", 0) < self._policy.min_paper_cycles:
            reasons.append("paper_cycles_below_minimum")
        if reasons:
            return self._decision(record, "hold", None, False, reasons, metrics)
        return self._decision(
            record,
            "promote",
            AlphaLifecycleState.ACTIVE.value,
            True,
            ["activation_requirements_passed"],
            metrics,
        )

    def _base_requirements(self, metrics: dict[str, Any]) -> list[str]:
        reasons = []
        if self._metric(metrics, "signal_count", 0) < self._policy.min_signal_count:
            reasons.append("signal_count_below_minimum")
        if self._metric(metrics, "order_count", 0) < self._policy.min_order_count:
            reasons.append("order_count_below_minimum")
        fill_ratio = self._metric(metrics, "fill_ratio", None)
        if fill_ratio is None or fill_ratio < self._policy.min_fill_ratio:
            reasons.append("fill_ratio_below_minimum")
        if self._metric(metrics, "denied_count", 0) > self._policy.max_denied_count:
            reasons.append("denied_count_above_maximum")
        return reasons

    def _should_throttle(self, state: str, metrics: dict[str, Any]) -> bool:
        if state not in {
            AlphaLifecycleState.PAPER_TRADING.value,
            AlphaLifecycleState.PROBATION_LIVE.value,
            AlphaLifecycleState.ACTIVE.value,
        }:
            return False
        fill_ratio = self._metric(metrics, "fill_ratio", 1.0)
        denied = self._metric(metrics, "denied_count", 0)
        slippage = self._metric(metrics, "average_slippage_bps", 0.0)
        return (
            fill_ratio < self._policy.throttle_fill_ratio
            or denied > self._policy.throttle_denied_count
            or slippage > self._policy.throttle_slippage_bps
        )

    def _should_retire(self, metrics: dict[str, Any]) -> bool:
        return self._metric(metrics, "denied_count", 0) >= self._policy.retire_denied_count

    def _decision(
        self,
        record: AlphaRecord,
        action: str,
        target_state: str | None,
        approved: bool,
        reasons: list[str],
        metrics: dict[str, Any],
    ) -> AlphaPromotionDecision:
        return AlphaPromotionDecision(
            alpha_id=record.alpha_id,
            current_state=record.state_value,
            action=action,
            target_state=target_state,
            approved=approved,
            reasons=reasons,
            metrics=metrics,
        )

    def _metric(self, metrics: dict[str, Any], key: str, default: Any) -> Any:
        value = metrics.get(key, default)
        return default if value is None else value
