"""TrainingContract v2.1 — single-contract, single-command training specification.

Replaces the multi-script pipeline with a YAML-driven contract that bundles
dataset, label, architecture, validation, quality gates, and output targets
into one auditable specification.

Usage:
    contract = TrainingContract.from_yaml("configs/training/barrier_12bar_xgboost.yaml")
    issues = contract.validate()
    if issues:
        for i in issues:
            print(f"  - {i}")
    else:
        run_pipeline(contract)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "training_contract.v2.1"

VALID_ARCHITECTURES = {
    "xgboost",
    "lightgbm",
    "deep_res_mlp",
    "transformer",
    "online_mlp",
    "online_sgd",
    "ou_params",
}
VALID_VALIDATION_METHODS = {"cpcv", "purged_wfo", "wfo", "random"}
VALID_OBJECTIVE_FUNCTIONS = {
    "binary_logloss",
    "multi_logloss",
    "reg_squarederror",
    "reg_huber",
    "custom_sharpe",
    "custom_profit_factor",
    "custom_weighted_logloss",
}
VALID_SAMPLE_WEIGHTING = {
    "none",
    "return_magnitude",
    "inverse_class_frequency",
    "abs_target",
    "loss_penalty",
}
VALID_STATUSES = {"shadow", "live", "retired", "failed"}


# ── Spec dataclasses ──


@dataclass
class DatasetSpec:
    path: str
    feature_schema: str = "v9_institutional_40"
    date_range: tuple[str, str] | None = None
    min_samples_per_class: int = 100
    sample_weighting: str = "none"
    loss_penalty_factor: float = 2.0  # used only when sample_weighting="loss_penalty"

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.path:
            issues.append("dataset.path is required")
        if self.sample_weighting not in VALID_SAMPLE_WEIGHTING:
            issues.append(
                f"Invalid sample_weighting '{self.sample_weighting}'. "
                f"Must be one of {sorted(VALID_SAMPLE_WEIGHTING)}"
            )
        if self.min_samples_per_class < 10:
            issues.append("min_samples_per_class must be >= 10")
        return issues


@dataclass
class LabelSpec:
    contract_id: str = ""
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.5
    horizon_bars: int = 12
    profitability_calibrated: bool = False
    label_mapping: str | None = None  # "drop_timeout_binary" for triple-barrier → binary
    spread_points: float = 30
    slippage_points: float = 10
    tick_value: float = 0.01
    tick_size: float = 0.001
    vol_scale_target: bool = False
    output_unit: str = (
        "bps"  # "bps" | "atr_multiple" — set to "atr_multiple" when vol_scale_target=True
    )

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.sl_atr_mult <= 0:
            issues.append("sl_atr_mult must be positive")
        if self.tp_atr_mult <= 0:
            issues.append("tp_atr_mult must be positive")
        if self.horizon_bars < 1:
            issues.append("horizon_bars must be >= 1")
        if not self.profitability_calibrated:
            issues.append(
                "Label contract has NOT been calibrated through profitability surface. "
                "SL/TP multipliers may produce negative expected value labels. "
                "Run calibrate_label_contract() to verify and set profitability_calibrated=true."
            )
        if self.spread_points < 0:
            issues.append("spread_points must be >= 0")
        if self.slippage_points < 0:
            issues.append("slippage_points must be >= 0")
        if self.output_unit not in ("bps", "atr_multiple"):
            issues.append(
                f"Invalid output_unit '{self.output_unit}'. Must be 'bps' or 'atr_multiple'."
            )
        return issues


@dataclass
class ArchitectureSpec:
    type: str = "xgboost"
    objective_function: str = "binary_logloss"
    search_space: str = "default"
    optuna_trials: int = 50
    n_seeds: int = 5
    custom_params: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.type not in VALID_ARCHITECTURES:
            issues.append(
                f"Invalid architecture type '{self.type}'. "
                f"Must be one of {sorted(VALID_ARCHITECTURES)}"
            )
        if self.objective_function not in VALID_OBJECTIVE_FUNCTIONS:
            issues.append(
                f"Invalid objective_function '{self.objective_function}'. "
                f"Must be one of {sorted(VALID_OBJECTIVE_FUNCTIONS)}"
            )
        if self.optuna_trials < 0:
            issues.append("optuna_trials must be >= 0")
        if self.n_seeds < 1:
            issues.append("n_seeds must be >= 1")
        return issues


@dataclass
class ValidationSpec:
    method: str = "cpcv"
    n_groups: int = 6
    n_test_groups: int = 2
    purge_bars: int = 12
    embargo_bars: int = 5
    val_ratio: float = 0.15
    test_ratio: float = 0.10

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.method not in VALID_VALIDATION_METHODS:
            issues.append(
                f"Invalid validation method '{self.method}'. "
                f"Must be one of {sorted(VALID_VALIDATION_METHODS)}"
            )
        if self.n_groups < 2:
            issues.append("n_groups must be >= 2")
        if self.n_test_groups < 1 or self.n_test_groups >= self.n_groups:
            issues.append(
                f"n_test_groups ({self.n_test_groups}) must be in [1, {self.n_groups - 1}]"
            )
        if self.purge_bars < 0:
            issues.append("purge_bars must be >= 0")
        if self.embargo_bars < 0:
            issues.append("embargo_bars must be >= 0")
        return issues


@dataclass
class QualityGateSpec:
    # Core metrics
    min_train_sharpe: float = 1.0
    min_train_win_rate: float = 0.50
    min_forward_sharpe: float = 1.0
    min_forward_win_rate: float = 0.50
    max_overfit_gap: float = 0.30

    # Downside-risk-adjusted metrics (replaces max_train_drawdown_pct)
    min_sortino_ratio: float = 0.8
    min_calmar_ratio: float = 0.3
    max_vol_scaled_dd_pct: float = 30.0  # max drawdown after 1%/trade vol scaling

    require_shap_stability: bool = True
    model_type: str = "tree"  # "tree", "deep_learning", "online"

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.min_train_sharpe < 0:
            issues.append("min_train_sharpe must be >= 0")
        if not 0 <= self.min_train_win_rate <= 1:
            issues.append("min_train_win_rate must be in [0, 1]")
        if not 0 <= self.min_forward_win_rate <= 1:
            issues.append("min_forward_win_rate must be in [0, 1]")
        if self.max_overfit_gap < 0:
            issues.append("max_overfit_gap must be >= 0")
        if self.min_sortino_ratio < 0:
            issues.append("min_sortino_ratio must be >= 0")
        if self.min_calmar_ratio < 0:
            issues.append("min_calmar_ratio must be >= 0")
        if not 0 < self.max_vol_scaled_dd_pct <= 100:
            issues.append("max_vol_scaled_dd_pct must be in (0, 100]")

        # Tiered quality gate validation
        if self.model_type == "deep_learning":
            if self.min_forward_sharpe < 0.5:
                issues.append(
                    f"Deep learning models require min_forward_sharpe >= 0.5, got {self.min_forward_sharpe}"
                )
            if self.max_overfit_gap > 2.0:
                issues.append(
                    f"Deep learning models require max_overfit_gap <= 2.0, got {self.max_overfit_gap}"
                )
        elif self.model_type == "tree":
            if self.min_forward_sharpe < 0.20:
                issues.append(
                    f"Tree models require min_forward_sharpe >= 0.20, got {self.min_forward_sharpe}"
                )
            if self.max_overfit_gap > 1.0:
                issues.append(
                    f"Tree models require max_overfit_gap <= 1.0, got {self.max_overfit_gap}"
                )
        elif self.model_type == "online":
            if self.min_forward_sharpe < 0.4:
                issues.append(
                    f"Online models require min_forward_sharpe >= 0.4, got {self.min_forward_sharpe}"
                )
        return issues


@dataclass
class OutputSpec:
    brain_id_template: str = "{arch}_{contract}_{timestamp}"
    model_dir: str = "data/models/institutional"
    config_dir: str = "configs/brains"
    registry_db: str = "data/training/registry.db"
    auto_register: bool = False
    initial_status: str = "shadow"

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.initial_status not in VALID_STATUSES:
            issues.append(
                f"Invalid initial_status '{self.initial_status}'. "
                f"Must be one of {sorted(VALID_STATUSES)}"
            )
        return issues


# ── Main Contract ──


@dataclass
class TrainingContract:
    schema_version: str
    contract_id: str
    dataset: DatasetSpec
    label: LabelSpec
    architecture: ArchitectureSpec
    validation: ValidationSpec
    quality_gates: QualityGateSpec
    output: OutputSpec
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Expected schema_version={SCHEMA_VERSION}, got {self.schema_version}")

    # ── Validation ──

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.contract_id:
            issues.append("contract_id is required")

        issues.extend(self.dataset.validate())
        issues.extend(self.label.validate())
        issues.extend(self.architecture.validate())
        issues.extend(self.validation.validate())
        issues.extend(self.quality_gates.validate())
        issues.extend(self.output.validate())

        # Cross-section consistency checks
        arch = self.architecture.type
        obj = self.architecture.objective_function
        if arch in ("xgboost", "lightgbm") and obj.startswith("custom_"):
            if self.dataset.sample_weighting == "none":
                issues.append(
                    f"Custom objective '{obj}' with tree model benefits from "
                    "sample_weighting='return_magnitude'; got 'none'"
                )

        return issues

    # ── Serialization ──

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "dataset": {
                "path": self.dataset.path,
                "feature_schema": self.dataset.feature_schema,
                "min_samples_per_class": self.dataset.min_samples_per_class,
                "sample_weighting": self.dataset.sample_weighting,
            },
            "label": {
                "contract_id": self.label.contract_id,
                "sl_atr_mult": self.label.sl_atr_mult,
                "tp_atr_mult": self.label.tp_atr_mult,
                "horizon_bars": self.label.horizon_bars,
                "profitability_calibrated": self.label.profitability_calibrated,
                "label_mapping": self.label.label_mapping,
                "spread_points": self.label.spread_points,
                "slippage_points": self.label.slippage_points,
                "tick_value": self.label.tick_value,
                "tick_size": self.label.tick_size,
                "vol_scale_target": self.label.vol_scale_target,
                "output_unit": self.label.output_unit,
            },
            "architecture": {
                "type": self.architecture.type,
                "objective_function": self.architecture.objective_function,
                "search_space": self.architecture.search_space,
                "optuna_trials": self.architecture.optuna_trials,
                "n_seeds": self.architecture.n_seeds,
            },
            "validation": {
                "method": self.validation.method,
                "n_groups": self.validation.n_groups,
                "n_test_groups": self.validation.n_test_groups,
                "purge_bars": self.validation.purge_bars,
                "embargo_bars": self.validation.embargo_bars,
            },
            "quality_gates": {
                "min_train_sharpe": self.quality_gates.min_train_sharpe,
                "min_train_win_rate": self.quality_gates.min_train_win_rate,
                "min_forward_sharpe": self.quality_gates.min_forward_sharpe,
                "min_forward_win_rate": self.quality_gates.min_forward_win_rate,
                "max_overfit_gap": self.quality_gates.max_overfit_gap,
                "min_sortino_ratio": self.quality_gates.min_sortino_ratio,
                "min_calmar_ratio": self.quality_gates.min_calmar_ratio,
                "max_vol_scaled_dd_pct": self.quality_gates.max_vol_scaled_dd_pct,
                "require_shap_stability": self.quality_gates.require_shap_stability,
                "model_type": self.quality_gates.model_type,
            },
            "output": {
                "brain_id_template": self.output.brain_id_template,
                "model_dir": self.output.model_dir,
                "config_dir": self.output.config_dir,
                "registry_db": self.output.registry_db,
                "auto_register": self.output.auto_register,
                "initial_status": self.output.initial_status,
            },
        }
        if self.dataset.date_range:
            d["dataset"]["date_range"] = list(self.dataset.date_range)
        if self.architecture.custom_params:
            d["architecture"]["custom_params"] = self.architecture.custom_params
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    # ── Factories ──

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingContract:
        ds = data.get("dataset", {})
        lbl = data.get("label", {})
        arch = data.get("architecture", {})
        val = data.get("validation", {})
        gates = data.get("quality_gates", {})
        out = data.get("output", {})

        dr = ds.get("date_range")
        date_range = tuple(dr) if dr else None

        return cls(
            schema_version=data["schema_version"],
            contract_id=data["contract_id"],
            dataset=DatasetSpec(
                path=ds.get("path", ""),
                feature_schema=ds.get("feature_schema", "v9_institutional_40"),
                date_range=date_range,
                min_samples_per_class=ds.get("min_samples_per_class", 100),
                sample_weighting=ds.get("sample_weighting", "none"),
            ),
            label=LabelSpec(
                contract_id=lbl.get("contract_id", ""),
                sl_atr_mult=lbl.get("sl_atr_mult", 2.0),
                tp_atr_mult=lbl.get("tp_atr_mult", 3.5),
                horizon_bars=lbl.get("horizon_bars", 12),
                profitability_calibrated=lbl.get("profitability_calibrated", False),
                label_mapping=lbl.get("label_mapping"),
                spread_points=lbl.get("spread_points", lbl.get("spread_pips", 30)),
                slippage_points=lbl.get("slippage_points", lbl.get("slippage_pips", 10)),
                tick_value=lbl.get("tick_value", 0.01),
                tick_size=lbl.get("tick_size", 0.001),
                vol_scale_target=lbl.get("vol_scale_target", False),
                output_unit=lbl.get("output_unit", "bps"),
            ),
            architecture=ArchitectureSpec(
                type=arch.get("type", "xgboost"),
                objective_function=arch.get("objective_function", "binary_logloss"),
                search_space=arch.get("search_space", "default"),
                optuna_trials=arch.get("optuna_trials", 50),
                n_seeds=arch.get("n_seeds", 5),
                custom_params=arch.get("custom_params", {}),
            ),
            validation=ValidationSpec(
                method=val.get("method", "cpcv"),
                n_groups=val.get("n_groups", 6),
                n_test_groups=val.get("n_test_groups", 2),
                purge_bars=val.get("purge_bars", 12),
                embargo_bars=val.get("embargo_bars", 5),
            ),
            quality_gates=QualityGateSpec(
                min_train_sharpe=gates.get("min_train_sharpe", 1.0),
                min_train_win_rate=gates.get("min_train_win_rate", 0.50),
                min_forward_sharpe=gates.get("min_forward_sharpe", 1.0),
                min_forward_win_rate=gates.get("min_forward_win_rate", 0.50),
                max_overfit_gap=gates.get("max_overfit_gap", 0.30),
                min_sortino_ratio=gates.get("min_sortino_ratio", 0.8),
                min_calmar_ratio=gates.get("min_calmar_ratio", 0.3),
                max_vol_scaled_dd_pct=gates.get("max_vol_scaled_dd_pct", 30.0),
                require_shap_stability=gates.get("require_shap_stability", True),
                model_type=gates.get("model_type", "tree"),
            ),
            output=OutputSpec(
                brain_id_template=out.get("brain_id_template", "{arch}_{contract}_{timestamp}"),
                model_dir=out.get("model_dir", "data/models/institutional"),
                config_dir=out.get("config_dir", "configs/brains"),
                registry_db=out.get("registry_db", "data/training/registry.db"),
                auto_register=out.get("auto_register", False),
                initial_status=out.get("initial_status", "shadow"),
            ),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> TrainingContract:
        import yaml

        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"YAML file {path} must contain a mapping, got {type(raw).__name__}")
        return cls.from_dict(raw)

    @classmethod
    def from_file(cls, path: str | Path) -> TrainingContract:
        """Auto-detect format and load. Supports .yaml and .json."""
        path = Path(path)
        ext = path.suffix.lower()
        if ext in (".yaml", ".yml"):
            return cls.from_yaml(path)
        if ext == ".json":
            import json

            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(raw)
        raise ValueError(f"Unsupported format: {ext} (expected .yaml, .yml, or .json)")
