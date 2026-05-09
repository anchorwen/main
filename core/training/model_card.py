"""Model Card generator — produces model_card.json for deployment awareness.

A model_card.json is the "label on the bottle" for every model artifact deployed
to inference. It records feature order, type contracts, preprocessing parameters,
training provenance, and performance characteristics so the serving layer can
validate inputs without reading source code.

Inspired by Google Model Card Toolkit and HuggingFace model cards, stripped down
for algorithmic trading deployment.

Usage:
    gen = ModelCardGenerator()
    card = gen.generate(
        architecture="deep_res_mlp",
        model_path=Path("data/models/deep_res_mlp_v1.onnx"),
        scaler_params={"mean": [...], "std": [...], "n_features": 40},
        feature_names=[f"f_{i}" for i in range(40)],
        metrics={"train_accuracy": 0.72, "best_val_accuracy": 0.68},
        recipe_id="deep-res-mlp-g2026.1",
    )
    gen.save(card, Path("data/models/deep_res_mlp_v1.model_card.json"))
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODEL_CARD_SCHEMA = "model_card.v2"


@dataclass
class ModelCard:
    """Structured metadata for a deployed model artifact."""

    schema_version: str = MODEL_CARD_SCHEMA
    architecture: str = ""
    model_name: str = ""
    model_path: str = ""
    created_at_utc: str = ""

    # Input contract
    n_features: int = 0
    feature_names: list[str] = field(default_factory=list)
    feature_order: list[str] = field(default_factory=list)  # canonical serving order
    input_dtype: str = "float64"

    # Preprocessing
    scaler_params: dict[str, Any] = field(default_factory=dict)  # {"mean": [...], "std": [...]}
    normalization_strategy: str = "standard"
    missing_value_policy: str = "error"

    # Training provenance
    recipe_id: str = ""
    training_dataset_id: str = ""
    train_samples: int = 0
    class_balance: dict[str, float] = field(default_factory=dict)

    # Performance
    metrics: dict[str, Any] = field(default_factory=dict)
    val_metrics: dict[str, Any] | None = None
    n_parameters: int = 0

    # Output contract
    output_names: list[str] = field(default_factory=list)  # ["direction", "risk", "vol"]
    output_dtypes: list[str] = field(default_factory=list)  # ["float32", "float32", "float32"]

    # Extra
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "architecture": self.architecture,
            "model_name": self.model_name,
            "model_path": self.model_path,
            "created_at_utc": self.created_at_utc,
            "input_contract": {
                "n_features": self.n_features,
                "feature_names": self.feature_names,
                "feature_order": self.feature_order or self.feature_names,
                "input_dtype": self.input_dtype,
            },
            "preprocessing": {
                "normalization_strategy": self.normalization_strategy,
                "scaler_params": self.scaler_params,
                "missing_value_policy": self.missing_value_policy,
            },
            "training_provenance": {
                "recipe_id": self.recipe_id,
                "training_dataset_id": self.training_dataset_id,
                "train_samples": self.train_samples,
                "class_balance": self.class_balance,
            },
            "performance": {
                "metrics": self.metrics,
                "val_metrics": self.val_metrics,
                "n_parameters": self.n_parameters,
            },
            "output_contract": {
                "output_names": self.output_names,
                "output_dtypes": self.output_dtypes,
            },
        }
        if self.tags:
            d["tags"] = self.tags
        if self.notes:
            d["notes"] = self.notes
        if self.extra:
            d["extra"] = self.extra
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelCard:
        inp = data.get("input_contract", {})
        prep = data.get("preprocessing", {})
        prov = data.get("training_provenance", {})
        perf = data.get("performance", {})
        out = data.get("output_contract", {})
        return cls(
            schema_version=data.get("schema_version", MODEL_CARD_SCHEMA),
            architecture=data.get("architecture", ""),
            model_name=data.get("model_name", ""),
            model_path=data.get("model_path", ""),
            created_at_utc=data.get("created_at_utc", ""),
            n_features=inp.get("n_features", 0),
            feature_names=inp.get("feature_names", []),
            feature_order=inp.get("feature_order", []),
            scaler_params=prep.get("scaler_params", {}),
            normalization_strategy=prep.get("normalization_strategy", "standard"),
            recipe_id=prov.get("recipe_id", ""),
            training_dataset_id=prov.get("training_dataset_id", ""),
            train_samples=prov.get("train_samples", 0),
            class_balance=prov.get("class_balance", {}),
            metrics=perf.get("metrics", {}),
            val_metrics=perf.get("val_metrics"),
            n_parameters=perf.get("n_parameters", 0),
            output_names=out.get("output_names", []),
            tags=data.get("tags", []),
            notes=data.get("notes", ""),
            extra=data.get("extra", {}),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> ModelCard:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.architecture:
            issues.append("architecture is required")
        if not self.model_path:
            issues.append("model_path is required")
        if self.n_features <= 0:
            issues.append("n_features must be positive")
        if self.feature_order and len(self.feature_order) != self.n_features:
            issues.append(
                f"feature_order length ({len(self.feature_order)}) != n_features ({self.n_features})"
            )
        return issues


class ModelCardGenerator:
    """Convenience builder for ModelCard with sensible defaults."""

    def generate(
        self,
        *,
        architecture: str,
        model_path: Path,
        feature_names: list[str],
        metrics: dict[str, Any],
        scaler_params: dict[str, Any] | None = None,
        recipe_id: str = "",
        dataset_id: str = "",
        train_samples: int = 0,
        class_balance: dict[str, float] | None = None,
        val_metrics: dict[str, Any] | None = None,
        n_parameters: int = 0,
        output_names: list[str] | None = None,
        model_name: str = "",
        tags: list[str] | None = None,
        notes: str = "",
        extra: dict[str, Any] | None = None,
    ) -> ModelCard:
        now = (
            datetime.now(UTC)
            .replace(tzinfo=None)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

        return ModelCard(
            architecture=architecture,
            model_name=model_name or f"{architecture}",
            model_path=str(Path(model_path).resolve()),
            created_at_utc=now,
            n_features=len(feature_names),
            feature_names=feature_names,
            feature_order=list(feature_names),
            scaler_params=scaler_params or {},
            recipe_id=recipe_id,
            training_dataset_id=dataset_id,
            train_samples=train_samples,
            class_balance=class_balance or {},
            metrics=metrics,
            val_metrics=val_metrics,
            n_parameters=n_parameters,
            output_names=output_names or [],
            output_dtypes=["float32"] * len(output_names or []),
            tags=tags or [],
            notes=notes,
            extra=extra or {},
        )

    def save(self, card: ModelCard, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(card.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return path
