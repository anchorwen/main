from core.brains.services.brain_registry_loader import BrainRegistryLoader
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer


def build_v9_shadow_runtime_loop():
    """Legacy entry point — builds a RuntimeLoop via ServiceContainer."""
    container = build_v9_shadow_container()
    return container.build_runtime_loop()


def build_v9_shadow_container() -> ServiceContainer:
    """Build a fully wired ServiceContainer for V9 shadow mode."""
    config = EnvironmentConfig.development(
        base_dir="D:\\cursor\\data",
        enable_feedback_loop=True,
        enable_audit_log=True,
        enable_metrics=True,
        enable_idempotency=False,
    )
    container = ServiceContainer(config).build()

    loader = BrainRegistryLoader()
    try:
        brain_entry = loader.load_json("D:\\cursor\\configs\\brains\\v9_institutional_01.json")
        container.brain_registry.register(brain_entry)
        container.governance_service.register_brain(
            brain_entry.get("brain_id", "v9_institutional_01"),
            "live",
        )
    except FileNotFoundError:
        pass

    return container


def build_v9_shadow_orchestrator():
    """Build a fully wired Orchestrator for V9 shadow mode."""
    container = build_v9_shadow_container()
    return container.build_orchestrator()
