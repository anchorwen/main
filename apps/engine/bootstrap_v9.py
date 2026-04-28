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
    brain_path = repo_root / "configs" / "brains" / "v9_institutional_01.json"
    loader = BrainRegistryLoader()
    brain_entry = loader.load_json(str(brain_path))
    # Registry JSON may ship with developer-local paths; resolve portable paths from repo root
    # so BrainFactory can always open normalization config (CI runners are not always `D:\cursor`).
    norm_path = repo_root / "configs" / "brains" / "v9_institutional_01.normalization.json"
    brain_entry["normalization_config_path"] = str(norm_path.resolve())
    artifact_path = Path(brain_entry.get("artifact_path", ""))
    repo_onnx = repo_root / "configs" / "brains" / "v9_institutional_brain.onnx"
    if repo_onnx.is_file():
        brain_entry["artifact_path"] = str(repo_onnx.resolve())
        brain_entry["enable_onnxruntime"] = bool(brain_entry.get("enable_onnxruntime", False))
    elif not artifact_path.is_file():
        # Missing ONNX artifact → deterministic numpy stub in V9OnnxBrainAdapter (matches CI/test baseline).
        brain_entry["enable_onnxruntime"] = False

    container.brain_registry.register(brain_entry)
    container.governance_service.register_brain(
        brain_entry.get("brain_id", "v9_institutional_01"),
        "live",
    )

    return container


def build_v9_shadow_orchestrator():
    """Build a fully wired Orchestrator for V9 shadow mode."""
    container = build_v9_shadow_container()
    return container.build_orchestrator()
