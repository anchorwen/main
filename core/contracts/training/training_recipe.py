"""Training Recipe — the single source of truth for one reproducible training run.

A Training Recipe combines model identity, label contract reference, data config,
training hyperparameters, and evaluation criteria. Two recipes that differ only by
seed produce the same model family.

Design principle: Recipe is the ONLY entry point for training. No ad-hoc CLI flags.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "training_recipe.v1"

# ── Supported values ──
VALID_ARCHITECTURES = {
    "mlp_multihead",
    "deep_res_mlp",
    "transformer",
    "xgboost",
    "lightgbm",
    "ou_params",
    "online_mlp",
    "online_sgd",
}
VALID_OPTIMIZERS = {"adam", "adamw", "sgd"}
VALID_NORM_STRATEGIES = {"fixed", "rolling_ewma", "rank"}
VALID_ROLES = {"prd", "chlg", "cabl", "stub"}
VALID_METRICS = {
    "accuracy",
    "precision",
    "recall",
    "f1",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "profit_factor",
    "win_rate",
    "avg_pnl",
    "direction_bias",
    "regime_consistency",
}
VALID_STABILITY_CHECKS = {
    "walk_forward",
    "seed_sensitivity",
    "regime_consistency",
    "data_augmentation_robustness",
}


@dataclass
class ModelIdentity:
    lane: str  # e.g. "sur", "mtx", "arb"
    role: str  # prd | chlg | cabl | stub
    generation: str  # e.g. "g2026.1"
    feature_contract_id: str  # e.g. "feat-v9-institutional-1.0.0"


@dataclass
class LabelContractRef:
    contract_id: str  # e.g. "label-survival-barrier-1.0.0"
    contract_path: str | None = None  # optional relative path


@dataclass
class DataAugmentation:
    enabled: bool = False
    volatility_scaling: list[float] = field(default_factory=lambda: [0.7, 0.85, 1.0, 1.15, 1.3])
    noise_std: float = 0.01


@dataclass
class DataConfig:
    dataset_slice_id: str
    train_date_range: tuple[str, str] | None = None
    val_date_range: tuple[str, str] | None = None
    min_samples_per_class: int = 100
    normalization_strategy: str = "fixed"
    normalization_halflife_days: int = 63
    data_augmentation: DataAugmentation = field(default_factory=DataAugmentation)


@dataclass
class TrainingConfig:
    epochs: int = 200
    seeds: list[int] = field(default_factory=lambda: [42])
    architecture: str = "mlp_multihead"
    input_dim: int = 40
    hidden_dims: list[int] = field(default_factory=lambda: [128, 64, 32])
    dropout: float = 0.3
    loss_weights: dict[str, float] = field(
        default_factory=lambda: {"direction": 1.0, "risk": 0.5, "volatility": 0.3}
    )
    optimizer: str = "adam"
    learning_rate: float = 0.001
    batch_size: int = 256
    early_stopping_patience: int = 20


@dataclass
class EvaluationConfig:
    metrics: list[str] = field(default_factory=lambda: ["accuracy", "f1", "sharpe_ratio"])
    regime_splits: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    stability_checks: list[str] = field(
        default_factory=lambda: ["walk_forward", "seed_sensitivity"]
    )


@dataclass
class TrainingRecipe:
    """Immutable specification of one complete training run.

    Usage:
        recipe = TrainingRecipe.from_file("blueprints/recipes/sur-g2026.1-recipe-001.json")
        issues = recipe.validate()
        cli_args = recipe.to_trainer_args()
    """

    schema_version: str
    recipe_id: str
    model_identity: ModelIdentity
    label_contract_ref: LabelContractRef
    data: DataConfig
    training: TrainingConfig
    evaluation: EvaluationConfig
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Expected schema_version={SCHEMA_VERSION}, got {self.schema_version}")

    # ── Factories ──

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingRecipe:
        """Parse from a validated dictionary."""
        mi = data["model_identity"]
        lcr = data["label_contract_ref"]
        d = data["data"]
        t = data["training"]
        e = data["evaluation"]

        # Parse data augmentation if present
        da_dict = d.get("data_augmentation", {})
        da = DataAugmentation(
            enabled=da_dict.get("enabled", False),
            volatility_scaling=da_dict.get("volatility_scaling", [0.7, 0.85, 1.0, 1.15, 1.3]),
            noise_std=da_dict.get("noise_std", 0.01),
        )

        # Parse date ranges
        train_dr = d.get("train_date_range")
        val_dr = d.get("val_date_range")

        return cls(
            schema_version=data["schema_version"],
            recipe_id=data["recipe_id"],
            model_identity=ModelIdentity(
                lane=mi["lane"],
                role=mi["role"],
                generation=mi["generation"],
                feature_contract_id=mi["feature_contract_id"],
            ),
            label_contract_ref=LabelContractRef(
                contract_id=lcr["contract_id"],
                contract_path=lcr.get("contract_path"),
            ),
            data=DataConfig(
                dataset_slice_id=d["dataset_slice_id"],
                train_date_range=tuple(train_dr) if train_dr else None,
                val_date_range=tuple(val_dr) if val_dr else None,
                min_samples_per_class=d.get("min_samples_per_class", 100),
                normalization_strategy=d.get("normalization_strategy", "fixed"),
                normalization_halflife_days=d.get("normalization_halflife_days", 63),
                data_augmentation=da,
            ),
            training=TrainingConfig(
                epochs=t["epochs"],
                seeds=t.get("seeds", [42]),
                architecture=t.get("architecture", "mlp_multihead"),
                input_dim=t.get("input_dim", 40),
                hidden_dims=t.get("hidden_dims", [128, 64, 32]),
                dropout=t.get("dropout", 0.3),
                loss_weights=t.get(
                    "loss_weights", {"direction": 1.0, "risk": 0.5, "volatility": 0.3}
                ),
                optimizer=t.get("optimizer", "adam"),
                learning_rate=t.get("learning_rate", 0.001),
                batch_size=t.get("batch_size", 256),
                early_stopping_patience=t.get("early_stopping_patience", 20),
            ),
            evaluation=EvaluationConfig(
                metrics=e.get("metrics", ["accuracy", "f1"]),
                regime_splits=e.get("regime_splits", {}),
                stability_checks=e.get("stability_checks", ["walk_forward", "seed_sensitivity"]),
            ),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> TrainingRecipe:
        """Load a Training Recipe from a JSON file."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to dict (round-trippable)."""
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "recipe_id": self.recipe_id,
            "model_identity": {
                "lane": self.model_identity.lane,
                "role": self.model_identity.role,
                "generation": self.model_identity.generation,
                "feature_contract_id": self.model_identity.feature_contract_id,
            },
            "label_contract_ref": {
                "contract_id": self.label_contract_ref.contract_id,
            },
            "data": {
                "dataset_slice_id": self.data.dataset_slice_id,
                "normalization_strategy": self.data.normalization_strategy,
                "normalization_halflife_days": self.data.normalization_halflife_days,
                "data_augmentation": {
                    "enabled": self.data.data_augmentation.enabled,
                    "volatility_scaling": self.data.data_augmentation.volatility_scaling,
                    "noise_std": self.data.data_augmentation.noise_std,
                },
            },
            "training": {
                "epochs": self.training.epochs,
                "seeds": self.training.seeds,
                "architecture": self.training.architecture,
                "input_dim": self.training.input_dim,
                "hidden_dims": self.training.hidden_dims,
                "dropout": self.training.dropout,
                "loss_weights": self.training.loss_weights,
                "optimizer": self.training.optimizer,
                "learning_rate": self.training.learning_rate,
                "batch_size": self.training.batch_size,
                "early_stopping_patience": self.training.early_stopping_patience,
            },
            "evaluation": {
                "metrics": self.evaluation.metrics,
                "regime_splits": self.evaluation.regime_splits,
                "stability_checks": self.evaluation.stability_checks,
            },
        }
        if self.label_contract_ref.contract_path:
            d["label_contract_ref"]["contract_path"] = self.label_contract_ref.contract_path
        if self.data.train_date_range:
            d["data"]["train_date_range"] = list(self.data.train_date_range)
        if self.data.val_date_range:
            d["data"]["val_date_range"] = list(self.data.val_date_range)
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    # ── CLI argument generation ──

    def to_trainer_args(self) -> list[str]:
        """Generate CLI arguments for the lane trainer.

        Converts the recipe into a flat list of --key value pairs that the
        lane trainer's argparse can consume.

        Example:
            ['--epochs', '200', '--lr', '0.001', '--batch-size', '256', ...]
        """
        t = self.training
        d = self.data
        e = self.evaluation

        args: list[str] = [
            "--recipe-id",
            self.recipe_id,
            "--label-contract",
            self.label_contract_ref.contract_id,
            "--epochs",
            str(t.epochs),
            "--lr",
            str(t.learning_rate),
            "--batch-size",
            str(t.batch_size),
            "--dropout",
            str(t.dropout),
            "--optimizer",
            t.optimizer,
            "--architecture",
            t.architecture,
            "--input-dim",
            str(t.input_dim),
            "--hidden-dims",
            ",".join(str(h) for h in t.hidden_dims),
            "--normalization",
            d.normalization_strategy,
            "--dataset-slice-id",
            d.dataset_slice_id,
        ]

        if d.normalization_strategy == "rolling_ewma":
            args.extend(["--norm-halflife", str(d.normalization_halflife_days)])

        if d.data_augmentation.enabled:
            args.append("--augment")
            args.extend(["--augment-noise", str(d.data_augmentation.noise_std)])
            args.extend(
                [
                    "--augment-vol-scales",
                    ",".join(str(v) for v in d.data_augmentation.volatility_scaling),
                ]
            )

        if e.metrics:
            args.extend(["--metrics", ",".join(e.metrics)])

        if self.label_contract_ref.contract_path:
            args.extend(["--label-contract-path", self.label_contract_ref.contract_path])

        return args

    # ── Validation ──

    def validate(self) -> list[str]:
        """Run self-consistency checks. Returns list of issues (empty = valid)."""
        issues: list[str] = []

        # Model identity
        mi = self.model_identity
        if mi.role not in VALID_ROLES:
            issues.append(f"Invalid role '{mi.role}'. Must be one of {VALID_ROLES}")

        # Architecture ↔ lane compatibility hints
        lane_arch_hints = {
            "sur": "mlp_multihead",
            "deepresmlp": "deep_res_mlp",
            "mtx": "transformer",
            "arb": "ou_params",
            "xgbinrepo": "xgboost",
            "lgbinrepo": "lightgbm",
            "online_mlp": "online_mlp",
            "online_sgd": "online_sgd",
        }
        expected_arch = lane_arch_hints.get(mi.lane)
        if expected_arch and self.training.architecture != expected_arch:
            issues.append(
                f"Lane '{mi.lane}' typically uses architecture '{expected_arch}', "
                f"but recipe specifies '{self.training.architecture}'"
            )

        # Training
        t = self.training
        if t.architecture not in VALID_ARCHITECTURES:
            issues.append(
                f"Invalid architecture '{t.architecture}'. Must be one of {VALID_ARCHITECTURES}"
            )
        if t.optimizer not in VALID_OPTIMIZERS:
            issues.append(f"Invalid optimizer '{t.optimizer}'. Must be one of {VALID_OPTIMIZERS}")
        if t.epochs < 1:
            issues.append("epochs must be >= 1")
        if not t.seeds:
            issues.append("at least one seed is required")
        if t.learning_rate <= 0:
            issues.append("learning_rate must be positive")
        if t.batch_size < 8:
            issues.append("batch_size must be >= 8")

        # Data
        d = self.data
        if d.normalization_strategy not in VALID_NORM_STRATEGIES:
            issues.append(f"Invalid normalization_strategy '{d.normalization_strategy}'")

        # Evaluation
        e = self.evaluation
        invalid_metrics = set(e.metrics) - VALID_METRICS
        if invalid_metrics:
            issues.append(f"Invalid metrics: {invalid_metrics}")
        invalid_checks = set(e.stability_checks) - VALID_STABILITY_CHECKS
        if invalid_checks:
            issues.append(f"Invalid stability_checks: {invalid_checks}")

        return issues
