from pathlib import Path

from core.brains.services.brain_registry_loader import BrainRegistryLoader
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer


def _repo_root() -> Path:
    # apps/engine/bootstrap_v9.py -> repository root
    return Path(__file__).resolve().parents[2]


def _resolve_data_base_dir() -> str:
    """Prefer the canonical shadow ledger root used by tests (D:/cursor/data) when present."""
    repo_data = _repo_root() / "data"
    repo_data.mkdir(parents=True, exist_ok=True)
    junction_data = Path("D:/cursor/data")
    if junction_data.exists():
        return str(junction_data)
    return str(repo_data)


def build_v9_shadow_runtime_loop():
    """Legacy entry point — builds a RuntimeLoop via ServiceContainer."""
    container = build_v9_shadow_container()
    return container.build_runtime_loop()


def build_v9_shadow_container() -> ServiceContainer:
    """Build a fully wired ServiceContainer for V9 shadow mode."""
    config = EnvironmentConfig.development(
        base_dir=_resolve_data_base_dir(),
        enable_feedback_loop=True,
        enable_audit_log=True,
        enable_metrics=True,
        enable_idempotency=False,
    )
    container = ServiceContainer(config).build()

    repo_root = _repo_root()
    # Use DeepResMLP V2 — the active institutional brain that superseded V9 ONNX
    brain_path = repo_root / "configs" / "brains" / "deepresmlp_v2_new.json"
    loader = BrainRegistryLoader()
    brain_entry = loader.load_json(str(brain_path))
    brain_entry["enable_onnxruntime"] = False

    container.brain_registry.register(brain_entry)  # type: ignore[reportOptionalMemberAccess]
    container.governance_service.register_brain(  # type: ignore[reportOptionalMemberAccess]
        brain_entry.get("brain_id", "DeepResMLP_V2_New"),
        "live",
    )

    # Register Online SGD V1 if config and artifact exist
    online_brain_path = repo_root / "configs" / "brains" / "online_learner_v1.json"
    online_weights = repo_root / "data" / "models" / "online_learner_weights.json"
    if online_brain_path.exists() and online_weights.exists():
        online_entry = loader.load_json(str(online_brain_path))
        online_entry["artifact_path"] = str(online_weights.resolve())
        container.brain_registry.register(online_entry)
        container.governance_service.register_brain(
            online_entry.get("brain_id", "Online_SGD_V1"),
            "candidate",
        )

    return container


def build_v9_shadow_orchestrator():
    """Build a fully wired Orchestrator for V9 shadow mode."""
    container = build_v9_shadow_container()
    return container.build_orchestrator()
