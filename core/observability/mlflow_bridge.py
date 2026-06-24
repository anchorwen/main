"""Optional MLflow / W&B integration bridge.

All functions gracefully degrade when the external package is not installed.
Usage is fire-and-forget:

    from core.observability.mlflow_bridge import log_training_run
    log_training_run(run_name="xgb_v10", params={...}, metrics={...}, artifacts=[...])
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MLFLOW_AVAILABLE = False
_WANDB_AVAILABLE = False

try:
    import mlflow  # noqa: F401

    _MLFLOW_AVAILABLE = True
except ImportError:
    pass

try:
    import wandb  # noqa: F401

    _WANDB_AVAILABLE = True
except ImportError:
    pass


def _ensure_dirs() -> Path:
    path = Path("data/mlflow_artifacts")
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_training_run(
    run_name: str,
    *,
    params: dict[str, Any] | None = None,
    metrics: dict[str, float] | None = None,
    tags: dict[str, str] | None = None,
    artifacts: list[str | Path] | None = None,
    backend: str = "auto",
) -> dict[str, str]:
    """Log a training run to MLflow or W&B (best-effort).

    Args:
        run_name: Unique name for this run.
        params: Hyperparameters.
        metrics: Evaluation metrics (all float).
        tags: Arbitrary key-value tags.
        artifacts: Paths to model files / ONNX / configs to upload.
        backend: "mlflow", "wandb", or "auto" (tries mlflow first, then wandb).

    Returns:
        dict with ``backend``, ``run_id``, and ``status`` keys.
    """
    result: dict[str, str] = {"backend": "none", "run_id": "", "status": "skipped"}

    if backend == "auto":
        if _MLFLOW_AVAILABLE:
            backend = "mlflow"
        elif _WANDB_AVAILABLE:
            backend = "wandb"
        else:
            result["status"] = "no_backend_available"
            return result

    params = params or {}
    metrics = metrics or {}
    tags = tags or {}
    artifacts = artifacts or []

    try:
        if backend == "mlflow" and _MLFLOW_AVAILABLE:
            import mlflow

            artifact_dir = _ensure_dirs() / run_name
            artifact_dir.mkdir(parents=True, exist_ok=True)

            mlflow.set_experiment("quant_os_training")
            with mlflow.start_run(run_name=run_name) as run:
                mlflow.log_params(params)
                mlflow.log_metrics(metrics)
                mlflow.set_tags(tags)
                for art in artifacts:
                    mlflow.log_artifact(str(art))
            result.update(
                {
                    "backend": "mlflow",
                    "run_id": run.info.run_id if run else "",
                    "status": "logged",
                }
            )
        elif backend == "wandb" and _WANDB_AVAILABLE:
            import wandb

            run = wandb.init(
                project="quant_os_training",
                name=run_name,
                config={**params, **tags},
                reinit=True,
            )
            run.log(metrics)
            for art in artifacts:
                run.save(str(art))
            wandb.finish()
            result.update(
                {
                    "backend": "wandb",
                    "run_id": run.id if run else "",
                    "status": "logged",
                }
            )
        else:
            result["status"] = f"backend_{backend}_not_available"
    except Exception as exc:  # BLE001:FOG
        logger.warning("mlflow_bridge: %s logging failed for %s: %s", backend, run_name, exc)
        result["status"] = f"error: {exc}"
    return result


def log_metric(key: str, value: float, *, step: int = 0) -> None:
    """Log a single metric mid-training (non-blocking, no-op if unavailable)."""
    try:
        if _MLFLOW_AVAILABLE:
            import mlflow

            if mlflow.active_run():
                mlflow.log_metric(key, value, step=step)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        pass
def is_available() -> dict[str, bool]:
    """Return which backends are available in the current environment."""
    return {"mlflow": _MLFLOW_AVAILABLE, "wandb": _WANDB_AVAILABLE}
