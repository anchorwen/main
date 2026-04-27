"""Alpha promotion gate tests."""

from core.alpha.contracts import AlphaLifecycleState, AlphaRecord
from core.alpha.lifecycle_service import AlphaLifecycleService
from core.alpha.performance_store import AlphaPerformanceStore
from core.alpha.promotion_gate import AlphaPromotionGate
from core.alpha.promotion_gate import AlphaPromotionPolicy
from core.alpha.schema_versions import SCHEMA_ALPHA_PROMOTION_DECISION
from core.alpha.registry import AlphaRegistry


def _record(state=AlphaLifecycleState.CANDIDATE):
    return AlphaRecord(alpha_id="alpha1", name="Alpha One", version="1.0", state=state, strategy_id="alpha1")


def _metrics(**overrides):
    metrics = {
        "signal_count": 5,
        "order_count": 5,
        "fill_ratio": 1.0,
        "denied_count": 0,
        "paper_cycles": 3,
        "average_slippage_bps": 1.0,
    }
    metrics.update(overrides)
    return metrics


class TestAlphaPromotionGate:
    def test_missing_snapshot_holds(self):
        decision = AlphaPromotionGate(AlphaPerformanceStore()).evaluate(_record())
        assert decision.action == "hold"
        assert decision.approved is False
        assert decision.reasons == ["performance_snapshot_missing"]
        assert decision.to_dict()["schema_version"] == SCHEMA_ALPHA_PROMOTION_DECISION

    def test_candidate_promotes_to_backtest_passed(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics())
        decision = AlphaPromotionGate(store).evaluate(_record())
        assert decision.action == "promote"
        assert decision.target_state == "backtest_passed"
        assert decision.approved is True

    def test_candidate_holds_when_requirements_fail(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics(order_count=0, fill_ratio=0.5))
        decision = AlphaPromotionGate(store).evaluate(_record())
        assert decision.action == "hold"
        assert "order_count_below_minimum" in decision.reasons
        assert "fill_ratio_below_minimum" in decision.reasons

    def test_backtest_passed_promotes_to_paper_trading(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics())
        decision = AlphaPromotionGate(store).evaluate(_record(AlphaLifecycleState.BACKTEST_PASSED))
        assert decision.target_state == "paper_trading"

    def test_paper_trading_promotes_to_probation_live(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics())
        decision = AlphaPromotionGate(store).evaluate(_record(AlphaLifecycleState.PAPER_TRADING))
        assert decision.target_state == "probation_live"

    def test_paper_trading_holds_for_low_cycles_or_high_slippage(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics(paper_cycles=1, average_slippage_bps=9.0))
        decision = AlphaPromotionGate(store).evaluate(_record(AlphaLifecycleState.PAPER_TRADING))
        assert decision.action == "hold"
        assert "paper_cycles_below_minimum" in decision.reasons
        assert "slippage_above_maximum" in decision.reasons

    def test_probation_promotes_to_active(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics())
        decision = AlphaPromotionGate(store).evaluate(_record(AlphaLifecycleState.PROBATION_LIVE))
        assert decision.target_state == "active"

    def test_active_throttles_on_bad_metrics(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics(fill_ratio=0.4))
        decision = AlphaPromotionGate(store).evaluate(_record(AlphaLifecycleState.ACTIVE))
        assert decision.action == "throttle"
        assert decision.target_state == "throttled"
        assert decision.approved is True

    def test_retire_overrides_throttle(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics(denied_count=5, fill_ratio=0.4))
        decision = AlphaPromotionGate(store).evaluate(_record(AlphaLifecycleState.ACTIVE))
        assert decision.action == "retire"
        assert decision.target_state == "retired"

    def test_apply_decision_updates_lifecycle(self):
        registry = AlphaRegistry()
        registry.register(_record(AlphaLifecycleState.BACKTEST_PASSED))
        lifecycle = AlphaLifecycleService(registry)
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics())
        decision = AlphaPromotionGate(store).apply(registry.require("alpha1"), lifecycle)
        assert decision.target_state == "paper_trading"
        assert registry.require("alpha1").state_value == "paper_trading"
        assert lifecycle.transitions("alpha1")[0].reason == "alpha_promotion_gate:promote"

    def test_policy_overrides_thresholds(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", _metrics(fill_ratio=0.9))
        policy = AlphaPromotionPolicy(min_fill_ratio=0.99)
        decision = AlphaPromotionGate(store, policy).evaluate(_record())
        assert decision.action == "hold"
        assert "fill_ratio_below_minimum" in decision.reasons
