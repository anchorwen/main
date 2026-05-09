"""Trainer Protocol — unified train() interface and TRAINER_REGISTRY.

Every trainer in the system conforms to this protocol. The registry maps
architecture names (e.g. "xgboost", "lightgbm", "deep_res_mlp") to their
train() implementation so the CRT pipeline can dispatch generically.

Usage:
    @register_trainer("deep_res_mlp")
    def train_deep_res_mlp(dataset, recipe, ...) -> TrainResult:
        ...

    # Dispatch
    trainer = TRAINER_REGISTRY["deep_res_mlp"]
    result = trainer(dataset, recipe, output_dir=Path("artifacts/"))
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# ── Result type ──


@dataclass
class TrainResult:
    """Standardised training result returned by all trainers.

    Attributes:
        architecture: architecture key (e.g. "xgboost", "deep_res_mlp").
        model_path: path to the saved artifact (.txt, .onnx, .json, etc.).
        metrics: dict of final evaluation metrics.
        completed_at_utc: ISO-8601 UTC timestamp.
        n_parameters: number of trainable parameters.
        train_samples: number of samples used for training.
        val_metrics: optional dict of validation metrics.
        scaler_path: optional path to scaler/normalization artifact.
        extra: arbitrary additional metadata.
    """

    architecture: str
    model_path: Path
    metrics: dict[str, Any]
    completed_at_utc: str = ""
    n_parameters: int = 0
    train_samples: int = 0
    val_metrics: dict[str, Any] | None = None
    scaler_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trainer": self.architecture,
            "artifact_primary": str(self.model_path),
            "metrics": {"train_finished": True, **self.metrics},
            "completed_at_utc": self.completed_at_utc,
            "n_parameters": self.n_parameters,
            "train_samples": self.train_samples,
            "val_metrics": self.val_metrics,
            "scaler_path": str(self.scaler_path) if self.scaler_path else None,
            **self.extra,
        }


# ── Protocol ──


@runtime_checkable
class TrainerProtocol(Protocol):
    """Callable signature for all training functions."""

    def __call__(
        self,
        dataset: Any,  # TrainingDataset
        recipe: Any,  # TrainingRecipe | None
        *,
        output_dir: Path,
        seed: int = 42,
        checkpoint_dir: Path | None = None,
    ) -> TrainResult: ...


# ── Registry ──

TRAINER_REGISTRY: dict[str, Callable[..., TrainResult]] = {}


def register_trainer(name: str):
    """Decorator to register a trainer function in TRAINER_REGISTRY.

    Usage:
        @register_trainer("xgboost")
        def train_xgboost(dataset, recipe, *, output_dir, seed=42, checkpoint_dir=None):
            ...
    """

    def decorator(fn: Callable[..., TrainResult]) -> Callable[..., TrainResult]:
        if name in TRAINER_REGISTRY:
            raise ValueError(f"Trainer '{name}' is already registered")
        TRAINER_REGISTRY[name] = fn
        return fn

    return decorator


def list_registered_trainers() -> list[str]:
    """Return sorted list of registered trainer names."""
    return sorted(TRAINER_REGISTRY.keys())
