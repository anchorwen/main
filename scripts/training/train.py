"""Unified training pipeline — single-contract, single-command.

Replaces the multi-script manual pipeline (dataset_builder → label alignment →
trainer invocation → result stitching) with a single YAML-driven entry point.

Usage:
    python scripts/training/train.py \\
        --contract configs/training/barrier_12bar_xgboost.yaml

    python scripts/training/train.py \\
        --contract configs/training/micro_3bar_xgboost.yaml \\
        --smoke  # 1 optuna trial, 1 seed, for quick validation

Flow:
    1. Load & validate TrainingContract v2.1
    2. Load dataset (NPZ/Parquet)
    3. Compute sample weights (return-magnitude / class balancing)
    4. Build custom objective (Sharpe-aligned / weighted logloss)
    5. Run Optuna hyperparameter search (optional)
    6. Multi-seed training with early stopping
    7. CPCV evaluation (every bar trained on at least once)
    8. Quality gate enforcement (Sharpe, win rate, drawdown, overfit gap)
    9. Model hashing + SQLite registry
    10. Generate brain config for live deployment
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np


class ModelQualityException(RuntimeError):
    """Hard veto: model failed mandatory quality thresholds and must not be deployed."""


from core.contracts.training.training_contract import (
    TrainingContract,
)
from core.training.custom_objectives import (
    compute_sample_weights,
    lightgbm_sharpe_eval,
    lightgbm_sharpe_obj,
    make_xgb_sharpe_obj,
)
from core.training.dataset import TrainingDataset
from core.training.model_hashing import hash_model_file
from core.training.training_registry import TrainingRunRecord, create_registry

# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline state
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class PipelineResult:
    contract_id: str
    status: str  # "PASSED" | "FAILED" | "SHADOW"
    model_path: str | None = None
    model_hash: str | None = None
    run_id: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    gate_results: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Step 1: Load dataset
# ═══════════════════════════════════════════════════════════════════════════════


def load_dataset(contract: TrainingContract) -> TrainingDataset:
    """Load and validate the dataset specified in the contract."""
    ds_path = Path(contract.dataset.path)
    if not ds_path.exists():
        raise FileNotFoundError(f"Dataset not found: {ds_path}")

    ds = TrainingDataset.from_file(ds_path, label_mapping=contract.label.label_mapping)
    print(f"[train] Loaded dataset: {ds.n_samples} samples, {ds.n_features} features")

    issues = ds.validate()
    if issues:
        for issue in issues:
            print(f"[train] WARNING: {issue}")

    # Check min samples per class (handle negative labels via unique)
    if len(ds.y) > 0:
        unique_vals, counts = np.unique(ds.y, return_counts=True)
        min_count = int(counts.min()) if len(counts) > 0 else 0
        print(
            f"[train] Label distribution: {dict(zip(unique_vals.astype(int), counts.astype(int), strict=False))}"
        )
    else:
        min_count = 0
    if min_count < contract.dataset.min_samples_per_class:
        print(
            f"[train] WARNING: Minority class has {min_count} samples "
            f"(min required: {contract.dataset.min_samples_per_class})"
        )

    if ds.has_timestamps:
        from core.training.dataset import get_date_range, validate_temporal_order

        min_d, max_d = get_date_range(ds.timestamps) if ds.timestamps is not None else ("", "")
        if min_d and max_d:
            print(f"[train] Date range: {min_d} → {max_d}")
        if not validate_temporal_order(ds.timestamps) if ds.timestamps is not None else True:
            print("[train] WARNING: Timestamps not in temporal order — sort before CPCV")

    return ds


# ═══════════════════════════════════════════════════════════════════════════════
# Step 2: Build training configuration
# ═══════════════════════════════════════════════════════════════════════════════


def _build_objective(contract: TrainingContract, pnl: np.ndarray | None = None):
    """Build the custom objective function based on contract settings.

    Returns (objective_callable_or_None, metric_callable_or_None).
    """
    obj = contract.architecture.objective_function
    arch = contract.architecture.type

    if obj == "custom_sharpe":
        if arch == "xgboost":
            return make_xgb_sharpe_obj(pnl), None
        elif arch == "lightgbm":
            return lightgbm_sharpe_obj, lightgbm_sharpe_eval
        return None, None
    elif obj == "custom_weighted_logloss":
        from core.training.custom_objectives import xgboost_weighted_logloss_obj

        if arch == "xgboost":
            return xgboost_weighted_logloss_obj(), None
        return None, None
    elif obj == "binary_logloss":
        # Use built-in binary:logistic with PnL-weighted samples.
        # No custom objective needed — XGBoost/LightGBM native loss is
        # well-calibrated and sample weights align it with trading returns.
        return None, None
    elif obj == "custom_profit_factor":
        if arch == "xgboost":
            return make_xgb_sharpe_obj(pnl), None
        elif arch == "lightgbm":
            return lightgbm_sharpe_obj, lightgbm_sharpe_eval
        return None, None
    elif obj == "reg_huber":
        delta = float(contract.architecture.custom_params.get("huber_delta", 1.0))
        if arch == "lightgbm":
            # LightGBM 4.x native Huber — Hessian handled in C++, safe for trees.
            import logging

            logging.getLogger(__name__).info(
                "reg_huber: using LightGBM native huber objective (alpha=%.2f)", delta
            )
            # Return None to use built-in; alpha passed via custom_params merged into params
            return None, None
        elif arch == "xgboost":
            import logging

            logging.getLogger(__name__).warning(
                "ARCHITECT_GATE: reg_huber + xgboost has zero-Hessian risk in L1 region. "
                "Prefer lightgbm for native Huber support. Proceeding with reg:squarederror "
                "as safe fallback — consider switching to lightgbm."
            )
            return None, None
        return None, None

    return None, None


def _resolve_train_mode(contract: TrainingContract) -> str:
    """Determine training mode from contract objective function."""
    obj = contract.architecture.objective_function
    if obj in ("reg_squarederror", "reg_huber"):
        return "reg"
    if obj in ("multi_logloss",):
        return "multi"
    return "cls"


# ═══════════════════════════════════════════════════════════════════════════════
# Step 3: Training
# ═══════════════════════════════════════════════════════════════════════════════


def _train_xgboost(
    X: np.ndarray,
    y: np.ndarray,
    contract: TrainingContract,
    *,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
    pnl: np.ndarray | None = None,
    sample_weight: np.ndarray | None = None,
    seed: int = 42,
) -> tuple[Any, dict[str, Any]]:
    """Train XGBoost with contract-specified settings."""
    from scripts.training.trainers.xgb_trainer import train_xgboost

    params: dict[str, Any] = {
        "random_state": seed,
        **contract.architecture.custom_params,
    }

    # Apply contract parameter overrides
    if contract.label.horizon_bars > 0:
        pass  # horizon is metadata, not an XGB param

    custom_obj, _ = _build_objective(contract, pnl)
    mode = _resolve_train_mode(contract)

    val_data = (X_val, y_val) if X_val is not None and y_val is not None else None

    booster, metrics = train_xgboost(
        X,
        y,
        params=params,
        val_data=val_data,
        regression=(mode == "reg"),
        multi_class=(mode == "multi"),
        custom_obj=custom_obj,
        sample_weight=sample_weight,
        pnl=pnl,
    )
    return booster, metrics


def _train_lightgbm(
    X: np.ndarray,
    y: np.ndarray,
    contract: TrainingContract,
    *,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
    pnl: np.ndarray | None = None,
    sample_weight: np.ndarray | None = None,
    seed: int = 42,
) -> tuple[Any, dict[str, Any]]:
    """Train LightGBM with contract-specified settings."""
    from scripts.training.trainers.lgb_trainer import train_lightgbm

    params: dict[str, Any] = {
        "random_state": seed,
        **contract.architecture.custom_params,
    }

    # Inject LightGBM native huber params (Correction 2: no custom Huber)
    if contract.architecture.objective_function == "reg_huber":
        delta = float(contract.architecture.custom_params.get("huber_delta", 1.0))
        params["objective"] = "huber"
        params["alpha"] = delta
        params.pop("huber_delta", None)

    custom_obj, custom_metric = _build_objective(contract, pnl)
    mode = _resolve_train_mode(contract)

    val_data = (X_val, y_val) if X_val is not None and y_val is not None else None

    booster, metrics = train_lightgbm(
        X,
        y,
        params=params,
        val_data=val_data,
        regression=(mode == "reg"),
        custom_obj=custom_obj,
        custom_metric=custom_metric,
        sample_weight=sample_weight,
    )
    return booster, metrics


def _train_deep_res_mlp(
    X: np.ndarray,
    y: np.ndarray,
    contract: TrainingContract,
    *,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
    pnl: np.ndarray | None = None,
    sample_weight: np.ndarray | None = None,
    seed: int = 42,
) -> tuple[Any, dict[str, Any]]:
    """Train DeepResMLP with contract-specified settings."""
    from scripts.training.trainers.deep_res_mlp_trainer import train_deep_res_mlp

    params = dict(contract.architecture.custom_params)
    epochs = int(params.get("epochs", 200))
    lr = float(params.get("lr", 3e-4))
    batch_size = int(params.get("batch_size", 128))
    dropout = float(params.get("dropout", 0.2))
    weight_decay = float(params.get("weight_decay", 1e-4))
    regression = params.get("regression", False)

    return train_deep_res_mlp(
        X,
        y,
        epochs=epochs,
        lr=lr,
        batch_size=batch_size,
        dropout=dropout,
        weight_decay=weight_decay,
        seed=seed,
        regression=regression,
    )


def _train_transformer(
    X: np.ndarray,
    y: np.ndarray,
    contract: TrainingContract,
    *,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
    pnl: np.ndarray | None = None,
    sample_weight: np.ndarray | None = None,
    seed: int = 42,
) -> tuple[Any, dict[str, Any]]:
    """Train Microstructure Transformer with contract-specified settings."""
    from scripts.training.trainers.transformer_trainer import train_transformer

    params = dict(contract.architecture.custom_params)
    epochs = int(params.get("epochs", 150))
    lr = float(params.get("lr", 1e-3))
    batch_size = int(params.get("batch_size", 256))
    dropout = float(params.get("dropout", 0.15))
    weight_decay = float(params.get("weight_decay", 1e-4))
    seq_len = int(params.get("seq_len", 32))
    d_model = int(params.get("d_model", 96))
    n_heads = int(params.get("n_heads", 4))
    num_layers = int(params.get("num_layers", 2))
    multi_class = bool(params.get("multi_class", False))

    return train_transformer(
        X,
        y,
        epochs=epochs,
        lr=lr,
        batch_size=batch_size,
        dropout=dropout,
        weight_decay=weight_decay,
        seed=seed,
        seq_len=seq_len,
        d_model=d_model,
        n_heads=n_heads,
        num_layers=num_layers,
        multi_class=multi_class,
    )


def _train_online_learner(
    X: np.ndarray,
    y: np.ndarray,
    contract: TrainingContract,
    *,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
    pnl: np.ndarray | None = None,
    sample_weight: np.ndarray | None = None,
    seed: int = 42,
) -> tuple[Any, dict[str, Any]]:
    """Train Online MLP with contract-specified settings."""
    from scripts.training.trainers.online_mlp_trainer import train_mlp

    params = dict(contract.architecture.custom_params)
    epochs = int(params.get("epochs", 50))
    lr = float(params.get("lr", 0.001))
    batch_size = int(params.get("batch_size", 64))
    n_features = int(params.get("n_features", 40))
    n_classes = int(params.get("n_classes", 3))

    return train_mlp(
        X,
        y.astype("int64"),
        n_features=n_features,
        n_classes=n_classes,
        epochs=epochs,
        lr=lr,
        batch_size=batch_size,
        seed=seed,
    )


def train_single(
    X: np.ndarray,
    y: np.ndarray,
    contract: TrainingContract,
    *,
    X_val: np.ndarray | None = None,
    y_val: np.ndarray | None = None,
    pnl: np.ndarray | None = None,
    sample_weight: np.ndarray | None = None,
    seed: int = 42,
) -> tuple[Any, dict[str, Any]]:
    """Train a single model with the contract-specified architecture."""
    arch = contract.architecture.type

    if arch == "xgboost":
        return _train_xgboost(
            X,
            y,
            contract,
            X_val=X_val,
            y_val=y_val,
            pnl=pnl,
            sample_weight=sample_weight,
            seed=seed,
        )
    elif arch == "lightgbm":
        return _train_lightgbm(
            X,
            y,
            contract,
            X_val=X_val,
            y_val=y_val,
            pnl=pnl,
            sample_weight=sample_weight,
            seed=seed,
        )
    elif arch == "deep_res_mlp":
        return _train_deep_res_mlp(
            X,
            y,
            contract,
            X_val=X_val,
            y_val=y_val,
            pnl=pnl,
            sample_weight=sample_weight,
            seed=seed,
        )
    elif arch == "transformer":
        return _train_transformer(
            X,
            y,
            contract,
            X_val=X_val,
            y_val=y_val,
            pnl=pnl,
            sample_weight=sample_weight,
            seed=seed,
        )
    elif arch in ("online_mlp", "online_sgd"):
        return _train_online_learner(
            X,
            y,
            contract,
            X_val=X_val,
            y_val=y_val,
            pnl=pnl,
            sample_weight=sample_weight,
            seed=seed,
        )
    else:
        raise ValueError(
            f"Architecture '{arch}' not supported by unified pipeline. "
            f"Use architecture-specific trainer directly."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Step 4: Financial metrics
# ═══════════════════════════════════════════════════════════════════════════════


def compute_financial_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    pnl: np.ndarray | None = None,
    *,
    annual_factor: int = 252,
    regression: bool = False,
) -> dict[str, float]:
    """Compute trading-aligned metrics from predictions.

    Args:
        y_true: True labels (0/1 binary, or continuous for regression).
        y_pred: Predicted probabilities or regression values.
        pnl: Optional P&L array for return-magnitude weighting.
        annual_factor: Annualization factor (252 for daily).
        regression: If True, y_pred/y_true are continuous; metrics use
            sign(pred) as position and y_true as realized return.

    Returns:
        Dict with sharpe_ratio, win_rate, profit_factor, max_drawdown, etc.
    """
    if regression:
        # Regression: use sign(prediction) as trade direction,
        # actual forward return (y_true) as realized PnL.
        positions = np.sign(y_pred)  # +1 long, -1 short, 0 flat
        # Zero out flat positions
        positions = np.where(np.abs(y_pred) < 1e-8, 0, positions)
        returns = positions * y_true.astype(np.float64)
    else:
        # Guard: detect degenerate models with no discriminative power.
        # When all predictions are near-identical (e.g. 0.5001–0.5036),
        # the threshold-based classifier always picks one class and any
        # "Sharpe" is pure class-imbalance artifact, not model skill.
        prob_std = float(np.std(y_pred))
        prob_range = float(np.max(y_pred) - np.min(y_pred))
        if prob_range < 0.01 and prob_std < 0.005:
            return {
                "sharpe_ratio": -999.0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "max_drawdown": 999.0,
                "expectancy": 0.0,
                "sortino_ratio": -999.0,
                "calmar_ratio": -999.0,
                "max_vol_scaled_dd": 100.0,
                "total_trades": 0,
            }

        # Convert to binary predictions using class-prior threshold.
        # Fixed 0.5 fails under extreme class imbalance (e.g. 83.7% TP):
        # the model mean clusters near the prior, so 0.5 always picks the
        # majority class — zero edge over baseline.  The prior threshold
        # asks: "is the model more confident than the base rate?"
        if y_pred.ndim > 1:
            pred_class = np.argmax(y_pred, axis=1)
        else:
            threshold = float(np.mean(y_true))
            pred_class = (y_pred > threshold).astype(np.int32)

        # Compute baseline: always-predict-majority-class returns
        # Subtracting baseline isolates model skill from class-imbalance artifact
        majority_class = int(np.bincount(y_true.astype(np.int32)).argmax())
        baseline_preds = np.full_like(y_pred, majority_class, dtype=np.int32)
        if y_pred.ndim > 1:
            baseline_preds[:] = majority_class

        # Map to positions: 1 = long, -1 = short, 0 = flat
        if pnl is not None and len(pnl) > 0:
            returns = np.where(pred_class == 1, pnl, np.where(pred_class == 0, -pnl, 0.0))
            baseline_returns = np.where(
                baseline_preds == 1, pnl, np.where(baseline_preds == 0, -pnl, 0.0)
            )
        else:
            direction = 2.0 * y_true.astype(np.float64) - 1.0
            pos = 2.0 * pred_class.astype(np.float64) - 1.0
            returns = pos * direction
            baseline_pos = 2.0 * baseline_preds.astype(np.float64) - 1.0
            baseline_returns = baseline_pos * direction

    # Baseline Sharpe (always-predict-majority)
    baseline_mean = float(np.mean(baseline_returns))
    baseline_std = float(np.std(baseline_returns)) + 1e-10
    baseline_sharpe = baseline_mean / baseline_std * np.sqrt(annual_factor)

    # Sharpe ratio (excess over baseline isolates model skill)
    mean_ret = float(np.mean(returns))
    std_ret = float(np.std(returns)) + 1e-10
    sharpe = mean_ret / std_ret * np.sqrt(annual_factor)
    excess_sharpe = sharpe - baseline_sharpe

    # Win rate
    wins = int(np.sum(returns > 0))
    losses = int(np.sum(returns < 0))
    total_trades = wins + losses
    win_rate = wins / max(total_trades, 1)

    # Profit factor
    gross_profit = float(np.sum(returns[returns > 0]))
    gross_loss = float(np.abs(np.sum(returns[returns < 0])))
    profit_factor = gross_profit / max(gross_loss, 1e-10)

    # Max drawdown (in return-space units, not percentage)
    cumulative = np.cumsum(returns)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = cumulative - running_max
    max_drawdown = float(np.abs(np.min(drawdowns)))

    # Expectancy
    expectancy = mean_ret

    # Sortino ratio (downside deviation)
    downside = returns[returns < 0]
    downside_std = float(np.std(downside)) + 1e-10 if len(downside) > 0 else 1e-10
    sortino = mean_ret / downside_std * np.sqrt(annual_factor)

    # Calmar ratio (annualized return / max drawdown)
    calmar = (mean_ret * annual_factor) / max(max_drawdown, 1e-10)

    # Vol-scaled drawdown: simulate 1% risk per trade
    # PnL values are in R-units where SL=3.0R. Scale so SL trade = -1% of equity.
    sl_r = 3.0  # SL distance in R-units (from calibrated labels)
    risk_pct = 0.01  # 1% account risk per trade
    account = 100.0
    equity_curve = [account]
    for r in returns:
        if r != 0:
            pnl_pct = r / sl_r * risk_pct * 100.0  # convert R-units to % of equity
            equity_curve.append(equity_curve[-1] * (1.0 + pnl_pct / 100.0))
        else:
            equity_curve.append(equity_curve[-1])
    eq_arr = np.array(equity_curve)
    peak_vol = np.maximum.accumulate(eq_arr)
    dd_vol = (eq_arr - peak_vol) / peak_vol * 100.0
    max_vol_scaled_dd = float(np.abs(np.min(dd_vol)))

    return {
        "sharpe_ratio": round(excess_sharpe, 4),
        "raw_sharpe": round(sharpe, 4),
        "baseline_sharpe": round(baseline_sharpe, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_drawdown, 4),
        "expectancy": round(expectancy, 6),
        "sortino_ratio": round(sortino, 4),
        "calmar_ratio": round(calmar, 4),
        "max_vol_scaled_dd": round(max_vol_scaled_dd, 2),
        "total_trades": total_trades,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Step 5: Quality gates
# ═══════════════════════════════════════════════════════════════════════════════


def check_quality_gates(
    train_metrics: dict[str, float],
    forward_metrics: dict[str, float],
    contract: TrainingContract,
) -> tuple[bool, dict[str, bool]]:
    """Check if results pass all quality gates.

    Returns (passed, gate_results_dict).
    """
    gates = contract.quality_gates
    results: dict[str, bool] = {}
    is_regression = _resolve_train_mode(contract) == "reg"

    results["train_sharpe"] = train_metrics.get("sharpe_ratio", 0.0) >= gates.min_train_sharpe
    results["train_win_rate"] = (
        True if is_regression else train_metrics.get("win_rate", 0.0) >= gates.min_train_win_rate
    )
    results["train_sortino"] = train_metrics.get("sortino_ratio", -999.0) >= gates.min_sortino_ratio
    results["train_calmar"] = train_metrics.get("calmar_ratio", -999.0) >= gates.min_calmar_ratio
    results["vol_scaled_dd"] = (
        train_metrics.get("max_vol_scaled_dd", 100.0) <= gates.max_vol_scaled_dd_pct
    )
    results["forward_sharpe"] = forward_metrics.get("sharpe_ratio", 0.0) >= gates.min_forward_sharpe
    results["forward_win_rate"] = (
        True
        if is_regression
        else forward_metrics.get("win_rate", 0.0) >= gates.min_forward_win_rate
    )
    results["overfit_gap"] = (
        abs(train_metrics.get("sharpe_ratio", 0.0) - forward_metrics.get("sharpe_ratio", 0.0))
        <= gates.max_overfit_gap
    )

    passed = all(results.values())
    return passed, results


# ═══════════════════════════════════════════════════════════════════════════════
# Step 6: Brain config generation
# ═══════════════════════════════════════════════════════════════════════════════


ARCH_TO_BRAIN_TYPE: dict[str, str] = {
    "xgboost": "xgboost_v9",
    "lightgbm": "lightgbm_v1",
    "deep_res_mlp": "deepresmlp",
    "transformer": "transformer_v5",
    "online_learner": "online_sgd",
}

CONTRACT_GROUP_MAGIC: dict[str, int] = {
    "barrier_12bar": 90001,
    "micro_3bar": 90002,
    "statarb_dynamic": 90003,
    "daily_swing": 90301,
    "micro_m15": 90101,
    "micro_h1": 90201,
    "m15_swing": 90310,
    "m30_swing": 90320,
    "h1_swing": 90330,
    "h4_swing": 90340,
}


def _derive_contract_group(contract_id: str) -> str:
    """Derive contract_group from contract_id by stripping arch/version suffix."""
    for group in CONTRACT_GROUP_MAGIC:
        if contract_id.startswith(group):
            return group
    return contract_id


def _auto_register_in_live_yaml(brain_config: dict[str, Any], config_path: Path) -> None:
    """Add a registry_entry for the new brain to live.yaml."""
    import yaml as _yaml

    live_yaml_path = Path("configs/live.yaml")
    if not live_yaml_path.exists():
        print("[train] WARNING: live.yaml not found, skip auto-register")
        return

    try:
        with open(live_yaml_path, encoding="utf-8") as f:
            live = _yaml.safe_load(f) or {}
    except (OSError, IOError, ImportError) as e:  # yaml import may fail
        print(f"[train] WARNING: Failed to read live.yaml, skip auto-register: {e}")
        return

    entries = live.setdefault("brains", {}).setdefault("registry_entries", [])
    rel_path = str(config_path).replace("\\", "/")
    brain_id = brain_config["brain_id"]

    # Don't duplicate
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path", "") == rel_path:
            print(f"[train] Brain {brain_id} already in live.yaml")
            return

    entry = {
        "path": rel_path,
        "enabled": True,
    }
    entries.append(entry)
    live["brains"]["registry_entries"] = entries

    try:
        with open(live_yaml_path, "w", encoding="utf-8") as f:
            _yaml.safe_dump(live, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"[train] Registered {brain_id} in live.yaml")
    except (OSError, IOError, ValueError) as e:
        print(f"[train] WARNING: Failed to update live.yaml: {e}")


def _auto_register_in_governance(brain_config: dict[str, Any]) -> None:
    """Register the new brain as candidate in governance_state.json."""
    gov_path = Path("data/governance_state.json")
    brain_id = brain_config["brain_id"]

    try:
        if gov_path.exists():
            with open(gov_path, encoding="utf-8") as f:
                state = json.load(f)
        else:
            state = {
                "schema_version": "governance_state.v1",
                "brain_states": {},
                "transition_log": [],
            }
    except (OSError, IOError) as e:
        print(f"[train] WARNING: Failed to read governance_state.json: {e}")
        return

    if brain_id in state.get("brain_states", {}):
        print(f"[train] Brain {brain_id} already in governance_state.json")
        return

    now_iso = datetime.now(UTC).isoformat()
    state.setdefault("brain_states", {})[brain_id] = {
        "brain_id": brain_id,
        "status": "candidate",
        "registered_at": now_iso,
        "last_transition_at": now_iso,
        "transition_count": 0,
        "freeze_count": 0,
    }
    state.setdefault("transition_log", []).append(
        {
            "brain_id": brain_id,
            "from_status": "none",
            "to_status": "candidate",
            "reason": "auto:training_pipeline — new model registered",
            "timestamp": now_iso,
        }
    )

    try:
        with open(gov_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"[train] Registered {brain_id} in governance_state.json (candidate)")
    except (OSError, IOError, ValueError) as e:
        print(f"[train] WARNING: Failed to update governance_state.json: {e}")


def _print_register_reminder(config_filename: str) -> None:
    print("\n  ╔══════════════════════════════════════════════════════════════╗")
    print("  ║  Brain auto-registered. Verify with one-click CLI:       ║")
    print("  ║                                                            ║")
    print("  ║  python scripts/brain.py validate                          ║")
    print("  ║  python scripts/brain.py list --group <group>              ║")
    print("  ╚══════════════════════════════════════════════════════════════╝")


def _write_model_meta_json(model_path: str | Path, contract: TrainingContract) -> None:
    """Write .meta.json alongside the model file for MetaSignalFilter consumers.

    Includes feature_names in the exact order the model was trained on,
    plus training metadata (contract_id, threshold, output_unit).
    """
    model_path = Path(model_path)
    # MetaSignalFilter.load() expects model.txt → model.meta.json
    meta_path = model_path.with_suffix(".meta.json")

    # Resolve feature names: prefer NPZ runtime_feature_names if available
    ds_path = Path(contract.dataset.path)
    feature_names: list[str] = []
    if ds_path.suffix == ".npz" and ds_path.exists():
        try:
            data = np.load(ds_path, allow_pickle=True)
            rt_names = data.get("runtime_feature_names")
            if rt_names is not None:
                feature_names = (
                    rt_names.tolist() if isinstance(rt_names, np.ndarray) else list(rt_names)
                )
            else:
                fn = data.get("feature_names")
                if fn is not None:
                    feature_names = fn.tolist() if isinstance(fn, np.ndarray) else list(fn)
        except (KeyError, AttributeError, TypeError):
            pass

    if not feature_names:
        feature_names = [f"f_{i}" for i in range(40)]  # fallback

    meta: dict[str, object] = {
        "schema_version": "model_meta.v1",
        "contract_id": contract.contract_id,
        "feature_names": feature_names,
        "n_features": len(feature_names),
        "output_unit": getattr(contract.label, "output_unit", "bps"),
        "threshold": None,  # set by optimize_meta_threshold.py
        "n_wins": 0,
        "win_rate": 0.0,
    }

    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[train] Meta metadata: {meta_path} ({len(feature_names)} features)")


def generate_brain_config(
    contract: TrainingContract,
    model_path: str,
    model_hash: str,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """Generate a brain_registry_entry.v1 config dict for live deployment.

    Delegates to the shared ``core.training.brain_config.build_brain_config()``
    which enforces the institutional contract (artifact_hash, git commit hash,
    magic, features from SSOT).
    """
    from core.training.brain_config import (
        ARCH_TO_BRAIN_TYPE,
        _derive_contract_group,
        build_brain_config,
        resolve_feature_names_for_schema,
    )

    arch = contract.architecture.type
    brain_type = ARCH_TO_BRAIN_TYPE.get(arch, f"{arch}_v1")
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    brain_id = contract.output.brain_id_template.format(
        arch=arch,
        contract=contract.contract_id,
        timestamp=ts,
    )
    contract_group = _derive_contract_group(contract.contract_id)
    features = resolve_feature_names_for_schema(contract.dataset.feature_schema)

    if not features:
        print(
            f"[WARN] {brain_id}: Could not resolve features for schema "
            f"{contract.dataset.feature_schema}, registration gate will reject"
        )

    return build_brain_config(
        brain_id=brain_id,
        brain_type=brain_type,
        feature_schema_id=contract.dataset.feature_schema,
        artifact_path=model_path,
        artifact_hash=model_hash,
        features=features or [],
        contract_id=contract.contract_id,
        contract_group=contract_group,
        label_horizon_bars=contract.label.horizon_bars,
        metrics=metrics,
        initial_status=contract.output.initial_status,
        model_version=contract.contract_id,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Step 7: Optuna hyperparameter optimization
# ═══════════════════════════════════════════════════════════════════════════════


XGB_SEARCH_SPACE: dict[str, Any] = {
    # Architect directive 2026-05-21: hardened against overfitting.
    "max_depth": ("int", 3, 6),
    "learning_rate": ("float", 0.005, 0.15),
    "subsample": ("float", 0.6, 0.9),
    "colsample_bytree": ("float", 0.6, 0.9),
    "min_child_weight": ("int", 1, 20),
    "reg_lambda": ("float", 0.01, 10.0),
    "reg_alpha": ("float", 0.01, 10.0),
    "gamma": ("float", 0.0, 5.0),
    "n_estimators": ("int", 100, 2000),
}

LGB_SEARCH_SPACE: dict[str, Any] = {
    "num_leaves": ("int", 15, 127),
    "learning_rate": ("float", 0.005, 0.15),
    "feature_fraction": ("float", 0.6, 0.9),
    "bagging_fraction": ("float", 0.6, 0.9),
    "bagging_freq": ("int", 1, 10),
    "min_data_in_leaf": ("int", 10, 100),
    "lambda_l1": ("float", 0.01, 10.0),
    "lambda_l2": ("float", 0.01, 10.0),
    "n_estimators": ("int", 100, 2000),
}

DEEP_RES_MLP_SEARCH_SPACE: dict[str, Any] = {
    "epochs": ("int", 50, 500),
    "lr": ("float", 1e-5, 1e-2),
    "batch_size": ("int", 32, 512),
    "dropout": ("float", 0.05, 0.5),
    "weight_decay": ("float", 1e-6, 1e-2),
}

TRANSFORMER_SEARCH_SPACE: dict[str, Any] = {
    "epochs": ("int", 50, 300),
    "lr": ("float", 1e-5, 5e-3),
    "batch_size": ("int", 64, 512),
    "dropout": ("float", 0.05, 0.4),
    "weight_decay": ("float", 1e-6, 1e-2),
    "d_model": ("int", 32, 128),
    "num_layers": ("int", 1, 4),
}

ONLINE_MLP_SEARCH_SPACE: dict[str, Any] = {
    "epochs": ("int", 20, 200),
    "lr": ("float", 1e-5, 1e-1),
    "batch_size": ("int", 16, 256),
}

ARCH_SEARCH_SPACES: dict[str, dict[str, Any]] = {
    "xgboost": XGB_SEARCH_SPACE,
    "lightgbm": LGB_SEARCH_SPACE,
    "deep_res_mlp": DEEP_RES_MLP_SEARCH_SPACE,
    "transformer": TRANSFORMER_SEARCH_SPACE,
    "online_mlp": ONLINE_MLP_SEARCH_SPACE,
    "online_sgd": ONLINE_MLP_SEARCH_SPACE,
}


def _sample_param(trial: Any, name: str, spec: tuple) -> Any:
    """Sample a hyperparameter from an Optuna trial given a search space spec."""
    kind, lo, hi = spec
    if kind == "int":
        return trial.suggest_int(name, lo, hi)
    elif kind == "float":
        use_log = lo > 1e-8  # log scale only when lower bound is strictly positive
        return trial.suggest_float(name, lo, hi, log=use_log)
    return lo


def run_optuna_search(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    contract: TrainingContract,
    *,
    pnl: np.ndarray | None = None,
    sample_weight: np.ndarray | None = None,
    n_trials: int = 50,
) -> dict[str, Any]:
    """Run Optuna hyperparameter search to maximize validation Sharpe.

    Uses the contract's objective function and search space. Each trial
    trains a single model on (X_train, y_train) and evaluates on (X_val, y_val).

    Returns the best hyperparameters found.
    """
    import optuna

    arch = contract.architecture.type
    search_space = ARCH_SEARCH_SPACES.get(arch, XGB_SEARCH_SPACE)

    def objective(trial: optuna.Trial) -> float:
        params: dict[str, Any] = {}
        for name, spec in search_space.items():
            params[name] = _sample_param(trial, name, spec)

        # Merge trial params into contract for this trial
        saved_params = dict(contract.architecture.custom_params)
        for k, v in params.items():
            contract.architecture.custom_params[k] = v

        try:
            model, _train_metrics = train_single(
                X_train,
                y_train,
                contract,
                X_val=X_val,
                y_val=y_val,
                pnl=pnl,
                sample_weight=sample_weight,
                seed=42,
            )
            # Evaluate on validation set
            if arch == "xgboost":
                import xgboost as xgb

                dval = xgb.DMatrix(X_val)
                val_preds = model.predict(dval)
            elif arch == "lightgbm":
                val_preds = model.predict(X_val)
            elif arch == "deep_res_mlp":
                import torch

                Xv = torch.from_numpy(np.asarray(X_val, dtype=np.float32))
                with torch.no_grad():
                    primary, _, _ = model(Xv)
                val_preds = torch.softmax(primary, dim=1)[:, 1].numpy().astype(np.float64)
            elif arch == "transformer":
                import torch

                Xv = torch.from_numpy(np.asarray(X_val, dtype=np.float32))
                with torch.no_grad():
                    raw = model(Xv).squeeze(-1).numpy()
                val_preds = (1.0 / (1.0 + np.exp(-raw))).astype(np.float64)
            elif arch in ("online_mlp", "online_sgd"):
                probs = model.forward_numpy(np.asarray(X_val, dtype=np.float64))
                val_preds = (
                    probs[:, 1].astype(np.float64) if probs.ndim > 1 else probs.astype(np.float64)
                )
            else:
                val_preds = model.predict(X_val)
            if hasattr(val_preds, "flatten"):
                val_preds = val_preds.astype(np.float64).flatten()
            val_metrics = compute_financial_metrics(
                y_val, val_preds, regression=_resolve_train_mode(contract) == "reg"
            )
            forward_s = val_metrics.get("sharpe_ratio", -999.0)
        except (ValueError, TypeError, KeyError):
            forward_s = -999.0
        finally:
            contract.architecture.custom_params = saved_params

        return float(forward_s)

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=5),
    )

    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)

    best = dict(study.best_params)
    print(f"[optuna] Best trial #{study.best_trial.number}: forward_sharpe={study.best_value:.4f}")
    print(f"[optuna] Best params: {best}")

    return best


# ═══════════════════════════════════════════════════════════════════════════════
# Step 0: Label profitability calibration (PRE-TRAINING gate)
# ═══════════════════════════════════════════════════════════════════════════════


def calibrate_label_contract(
    contract: TrainingContract,
    price_data_path: str | Path,
    *,
    symbol: str = "XAUUSDc",
    timeframe: str = "M5",
) -> dict[str, Any]:
    """Validate that the contract's SL/TP configuration produces positive EV.

    Loads OHLC price data, runs the profitability surface scan, and checks
    whether the current label contract (SL, TP, horizon) has positive expected
    value after transaction costs.

    If the current configuration is unprofitable, recommends the best
    alternative from the surface.

    Returns a dict with calibration results. Sets
    contract.label.profitability_calibrated = True on success.
    """
    from core.training.profitability_calibrator import (
        compute_profitability_surface,
        recommend_label_contract,
    )

    price_path = Path(price_data_path)
    if not price_path.exists():
        raise FileNotFoundError(f"Price data not found: {price_path}")

    # Load OHLC data
    if price_path.suffix == ".npz":
        raw = np.load(price_path, allow_pickle=True)
        highs = raw["highs"] if "highs" in raw else raw["h"]
        lows = raw["lows"] if "lows" in raw else raw["l"]
        closes = raw["closes"] if "closes" in raw else raw["c"]
    elif price_path.suffix in (".parquet",):
        import pandas as pd

        df = pd.read_parquet(price_path)
        highs = df["high"].values if "high" in df.columns else df["highs"].values
        lows = df["low"].values if "low" in df.columns else df["lows"].values
        closes = df["close"].values if "close" in df.columns else df["closes"].values
    else:
        raise ValueError(f"Unsupported price data format: {price_path.suffix}")

    highs_arr = np.asarray(highs, dtype=np.float64)
    lows_arr = np.asarray(lows, dtype=np.float64)
    closes_arr = np.asarray(closes, dtype=np.float64)

    horizon = contract.label.horizon_bars

    print(
        f"[calibrate] Scanning profitability surface for "
        f"SL={contract.label.sl_atr_mult}, TP={contract.label.tp_atr_mult}, "
        f"horizon={horizon} bars..."
    )
    print(
        f"[calibrate] Price data: {len(closes_arr)} bars, "
        f"spread={contract.label.spread_points} pts, "
        f"slippage={contract.label.slippage_points} pts"
    )

    surface = compute_profitability_surface(
        highs_arr,
        lows_arr,
        closes_arr,
        horizon_bars=horizon,
        atr_period=14,
        side="both",
        symbol=symbol,
        timeframe=timeframe,
        spread_points=contract.label.spread_points,
        slippage_points=contract.label.slippage_points,
    )

    if not surface.points:
        raise ValueError(
            f"No statistically reliable (SL,TP) configurations found. "
            f"Need more price data (got {len(closes_arr)} bars)."
        )

    # Check current contract's (SL, TP) EV
    current_sl = contract.label.sl_atr_mult
    current_tp = contract.label.tp_atr_mult
    current_ev = None
    for p in surface.points:
        if abs(p.sl_atr_mult - current_sl) < 0.001 and abs(p.tp_atr_mult - current_tp) < 0.001:
            current_ev = p.expected_pnl_r
            break

    recommendation = recommend_label_contract(
        surface,
        min_expected_pnl=0.05,
        min_reward_risk=1.5,
        prefer_higher_tp=True,
    )

    result: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "total_bars": len(closes_arr),
        "horizon_bars": horizon,
        "current_sl": current_sl,
        "current_tp": current_tp,
        "current_ev": current_ev,
    }

    if current_ev is not None and current_ev <= 0:
        print(
            f"[calibrate] WARNING: Current SL={current_sl}/TP={current_tp} "
            f"has EV={current_ev:.4f}R — NEGATIVE expected value!"
        )
        if recommendation:
            print(
                f"[calibrate] RECOMMENDED: SL={recommendation['sl_atr_mult']}, "
                f"TP={recommendation['tp_atr_mult']}, "
                f"EV={recommendation['expected_pnl_r']:.4f}R, "
                f"TP_hit_rate={recommendation['tp_hit_rate']:.2%}"
            )
            result["recommendation"] = recommendation
            result["recommend_action"] = (
                f"Replace SL={current_sl}/TP={current_tp} with "
                f"SL={recommendation['sl_atr_mult']}/TP={recommendation['tp_atr_mult']} "
                f"in the training contract YAML."
            )
    elif current_ev is not None and current_ev > 0:
        print(
            f"[calibrate] Current SL={current_sl}/TP={current_tp} "
            f"EV={current_ev:.4f}R — POSITIVE ✓"
        )
        contract.label.profitability_calibrated = True
    else:
        print(
            f"[calibrate] Current SL={current_sl}/TP={current_tp} not in scanned grid "
            f"(grid: SL={surface.sl_range}, TP={surface.tp_range})."
        )
        if recommendation:
            result["recommendation"] = recommendation

    # Always log the best config found
    best = surface.best_config()
    if best:
        result["best_config"] = {
            "sl_atr_mult": best.sl_atr_mult,
            "tp_atr_mult": best.tp_atr_mult,
            "expected_pnl_r": best.expected_pnl_r,
            "tp_hit_rate": best.tp_hit_rate,
            "sl_hit_rate": best.sl_hit_rate,
            "timeout_rate": best.timeout_rate,
        }

    result["profitable_configs_count"] = len(surface.profitable_configs())
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════


def run_pipeline(
    contract_path: str | Path,
    *,
    smoke: bool = False,
    price_data_path: str | Path | None = None,
) -> PipelineResult:
    """Execute the full training pipeline from contract.

    Args:
        contract_path: Path to TrainingContract YAML/JSON file.
        smoke: If True, run 1 trial, 1 seed for fast validation.
        price_data_path: Optional path to OHLC price data for label
            profitability calibration (.npz or .parquet). If provided
            and contract.label.profitability_calibrated is False,
            runs calibrate_label_contract() before training.

    Returns:
        PipelineResult with status, metrics, model path, and errors.
    """
    t_start = time.perf_counter()
    result = PipelineResult(contract_id="", status="FAILED")

    # ── Load contract ──
    try:
        contract = TrainingContract.from_file(contract_path)
        result.contract_id = contract.contract_id
        print(f"[train] Contract: {contract.contract_id} (schema {contract.schema_version})")

        issues = contract.validate()
        if issues:
            print("[train] Contract validation issues:")
            for i in issues:
                print(f"  - {i}")
            # Non-fatal for hints, but check for critical issues
            critical = [i for i in issues if "required" in i.lower() or "invalid" in i.lower()]
            if critical:
                result.errors = critical
                return result
    except (ValueError, TypeError, KeyError, OSError, IOError) as e:
        result.errors = [f"Contract loading failed: {e}"]
        print(f"[train] ERROR: {e}")
        return result

    # ── Label profitability calibration ──
    if not contract.label.profitability_calibrated:
        if price_data_path is not None:
            print("[train] Label contract not calibrated — running profitability surface scan...")
            try:
                cal_result = calibrate_label_contract(
                    contract,
                    price_data_path,
                    symbol=getattr(contract, "_symbol", "XAUUSDc"),
                    timeframe=getattr(contract, "_timeframe", "M5"),
                )
                if cal_result.get("current_ev") is not None and cal_result["current_ev"] <= 0:
                    print(
                        "[train] CRITICAL WARNING: Label contract has NEGATIVE expected value. "
                        "Training may produce unprofitable models."
                    )
                    if cal_result.get("recommend_action"):
                        print(f"[train] → {cal_result['recommend_action']}")
            except (ValueError, KeyError, OSError, ImportError) as e:
                print(f"[train] WARNING: Profitability calibration failed (non-fatal): {e}")
        else:
            print(
                "[train] WARNING: Label contract not calibrated for profitability. "
                "Pass --price-data to enable automatic calibration, or set "
                "profitability_calibrated: true in the contract YAML."
            )

    # ── Smoke mode overrides ──
    if smoke:
        contract.architecture.optuna_trials = 0
        contract.architecture.n_seeds = 1
        print("[train] SMOKE MODE: 0 optuna trials, 1 seed")

    # ── Load dataset ──
    try:
        ds = load_dataset(contract)
    except (FileNotFoundError, ValueError, KeyError) as e:
        result.errors = [f"Dataset loading failed: {e}"]
        print(f"[train] ERROR: {e}")
        return result

    # ── Regression mode: swap classification labels for regression labels ──
    train_mode = _resolve_train_mode(contract)
    if train_mode == "reg":
        if ds.y_reg is not None:
            print(
                f"[train] Regression mode — using y_reg labels "
                f"(mean={float(np.mean(ds.y_reg)):.4f}, std={float(np.std(ds.y_reg)):.4f})"
            )
            y_reg = ds.y_reg.ravel().astype(np.float64)

            # ── Volatility-scaled targets (anti-collapse) ──
            if contract.label.vol_scale_target:
                # Divide regression target by ATR to convert from bps to ATR-multiples.
                # This eliminates heteroskedasticity: a +10 bps move during 0.5 ATR
                # gets the same label as a +20 bps move during 1.0 ATR.
                atr_col = (
                    ds.feature_names.index("M5_ATR_14") if "M5_ATR_14" in ds.feature_names else 2
                )
                atr_vals = ds.X[:, atr_col]
                atr_safe = np.maximum(atr_vals, 1e-6)
                y_reg = y_reg / atr_safe
                contract.label.output_unit = "atr_multiple"
                print(
                    f"[train] Vol-scaled regression labels: "
                    f"mean={float(np.mean(y_reg)):.6f} ATR-multiple, "
                    f"std={float(np.std(y_reg)):.6f}"
                )
            else:
                contract.label.output_unit = "bps"

            ds.y = y_reg
        else:
            print("[train] ERROR: Regression mode requires y_reg in dataset, but none found")
            result.errors = ["Regression labels (y_reg) not found in dataset"]
            return result

    # ── Sample weights ──
    pnl_array: np.ndarray | None = None
    sample_weight: np.ndarray | None = None

    # Try to load PnL from metadata or NPZ
    ds_path = Path(contract.dataset.path)
    if ds_path.suffix == ".npz":
        try:
            raw = np.load(ds_path, allow_pickle=True)
            _pnl_r = raw.get("pnl_r")
            pnl_raw = _pnl_r if _pnl_r is not None else raw.get("pnl")
            if pnl_raw is not None:
                pnl_array = np.asarray(pnl_raw, dtype=np.float64)
        except (KeyError, AttributeError, TypeError):
            pass

    # ── Label preprocessing: directional (-1/0/1) → binary (0/1) ──
    if train_mode == "reg":
        # Regression mode: labels are continuous floats — no remapping needed
        print(
            f"[train] Regression mode — continuous labels "
            f"(std={float(np.std(ds.y)):.2f}, mean={float(np.mean(ds.y)):.2f})"
        )
    else:
        y_raw = ds.y.astype(np.int32)
        unique_labels = set(np.unique(y_raw))
        if unique_labels == {-1, 0, 1}:
            print(
                "[train] Detected directional labels (-1/0/1) — remapping to binary (TP=1, rest=0)"
            )
            y_binary = np.where(y_raw == 1, 1, 0).astype(np.int32)
            ds.y = y_binary
            print(f"[train] Remapped: {np.sum(y_binary==1)} TP, {np.sum(y_binary==0)} non-TP")

    if contract.dataset.sample_weighting != "none":
        sample_weight = compute_sample_weights(
            ds.y,
            pnl=pnl_array,
            method=contract.dataset.sample_weighting,
            loss_penalty_factor=contract.dataset.loss_penalty_factor,
        )
        print(
            f"[train] Sample weights: {contract.dataset.sample_weighting} "
            f"(mean={sample_weight.mean():.3f})"
        )

    # ── Train/val/test split ──
    # Detect pre-split datasets (X_val, y_val, X_test, y_test in NPZ)
    pre_split = False
    train_sw: np.ndarray | None = None
    train_pnl: np.ndarray | None = None
    try:
        raw = np.load(ds_path, allow_pickle=True)
        if "X_val" in raw and "y_val" in raw:
            pre_split = True
            # Use ds.X/ds.y for train (already went through label preprocessing)
            X_train, y_train = ds.X, ds.y

            # Load val arrays and apply same label preprocessing
            X_val = raw["X_val"]
            y_val_raw = np.asarray(raw["y_val"], dtype=np.int32).ravel()
            unique_v = set(np.unique(y_val_raw))
            if train_mode != "reg" and unique_v == {-1, 0, 1}:
                y_val = np.where(y_val_raw == 1, 1, 0).astype(np.int32)
            else:
                y_val = y_val_raw

            X_test_raw = raw.get("X_test")
            y_test_raw = raw.get("y_test")
            if X_test_raw is not None and y_test_raw is not None:
                X_test = X_test_raw
                y_test_r = np.asarray(y_test_raw, dtype=np.int32).ravel()
                unique_t = set(np.unique(y_test_r))
                if train_mode != "reg" and unique_t == {-1, 0, 1}:
                    y_test = np.where(y_test_r == 1, 1, 0).astype(np.int32)
                else:
                    y_test = y_test_r
            else:
                X_test, y_test = X_val, y_val

            # Handle pre-split PnL (train only; eval uses label-based returns)
            n_train_pre = len(X_train)
            train_pnl = pnl_array[:n_train_pre] if pnl_array is not None and len(pnl_array) >= n_train_pre else pnl_array

            # Recompute sample weights for train (after label remapping)
            if contract.dataset.sample_weighting != "none":
                sample_weight = compute_sample_weights(
                    y_train,
                    pnl=train_pnl,
                    method=contract.dataset.sample_weighting,
                    loss_penalty_factor=contract.dataset.loss_penalty_factor,
                )
            train_sw = sample_weight

            n_train, n_val, n_test = len(X_train), len(X_val), len(X_test)
            print(f"[train] Pre-split dataset: train={n_train}, val={n_val}, test={n_test}")
    except (ValueError, KeyError, TypeError):
        print("[train] WARNING: Could not parse pre-split dataset, using sequential split")

    if not pre_split:
        n = ds.n_samples
        n_val = int(n * contract.validation.val_ratio)
        n_test = int(n * contract.validation.test_ratio)
        n_train = n - n_val - n_test

        X_train, y_train = ds.X[:n_train], ds.y[:n_train]
        X_val, y_val = ds.X[n_train : n_train + n_val], ds.y[n_train : n_train + n_val]
        X_test, y_test = ds.X[n_train + n_val :], ds.y[n_train + n_val :]

        train_pnl = pnl_array[:n_train] if pnl_array is not None else None
        train_sw = sample_weight[:n_train] if sample_weight is not None else None

        print(f"[train] Split: train={n_train}, val={n_val}, test={n_test}")

    # ── Apply vol-scaling to val/test labels if regression mode ──
    if train_mode == "reg" and contract.label.vol_scale_target:
        atr_idx = ds.feature_names.index("M5_ATR_14") if "M5_ATR_14" in ds.feature_names else 2
        # Val ATR from X_val
        val_atr = np.maximum(X_val[:, atr_idx], 1e-6)
        y_val = (
            np.asarray(y_val, dtype=np.float64).ravel()
            / np.asarray(val_atr, dtype=np.float64).ravel()
        )
        # Test ATR from X_test if available
        if X_test is not None:
            test_atr = np.maximum(X_test[:, atr_idx], 1e-6)
            y_test = (
                np.asarray(y_test, dtype=np.float64).ravel()
                / np.asarray(test_atr, dtype=np.float64).ravel()
            )
        y_train = np.asarray(y_train, dtype=np.float64).ravel()  # already scaled above

    # Ensure arrays are correctly typed for model training
    X_train = np.asarray(X_train, dtype=np.float64)
    X_val = np.asarray(X_val, dtype=np.float64)
    if X_test is not None:
        X_test = np.asarray(X_test, dtype=np.float64)
    y_dtype = np.float64 if train_mode == "reg" else np.int32
    y_train = np.asarray(y_train, dtype=y_dtype).ravel()
    y_val = np.asarray(y_val, dtype=y_dtype).ravel()
    if y_test is not None:
        y_test = np.asarray(y_test, dtype=y_dtype).ravel()

    # ── Optuna hyperparameter optimization ──
    if contract.architecture.optuna_trials > 0:
        print(f"[train] Running Optuna search ({contract.architecture.optuna_trials} trials)...")
        best_params = run_optuna_search(
            X_train,
            y_train,
            X_val,
            y_val,
            contract,
            pnl=train_pnl,
            sample_weight=train_sw,
            n_trials=contract.architecture.optuna_trials,
        )
        # Merge optimized params into contract for subsequent training
        contract.architecture.custom_params.update(best_params)
        print(f"[train] Optuna complete. Updated params: {contract.architecture.custom_params}")

    # ── Multi-seed training ──
    try:
        arch = contract.architecture.type
        seeds = [42 + i * 11 for i in range(contract.architecture.n_seeds)]

        best_sharpe = -999.0
        best_model: Any = None
        best_metrics: dict[str, Any] = {}
        best_seed = seeds[0]

        for seed in seeds:
            print(f"[train] Training {arch} with seed={seed}...")
            model, train_metrics = train_single(
                X_train,
                y_train,
                contract,
                X_val=X_val,
                y_val=y_val,
                pnl=train_pnl,
                sample_weight=train_sw,
                seed=seed,
            )

            # Evaluate forward Sharpe on test set
            if arch == "xgboost":
                import xgboost as xgb

                dtest = xgb.DMatrix(X_test)
                test_preds = model.predict(dtest)
            elif arch == "lightgbm":
                test_preds = model.predict(X_test)
            elif arch == "deep_res_mlp":
                import torch

                Xt = torch.from_numpy(np.asarray(X_test, dtype=np.float32))
                with torch.no_grad():
                    primary, _risk, _vol = model(Xt)
                test_preds = torch.softmax(primary, dim=1)[:, 1].numpy().astype(np.float64)
            elif arch == "transformer":
                import torch

                Xt = torch.from_numpy(np.asarray(X_test, dtype=np.float32))
                with torch.no_grad():
                    raw = model(Xt).squeeze(-1).numpy()
                test_preds = (1.0 / (1.0 + np.exp(-raw))).astype(np.float64)
            elif arch in ("online_mlp", "online_sgd"):
                probs = model.forward_numpy(np.asarray(X_test, dtype=np.float64))
                test_preds = (
                    probs[:, 1].astype(np.float64) if probs.ndim > 1 else probs.astype(np.float64)
                )
            else:
                test_preds = np.zeros(len(X_test), dtype=np.float64)

            forward_metrics = compute_financial_metrics(
                y_test,
                test_preds,
                regression=(train_mode == "reg"),
            )

            # Train-set predictions for overfit gap calculation
            if arch == "xgboost":
                import xgboost as xgb

                train_preds_arr = model.predict(xgb.DMatrix(X_train))
            elif arch == "lightgbm":
                train_preds_arr = model.predict(X_train)
            elif arch == "deep_res_mlp":
                import torch

                Xtr = torch.from_numpy(np.asarray(X_train, dtype=np.float32))
                with torch.no_grad():
                    primary, _risk, _vol = model(Xtr)
                train_preds_arr = torch.softmax(primary, dim=1)[:, 1].numpy().astype(np.float64)
            elif arch == "transformer":
                import torch

                Xtr = torch.from_numpy(np.asarray(X_train, dtype=np.float32))
                with torch.no_grad():
                    raw = model(Xtr).squeeze(-1).numpy()
                train_preds_arr = (1.0 / (1.0 + np.exp(-raw))).astype(np.float64)
            elif arch in ("online_mlp", "online_sgd"):
                probs = model.forward_numpy(np.asarray(X_train, dtype=np.float64))
                train_preds_arr = (
                    probs[:, 1].astype(np.float64) if probs.ndim > 1 else probs.astype(np.float64)
                )
            else:
                train_preds_arr = np.zeros(len(y_train), dtype=np.float64)
            train_fin = compute_financial_metrics(
                y_train, train_preds_arr, regression=(train_mode == "reg")
            )

            # Prediction collapse monitor: regression models must learn variance
            if train_mode == "reg":
                pred_std = float(np.std(train_preds_arr))
                target_std = float(np.std(y_train)) + 1e-10
                collapse_ratio = pred_std / target_std
                if collapse_ratio < 0.1:
                    print(
                        f"[train] COLLAPSE_ERROR seed={seed}: pred_std={pred_std:.4f}, "
                        f"target_std={target_std:.4f}, ratio={collapse_ratio:.4f} — "
                        "model predicts near-constant values, Huber regression failed to learn"
                    )
                elif collapse_ratio < 0.3:
                    print(
                        f"[train] COLLAPSE_WARN seed={seed}: pred_std={pred_std:.4f}, "
                        f"target_std={target_std:.4f}, ratio={collapse_ratio:.4f} — "
                        "low signal, check huber_delta"
                    )

            forward_sharpe = forward_metrics.get("sharpe_ratio", -999.0)
            print(
                f"[train]   seed={seed}: train_sharpe={train_fin.get('sharpe_ratio', 'N/A')}, "
                f"forward_sharpe={forward_sharpe}"
            )

            if forward_sharpe > best_sharpe:
                best_sharpe = forward_sharpe
                best_model = model
                best_metrics = {
                    "train_sharpe": train_fin.get("sharpe_ratio"),
                    "forward_sharpe": forward_sharpe,
                    "train_win_rate": train_fin.get("win_rate"),
                    "forward_win_rate": forward_metrics.get("win_rate"),
                    "train_profit_factor": train_fin.get("profit_factor"),
                    "train_max_drawdown": train_fin.get("max_drawdown"),
                    "train_sortino": train_fin.get("sortino_ratio"),
                    "train_calmar": train_fin.get("calmar_ratio"),
                    "train_vol_scaled_dd": train_fin.get("max_vol_scaled_dd"),
                    "overfit_gap": round(
                        abs(train_fin.get("sharpe_ratio", 0.0) - forward_sharpe), 4
                    ),
                    "cpcv_sharpe_std": 0.0,  # filled by CPCV if enabled
                    "train_metrics_raw": train_metrics,
                    "forward_metrics_raw": forward_metrics,
                }
                best_seed = seed

        result.metrics = best_metrics
        print(f"[train] Best seed: {best_seed} (forward_sharpe={best_sharpe:.4f})")

    except (ValueError, RuntimeError, ImportError, OSError, MemoryError) as e:
        result.errors = [f"Training failed: {e}"]
        import traceback
        traceback.print_exc()
        print(f"[train] ERROR during training: {e}")
        import traceback

        traceback.print_exc()
        return result

    # ── CPCV evaluation (if enabled) ──
    if contract.validation.method == "cpcv" and ds.has_timestamps:
        try:
            from core.training.cpcv import combinatorial_purged_cv

            folds = combinatorial_purged_cv(
                timestamps=ds.timestamps,
                n_groups=contract.validation.n_groups,
                n_test_groups=contract.validation.n_test_groups,
                purge_bars=contract.validation.purge_bars,
                embargo_bars=contract.validation.embargo_bars,
            )
            print(
                f"[train] CPCV: {len(folds)} folds "
                f"({contract.validation.n_groups} groups, "
                f"{contract.validation.n_test_groups} test groups)"
            )

            fold_sharpes: list[float] = []
            for fold in folds:
                X_tr = ds.X[fold.train_idx]
                y_tr = ds.y[fold.train_idx]
                X_te = ds.X[fold.test_idx]
                y_te = ds.y[fold.test_idx]

                if arch == "xgboost":
                    import xgboost as xgb

                    dtrain_cpcv = xgb.DMatrix(X_tr, label=y_tr)
                    dtest_cpcv = xgb.DMatrix(X_te)
                    fold_model = xgb.train(
                        params={
                            "max_depth": 4,
                            "learning_rate": 0.05,
                            "n_estimators": 100,
                            "objective": "binary:logistic",
                            "verbosity": 0,
                        },
                        dtrain=dtrain_cpcv,
                        num_boost_round=100,
                        verbose_eval=False,
                    )
                    fold_preds = fold_model.predict(dtest_cpcv)
                else:
                    fold_preds = (
                        best_model.predict(X_te) if best_model is not None else np.zeros(len(y_te))
                    )

                fold_metrics = compute_financial_metrics(
                    y_te, fold_preds, regression=(train_mode == "reg")
                )
                fold.metrics = fold_metrics
                fold_sharpes.append(fold_metrics.get("sharpe_ratio", 0.0))

            if fold_sharpes:
                cpcv_sharpe_mean = float(np.mean(fold_sharpes))
                cpcv_sharpe_std = (
                    float(np.std(fold_sharpes, ddof=1)) if len(fold_sharpes) > 1 else 0.0
                )
                result.metrics["cpcv_sharpe_mean"] = round(cpcv_sharpe_mean, 4)
                result.metrics["cpcv_sharpe_std"] = round(cpcv_sharpe_std, 4)
                print(f"[train] CPCV Sharpe: {cpcv_sharpe_mean:.4f} ± {cpcv_sharpe_std:.4f}")

        except (ValueError, KeyError, RuntimeError) as e:
            print(f"[train] WARNING: CPCV evaluation failed (non-fatal): {e}")

    # ── Quality gates ──
    # FIX-20260528-013: Prefer CPCV metrics for quality gates.
    # CPCV is the proper cross-validated OOS estimate; single-seed forward
    # metrics are noisy and can be negative even when CPCV is positive.
    cpcv_sharpe = result.metrics.get("cpcv_sharpe_mean")
    if cpcv_sharpe is not None:
        forward_sharpe_for_gates = cpcv_sharpe
        print(f"[train] Using CPCV Sharpe ({cpcv_sharpe:.4f}) for quality gate check")
    else:
        forward_sharpe_for_gates = best_metrics.get("forward_sharpe", 0.0) or 0.0

    train_fin_for_gates = {
        "sharpe_ratio": best_metrics.get("train_sharpe", 0.0) or 0.0,
        "win_rate": best_metrics.get("train_win_rate", 0.0) or 0.0,
        "max_drawdown": best_metrics.get("train_max_drawdown", 0.0) or 0.0,
        "sortino_ratio": best_metrics.get("train_sortino", -999.0) or -999.0,
        "calmar_ratio": best_metrics.get("train_calmar", -999.0) or -999.0,
        "max_vol_scaled_dd": best_metrics.get("train_vol_scaled_dd", 100.0) or 100.0,
    }
    forward_fin_for_gates = {
        "sharpe_ratio": forward_sharpe_for_gates,
        "win_rate": best_metrics.get("forward_win_rate", 0.0) or 0.0,
    }

    gate_passed, gate_results = check_quality_gates(
        train_fin_for_gates, forward_fin_for_gates, contract
    )
    result.gate_results = gate_results
    result.metrics["quality_gate_passed"] = gate_passed

    if not gate_passed:
        failed_gates = [k for k, v in gate_results.items() if not v]
        print(f"[train] QUALITY GATES FAILED: {failed_gates}")
        for g in failed_gates:
            print(f"  - {g}: {gate_results[g]}")
        raise ModelQualityException(
            f"Hard veto: model {contract.contract_id} failed quality gates: {failed_gates}. "
            f"Model must not be deployed. Fix data/features/hyperparams and retrain."
        )
    print("[train] All quality gates PASSED")

    # ── Save model ──
    model_dir = Path(contract.output.model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    arch = contract.architecture.type

    if arch == "xgboost":
        model_path = model_dir / f"{contract.contract_id}_{ts}.json"
        best_model.save_model(str(model_path))
    elif arch == "lightgbm":
        model_path = model_dir / f"{contract.contract_id}_{ts}.txt"
        best_model.save_model(str(model_path))
    elif arch == "deep_res_mlp":
        model_path = model_dir / f"{contract.contract_id}_{ts}.onnx"
        from scripts.training.trainers.deep_res_mlp_trainer import (
            export_onnx as export_onnx_mlp,
        )

        export_onnx_mlp(best_model, model_path)
    elif arch == "transformer":
        model_path = model_dir / f"{contract.contract_id}_{ts}.onnx"
        from scripts.training.trainers.transformer_trainer import (
            export_onnx as export_onnx_tf,
        )

        export_onnx_tf(best_model, model_path, output_dim=3)
    elif arch in ("online_mlp", "online_sgd"):
        model_path = model_dir / f"{contract.contract_id}_{ts}.json"
        best_model.save(str(model_path))
    else:
        model_path = model_dir / f"{contract.contract_id}_{ts}.pkl"
        import pickle

        with open(model_path, "wb") as f:
            pickle.dump(best_model, f)

    result.model_path = str(model_path)
    print(f"[train] Model saved: {model_path}")

    # ── Meta metadata (for MetaSignalFilter / Stage 2 model consumers) ──
    _write_model_meta_json(model_path, contract)

    # ── Model hash ──
    try:
        model_hash = hash_model_file(model_path)
        result.model_hash = model_hash
        print(f"[train] Model hash: {model_hash}")
    except (OSError, IOError) as e:
        print(f"[train] WARNING: Model hashing failed: {e}")

    # ── Registry ──
    try:
        registry = create_registry(contract.output.registry_db)

        record = TrainingRunRecord()
        record.contract_id = contract.contract_id
        record.timestamp = datetime.now(UTC)
        record.train_sharpe = best_metrics.get("train_sharpe")
        record.forward_sharpe = best_metrics.get("forward_sharpe")
        record.overfit_gap = best_metrics.get("overfit_gap")
        record.train_win_rate = best_metrics.get("train_win_rate")
        record.forward_win_rate = best_metrics.get("forward_win_rate")
        record.profit_factor = best_metrics.get("train_profit_factor")
        record.max_drawdown = best_metrics.get("train_max_drawdown")
        record.cpcv_sharpe_std = best_metrics.get("cpcv_sharpe_std")
        record.arch = arch
        record.feature_schema = contract.dataset.feature_schema
        record.n_samples = ds.n_samples
        record.n_features = ds.n_features
        record.quality_gate_passed = gate_passed
        record.status = contract.output.initial_status if gate_passed else "FAILED"
        record.model_path = str(model_path)
        record.model_hash = model_hash

        registry.add_or_update(record)
        result.run_id = record.run_id
        print(f"[train] Registered run: {record.run_id} (status={record.status})")
    except (OSError, IOError, ValueError) as e:
        print(f"[train] WARNING: Registry write failed (non-fatal): {e}")

    # ── Brain config ──
    if gate_passed and contract.output.auto_register:
        try:
            brain_config = generate_brain_config(
                contract, str(model_path), model_hash or "", result.metrics
            )

            # Registration gate — block deployment if any check fails
            from core.deployment.brain_registration_gate import BrainRegistrationGate

            project_root = Path(__file__).resolve().parents[2]
            gate = BrainRegistrationGate(project_root=project_root)
            gate_result = gate.validate(brain_config)
            if not gate_result.passed:
                print(f"[train] REJECTED {brain_config['brain_id']}:")
                for check, detail in gate_result.failures:
                    print(f"  [FAIL] {check}: {detail}")
                raise RuntimeError(
                    f"Registration gate rejected {brain_config['brain_id']}: "
                    f"{len(gate_result.failures)} check(s) failed"
                )

            config_dir = Path(contract.output.config_dir)
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / f"{brain_config['brain_id']}.json"
            config_path.write_text(
                json.dumps(brain_config, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"[train] Brain config: {config_path}")

            # Auto-register in live.yaml
            _auto_register_in_live_yaml(brain_config, config_path)
            # Auto-register in governance_state.json
            _auto_register_in_governance(brain_config)
            _print_register_reminder(config_path.name)
        except (ValueError, RuntimeError, KeyError, OSError, IOError) as e:
            print(f"[train] WARNING: Brain config generation failed: {e}")

    # ── Finalize ──
    result.elapsed_seconds = round(time.perf_counter() - t_start, 1)
    if smoke:
        result.status = "PASSED"  # smoke mode: gates are informational only
        if not gate_passed:
            print("[train] SMOKE: Quality gates bypassed (informational only)")
    else:
        result.status = "PASSED" if gate_passed else "FAILED"
    print(f"[train] Pipeline complete: {result.status} ({result.elapsed_seconds}s)")

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="train",
        description="Unified training pipeline — single contract, single command",
    )
    p.add_argument(
        "--contract",
        type=Path,
        required=True,
        help="Path to TrainingContract YAML/JSON file",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke test: 0 Optuna trials, 1 seed",
    )
    p.add_argument(
        "--price-data",
        type=Path,
        default=None,
        help="Path to OHLC price data (.npz or .parquet) for label profitability calibration",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override model output directory",
    )
    p.add_argument(
        "--registry-db",
        type=str,
        default=None,
        help="Override registry database path",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.contract.exists():
        print(f"Contract file not found: {args.contract}", file=sys.stderr)
        return 2

    result = run_pipeline(
        contract_path=args.contract,
        smoke=args.smoke,
        price_data_path=args.price_data,
    )

    # Print summary
    print()
    print("=" * 60)
    print(f"  Pipeline: {result.status}")
    print(f"  Contract: {result.contract_id}")
    if result.model_path:
        print(f"  Model: {result.model_path}")
    if result.model_hash:
        print(f"  Hash: {result.model_hash}")
    if result.run_id:
        print(f"  Run ID: {result.run_id}")
    print(f"  Time: {result.elapsed_seconds}s")
    if result.metrics:
        print(f"  Train Sharpe: {result.metrics.get('train_sharpe', 'N/A')}")
        print(f"  Forward Sharpe: {result.metrics.get('forward_sharpe', 'N/A')}")
        print(f"  Overfit Gap: {result.metrics.get('overfit_gap', 'N/A')}")
    if result.gate_results:
        print(f"  Quality Gates: {result.gate_results}")
    if result.errors:
        print(f"  Errors: {result.errors}")
    print("=" * 60)

    return 0 if result.status == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
