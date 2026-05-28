"""build_meta_features.py — Stage 1 → Stage 2 OOF meta-feature bridge.

Generates out-of-fold (OOF) predictions from a LightGBM Huber regressor
using 5-fold cross_val_predict. These OOF predictions, combined with
regime-aware meta-features, form the feature set for Stage 2
(XGBoost binary classifier for P(TP|signal)).

Correction 3 (Architect mandated): NEVER uses in-sample predictions.
All Stage 2 features come from OOF cross_val_predict or raw market
features available at prediction time.

Usage:
    python scripts/training/build_meta_features.py \
        --contract configs/training/barrier_12bar_regression_huber.yaml \
        --output data/training/meta_features.npz
"""

from __future__ import annotations

import argparse
import sys
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.model_selection import KFold

from core.contracts.training.training_contract import TrainingContract


def _compute_session_features(timestamps: np.ndarray) -> np.ndarray:
    """Compute session sin/cos encoding from UTC timestamps.

    Forex sessions have distinct volatility/regime characteristics.
    Encoding session time as sin/cos preserves the circular nature of
    the 24h cycle, helping Stage 2 learn session-dependent filtering.

    Returns:
        (n_samples, 2) array of [session_sin, session_cos].
    """
    # Convert to hours in UTC
    import time as _time_module

    hours = np.array(
        [
            _time_module.gmtime(float(ts)).tm_hour + _time_module.gmtime(float(ts)).tm_min / 60.0
            for ts in timestamps
        ]
    )
    radians = 2.0 * np.pi * hours / 24.0
    return np.column_stack([np.sin(radians), np.cos(radians)])


def _compute_atr_percentile(atr_values: np.ndarray, window: int = 100) -> np.ndarray:
    """Rolling percentile rank of ATR within a trailing window.

    Low ATR percentile = quiet market, high = volatile. Stage 2 can
    learn that signals during extreme ATR are less reliable.

    Args:
        atr_values: ATR time series.
        window: Rolling window size (default 100 bars).

    Returns:
        Percentile ranks in [0, 1], same length as input.
    """
    from collections import deque

    result = np.zeros(len(atr_values), dtype=np.float64)
    buf: deque[float] = deque(maxlen=window)
    for i, val in enumerate(atr_values):
        buf.append(float(val))
        if len(buf) >= 10:
            sorted_buf = sorted(buf)
            rank = sorted_buf.index(float(val))
            result[i] = rank / max(len(sorted_buf) - 1, 1)
        else:
            result[i] = 0.5  # neutral when insufficient history
    return result


def _purged_walk_forward_splits(
    timestamps: np.ndarray,
    n_folds: int = 5,
    purge_bars: int = 12,
    embargo_bars: int = 6,
):
    """Generate (train_idx, val_idx) pairs with purge gap and embargo.

    Splits the data chronologically into n_folds contiguous blocks.  For
    each block (used as the validation set), training data is drawn from
    all earlier blocks, with a purge gap of `purge_bars` and an embargo of
    `embargo_bars` after the validation block.

    Args:
        timestamps: 1-D array of per-sample timestamps (sorted ascending).
        n_folds: Number of walk-forward folds.
        purge_bars: Number of bars to exclude between train and test.
        embargo_bars: Number of bars to exclude after test (prevents test
            information leaking into future train folds).

    Yields:
        (train_indices, val_indices) as lists of integer positions.
    """
    n = len(timestamps)
    if n < n_folds * 2:
        raise ValueError(f"Not enough samples ({n}) for {n_folds} folds")

    fold_boundaries = np.linspace(0, n, n_folds + 1, dtype=int)
    min_train = max(100, purge_bars * 2)  # minimum training samples per fold

    for i in range(n_folds):
        val_start = fold_boundaries[i]
        val_end = fold_boundaries[i + 1]

        # Train: everything before (val_start - purge_bars)
        train_end = max(0, val_start - purge_bars)
        if train_end < min_train:
            continue  # skip fold — insufficient training data

        train_idx = list(range(0, train_end))
        val_idx = list(range(val_start, val_end))

        if len(val_idx) < purge_bars:
            continue  # skip fold — validation too small

        yield train_idx, val_idx


def _compute_rolling_hit_rate(
    oof_preds: np.ndarray,
    y_true: np.ndarray,
    window: int = 20,
    lag: int = 12,
) -> np.ndarray:
    """Rolling directional accuracy of OOF predictions (lagged to avoid leakage).

    A high rolling hit rate suggests the current regime is learnable;
    a low hit rate signals regime shift — Stage 2 should be cautious.

    The lag parameter shifts the hit rate window back by `lag` bars, so that
    the hit rate at bar `i` only uses information from bars [i-window-lag, i-lag].
    This prevents using future returns that haven't been realized yet.

    Args:
        oof_preds: OOF regression predictions (signed bps).
        y_true: True regression labels (signed bps).
        window: Rolling window size.
        lag: Minimum lookback (horizon_bars) to prevent data leakage.

    Returns:
        Rolling hit rate in [0, 1].
    """
    n = len(oof_preds)
    result = np.full(n, 0.5, dtype=np.float64)  # neutral prior
    direction_correct = (np.sign(oof_preds) == np.sign(y_true)).astype(np.float64)
    for i in range(window + lag, n):
        start = i - window - lag
        end = i - lag
        result[i] = float(np.mean(direction_correct[start:end]))
    return result


def _build_pit_oof_features(
    X: np.ndarray,
    y_target: np.ndarray,
    ts_array: np.ndarray | None,
    feature_names: list[str],
    fold_splits: list[tuple[list[int], list[int]]],
    lgb_params: dict[str, object],
    *,
    n_folds: int = 5,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Generate OOF predictions and meta-features row-by-row with deque.

    Pixel-level mirror of MetaSignalFilter._compute_runtime_meta_features().
    Each fold: train LGB on train set, then iterate val set chronologically
    with rolling deques. Z-score and percentile features use only past data.

    Cross-fold deque clearance ensures every fold independently replicates
    cold-start behavior (empty buffer → warm-up transition).

    Returns:
        (oof_preds, meta_arrays) where meta_arrays is a dict mapping
        feature name → (n_samples,) or (n_samples, 2) numpy array.
    """
    import lightgbm as lgb

    n = len(X)
    oof_preds = np.zeros(n, dtype=np.float64)
    oof_zscores = np.zeros(n, dtype=np.float64)
    atr_percentiles = np.full(n, 0.5, dtype=np.float64)
    session_sins = np.zeros(n, dtype=np.float64)
    session_coss = np.zeros(n, dtype=np.float64)

    # Micro-derived meta features (computed only when micro columns exist)
    has_micro = "avg_spread" in feature_names
    spread_zscores = np.zeros(n, dtype=np.float64)
    oim_divergences = np.zeros(n, dtype=np.float64)
    toxicity_scores = np.zeros(n, dtype=np.float64)

    # Column indices
    atr_col = feature_names.index("M5_ATR_14") if "M5_ATR_14" in feature_names else 2
    avg_spread_col = feature_names.index("avg_spread") if has_micro else -1
    oim_col = feature_names.index("OIM") if has_micro else -1
    tick_velocity_col = feature_names.index("tick_velocity") if has_micro else -1
    tick_return_col = feature_names.index("tick_return") if has_micro else -1

    total_folds = len(fold_splits)

    for fold_idx, (train_idx, val_idx) in enumerate(fold_splits):
        train_idx = np.asarray(train_idx, dtype=np.intp)
        val_idx = np.asarray(val_idx, dtype=np.intp)

        # Train Stage 1 on this fold's train set
        X_tr, y_tr = X[train_idx], y_target[train_idx]
        dtrain = lgb.Dataset(X_tr, label=y_tr)
        booster = lgb.train(
            dict(lgb_params),
            dtrain,
            valid_sets=[dtrain],
            valid_names=["train"],
            callbacks=[lgb.early_stopping(20, verbose=False), lgb.log_evaluation(0)],
        )

        # ── PiT rolling deques (pixel mirror of MetaSignalFilter buffers) ──
        pred_deque: deque[float] = deque(maxlen=20)
        pred_deque.clear()  # Cross-fold clearance: cold-start for every fold
        atr_deque: deque[float] = deque(maxlen=100)
        atr_deque.clear()
        spread_deque: deque[float] = deque(maxlen=100)
        spread_deque.clear()

        for i in val_idx:  # chronological order guaranteed by purged splits
            # ── Stage 1 prediction ──
            pred = float(booster.predict(X[i : i + 1])[0])
            oof_preds[i] = pred

            # ── oof_pred_zscore_20 (mirrors runtime lines 456-462) ──
            if len(pred_deque) >= 2:
                buf = np.array(pred_deque, dtype=np.float64)
                buf_std = float(np.std(buf))
                raw_z = (pred - float(np.mean(buf))) / max(buf_std, 1e-6)
                oof_zscores[i] = float(np.clip(raw_z, -5.0, 5.0))

            # ── atr_percentile_100 (mirrors runtime lines 466-472) ──
            atr_val = float(X[i, atr_col])
            if len(atr_deque) >= 10:
                sorted_buf = sorted(atr_deque)
                try:
                    rank = sorted_buf.index(atr_val)
                except ValueError:
                    rank = 0
                atr_percentiles[i] = rank / max(len(sorted_buf) - 1, 1)

            # ── Micro-derived meta features (mirrors runtime lines 489-515) ──
            if has_micro:
                # spread_zscore (line 490-497)
                avg_spread = float(X[i, avg_spread_col])
                if len(spread_deque) >= 10:
                    sbuf = np.array(spread_deque, dtype=np.float64)
                    s_std = float(np.std(sbuf))
                    if s_std > 1e-8:
                        spread_zscores[i] = (avg_spread - float(np.mean(sbuf))) / s_std

                # oim_divergence (line 500-506)
                oim = float(X[i, oim_col])
                tick_return = float(X[i, tick_return_col])
                if abs(oim) > 0.01 and abs(tick_return) > 1e-6:
                    oim_dir = 1.0 if oim > 0 else -1.0
                    price_dir = 1.0 if tick_return > 0 else -1.0
                    oim_divergences[i] = -oim_dir * price_dir

                # toxicity_score (line 510-514)
                tick_velocity = float(X[i, tick_velocity_col])
                if atr_val > 1e-6 and tick_velocity > 0:
                    tox = tick_velocity / max(atr_val, 1e-6)
                    toxicity_scores[i] = float(np.clip(tox / 1000.0, 0.0, 10.0))

            # ── Session sin/cos (mirrors runtime lines 481-484) ──
            if ts_array is not None:
                session_sins[i], session_coss[i] = _compute_session_features(ts_array[i : i + 1])[0]

            # ── Append to deques AFTER computing all features (no lookahead) ──
            pred_deque.append(pred)
            if atr_val > 0:
                atr_deque.append(atr_val)
            if has_micro:
                avg_spread_v = float(X[i, avg_spread_col])
                if avg_spread_v == avg_spread_v:  # not NaN
                    spread_deque.append(avg_spread_v)

        # Log fold summary
        val_preds = oof_preds[val_idx]
        if ts_array is not None and len(val_idx) > 0:
            val_ts = ts_array[val_idx]
            print(
                f"[meta]   Fold {fold_idx + 1}/{total_folds}: "
                f"n_train={len(train_idx)}, n_val={len(val_idx)}, "
                f"pred_mean={float(np.mean(val_preds)):.4f}, "
                f"pred_std={float(np.std(val_preds)):.4f}, "
                f"val_time=[{val_ts[0]:.0f}, {val_ts[-1]:.0f}]"
            )
        else:
            print(
                f"[meta]   Fold {fold_idx + 1}/{total_folds}: "
                f"n_train={len(train_idx)}, n_val={len(val_idx)}, "
                f"pred_mean={float(np.mean(val_preds)):.4f}"
            )

    # Assemble meta-feature arrays dict
    meta_arrays: dict[str, np.ndarray] = {
        "oof_pred": oof_preds,
        "oof_pred_zscore_20": oof_zscores,
        "atr_percentile_100": atr_percentiles,
        "session_sin": session_sins,
        "session_cos": session_coss,
    }
    if has_micro:
        meta_arrays["spread_zscore"] = spread_zscores
        meta_arrays["oim_divergence"] = oim_divergences
        meta_arrays["toxicity_score"] = toxicity_scores

    return oof_preds, meta_arrays


def build_meta_features(
    contract_path: str | Path,
    output_path: str | Path,
    *,
    n_folds: int = 5,
    seed: int = 42,
    huber_delta_override: float | None = None,
    no_rolling_hit_rate: bool = False,
    purged_cv: bool = False,
    purge_bars: int = 12,
    embargo_bars: int = 6,
    pit_cv: bool = False,
    mode: str = "regression",
) -> None:
    """Build Stage 2 meta-feature dataset from Stage 1 OOF predictions.

    Args:
        contract_path: Path to TrainingContract YAML (Stage 1 config).
        output_path: Where to save the Stage 2 feature NPZ.
        n_folds: Number of CV folds for OOF prediction generation.
        seed: Random seed for reproducibility.
        purged_cv: Use purged walk-forward splits (time-series safe) instead
            of shuffled KFold. Requires timestamps in the dataset.
        purge_bars: Number of bars to exclude between train and test (default 12 = horizon).
        embargo_bars: Number of bars to exclude after test set (default 6).
        pit_cv: Generate OOF features row-by-row with deque(maxlen=20),
            matching MetaSignalFilter's runtime behavior pixel-for-pixel.
            Requires purged_cv=True. Eliminates Training-Serving Skew.
        mode: "regression" (default, Huber loss, continuous target) or
            "binary" (binary_logloss, drop-timeout, probability output).
    """
    contract = TrainingContract.from_file(contract_path)
    ds_path = Path(contract.dataset.path)

    if not ds_path.exists():
        print(f"[meta] ERROR: Dataset not found: {ds_path}")
        sys.exit(1)

    is_binary = mode == "binary"
    print(f"[meta] Loading dataset: {ds_path} (mode={mode})")
    raw = np.load(ds_path, allow_pickle=True)

    X = np.asarray(raw["X"], dtype=np.float64)
    feature_names = list(raw.get("feature_names", [f"f_{i}" for i in range(X.shape[1])]))

    # Stage 1 labels
    y_dir = np.asarray(raw["y"], dtype=np.int32).ravel()
    timestamps = raw.get("timestamps")
    ts_array = np.asarray(timestamps, dtype=np.float64).ravel() if timestamps is not None else None

    if is_binary:
        # ── Binary mode: drop timeout (y==0), remap {-1→0, 1→1} ──
        keep_mask = y_dir != 0
        X = X[keep_mask]
        y_dir = y_dir[keep_mask]
        if ts_array is not None:
            ts_array = ts_array[keep_mask]
        y_reg = None  # not used in binary mode
        # Remap: -1 (SL) → 0, 1 (TP) → 1
        y_binary = np.where(y_dir == -1, 0, 1).astype(np.int32)
        y_target = y_binary.astype(np.float64)
        print(
            f"[meta] Binary mode: dropped {(~keep_mask).sum()} timeout samples, {X.shape[0]} remaining"
        )
        print(f"[meta] Binary labels: {np.sum(y_binary==1)} TP, {np.sum(y_binary==0)} SL")
        collapse_target_std = float(np.std(y_target))
    else:
        # ── Regression mode: continuous PnL target ──
        y_reg = raw.get("y_reg")
        if y_reg is None:
            print("[meta] ERROR: Dataset missing 'y_reg' field (regression labels required)")
            sys.exit(1)
        y_reg = np.asarray(y_reg, dtype=np.float64).ravel()
        y_target = y_reg
        # Stage 2 labels: binary TP/SL
        unique_labels = set(np.unique(y_dir))
        if unique_labels == {-1, 0, 1}:
            y_binary = np.where(y_dir == 1, 1, 0).astype(np.int32)
        else:
            y_binary = y_dir
        print(f"[meta] Binary labels: {np.sum(y_binary==1)} TP, {np.sum(y_binary==0)} non-TP")
        collapse_target_std = float(np.std(y_target))

    print(f"[meta] X: {X.shape}, y_target: {y_target.shape}")
    print(
        f"[meta] y_target stats: mean={float(np.mean(y_target)):.4f}, std={float(np.std(y_target)):.4f}"
    )

    if pit_cv and not purged_cv:
        print("[meta] WARNING: --pit-cv requires --purged-cv. Enabling purged CV.")
        purged_cv = True

    # ── Stage 1: Generate OOF predictions ──
    if pit_cv:
        print(
            f"[meta] Running PiT (Point-in-Time) OOF generation: {n_folds} folds, deque-based row-by-row..."
        )
    else:
        obj_name = "binary" if is_binary else "Huber"
        print(f"[meta] Running {n_folds}-fold cross_val_predict with LightGBM native {obj_name}...")

    import lightgbm as lgb

    lgb_params: dict[str, object]
    huber_delta: float
    if is_binary:
        n_pos = int(np.sum(y_binary == 1))
        n_neg = int(np.sum(y_binary == 0))
        scale_pos_weight = n_neg / max(n_pos, 1)
        lgb_params = {
            "objective": "binary",
            "boosting_type": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "min_data_in_leaf": 20,
            "lambda_l1": 0.1,
            "lambda_l2": 0.1,
            "max_depth": -1,
            "random_state": seed,
            "n_jobs": -1,
            "verbosity": -1,
            "num_iterations": 500,
            "scale_pos_weight": scale_pos_weight,
        }
        print(
            f"[meta] Binary class weights: scale_pos_weight={scale_pos_weight:.4f} ({n_neg} neg / {n_pos} pos)"
        )
        huber_delta = 1.0  # unused in binary mode
    else:
        huber_delta = (
            huber_delta_override
            if huber_delta_override is not None
            else float(contract.architecture.custom_params.get("huber_delta", 1.0))
        )
        lgb_params = {
            "objective": "huber",
            "alpha": huber_delta,
            "boosting_type": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "min_data_in_leaf": 20,
            "lambda_l1": 0.1,
            "lambda_l2": 0.1,
            "max_depth": -1,
            "random_state": seed,
            "n_jobs": -1,
            "verbosity": -1,
            "num_iterations": 500,
        }

    if purged_cv and ts_array is not None:
        print(
            f"[meta] Using purged walk-forward CV: {n_folds} folds, purge={purge_bars}, embargo={embargo_bars}"
        )
        fold_splits = list(
            _purged_walk_forward_splits(
                ts_array, n_folds=n_folds, purge_bars=purge_bars, embargo_bars=embargo_bars
            )
        )
    else:
        if purged_cv:
            print(
                "[meta] WARNING: purged_cv=True but no timestamps — falling back to shuffled KFold"
            )
        kf = KFold(n_splits=n_folds, shuffle=True, random_state=seed)
        n_samples = len(X)
        fold_splits = list(kf.split(np.arange(n_samples)))

    # ── Determine which meta-features to compute ──
    has_micro = "avg_spread" in feature_names

    if pit_cv:
        # ── PiT path: row-by-row deque, pixel mirror of runtime ──
        oof_preds, pit_meta = _build_pit_oof_features(
            X,
            y_target,
            ts_array,
            feature_names,
            fold_splits,
            lgb_params,
            n_folds=len(fold_splits),
        )

        # Collapse check
        pred_std = float(np.std(oof_preds))
        target_std = collapse_target_std
        collapse_ratio = pred_std / max(target_std, 1e-10)
        print(
            f"[meta] PiT OOF collapse ratio: {collapse_ratio:.4f} (pred_std={pred_std:.4f}, target_std={target_std:.4f})"
        )
        if is_binary:
            if pred_std < 0.05:
                print("[meta] WARNING: PiT OOF probabilities have very low variance — weak signal")
        else:
            if collapse_ratio < 0.1:
                print(
                    "[meta] CRITICAL: PiT OOF predictions collapsed — Huber model failed to learn variance"
                )

        # ── Assemble meta-features from PiT arrays ──
        print("[meta] Assembling PiT meta-features...")
        meta_features: list[np.ndarray] = []
        meta_names: list[str] = []

        # 1. OOF prediction
        meta_features.append(oof_preds.reshape(-1, 1))
        meta_names.append("oof_pred")

        # 2. OOF prediction z-score (PiT-computed)
        meta_features.append(pit_meta["oof_pred_zscore_20"].reshape(-1, 1))
        meta_names.append("oof_pred_zscore_20")

        # 3. ATR percentile (PiT-computed)
        meta_features.append(pit_meta["atr_percentile_100"].reshape(-1, 1))
        meta_names.append("atr_percentile_100")

        # 4-5. Vol z-score + Hurst (direct column copies, no lookahead risk)
        vol_col = feature_names.index("M5_Vol_ZScore") if "M5_Vol_ZScore" in feature_names else 5
        meta_features.append(X[:, vol_col].reshape(-1, 1))
        meta_names.append("vol_zscore")

        hurst_col = feature_names.index("M5_Hurst") if "M5_Hurst" in feature_names else 37
        meta_features.append(X[:, hurst_col].reshape(-1, 1))
        meta_names.append("hurst_m5")

        # 6-7. Session sin/cos (per-row from timestamp, PiT-computed)
        meta_features.append(pit_meta["session_sin"].reshape(-1, 1))
        meta_features.append(pit_meta["session_cos"].reshape(-1, 1))
        meta_names.extend(["session_sin", "session_cos"])

        # 8-10. Micro-derived meta features (PiT-computed when available)
        if has_micro:
            meta_features.append(pit_meta["spread_zscore"].reshape(-1, 1))
            meta_names.append("spread_zscore")
            meta_features.append(pit_meta["oim_divergence"].reshape(-1, 1))
            meta_names.append("oim_divergence")
            meta_features.append(pit_meta["toxicity_score"].reshape(-1, 1))
            meta_names.append("toxicity_score")
    else:
        # ── Legacy path: batch cross_val_predict + post-hoc meta features ──
        oof_preds = np.zeros(len(X), dtype=np.float64)

        for fold_idx, (train_idx, val_idx) in enumerate(fold_splits):
            train_idx = np.asarray(train_idx, dtype=np.intp)
            val_idx = np.asarray(val_idx, dtype=np.intp)
            X_tr, X_val = X[train_idx], X[val_idx]
            y_tr, y_val = y_target[train_idx], y_target[val_idx]

            dtrain = lgb.Dataset(X_tr, label=y_tr)
            dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)

            booster = lgb.train(
                dict(lgb_params),
                dtrain,
                valid_sets=[dtrain, dval],
                valid_names=["train", "val"],
                callbacks=[
                    lgb.early_stopping(20, verbose=False),
                    lgb.log_evaluation(0),
                ],
            )

            oof_preds[val_idx] = booster.predict(X_val)
            if ts_array is not None and purged_cv and len(val_idx) > 0:
                val_ts = ts_array[val_idx]
                print(
                    f"[meta]   Fold {fold_idx + 1}/{len(fold_splits)}: "
                    f"n_train={len(train_idx)}, n_val={len(val_idx)}, "
                    f"pred_mean={float(np.mean(oof_preds[val_idx])):.4f}, "
                    f"val_time=[{val_ts[0]:.0f}, {val_ts[-1]:.0f}]"
                )
            else:
                print(
                    f"[meta]   Fold {fold_idx + 1}/{len(fold_splits)}: "
                    f"n_train={len(train_idx)}, n_val={len(val_idx)}, "
                    f"pred_mean={float(np.mean(oof_preds[val_idx])):.4f}"
                )

        # Collapse check
        pred_std = float(np.std(oof_preds))
        target_std = collapse_target_std
        collapse_ratio = pred_std / max(target_std, 1e-10)
        print(
            f"[meta] OOF collapse ratio: {collapse_ratio:.4f} (pred_std={pred_std:.4f}, target_std={target_std:.4f})"
        )
        if is_binary:
            if pred_std < 0.05:
                print("[meta] WARNING: OOF probabilities have very low variance — weak signal")
        else:
            if collapse_ratio < 0.1:
                print(
                    "[meta] CRITICAL: OOF predictions collapsed — Huber model failed to learn variance"
                )
                print(
                    "[meta] Consider: reducing huber_delta, increasing num_iterations, or checking label quality"
                )

        # ── Meta-feature engineering (post-hoc, legacy) ──
        print("[meta] Computing meta-features...")

        meta_features = []
        meta_names = []

        # 1. OOF prediction
        meta_features.append(oof_preds.reshape(-1, 1))
        meta_names.append("oof_pred")

        # 2. OOF prediction z-score (post-hoc rolling 20-bar)
        oof_z = np.zeros(len(oof_preds), dtype=np.float64)
        for i in range(20, len(oof_preds)):
            window = oof_preds[i - 20 : i]
            w_std = float(np.std(window)) + 1e-10
            raw_z = (oof_preds[i] - float(np.mean(window))) / w_std
            oof_z[i] = float(np.clip(raw_z, -5.0, 5.0))
        meta_features.append(oof_z.reshape(-1, 1))
        meta_names.append("oof_pred_zscore_20")

        # 3. ATR percentile (post-hoc rolling, not PiT)
        atr_col = feature_names.index("M5_ATR_14") if "M5_ATR_14" in feature_names else 2
        atr_values = X[:, atr_col]
        atr_pct = _compute_atr_percentile(atr_values).reshape(-1, 1)
        meta_features.append(atr_pct)
        meta_names.append("atr_percentile_100")

        # 4-5. Vol zscore + Hurst
        vol_col = feature_names.index("M5_Vol_ZScore") if "M5_Vol_ZScore" in feature_names else 5
        meta_features.append(X[:, vol_col].reshape(-1, 1))
        meta_names.append("vol_zscore")

        hurst_col = feature_names.index("M5_Hurst") if "M5_Hurst" in feature_names else 37
        meta_features.append(X[:, hurst_col].reshape(-1, 1))
        meta_names.append("hurst_m5")

        # 6-7. Session sin/cos
        if ts_array is not None:
            session = _compute_session_features(ts_array)
            meta_features.append(session)
            meta_names.extend(["session_sin", "session_cos"])
        else:
            print("[meta] No timestamps — skipping session features")

    # 8. Rolling hit rate (lagged by horizon_bars to avoid leakage)
    # Skip when --no-rolling-hit-rate is set (not available at inference time)
    if not no_rolling_hit_rate:
        lag = contract.label.horizon_bars
        hit_rate = _compute_rolling_hit_rate(oof_preds, y_target, window=20, lag=lag).reshape(-1, 1)
        meta_features.append(hit_rate)
        meta_names.append("rolling_hit_rate_20")
    else:
        print(
            "[meta] --no-rolling-hit-rate: skipping rolling_hit_rate_20 (runtime-safe feature set)"
        )

    # ── Assemble Stage 2 feature matrix ──
    meta_arr = np.column_stack(meta_features)
    X_stage2 = np.column_stack([X, meta_arr])
    stage2_feature_names = feature_names + meta_names
    # Runtime feature names exclude rolling_hit_rate_20 (47 = 40 V9 + 7 meta)
    runtime_feature_names = stage2_feature_names.copy()

    print(f"[meta] Stage 2 feature matrix: {X_stage2.shape}")
    print(f"[meta] Original features: {len(feature_names)}")
    print(f"[meta] Meta features: {len(meta_names)}")
    print(f"[meta] Total: {len(stage2_feature_names)}")
    print(f"[meta] Runtime features (no rolling_hit_rate): {len(runtime_feature_names)}")

    # ── Save ──
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs: dict[str, Any] = {
        "X": X_stage2,
        "y": y_binary,
        "y_reg": y_target if not is_binary else y_binary.astype(np.float64),
        "oof_preds": oof_preds,
        "meta_feature_names": np.array(meta_names, dtype=str),
        "feature_names": np.array(stage2_feature_names, dtype=str),
        "runtime_feature_names": np.array(runtime_feature_names, dtype=str),
        "contract_id": contract.contract_id,
        "n_folds": n_folds,
        "collapse_ratio": collapse_ratio,
    }
    # FIX-20260528-013: Preserve timestamps for CPCV evaluation
    if ts_array is not None:
        save_kwargs["timestamps"] = ts_array
    np.savez_compressed(output_path, **save_kwargs)
    print(f"[meta] Saved Stage 2 dataset to: {output_path}")
    print("[meta] Done.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build OOF meta-features for Stage 2 training")
    ap.add_argument("--contract", required=True, help="Path to Stage 1 TrainingContract YAML")
    ap.add_argument("--output", default="data/training/meta_features.npz", help="Output NPZ path")
    ap.add_argument("--n-folds", type=int, default=5, help="Number of CV folds (default: 5)")
    ap.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    ap.add_argument(
        "--huber-delta", type=float, default=None, help="Override huber_delta from contract config"
    )
    ap.add_argument(
        "--no-rolling-hit-rate",
        action="store_true",
        help="Exclude rolling_hit_rate_20 (not available at inference time)",
    )
    ap.add_argument(
        "--purged-cv",
        action="store_true",
        help="Use purged walk-forward CV (time-series safe) instead of shuffled KFold",
    )
    ap.add_argument(
        "--purge-bars", type=int, default=12, help="Purge gap in bars (default: 12 = horizon)"
    )
    ap.add_argument("--embargo-bars", type=int, default=6, help="Embargo gap in bars (default: 6)")
    ap.add_argument(
        "--pit-cv",
        action="store_true",
        help="Generate OOF features row-by-row with deque (PiT), mirroring runtime MetaSignalFilter exactly",
    )
    ap.add_argument(
        "--mode",
        choices=["regression", "binary"],
        default="regression",
        help="Stage 1 mode: regression (Huber, continuous) or binary (drop-timeout, probability). Default: regression",
    )
    args = ap.parse_args(argv)

    build_meta_features(
        contract_path=args.contract,
        output_path=args.output,
        n_folds=args.n_folds,
        seed=args.seed,
        huber_delta_override=args.huber_delta,
        no_rolling_hit_rate=args.no_rolling_hit_rate,
        purged_cv=args.purged_cv,
        purge_bars=args.purge_bars,
        embargo_bars=args.embargo_bars,
        pit_cv=args.pit_cv,
        mode=args.mode,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
