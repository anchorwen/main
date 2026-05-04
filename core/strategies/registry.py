"""Strategy plugin registry and runner."""

from dataclasses import asdict
from typing import Any

from core.strategies.contracts import AlphaAgent, Signal, StrategyHealth, StrategyMetadata
from core.strategies.schema_versions import SCHEMA_STRATEGY_HEALTH_REPORT


class StrategyPluginRegistry:
    """In-memory registry for AlphaAgent strategy plugins."""

    def __init__(self):
        self._agents: dict[str, AlphaAgent] = {}

    def register(self, agent: AlphaAgent) -> None:
        metadata = agent.metadata()
        if metadata.strategy_id in self._agents:
            raise ValueError(f"Strategy already registered: {metadata.strategy_id}")
        self._agents[metadata.strategy_id] = agent

    def remove(self, strategy_id: str) -> None:
        self._agents.pop(strategy_id, None)

    def get(self, strategy_id: str) -> AlphaAgent | None:
        return self._agents.get(strategy_id)

    def list_agents(self) -> list[AlphaAgent]:
        return list(self._agents.values())

    def list_metadata(self) -> list[dict[str, Any]]:
        return [asdict(agent.metadata()) for agent in self.list_agents()]

    def list_healthy_agents(self) -> list[AlphaAgent]:
        return [agent for agent in self.list_agents() if agent.health().status == "healthy"]


class StrategyPluginRunner:
    """Runs registered strategy plugins and returns signals."""

    def __init__(self, registry: StrategyPluginRegistry):
        self._registry = registry

    def warmup_all(self, context: dict[str, Any] | None = None) -> None:
        for agent in self._registry.list_agents():
            agent.warmup(context or {})

    def run_all(self, feature_snapshot: Any, context: dict[str, Any] | None = None) -> list[Signal]:
        signals = []
        for agent in self._registry.list_healthy_agents():
            signals.append(agent.generate_signal(feature_snapshot, context or {}))
        return signals

    def health_report(self) -> dict[str, Any]:
        strategies = []
        for agent in self._registry.list_agents():
            metadata: StrategyMetadata = agent.metadata()
            health: StrategyHealth = agent.health()
            strategies.append(
                {
                    "strategy_id": metadata.strategy_id,
                    "name": metadata.name,
                    "version": metadata.version,
                    "status": health.status,
                    "message": health.message,
                    "metrics": health.metrics,
                }
            )
        return {
            "schema_version": SCHEMA_STRATEGY_HEALTH_REPORT,
            "strategy_count": len(strategies),
            "healthy_count": len([s for s in strategies if s["status"] == "healthy"]),
            "strategies": strategies,
        }
