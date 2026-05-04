"""Alpha portfolio allocation MVP."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.alpha.contracts import AlphaLifecycleState, AlphaRecord
from core.alpha.performance_store import AlphaPerformanceStore
from core.alpha.registry import AlphaRegistry
from core.alpha.schema_versions import SCHEMA_ALPHA_PORTFOLIO_ALLOCATION


@dataclass(frozen=True)
class AlphaAllocationPolicy:
    total_notional: float = 100_000.0
    min_score: float = 0.01
    state_multipliers: dict[str, float] = field(
        default_factory=lambda: {
            AlphaLifecycleState.ACTIVE.value: 1.0,
            AlphaLifecycleState.PROBATION_LIVE.value: 0.35,
            AlphaLifecycleState.PAPER_TRADING.value: 0.0,
            AlphaLifecycleState.BACKTEST_PASSED.value: 0.0,
            AlphaLifecycleState.CANDIDATE.value: 0.0,
            AlphaLifecycleState.THROTTLED.value: 0.05,
            AlphaLifecycleState.RETIRED.value: 0.0,
        }
    )


@dataclass(frozen=True)
class AlphaAllocationRecommendation:
    alpha_id: str
    state: str
    score: float
    target_weight: float
    max_notional: float
    risk_tier: str
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_id": self.alpha_id,
            "state": self.state,
            "score": self.score,
            "target_weight": self.target_weight,
            "max_notional": self.max_notional,
            "risk_tier": self.risk_tier,
            "reason": self.reason,
            "metrics": self.metrics,
        }


class AlphaPortfolioAllocator:
    """Builds deterministic portfolio allocation recommendations for Alpha assets."""

    def __init__(
        self,
        registry: AlphaRegistry,
        performance_store: AlphaPerformanceStore,
        policy: AlphaAllocationPolicy | None = None,
    ):
        self._registry = registry
        self._performance = performance_store
        self._policy = policy or AlphaAllocationPolicy()

    def allocate(self) -> dict[str, Any]:
        recommendations = [self._recommend(record) for record in self._registry.list_records()]
        allocatable = [rec for rec in recommendations if rec.score >= self._policy.min_score]
        score_total = sum(rec.score for rec in allocatable)
        normalized = []
        for rec in recommendations:
            weight = (
                round(rec.score / score_total, 6) if rec in allocatable and score_total > 0 else 0.0
            )
            normalized.append(
                AlphaAllocationRecommendation(
                    alpha_id=rec.alpha_id,
                    state=rec.state,
                    score=rec.score,
                    target_weight=weight,
                    max_notional=round(weight * self._policy.total_notional, 2),
                    risk_tier=rec.risk_tier,
                    reason=rec.reason,
                    metrics=rec.metrics,
                )
            )
        return {
            "schema_version": SCHEMA_ALPHA_PORTFOLIO_ALLOCATION,
            "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            "total_notional": self._policy.total_notional,
            "alpha_count": len(recommendations),
            "allocatable_count": len(allocatable),
            "recommendations": [
                rec.to_dict()
                for rec in sorted(normalized, key=lambda item: (-item.target_weight, item.alpha_id))
            ],
        }

    def _recommend(self, record: AlphaRecord) -> AlphaAllocationRecommendation:
        snapshot = self._performance.latest(record.alpha_id)
        metrics = snapshot.metrics if snapshot else {}
        state_multiplier = self._policy.state_multipliers.get(record.state_value, 0.0)
        if state_multiplier <= 0:
            return self._zero(record, metrics, f"state_not_allocatable:{record.state_value}")
        if not metrics:
            return self._zero(record, metrics, "performance_missing")
        quality_score = self._quality_score(metrics)
        score = round(state_multiplier * quality_score, 6)
        risk_tier = self._risk_tier(score, metrics)
        reason = "allocatable" if score >= self._policy.min_score else "score_below_minimum"
        return AlphaAllocationRecommendation(
            alpha_id=record.alpha_id,
            state=record.state_value,
            score=score,
            target_weight=0.0,
            max_notional=0.0,
            risk_tier=risk_tier,
            reason=reason,
            metrics=metrics,
        )

    def _quality_score(self, metrics: dict[str, Any]) -> float:
        fill_ratio = self._metric(metrics, "fill_ratio", 0.0)
        denied = self._metric(metrics, "denied_count", 0.0)
        slippage = self._metric(metrics, "average_slippage_bps", 0.0)
        orders_per_signal = self._metric(metrics, "orders_per_signal", 0.0)
        signal_count = self._metric(metrics, "signal_count", 0.0)
        activity = min(signal_count / 10.0, 1.0)
        denied_penalty = min(denied * 0.15, 0.75)
        slippage_penalty = min(max(slippage, 0.0) / 50.0, 0.5)
        conversion = min(orders_per_signal, 1.0)
        score = (
            (fill_ratio * 0.45)
            + (conversion * 0.25)
            + (activity * 0.30)
            - denied_penalty
            - slippage_penalty
        )
        return max(0.0, min(1.0, score))

    def _zero(
        self, record: AlphaRecord, metrics: dict[str, Any], reason: str
    ) -> AlphaAllocationRecommendation:
        return AlphaAllocationRecommendation(
            alpha_id=record.alpha_id,
            state=record.state_value,
            score=0.0,
            target_weight=0.0,
            max_notional=0.0,
            risk_tier="none",
            reason=reason,
            metrics=metrics,
        )

    def _risk_tier(self, score: float, metrics: dict[str, Any]) -> str:
        if score >= 0.75 and self._metric(metrics, "denied_count", 0.0) == 0:
            return "standard"
        if score >= 0.35:
            return "reduced"
        if score > 0:
            return "minimal"
        return "none"

    def _metric(self, metrics: dict[str, Any], key: str, default: float) -> float:
        value = metrics.get(key, default)
        return default if value is None else float(value)
