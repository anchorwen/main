"""Strategy plugin protocol tests."""

from dataclasses import asdict

import pytest

from core.strategies.contracts import RequiredFeature, Signal, StrategyHealth, StrategyMetadata
from core.strategies.examples import ThresholdAlphaAgent
from core.strategies.registry import StrategyPluginRegistry, StrategyPluginRunner
from core.strategies.schema_versions import SCHEMA_SIGNAL


class TestStrategyContracts:
    def test_metadata_dataclass(self):
        metadata = StrategyMetadata(strategy_id="alpha1", name="Alpha", version="1.0")
        assert metadata.strategy_id == "alpha1"
        assert asdict(metadata)["risk_profile"] == "standard"

    def test_required_feature_defaults(self):
        feature = RequiredFeature(name="ema_bias")
        assert feature.timeframe == "M1"
        assert feature.lookback == 1
        assert feature.required is True

    def test_signal_validation(self):
        agent = ThresholdAlphaAgent("alpha1", "x", 1.0, -1.0)
        agent.warmup({})
        signal = agent.generate_signal({"x": 2.0}, {})
        assert signal.schema_version == SCHEMA_SIGNAL
        assert signal.side == "buy"
        assert signal.to_dict()["strategy_id"] == "alpha1"

    def test_signal_rejects_invalid_side(self):
        with pytest.raises(ValueError):
            Signal(
                schema_version=SCHEMA_SIGNAL,
                signal_id="sig1",
                strategy_id="alpha1",
                symbol="XAUUSD",
                side="invalid",
                strength=0.1,
                confidence=0.1,
                generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            )

    def test_signal_rejects_invalid_confidence(self):
        with pytest.raises(ValueError):
            Signal(
                schema_version=SCHEMA_SIGNAL,
                signal_id="sig1",
                strategy_id="alpha1",
                symbol="XAUUSD",
                side="buy",
                strength=0.1,
                confidence=2.0,
                generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
            )

    def test_health_validation(self):
        assert StrategyHealth(status="healthy").status == "healthy"
        with pytest.raises(ValueError):
            StrategyHealth(status="unknown")


class TestStrategyRegistryRunner:
    def test_register_and_list_metadata(self):
        registry = StrategyPluginRegistry()
        agent = ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0)
        registry.register(agent)
        assert registry.get("alpha1") is agent
        assert registry.list_metadata()[0]["strategy_id"] == "alpha1"

    def test_duplicate_registration_rejected(self):
        registry = StrategyPluginRegistry()
        registry.register(ThresholdAlphaAgent("alpha1", "x", 1.0, -1.0))
        with pytest.raises(ValueError):
            registry.register(ThresholdAlphaAgent("alpha1", "x", 1.0, -1.0))

    def test_remove_agent(self):
        registry = StrategyPluginRegistry()
        registry.register(ThresholdAlphaAgent("alpha1", "x", 1.0, -1.0))
        registry.remove("alpha1")
        assert registry.get("alpha1") is None

    def test_runner_warmup_and_run_all(self):
        registry = StrategyPluginRegistry()
        registry.register(ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0))
        runner = StrategyPluginRunner(registry)
        assert runner.run_all({"ema_bias": 2.0}, {}) == []
        runner.warmup_all({})
        signals = runner.run_all({"ema_bias": 2.0}, {})
        assert len(signals) == 1
        assert signals[0].side == "buy"

    def test_runner_skips_degraded_agents(self):
        registry = StrategyPluginRegistry()
        registry.register(ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0))
        runner = StrategyPluginRunner(registry)
        assert runner.health_report()["strategy_count"] == 1
        assert runner.health_report()["healthy_count"] == 0
        assert runner.run_all({"ema_bias": 2.0}, {}) == []

    def test_explain_signal(self):
        agent = ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0)
        agent.warmup({})
        signal = agent.generate_signal({"ema_bias": -2.0}, {})
        explanation = agent.explain(signal)
        assert signal.side == "sell"
        assert explanation["thresholds"]["sell"] == -1.0
