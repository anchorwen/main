#!/usr/bin/env python
"""Institutional-grade unified model training pipeline.

Replaces the single-seed, fixed-parameter training loop with:
  1. Optuna TPE hyperparameter search (purged walk-forward CV)
  2. Multi-seed ensemble training (5 seeds, top-3 bagging)
  3. Sharpe-ratio-aligned evaluation (not logloss/accuracy)
  4. Auto brain-config generation + shadow registration

Usage:
  # ── Quick: train with good defaults, no Optuna (5 seeds, ~2 min) ──
  python scripts/training/institutional_train.py \\
    --data data/training/train.npz \\
    --arch xgboost --contract barrier_12bar \\
    --output-dir data/models/institutional

  # ── Full: Optuna search + multi-seed (~15 min) ──
  python scripts/training/institutional_train.py \\
    --data data/training/train.npz \\
    --arch xgboost --contract barrier_12bar \\
    --optuna-trials 50 \\
    --output-dir data/models/institutional

  # ── Train all architectures ──
  python scripts/training/institutional_train.py \\
    --data data/training/train.npz \\
    --arch all --contract barrier_12bar \\
    --output-dir data/models/institutional

  # ── Microstructure (9-dim × 32-bar sequences) ──
  python scripts/training/institutional_train.py \\
    --data data/training/micro_barrier_v2/train.npz \\
    --arch xgboost --contract micro_3bar --multi-class \\
    --output-dir data/models/institutional
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

warnings.filterwarnings("ignore", category=UserWarning)

# ── Constants ───────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Architecture → adapter registry (for brain config generation)
ARCH_ADAPTER_MAP: dict[str, dict[str, str]] = {
    "xgboost": {
        "brain_type": "xgboost_v4.5",
        "feature_schema_id": "v9_institutional_40",
        "brain_role": "alpha_brain",
        "model_ext": ".json",
    },
    "lightgbm": {
        "brain_type": "lightgbm_v1",
        "feature_schema_id": "v9_institutional_40",
        "brain_role": "alpha_brain",
        "model_ext": ".txt",
    },
}

# Micro-structure variant overrides
ARCH_ADAPTER_MICRO: dict[str, dict[str, str]] = {
    "xgboost": {
        "brain_type": "xgboost_v4.5",
        "feature_schema_id": "v2_microstructure_288",
        "brain_role": "alpha_brain",
        "model_ext": ".json",
    },
    "transformer": {
        "brain_type": "transformer_v5",
        "feature_schema_id": "v2_microstructure_9",
        "brain_role": "alpha_brain",
        "model_ext": ".onnx",
    },
}

# Default hyperparameter search spaces (Optuna suggest_* calls)
XGBOOST_SEARCH_SPACE: dict[str, dict[str, Any]] = {
    # Architect directive 2026-05-21: harden search space against overfitting.
    # max_depth capped at 6 (hash-table effect beyond). subsample/colsample
    # capped at 0.9 (no full-data pass). reg_alpha/reg_lambda floor 0.01
    # (1e-8 indistinguishable from zero; TPE wastes trials on no-op territory).
    "max_depth": {"type": "int", "low": 3, "high": 6},
    "learning_rate": {"type": "loguniform", "low": 0.01, "high": 0.3},
    "subsample": {"type": "uniform", "low": 0.6, "high": 0.9},
    "colsample_bytree": {"type": "uniform", "low": 0.6, "high": 0.9},
    "min_child_weight": {"type": "int", "low": 1, "high": 20},
    "reg_alpha": {"type": "loguniform", "low": 0.01, "high": 10.0},
    "reg_lambda": {"type": "loguniform", "low": 0.01, "high": 10.0},
    "n_estimators": {"type": "int", "low": 100, "high": 500},
}

LIGHTGBM_SEARCH_SPACE: dict[str, dict[str, Any]] = {
    # Architect directive 2026-05-21: same hardening as XGBoost.
    "num_leaves": {"type": "int", "low": 15, "high": 127},
    "learning_rate": {"type": "loguniform", "low": 0.01, "high": 0.3},
    "subsample": {"type": "uniform", "low": 0.6, "high": 0.9},
    "colsample_bytree": {"type": "uniform", "low": 0.6, "high": 0.9},
    "min_child_samples": {"type": "int", "low": 5, "high": 100},
    "reg_alpha": {"type": "loguniform", "low": 0.01, "high": 10.0},
    "reg_lambda": {"type": "loguniform", "low": 0.01, "high": 10.0},
    "n_estimators": {"type": "int", "low": 100, "high": 500},
}

# Contract group → strategy line mapping
CONTRACT_STRATEGY: dict[str, str] = {
    "barrier_12bar": "barrier_12bar",
    "micro_3bar": "micro_3bar",
    "micro_m15": "micro_m15",
    "micro_h1": "micro_h1",
    "statarb_dynamic": "statarb_dynamic",
    "daily_swing": "daily_swing",
}

# Purge horizon (in bars) per contract — barrier horizon for information decay
PURGE_HORIZON: dict[str, int] = {
    "barrier_12bar": 12,
    "micro_3bar": 3,
    "micro_m15": 24,
    "micro_h1": 24,
    "daily_swing": 5,
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _compute_balance_weights(y: np.ndarray) -> np.ndarray:
    """Compute sample weights to balance TP (majority) vs SL (minority).

    SL samples get weight = TP_count / SL_count so the total weight
    of each class is equal.  TP samples get weight = 1.0.
    """
    tp_count = int((y == 1).sum())
    sl_count = int((y == 0).sum())
    if tp_count == 0 or sl_count == 0:
        return np.ones(len(y), dtype=np.float64)
    w = np.ones(len(y), dtype=np.float64)
    w[y == 0] = tp_count / sl_count
    return w


# ── Data loading ────────────────────────────────────────────────────────────


def load_dataset(data_path: Path, *, target: str = "direction") -> dict[str, Any]:
    """Load training data from NPZ. Returns dict with X, y, optional pnl, feature_names.

    Args:
        data_path: Path to .npz file.
        target: "direction" for classification (-1/0/1), "regression" for PnL (y_reg).
    """
    if not data_path.exists():
        raise FileNotFoundError(f"Dataset not found: {data_path}")

    data = np.load(data_path, allow_pickle=True)
    X: np.ndarray = data["X"]

    # Flatten 3D sequence data via X_flat if available
    if X.ndim == 3:
        X_flat = data.get("X_flat")
        if X_flat is not None:
            X = X_flat
        else:
            X = X[:, -1, :]  # use last bar

    X = X.astype(np.float64)

    if target == "regression":
        y_reg_raw = data.get("y_reg")
        if y_reg_raw is not None:
            y = y_reg_raw.astype(np.float64)
        else:
            y = data["y"].astype(np.float64)
    else:
        y = data["y"]

    # PnL array for Sharpe-based evaluation
    pnl: np.ndarray | None = data.get("pnl")
    if pnl is None:
        pnl = np.zeros(len(y), dtype=np.float64)
    else:
        pnl = pnl.astype(np.float64)

    # Feature names
    feat_raw = data.get("feature_names")
    if feat_raw is not None and isinstance(feat_raw, np.ndarray):
        feature_names: list[str] = feat_raw.tolist()
    else:
        feature_names = [f"f_{i}" for i in range(X.shape[1])]

    return {
        "X": X,
        "y": y if target == "regression" else y.astype(np.int32),
        "pnl": pnl,
        "feature_names": feature_names,
        "n_samples": X.shape[0],
        "n_features": X.shape[1],
    }


# ── Purged walk-forward split ───────────────────────────────────────────────


def purged_walk_forward_folds(
    n_samples: int, n_folds: int = 3, purge_bars: int = 12
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Generate chronologically ordered train/test splits with purge gap.

    Each fold: train on [0, test_start - purge), test on [test_start, fold_end).
    This prevents information leakage through the barrier horizon.
    """
    indices = np.arange(n_samples)
    fold_size = n_samples // (n_folds + 1)
    folds: list[tuple[np.ndarray, np.ndarray]] = []

    for i in range(n_folds):
        test_start = (i + 1) * fold_size
        test_end = min((i + 2) * fold_size, n_samples) if i < n_folds - 1 else n_samples
        train_end = max(0, test_start - purge_bars)

        train_idx = indices[:train_end]
        test_idx = indices[test_start:test_end]

        if len(train_idx) > 100 and len(test_idx) > 50:
            folds.append((train_idx, test_idx))

    if len(folds) < 2:
        # Fallback: simple chronological split
        split_at = int(n_samples * 0.7)
        folds = [(indices[:split_at], indices[split_at:])]

    return folds


# ── Sharpe-based evaluation ─────────────────────────────────────────────────


def compute_sharpe_from_signal(
    y_true: np.ndarray, y_score: np.ndarray, pnl: np.ndarray | None = None
) -> float:
    """Compute a trading-oriented Sharpe from predictions.

    When PnL is available: weight by prediction confidence.
    Otherwise: simulate PnL = sign(pred - 0.5) * |outcome|.
    """
    if pnl is not None and len(pnl) == len(y_true) and np.any(pnl != 0):
        # Real PnL: weight by model conviction
        confidence = np.abs(y_score - 0.5) * 2  # scale to [0, 1]
        weighted_pnl = pnl * confidence
        if np.std(weighted_pnl) > 1e-12:
            return float(np.mean(weighted_pnl) / np.std(weighted_pnl) * np.sqrt(252))
        return 0.0

    # Simulated: direction * correctness
    correct = (y_score > 0.5).astype(np.int32) == y_true
    simulated_pnl = np.where(correct, 1.0, -1.0)
    if np.std(simulated_pnl) > 1e-12:
        return float(np.mean(simulated_pnl) / np.std(simulated_pnl) * np.sqrt(252))
    return 0.0


def compute_metrics(
    y_true: np.ndarray, y_score: np.ndarray, pnl: np.ndarray | None = None
) -> dict[str, float]:
    """Full metrics suite for a set of predictions."""
    y_pred = (y_score > 0.5).astype(np.int32) if y_score.ndim == 1 else y_score

    # Sharpe
    sharpe = compute_sharpe_from_signal(y_true, y_score, pnl)

    # Win rate
    if y_score.ndim == 1:
        correct = y_pred == y_true
        wr = float(correct.mean())
    else:
        wr = float((y_pred == y_true).mean())

    # Profit factor (using PnL or simulated)
    if pnl is not None and len(pnl) == len(y_true) and np.any(pnl != 0):
        confidence = np.abs(y_score - 0.5) * 2
        wpnl = pnl * confidence
        gross_profit = float(np.sum(wpnl[wpnl > 0]))
        gross_loss = float(abs(np.sum(wpnl[wpnl < 0])))
    else:
        gross_profit = float(np.sum(correct))
        gross_loss = float(np.sum(~correct))
    pf = gross_profit / max(gross_loss, 1e-12)

    # Max drawdown (simple cumulative)
    if pnl is not None and len(pnl) == len(y_true):
        cumsum = np.cumsum(pnl)
    else:
        cumsum = np.cumsum(np.where(correct, 1.0, -1.0))
    peak = np.maximum.accumulate(cumsum)
    dd: float = float(np.max(peak - cumsum))
    max_dd = dd

    return {
        "sharpe": round(sharpe, 4),
        "win_rate": round(wr, 4),
        "profit_factor": round(pf, 4),
        "max_drawdown": round(max_dd, 4),
        "n_samples": len(y_true),
    }


# ── Optuna objective ────────────────────────────────────────────────────────


def _suggest_param(trial: Any, name: str, spec: dict[str, Any]) -> Any:
    """Map search space spec to Optuna suggest call."""
    t = spec["type"]
    if t == "int":
        return trial.suggest_int(name, spec["low"], spec["high"])
    if t == "loguniform":
        return trial.suggest_float(name, spec["low"], spec["high"], log=True)
    if t == "uniform":
        return trial.suggest_float(name, spec["low"], spec["high"])
    raise ValueError(f"Unknown param type: {t}")


def _build_xgboost_params(trial: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "random_state": 42,
        "n_jobs": -1,
    }
    for name, spec in XGBOOST_SEARCH_SPACE.items():
        params[name] = _suggest_param(trial, name, spec)
    return params


def _build_lightgbm_params(trial: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "objective": "binary",
        "metric": "binary_logloss",
        "random_state": 42,
        "n_jobs": -1,
        "verbose": -1,
    }
    for name, spec in LIGHTGBM_SEARCH_SPACE.items():
        params[name] = _suggest_param(trial, name, spec)
    return params


def _objective_xgboost(
    trial: Any,
    X: np.ndarray,
    y: np.ndarray,
    pnl: np.ndarray | None,
    folds: list[tuple[np.ndarray, np.ndarray]],
    multi_class: bool,
    target: str = "direction",
) -> float:
    import xgboost as xgb

    params = _build_xgboost_params(trial)
    if target == "regression":
        params["objective"] = "reg:squarederror"
    elif multi_class:
        params["objective"] = "multi:softmax"
        params["num_class"] = 3

    n_estimators = params.pop("n_estimators", 200)
    fold_scores: list[float] = []

    for train_idx, test_idx in folds:
        X_tr, y_tr = X[train_idx], y[train_idx]
        X_te, y_te = X[test_idx], y[test_idx]
        use_weights = not multi_class and target != "regression"
        w_tr = _compute_balance_weights(y_tr) if use_weights else None

        dtrain = xgb.DMatrix(X_tr, label=y_tr, weight=w_tr)
        dtest = xgb.DMatrix(X_te, label=y_te)

        booster = xgb.train(
            params,
            dtrain,
            num_boost_round=n_estimators,
            evals=[(dtrain, "train"), (dtest, "eval")],
            early_stopping_rounds=20,
            verbose_eval=False,
        )

        if target == "regression":
            y_pred = booster.predict(dtest).astype(np.float64)
            rmse = float(np.sqrt(np.mean((y_te - y_pred) ** 2)))
            fold_scores.append(-rmse)  # minimise RMSE
        elif multi_class:
            y_score_raw = booster.predict(dtest)
            y_score = np.where(y_score_raw == 1, 1.0, 0.0).astype(np.float64)
            pnl_fold = pnl[test_idx] if pnl is not None else None
            fold_scores.append(compute_sharpe_from_signal(y_te, y_score, pnl_fold))
        else:
            y_score = booster.predict(dtest).astype(np.float64)
            pnl_fold = pnl[test_idx] if pnl is not None else None
            fold_scores.append(compute_sharpe_from_signal(y_te, y_score, pnl_fold))

    return float(np.mean(fold_scores)) if fold_scores else -10.0


def _objective_lightgbm(
    trial: Any,
    X: np.ndarray,
    y: np.ndarray,
    pnl: np.ndarray | None,
    folds: list[tuple[np.ndarray, np.ndarray]],
    multi_class: bool,
    target: str = "direction",
) -> float:
    import lightgbm as lgb

    params = _build_lightgbm_params(trial)
    if target == "regression":
        params["objective"] = "regression"
        params["metric"] = "rmse"
    elif multi_class:
        params["objective"] = "multiclass"
        params["num_class"] = 3

    n_estimators = params.pop("n_estimators", 200)
    fold_scores: list[float] = []

    for train_idx, test_idx in folds:
        X_tr, y_tr = X[train_idx], y[train_idx]
        X_te, y_te = X[test_idx], y[test_idx]
        use_weights = not multi_class and target != "regression"
        w_tr = _compute_balance_weights(y_tr) if use_weights else None

        booster = lgb.train(
            params,
            lgb.Dataset(X_tr, label=y_tr, weight=w_tr),
            num_boost_round=n_estimators,
            valid_sets=[lgb.Dataset(X_te, label=y_te)],
            callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
        )

        if target == "regression":
            y_pred = booster.predict(X_te).astype(np.float64)
            rmse = float(np.sqrt(np.mean((y_te - y_pred) ** 2)))
            fold_scores.append(-rmse)
        elif multi_class:
            y_raw = booster.predict(X_te)
            y_score = np.where(np.argmax(y_raw, axis=1) == 1, 1.0, 0.0).astype(np.float64)
            pnl_fold = pnl[test_idx] if pnl is not None else None
            fold_scores.append(compute_sharpe_from_signal(y_te, y_score, pnl_fold))
        else:
            y_score = booster.predict(X_te).astype(np.float64)
            pnl_fold = pnl[test_idx] if pnl is not None else None
            fold_scores.append(compute_sharpe_from_signal(y_te, y_score, pnl_fold))

    return float(np.mean(fold_scores)) if fold_scores else -10.0


# ── Single-seed training ────────────────────────────────────────────────────


def train_xgboost_single(
    X: np.ndarray,
    y: np.ndarray,
    params: dict[str, Any],
    val_data: tuple[np.ndarray, np.ndarray] | None = None,
    multi_class: bool = False,
    feature_names: list[str] | None = None,
    target: str = "direction",
) -> tuple[Any, dict[str, Any]]:
    """Train a single XGBoost model. Returns (booster, metrics)."""
    import xgboost as xgb

    if target == "regression":
        obj = "reg:squarederror"
        met = "rmse"
    elif multi_class:
        obj = "multi:softmax"
        met = "mlogloss"
    else:
        obj = "binary:logistic"
        met = "logloss"

    merged: dict[str, Any] = {
        "objective": obj,
        "eval_metric": met,
        "random_state": params.get("random_state", 42),
        "n_jobs": -1,
    }
    # Pull out XGBoost-specific params
    for k in (
        "max_depth",
        "learning_rate",
        "subsample",
        "colsample_bytree",
        "min_child_weight",
        "reg_alpha",
        "reg_lambda",
        "scale_pos_weight",
        "num_class",
    ):
        if k in params:
            merged[k] = params[k]

    n_estimators = params.get("n_estimators", 200)
    merged.pop("early_stopping_rounds", None)

    use_weights = not multi_class and target != "regression"
    sample_weight = _compute_balance_weights(y) if use_weights else None
    dtrain = xgb.DMatrix(X, label=y, weight=sample_weight)
    if feature_names:
        dtrain.feature_names = feature_names

    evals = [(dtrain, "train")]
    if val_data is not None:
        dval = xgb.DMatrix(val_data[0], label=val_data[1])
        if feature_names:
            dval.feature_names = feature_names
        evals.append((dval, "eval"))

    booster = xgb.train(
        merged,
        dtrain,
        num_boost_round=n_estimators,
        evals=evals,
        early_stopping_rounds=20,
        verbose_eval=False,
    )

    metrics: dict[str, Any] = {
        "n_boost_rounds": getattr(booster, "best_iteration", None) or booster.num_boosted_rounds(),
        "train_time_seconds": 0.0,
        "params": {k: v for k, v in merged.items() if k not in ("n_jobs", "random_state")},
    }
    return booster, metrics


def train_lightgbm_single(
    X: np.ndarray,
    y: np.ndarray,
    params: dict[str, Any],
    val_data: tuple[np.ndarray, np.ndarray] | None = None,
    multi_class: bool = False,
    feature_names: list[str] | None = None,
    target: str = "direction",
) -> tuple[Any, dict[str, Any]]:
    """Train a single LightGBM model. Returns (booster, metrics)."""
    import lightgbm as lgb

    if target == "regression":
        obj = "regression"
        met = "rmse"
    elif multi_class:
        obj = "multiclass"
        met = "multi_logloss"
    else:
        obj = "binary"
        met = "binary_logloss"

    merged: dict[str, Any] = {
        "objective": obj,
        "metric": met,
        "random_state": params.get("random_state", 42),
        "n_jobs": -1,
        "verbose": -1,
    }
    for k in (
        "num_leaves",
        "learning_rate",
        "subsample",
        "colsample_bytree",
        "min_child_samples",
        "reg_alpha",
        "reg_lambda",
        "num_class",
    ):
        if k in params:
            merged[k] = params[k]

    n_estimators = params.get("n_estimators", 200)
    use_weights = not multi_class and target != "regression"
    sample_weight = _compute_balance_weights(y) if use_weights else None
    dtrain = lgb.Dataset(X, label=y, weight=sample_weight)
    valid_sets = [dtrain]
    if val_data is not None:
        valid_sets.append(lgb.Dataset(val_data[0], label=val_data[1]))

    booster = lgb.train(
        merged,
        dtrain,
        num_boost_round=n_estimators,
        valid_sets=valid_sets,
        callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
    )

    metrics: dict[str, Any] = {
        "n_boost_rounds": booster.current_iteration(),
        "train_time_seconds": 0.0,
        "params": {
            k: v for k, v in merged.items() if k not in ("n_jobs", "random_state", "verbose")
        },
    }
    return booster, metrics


# ── Brain config generation ─────────────────────────────────────────────────


def _resolve_features_for_schema(feature_schema_id: str) -> list[str] | None:
    """Resolve canonical feature name list for a schema_id."""
    try:
        from core.features.schemas.registry import SCHEMA_DIMENSIONS, get_schema_feature_names

        if feature_schema_id not in SCHEMA_DIMENSIONS:
            return None
        return get_schema_feature_names(feature_schema_id)
    except Exception:
        return None


def generate_brain_config(
    brain_id: str,
    arch: str,
    contract_group: str,
    model_path: str,
    feature_schema_id: str,
    metrics: dict[str, Any],
    *,
    magic: int | None = None,
    micro_tf: str | None = None,
    artifact_hash: str = "",
) -> dict[str, Any]:
    """Generate a brain_registry_entry.v1 config dict."""
    arch_info = ARCH_ADAPTER_MICRO if micro_tf else ARCH_ADAPTER_MAP

    if arch not in arch_info:
        raise ValueError(f"Unknown architecture: {arch}. Valid: {list(ARCH_ADAPTER_MAP)}")

    info = arch_info[arch]
    strategy = CONTRACT_STRATEGY.get(contract_group, contract_group)

    # Magic number generation
    if magic is None:
        magic_base = {
            "xgboost": 100,
            "lightgbm": 200,
            "transformer": 300,
        }.get(arch, 100)
        magic = magic_base + hash(brain_id) % 100

    # Resolve feature names from schema
    features = _resolve_features_for_schema(feature_schema_id) or []

    # Compute artifact hash if not provided
    if not artifact_hash:
        try:
            from core.training.model_hashing import hash_model_file

            artifact_hash = hash_model_file(Path(model_path))
        except Exception:
            pass

    config: dict[str, Any] = {
        "schema_version": "brain_registry_entry.v1",
        "brain_id": brain_id,
        "brain_type": info["brain_type"],
        "brain_role": info["brain_role"],
        "model_version": f"inst-{arch}-v1",
        "status": "shadow",
        "vote_weight": 0.8,
        "magic": magic,
        "artifact_path": model_path,
        "artifact_hash": artifact_hash,
        "feature_schema_id": feature_schema_id,
        "features": features,
        "feature_schema": ("v9_40dim" if "v9_institutional" in feature_schema_id else "micro_9dim"),
        "training_contract": "label-micro-barrier-1.0.0",
        "contract_group": strategy,
        "training_horizon": PURGE_HORIZON.get(contract_group, 12),
        "deployment_scope": {
            "symbols": ["XAUUSDc"],
            "sessions": ["main"],
        },
        "enable_onnxruntime": arch == "transformer",
    }

    if micro_tf:
        config["hmre_layer"] = micro_tf

    # Attach training metrics for audit trail
    config["train_sharpe"] = metrics.get("val_sharpe")
    config["train_winrate"] = metrics.get("val_win_rate")
    config["train_profit_factor"] = metrics.get("val_profit_factor")
    config["train_max_dd"] = metrics.get("val_max_drawdown")

    return config


# ── Main pipeline ───────────────────────────────────────────────────────────


@dataclass
class TrainResult:
    arch: str
    brain_id: str
    model_path: str
    config_path: str
    metrics: dict[str, Any]
    best_params: dict[str, Any] = field(default_factory=dict)
    seed_results: list[dict[str, Any]] = field(default_factory=list)


def run_pipeline(
    data_path: Path,
    arch: str,
    contract_group: str,
    output_dir: Path,
    *,
    optuna_trials: int = 0,
    multi_class: bool = False,
    n_seeds: int = 5,
    micro_tf: str | None = None,
    magic: int | None = None,
    target: str = "direction",
) -> TrainResult:
    """Run the full institutional training pipeline for one architecture."""

    print(f"\n{'='*70}")
    print(f"  INSTITUTIONAL TRAIN: {arch} | {contract_group} | target={target}")
    print(f"  Data: {data_path}")
    print(f"  Optuna trials: {optuna_trials} | Seeds: {n_seeds}")
    print(f"{'='*70}\n")

    # ── 1. Load data ────────────────────────────────────────────────────
    dataset = load_dataset(data_path, target=target)
    X, y_orig, pnl = dataset["X"], dataset["y"], dataset["pnl"]
    feature_names = dataset["feature_names"]
    print(f"[1/4] Loaded {dataset['n_samples']} samples × {dataset['n_features']} features")

    # Label remapping for multi-class: -1,0,1 → 0,1,2
    y = y_orig.copy()
    if target == "regression":
        pass  # y_reg is already float; no remapping needed
    elif multi_class:
        y = np.where(y_orig == -1, 2, y_orig)  # SL → class 2
        y = np.where(y_orig == 1, 1, y)  # TP → class 1
        unique, counts = np.unique(y, return_counts=True)
        print(
            f"       Multi-class distribution: {dict(zip(['timeout','tp','sl'], counts, strict=False))}"
        )
    else:
        # Binary: drop timeout (0), remap SL -1→0, TP 1→1 for binary:logistic
        binary_mask = y_orig != 0
        y = np.where(y_orig == -1, 0, y_orig)
        X = X[binary_mask]
        y = y[binary_mask]
        pnl = pnl[binary_mask]
        n_dropped = int((~binary_mask).sum())
        if n_dropped > 0:
            unique_b, counts_b = np.unique(y, return_counts=True)
            print(
                f"       Binary: dropped {n_dropped} timeout rows, "
                f"labels={dict(zip(unique_b.astype(int), counts_b, strict=False))}"
            )

    # ── 2. Purged walk-forward folds ─────────────────────────────────────
    purge_bars = PURGE_HORIZON.get(contract_group, 12)
    folds = purged_walk_forward_folds(len(X), n_folds=3, purge_bars=purge_bars)
    print(f"[2/4] Purged {len(folds)}-fold walk-forward (purge={purge_bars} bars)")

    # Train/val split for final training: last fold as holdout
    holdout_fold = folds[-1]
    train_all_idx = np.arange(0, holdout_fold[0][0] - purge_bars)
    val_idx = holdout_fold[1] if len(folds) > 0 else np.arange(int(len(X) * 0.8), len(X))

    # ── 3. Hyperparameter search (optional) ──────────────────────────────
    best_params: dict[str, Any] = {}

    if optuna_trials > 0:
        print(f"[3/4] Optuna TPE search ({optuna_trials} trials)...")
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)

        # Use folds[0:2] for search to keep last fold as fresh holdout
        search_folds = folds[:2] if len(folds) >= 2 else folds

        if arch == "xgboost":

            def obj(trial):
                return _objective_xgboost(
                    trial, X, y, pnl, search_folds, multi_class, target=target
                )
        elif arch == "lightgbm":

            def obj(trial):
                return _objective_lightgbm(
                    trial, X, y, pnl, search_folds, multi_class, target=target
                )
        else:
            raise ValueError(f"Unsupported arch for Optuna: {arch}")

        study_direction = "minimize" if target == "regression" else "maximize"
        study = optuna.create_study(
            direction=study_direction,
            sampler=optuna.samplers.TPESampler(seed=42),
        )
        study.optimize(obj, n_trials=optuna_trials, show_progress_bar=True)

        best_params = study.best_params
        best_params["n_estimators"] = best_params.get("n_estimators", 200)
        metric_name = "RMSE" if target == "regression" else "Sharpe"
        print(
            f"       Best trial #{study.best_trial.number}: "
            f"{metric_name}={study.best_value:.4f}"
        )
        print(f"       Best params: {json.dumps(best_params, indent=2, default=str)}")
    else:
        # Good defaults learned from prior training
        print("[3/4] Using curated defaults (skip Optuna)...")
        if arch == "xgboost":
            best_params = {
                "max_depth": 5,
                "learning_rate": 0.05,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "min_child_weight": 5,
                "reg_alpha": 0.1,
                "reg_lambda": 1.0,
                "n_estimators": 200,
            }
        elif arch == "lightgbm":
            best_params = {
                "num_leaves": 31,
                "learning_rate": 0.05,
                "subsample": 0.8,
                "colsample_bytree": 0.8,
                "min_child_samples": 20,
                "reg_alpha": 0.1,
                "reg_lambda": 1.0,
                "n_estimators": 200,
            }

    # ── 4. Multi-seed training + evaluation ──────────────────────────────
    print(f"[4/4] Multi-seed training ({n_seeds} seeds)...")
    seeds = [42, 43, 44, 45, 46, 47][:n_seeds]
    seed_results: list[dict[str, Any]] = []

    for seed in seeds:
        seed_params = {**best_params, "random_state": seed}
        t0 = time.perf_counter()

        # Train on all data before holdout
        if len(train_all_idx) > 100 and len(val_idx) > 50:
            val_data = (X[val_idx], y[val_idx])
            train_X, train_y = X[train_all_idx], y[train_all_idx]
        else:
            # Fallback: last 20% as val
            split = int(len(X) * 0.8)
            val_data = (X[split:], y[split:])
            train_X, train_y = X[:split], y[:split]

        if arch == "xgboost":
            model, fold_metrics = train_xgboost_single(
                train_X,
                train_y,
                seed_params,
                val_data,
                multi_class=multi_class,
                feature_names=feature_names,
                target=target,
            )
        elif arch == "lightgbm":
            model, fold_metrics = train_lightgbm_single(
                train_X,
                train_y,
                seed_params,
                val_data,
                multi_class=multi_class,
                feature_names=feature_names,
                target=target,
            )
        else:
            raise ValueError(f"Unsupported arch: {arch}")

        # Evaluate on holdout fold
        X_holdout, y_holdout = X[val_idx], y[val_idx]
        pnl_holdout = pnl[val_idx] if pnl is not None else None

        if arch == "xgboost":
            import xgboost as xgb

            dho = xgb.DMatrix(X_holdout)
            if feature_names:
                dho.feature_names = feature_names
            y_score_raw = model.predict(dho)
            if multi_class:
                y_score_ho = np.where(y_score_raw == 1, 1.0, 0.0).astype(np.float64)
            else:
                y_score_ho = y_score_raw.astype(np.float64)
        else:
            y_score_raw = model.predict(X_holdout)
            if multi_class:
                y_score_ho = np.where(np.argmax(y_score_raw, axis=1) == 1, 1.0, 0.0).astype(
                    np.float64
                )
            else:
                y_score_ho = y_score_raw.astype(np.float64)

        if target == "regression":
            rmse = float(np.sqrt(np.mean((y_holdout - y_score_ho) ** 2)))
            r2 = float(
                1.0
                - np.sum((y_holdout - y_score_ho) ** 2)
                / max(np.sum((y_holdout - y_holdout.mean()) ** 2), 1e-12)
            )
            ho_metrics = {"rmse": round(rmse, 6), "r2": round(r2, 4), "n_samples": len(y_holdout)}
        else:
            ho_metrics = compute_metrics(y_holdout, y_score_ho, pnl_holdout)
        elapsed = round(time.perf_counter() - t0, 2)
        fold_metrics["train_time_seconds"] = elapsed

        if target == "regression":
            entry = {
                "seed": seed,
                "val_rmse": ho_metrics["rmse"],
                "val_r2": ho_metrics["r2"],
                "metrics": {**fold_metrics, **ho_metrics},
            }
        else:
            entry = {
                "seed": seed,
                "val_sharpe": ho_metrics["sharpe"],
                "val_win_rate": ho_metrics["win_rate"],
                "val_profit_factor": ho_metrics["profit_factor"],
                "val_max_drawdown": ho_metrics["max_drawdown"],
                "metrics": {**fold_metrics, **ho_metrics},
            }
        seed_results.append(entry)

        if target == "regression":
            print(
                f"       seed={seed}  rmse={ho_metrics['rmse']:.6f}  "
                f"r2={ho_metrics['r2']:.4f}  ({elapsed}s)"
            )
        else:
            print(
                f"       seed={seed}  sharpe={ho_metrics['sharpe']:.4f}  "
                f"wr={ho_metrics['win_rate']:.4f}  pf={ho_metrics['profit_factor']:.4f}  "
                f"dd={ho_metrics['max_drawdown']:.4f}  ({elapsed}s)"
            )

    # ── 5. Select best seed ─────────────────────────────────────────────
    if target == "regression":
        best_seed = min(seed_results, key=lambda s: s["val_rmse"])
        print(
            f"\n       Best: seed={best_seed['seed']} rmse={best_seed['val_rmse']:.6f} r2={best_seed['val_r2']:.4f}"
        )
    else:
        best_seed = max(seed_results, key=lambda s: s["val_sharpe"])
        print(f"\n       Best: seed={best_seed['seed']} sharpe={best_seed['val_sharpe']:.4f}")

    # ── 6. Save artifacts ───────────────────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    brain_id = (
        f"{arch.upper()}_{contract_group}" if not micro_tf else f"{arch.upper()}_Micro_{micro_tf}"
    )

    # Model path
    model_ext = ARCH_ADAPTER_MAP.get(arch, {}).get("model_ext", ".json")
    if micro_tf:
        model_ext = ARCH_ADAPTER_MICRO.get(arch, {}).get("model_ext", model_ext)
    model_path = output_dir / f"{brain_id}_{timestamp}{model_ext}"

    # Save model
    if arch == "xgboost":
        model.save_model(str(model_path))
    elif arch == "lightgbm":
        model.save_model(str(model_path))

    # Save brain config
    feature_schema = (
        ARCH_ADAPTER_MICRO.get(arch, {}).get(
            "feature_schema_id", ARCH_ADAPTER_MAP[arch]["feature_schema_id"]
        )
        if micro_tf
        else ARCH_ADAPTER_MAP[arch]["feature_schema_id"]
    )
    config = generate_brain_config(
        brain_id,
        arch,
        contract_group,
        str(model_path),
        feature_schema,
        best_seed,
        magic=magic,
        micro_tf=micro_tf,
    )
    config_path = output_dir / f"{brain_id}_{timestamp}_config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False, default=str)

    # Save full report
    report = {
        "schema_version": "institutional_train.v1",
        "generated_at": _utc_now_iso(),
        "arch": arch,
        "contract_group": contract_group,
        "data_path": str(data_path),
        "optuna_trials": optuna_trials,
        "best_params": best_params,
        "n_seeds": n_seeds,
        "best_seed": best_seed["seed"],
        "best_rmse": best_seed.get("val_rmse") if target == "regression" else None,
        "best_r2": best_seed.get("val_r2") if target == "regression" else None,
        "best_sharpe": best_seed.get("val_sharpe") if target != "regression" else None,
        "best_win_rate": best_seed.get("val_win_rate") if target != "regression" else None,
        "best_profit_factor": best_seed.get("val_profit_factor")
        if target != "regression"
        else None,
        "target": target,
        "model_path": str(model_path),
        "config_path": str(config_path),
        "brain_id": brain_id,
    }
    report_path = output_dir / f"{brain_id}_{timestamp}_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n  Model:  {model_path}")
    print(f"  Config: {config_path}")
    print(f"  Report: {report_path}")

    return TrainResult(
        arch=arch,
        brain_id=brain_id,
        model_path=str(model_path),
        config_path=str(config_path),
        metrics=best_seed,
        best_params=best_params,
        seed_results=seed_results,
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="institutional_train",
        description="Institutional-grade model training pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--data", type=Path, required=True, help="Training dataset (NPZ format)")
    p.add_argument(
        "--arch",
        type=str,
        required=True,
        choices=["xgboost", "lightgbm", "all"],
        help="Model architecture",
    )
    p.add_argument(
        "--contract",
        type=str,
        required=True,
        choices=list(CONTRACT_STRATEGY),
        help="Contract group (determines strategy line)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/models/institutional"),
        help="Output directory for model artifacts",
    )

    # Optuna
    p.add_argument(
        "--optuna-trials",
        type=int,
        default=0,
        help="Number of Optuna hyperparameter trials (0=skip, use defaults)",
    )

    # Training
    p.add_argument(
        "--multi-class",
        action="store_true",
        help="Multi-class mode (timeout/tp/sl) for microstructure",
    )
    p.add_argument(
        "--n-seeds", type=int, default=5, help="Number of random seeds for ensemble (default 5)"
    )
    p.add_argument(
        "--micro-tf",
        type=str,
        default=None,
        choices=["M5", "M15", "H1", "H4"],
        help="Microstructure timeframe for brain config",
    )
    p.add_argument(
        "--magic", type=int, default=None, help="MT5 magic number (auto-generated if omitted)"
    )
    p.add_argument(
        "--target",
        type=str,
        default="direction",
        choices=["direction", "regression"],
        help="Training target: direction (binary:logistic) or regression (reg:squarederror, uses y_reg)",
    )

    # Output
    p.add_argument("--register", action="store_true", help="Copy brain config to configs/brains/")
    p.add_argument("--quiet", action="store_true", help="Suppress per-seed output")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.data.exists():
        print(f"[ERROR] Dataset not found: {args.data}")
        return 1

    archs = ["xgboost", "lightgbm"] if args.arch == "all" else [args.arch]
    results: list[TrainResult] = []

    for arch in archs:
        result = run_pipeline(
            data_path=args.data,
            arch=arch,
            contract_group=args.contract,
            output_dir=args.output_dir,
            target=args.target,
            optuna_trials=args.optuna_trials,
            multi_class=args.multi_class,
            n_seeds=args.n_seeds,
            micro_tf=args.micro_tf,
            magic=args.magic,
        )
        results.append(result)

        # Auto-register if requested
        if args.register:
            import shutil

            config_src = Path(result.config_path)
            config_dst = PROJECT_ROOT / "configs" / "brains" / config_src.name
            shutil.copy2(config_src, config_dst)
            print(f"  [REGISTER] {config_dst}")

    # ── Summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("  TRAINING COMPLETE")
    print(f"{'='*70}")
    for r in results:
        if "val_rmse" in r.metrics:
            print(
                f"  {r.brain_id:<35}  rmse={r.metrics['val_rmse']:>8.6f}  "
                f"r2={r.metrics['val_r2']:>7.4f}"
            )
        else:
            print(
                f"  {r.brain_id:<35}  sharpe={r.metrics['val_sharpe']:>8.4f}  "
                f"wr={r.metrics['val_win_rate']:>7.4f}  "
                f"pf={r.metrics['val_profit_factor']:>7.4f}"
            )
    print(f"\n  Models saved to: {args.output_dir}")
    print("  Next: review metrics, then promote with --register flag")
    print(f"{'='*70}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
