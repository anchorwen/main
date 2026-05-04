"""Alpha registry and lifecycle tests."""

import pytest

from core.alpha.contracts import AlphaLifecycleState, AlphaRecord
from core.alpha.lifecycle_service import AlphaLifecycleService
from core.alpha.registry import AlphaRegistry
from core.alpha.schema_versions import SCHEMA_ALPHA_LIFECYCLE_SUMMARY, SCHEMA_ALPHA_REGISTRY


def _record(alpha_id="alpha1", state=AlphaLifecycleState.CANDIDATE):
    return AlphaRecord(
        alpha_id=alpha_id,
        name="Alpha One",
        version="1.0",
        state=state,
        strategy_id=alpha_id,
        tags=("test",),
        metadata={"family": "threshold"},
        performance={"sharpe": 1.2},
        risk_profile={"tier": "standard"},
    )


class TestAlphaContracts:
    def test_alpha_record_to_dict(self):
        record = _record()
        payload = record.to_dict()
        assert payload["alpha_id"] == "alpha1"
        assert payload["state"] == "candidate"
        assert payload["tags"] == ["test"]

    def test_alpha_record_validation(self):
        with pytest.raises(ValueError):
            AlphaRecord(alpha_id="", name="x", version="1")
        with pytest.raises(ValueError):
            AlphaRecord(alpha_id="a", name="", version="1")
        with pytest.raises(ValueError):
            AlphaRecord(alpha_id="a", name="x", version="")
        with pytest.raises(ValueError):
            AlphaRecord(alpha_id="a", name="x", version="1", state="unknown")


class TestAlphaRegistry:
    def test_register_get_list_and_remove(self):
        registry = AlphaRegistry()
        record = registry.register(_record())
        assert registry.get("alpha1") == record
        assert registry.require("alpha1") == record
        assert registry.list_records()[0].alpha_id == "alpha1"
        assert registry.list_records(state="candidate")[0].alpha_id == "alpha1"
        registry.remove("alpha1")
        assert registry.get("alpha1") is None

    def test_duplicate_registration_rejected(self):
        registry = AlphaRegistry()
        registry.register(_record())
        with pytest.raises(ValueError):
            registry.register(_record())

    def test_require_unknown_rejected(self):
        with pytest.raises(ValueError):
            AlphaRegistry().require("missing")

    def test_upsert_and_to_dict(self):
        registry = AlphaRegistry()
        registry.upsert(_record())
        payload = registry.to_dict()
        assert payload["schema_version"] == SCHEMA_ALPHA_REGISTRY
        assert payload["alpha_count"] == 1
        assert payload["records"][0]["alpha_id"] == "alpha1"


class TestAlphaLifecycleService:
    def test_full_lifecycle_path(self):
        registry = AlphaRegistry()
        registry.register(_record())
        service = AlphaLifecycleService(registry)
        service.mark_backtest_passed("alpha1")
        service.start_paper_trading("alpha1")
        service.promote_to_probation_live("alpha1")
        service.activate("alpha1")
        assert registry.require("alpha1").state_value == "active"
        assert len(service.transitions("alpha1")) == 4
        assert service.transitions("alpha1")[-1].to_state == "active"

    def test_throttle_and_recover_to_probation(self):
        registry = AlphaRegistry()
        registry.register(_record(state=AlphaLifecycleState.ACTIVE))
        service = AlphaLifecycleService(registry)
        service.throttle("alpha1", "drawdown")
        service.promote_to_probation_live("alpha1", "recovered")
        assert registry.require("alpha1").state_value == "probation_live"

    def test_retire_terminal(self):
        registry = AlphaRegistry()
        registry.register(_record())
        service = AlphaLifecycleService(registry)
        service.retire("alpha1")
        assert registry.require("alpha1").state_value == "retired"
        with pytest.raises(ValueError):
            service.mark_backtest_passed("alpha1")

    def test_invalid_transition_rejected(self):
        registry = AlphaRegistry()
        registry.register(_record())
        service = AlphaLifecycleService(registry)
        with pytest.raises(ValueError):
            service.activate("alpha1")

    def test_summarize(self):
        registry = AlphaRegistry()
        registry.register(_record("alpha1"))
        registry.register(_record("alpha2", state=AlphaLifecycleState.PAPER_TRADING))
        service = AlphaLifecycleService(registry)
        service.mark_backtest_passed("alpha1")
        summary = service.summarize()
        assert summary["schema_version"] == SCHEMA_ALPHA_LIFECYCLE_SUMMARY
        assert summary["alpha_count"] == 2
        assert summary["by_state"]["backtest_passed"] == 1
        assert summary["by_state"]["paper_trading"] == 1
        assert summary["transition_count"] == 1
