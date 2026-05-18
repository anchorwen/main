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


def _wire_meta_pipeline(container: ServiceContainer, repo_root: Path) -> None:
    """Wire the two-stage meta-labeling pipeline into the container.

    Stage 1: Meta_Stage1_Huber_V1 (LightGBM Huber regression) — registered as
    a shadow brain.

    Stage 2: MetaSignalFilter (LightGBM binary classifier + optional MLP ensemble)
    — attached to the container for runtime signal filtering.
    """
    loader = BrainRegistryLoader()

    # ── Stage 1: Register Huber regression brain ──
    stage1_config_path = repo_root / "configs" / "brains" / "meta_stage1_huber_v1.json"
    stage1_model = (
        repo_root
        / "data"
        / "models"
        / "institutional"
        / "barrier_12bar_regression_huber_20260516_105558.txt"
    )
    if stage1_config_path.exists() and stage1_model.exists():
        stage1_entry = loader.load_json(str(stage1_config_path))
        stage1_entry["artifact_path"] = str(stage1_model.resolve())
        container.brain_registry.register(stage1_entry)
        container.governance_service.register_brain(
            stage1_entry.get("brain_id", "Meta_Stage1_Huber_V1"),
            "shadow",
        )

    # ── Stage 2: Load MetaSignalFilter ──
    filter_config_path = repo_root / "configs" / "brains" / "meta_stage2_filter_v3.json"
    if filter_config_path.exists():
        import json

        from core.execution.meta_signal_filter import MetaSignalFilter

        with open(filter_config_path, encoding="utf-8") as f:
            fc = json.load(f)

        model_path = fc.get("model_path", "")
        resolved_model = (
            repo_root / model_path if not Path(model_path).is_absolute() else Path(model_path)
        )

        if resolved_model.exists():
            # Resolve MLP model path
            mlp_path = fc.get("mlp_model_path", "")
            resolved_mlp: str | None = None
            if mlp_path:
                candidate_mlp = (
                    repo_root / mlp_path if not Path(mlp_path).is_absolute() else Path(mlp_path)
                )
                if candidate_mlp.exists():
                    resolved_mlp = str(candidate_mlp)

            # Resolve micro scaler path
            scaler_path = fc.get("micro_scaler_path", "")
            resolved_scaler: str | None = None
            if scaler_path:
                candidate_scaler = (
                    repo_root / scaler_path
                    if not Path(scaler_path).is_absolute()
                    else Path(scaler_path)
                )
                if candidate_scaler.exists():
                    resolved_scaler = str(candidate_scaler)

            # Resolve calibrator path (Platt scaling)
            calibrator_path = fc.get("calibrator_path", "")
            resolved_calibrator: str | None = None
            if calibrator_path:
                candidate_cal = (
                    repo_root / calibrator_path
                    if not Path(calibrator_path).is_absolute()
                    else Path(calibrator_path)
                )
                if candidate_cal.exists():
                    resolved_calibrator = str(candidate_cal)

            # Parse conformal prediction config
            conformal_cfg = fc.get("conformal", {})
            conformal_mode = bool(conformal_cfg.get("enabled", False))
            conformal_window = int(conformal_cfg.get("window", 500))
            conformal_percentile = float(conformal_cfg.get("percentile", 80.0))
            conformal_min_threshold = float(conformal_cfg.get("min_threshold", 0.50))

            # Parse ensemble weights from config
            raw_weights = fc.get("ensemble_weights", [0.6, 0.4])
            ensemble_weights = (
                (float(raw_weights[0]), float(raw_weights[1])) if len(raw_weights) >= 2 else None
            )

            filt = MetaSignalFilter(
                model_path=str(resolved_model),
                mlp_model_path=resolved_mlp,
                threshold=fc.get("threshold", 0.65),
                enabled=True,
                mode=fc.get("mode", "binary"),
                ensemble_weights=ensemble_weights,
                micro_scaler_path=resolved_scaler,
                calibrator_path=resolved_calibrator,
                conformal_mode=conformal_mode,
                conformal_window=conformal_window,
                conformal_percentile=conformal_percentile,
                min_threshold=conformal_min_threshold,
            )
            if filt.load():
                container.meta_signal_filter = filt
                import sys

                sys.stderr.write(
                    json.dumps(
                        {
                            "event": "meta_pipeline_wired",
                            "stage1_brain": "Meta_Stage1_Huber_V1",
                            "stage2_filter": str(resolved_model),
                            "threshold": fc.get("threshold", 0.65),
                            "features": len(filt._feature_names),
                            "mlp_loaded": filt._mlp_model is not None,
                            "lgb_loaded": filt._model is not None,
                            "calibrator_loaded": filt._calibrator is not None,
                            "conformal_enabled": filt._conformal_mode,
                            "ensemble_weights": list(filt._ensemble_weights),
                            "micro_scaler_loaded": filt._micro_scaler is not None,
                        }
                    )
                    + "\n"
                )


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
    # Use LightGBM V3 — the active institutional brain for barrier_12bar
    brain_path = (
        repo_root / "configs" / "brains" / "lgb_barrier_12bar_lightgbm_v3_20260517_084114.json"
    )
    loader = BrainRegistryLoader()
    brain_entry = loader.load_json(str(brain_path))
    brain_entry["enable_onnxruntime"] = False

    container.brain_registry.register(brain_entry)
    container.governance_service.register_brain(
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

    # ── Two-Stage Meta-Labeling: Stage 1 Huber + Stage 2 Filter ──
    _wire_meta_pipeline(container, repo_root)

    return container


def build_v9_shadow_orchestrator():
    """Build a fully wired Orchestrator for V9 shadow mode."""
    container = build_v9_shadow_container()
    return container.build_orchestrator()
