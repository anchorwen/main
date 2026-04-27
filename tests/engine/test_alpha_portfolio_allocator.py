"""Alpha portfolio allocator tests."""

from core.alpha.contracts import AlphaLifecycleState, AlphaRecord
from core.alpha.performance_store import AlphaPerformanceStore
from core.alpha.portfolio_allocator import AlphaAllocationPolicy, AlphaPortfolioAllocator
from core.alpha.registry import AlphaRegistry
from core.alpha.schema_versions import SCHEMA_ALPHA_PORTFOLIO_ALLOCATION


def _record(alpha_id, state):
    return AlphaRecord(alpha_id=alpha_id, name=alpha_id, version="1.0", state=state, strategy_id=alpha_id)


def _metrics(**overrides):
    metrics = {
        "signal_count": 10,
        "order_count": 10,
        "fill_ratio": 1.0,
        "denied_count": 0,
        "average_slippage_bps": 1.0,
        "orders_per_signal": 1.0,
    }
    metrics.update(overrides)
    return metrics


class TestAlphaPortfolioAllocator:
    def test_allocate_active_and_probation_weights(self):
        registry = AlphaRegistry()
        registry.register(_record("alpha1", AlphaLifecycleState.ACTIVE))
        registry.register(_record("alpha2", AlphaLifecycleState.PROBATION_LIVE))
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics())
        store.record_snapshot("alpha2", _metrics())
        result = AlphaPortfolioAllocator(registry, store, AlphaAllocationPolicy(total_notional=1000)).allocate()
        assert result["schema_version"] == SCHEMA_ALPHA_PORTFOLIO_ALLOCATION
        assert result["alpha_count"] == 2
        assert result["allocatable_count"] == 2
        recs = {row["alpha_id"]: row for row in result["recommendations"]}
        assert recs["alpha1"]["target_weight"] > recs["alpha2"]["target_weight"]
        assert recs["alpha1"]["max_notional"] + recs["alpha2"]["max_notional"] == 1000.0

    def test_non_allocatable_states_get_zero(self):
        registry = AlphaRegistry()
        registry.register(_record("alpha1", AlphaLifecycleState.PAPER_TRADING))
        registry.register(_record("alpha2", AlphaLifecycleState.RETIRED))
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics())
        store.record_snapshot("alpha2", _metrics())
        result = AlphaPortfolioAllocator(registry, store).allocate()
        assert result["allocatable_count"] == 0
        assert all(row["target_weight"] == 0.0 for row in result["recommendations"])

    def test_missing_performance_gets_zero(self):
        registry = AlphaRegistry()
        registry.register(_record("alpha1", AlphaLifecycleState.ACTIVE))
        result = AlphaPortfolioAllocator(registry, AlphaPerformanceStore()).allocate()
        rec = result["recommendations"][0]
        assert rec["score"] == 0.0
        assert rec["reason"] == "performance_missing"

    def test_bad_metrics_reduce_score_and_risk_tier(self):
        registry = AlphaRegistry()
        registry.register(_record("alpha1", AlphaLifecycleState.ACTIVE))
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics(fill_ratio=0.4, denied_count=2, average_slippage_bps=20, orders_per_signal=0.2))
        result = AlphaPortfolioAllocator(registry, store).allocate()
        rec = result["recommendations"][0]
        assert rec["score"] < 0.35
        assert rec["risk_tier"] in {"minimal", "none"}
