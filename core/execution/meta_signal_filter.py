"""Meta-model signal filter — Stage 2 of two-stage meta-labeling.

Filters directional trading signals using a trained LightGBM model (and
optional MLP ensemble) that predicts P(TP hit | direction, context features).
Signals with low P(win) are discarded before they reach the execution layer.

Integration point: after consensus aggregation, before capital allocation.

v4.1 (2026-05-16): Unified 49-dim V9+Micro support.
    - Accepts 49-dim unified dict (40 V9 + 9 microstructure features)
    - Z-score normalizes raw micro features via pre-fit StandardScaler
      (Scaling Toxicity Fix — prevents MLP gradient explosion)
    - Micro data gate: rejects signal when micro features are NaN
      (Imputation Ghosts Fix — no blind guessing with zeros)
    - 3 new micro-derived runtime meta-features: spread_zscore, oim_divergence,
      toxicity_score
    - Dual-model ensemble support (LGB + MLP probability averaging)

v4.0 (2026-05-16): Runtime meta-feature computation from V9 institutional features.
    Replaces the hardcoded 15-feature _runtime_feature_map with 47-feature runtime
    meta-feature computation (40 V9 + 7 meta).  Supports cold-start warm-up guard,
    output_unit conversion (bps vs atr_multiple), and both "binary" and "bandit" modes.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES


@dataclass
class FilterResult:
    """Result of meta-model signal filtering."""

    passed: bool
    p_win: float
    threshold: float
    reason: str = ""
    exhaustion_factor: float = 1.0


class MetaSignalFilter:
    """LightGBM-based signal quality filter with runtime meta-feature computation.

    Loads a trained Stage 2 classifier and evaluates each trading signal's
    P(TP|signal) using 47 runtime-computable features (40 V9 institutional +
    7 meta-features derived from Stage 1 prediction + V9 features + timestamp).

    Usage::

        filt = MetaSignalFilter(model_path="data/models/meta_filter/model.txt")
        filt.load()

        result = filt.filter(
            direction=1,
            s1_prediction=12.5,      # Stage 1 raw prediction (bps or ATR-multiple)
            v9_features={"M5_Ret_1": 0.02, "M5_ATR_14": 1.5, ...},
            timestamp_utc=1715875200.0,
        )
        if result.passed:
            execute(signal)
    """

    # Legacy feature names (v3.1 fallback, 15 features)
    META_FEATURE_NAMES = [
        "s1_direction",
        "s1_confidence",
        "m5_rsi",
        "m5_macd",
        "h1_ret",
        "h1_macd",
        "m5_vol_zscore",
        "m5_ou_theta",
        "m5_hurst",
        "atr_percentile",
        "rsi_distance",
        "h1_trend_strength",
        "direction_x_rsi",
        "direction_x_macd",
        "direction_x_h1",
    ]

    def __init__(
        self,
        *,
        model_path: str | None = None,
        mlp_model_path: str | None = None,
        threshold: float = 0.30,
        enabled: bool = True,
        mode: str = "binary",
        ensemble_weights: tuple[float, float] | None = None,
        micro_scaler_path: str | None = None,
        # ── Protocol 2: Platt Scaling calibration ──
        calibrator_path: str | None = None,
        # ── Protocol 3: Conformal prediction thresholding ──
        conformal_mode: bool = False,
        conformal_window: int = 500,
        conformal_percentile: float = 80.0,
        min_threshold: float = 0.50,
        conformal_max_age_days: float = 14.0,
    ) -> None:
        self.model_path = model_path
        self.mlp_model_path = mlp_model_path
        self.threshold = threshold
        self.enabled = enabled
        self.mode = mode
        self._model: Any = None
        self._mlp_model: Any = None
        self._feature_names: list[str] = []
        self._n_wins: int = 0
        self._win_rate: float = 0.0
        self._output_unit: str = "bps"

        # v3.2: Ensemble weights (LGB, MLP) — optimized post-training, not hardcoded 50/50
        self._ensemble_weights: tuple[float, float] = ensemble_weights or (0.6, 0.4)

        # v4.1: Microstructure StandardScaler (Scaling Toxicity Fix)
        self._micro_scaler: Any = None
        self._micro_scaler_path = micro_scaler_path
        if micro_scaler_path and os.path.exists(micro_scaler_path):
            try:
                import joblib

                self._micro_scaler = joblib.load(micro_scaler_path)
            except Exception:
                pass

        # Protocol 2: Platt Scaling calibrator (smooth sigmoid, no step collapse)
        self._calibrator: Any = None
        self._calibrator_path = calibrator_path

        # Protocol 3: Conformal prediction
        self._conformal_mode = conformal_mode
        self._conformal_window = conformal_window
        self._conformal_percentile = conformal_percentile
        self._min_threshold = min_threshold
        self._conformal_max_age_days = conformal_max_age_days

        # v4.0: Rolling buffers for runtime meta-feature computation
        self._pred_buffer: deque[float] = deque(maxlen=20)
        self._atr_buffer: deque[float] = deque(maxlen=100)

        # v4.1: Rolling buffers for micro-derived meta features (EWMA)
        self._micro_spread_buffer: deque[float] = deque(maxlen=100)
        self._micro_oim_buffer: deque[float] = deque(maxlen=100)

        # Protocol 3: Prediction history for conformal thresholding
        # Stores (timestamp, probability) tuples for time-decayed filtering.
        self._pred_history: deque[tuple[float, float]] = deque(maxlen=conformal_window)

    # ── Public API ──

    def load(self) -> bool:
        """Load the trained model (LightGBM .txt or MLP .json).

        For .json files, loads as OnlineMLP primary model (no LGB needed).
        For .txt files, loads as LightGBM model with optional MLP ensemble partner.

        Returns True if at least one model is ready.
        """
        if not self.enabled:
            return False
        if not self.model_path or not os.path.exists(self.model_path):
            return False

        # Determine model type from file extension
        is_mlp_primary = self.model_path.endswith(".json")

        try:
            # Load metadata (same for both model types)
            meta_path = self.model_path.rsplit(".", 1)[0] + ".meta.json"
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                self._feature_names = meta.get("feature_names", self.META_FEATURE_NAMES)
                self._n_wins = meta.get("n_wins", 0)
                self._win_rate = meta.get("win_rate", 0)
                stored_threshold = meta.get("threshold")
                if stored_threshold is not None:
                    self.threshold = float(stored_threshold)
                self._output_unit = meta.get("output_unit", "bps")

            if is_mlp_primary:
                # ── MLP as primary model ──
                from core.brains.online_mlp_model import OnlineMLP

                self._mlp_model = OnlineMLP.load(self.model_path)
                self._model = None  # no LGB model
                self._load_calibrator()
                return True
            else:
                # ── LightGBM as primary model ──
                import lightgbm as lgb

                self._model = lgb.Booster(model_file=self.model_path)
                # Fallback: if .meta.json is missing, get feature names from the booster itself
                if not self._feature_names:
                    try:
                        self._feature_names = self._model.feature_name()
                    except Exception:
                        pass
                self._load_mlp_model()  # optional ensemble partner
                self._load_calibrator()
                return True
        except Exception:
            return False

    def _load_calibrator(self) -> None:
        """Load Platt scaling calibrator (LogisticRegression on log-odds).

        Protocol 2: Smooth sigmoid calibration that avoids the step-function
        collapse of IsotonicRegression.  Stores only 2 floats (coef_ + intercept_).
        """
        if not self._calibrator_path or not os.path.exists(self._calibrator_path):
            if self._conformal_mode:
                sys.stderr.write(
                    json.dumps(
                        {
                            "event": "conformal_warning",
                            "message": "Conformal mode enabled but no calibrator loaded — falling back to fixed threshold",
                        }
                    )
                    + "\n"
                )
            return
        try:
            import joblib

            self._calibrator = joblib.load(self._calibrator_path)
        except Exception as e:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "calibrator_load_error",
                        "path": self._calibrator_path,
                        "error": str(e),
                    }
                )
                + "\n"
            )
            self._calibrator = None

    def _load_mlp_model(self) -> None:
        """Load the optional MLP ensemble model for probability averaging."""
        if not self.mlp_model_path or not os.path.exists(self.mlp_model_path):
            return
        try:
            from core.brains.online_mlp_model import OnlineMLP

            self._mlp_model = OnlineMLP.load(self.mlp_model_path)
        except Exception:
            self._mlp_model = None

    def filter(
        self,
        direction: int,
        s1_prediction: float | None = None,
        v9_features: dict[str, float] | None = None,
        timestamp_utc: float | None = None,
        *,
        s1_confidence: float | None = None,
        features: dict[str, float] | None = None,
        atr_percentile: float | None = None,
    ) -> FilterResult:
        """Evaluate a directional signal and decide whether to keep it.

        **v4.1 path (recommended)** — unified V9+Micro with micro data gate::

            result = filt.filter(
                direction=1,
                s1_prediction=12.5,       # Stage 1 raw prediction
                v9_features={...},        # 49-dim unified dict (40 V9 + 9 micro)
                timestamp_utc=1715875200.0,
            )

        **v4.0 path** — 40 V9 features only (no micro data)::

            result = filt.filter(
                direction=1,
                s1_prediction=12.5,
                v9_features={...},        # 40 V9 institutional features
                timestamp_utc=1715875200.0,
            )

        **Legacy path (deprecated)** — 15-feature handcrafted mapping::

            result = filt.filter(
                direction=1,
                s1_confidence=0.7,
                features={"m5_rsi": 65.2, "m5_macd": 0.15, ...},
                atr_percentile=0.5,
            )

        Args:
            direction: 1=long, -1=short.
            s1_prediction: Stage 1 raw prediction (bps or ATR-multiple).
            v9_features: Dict of features (40 V9, or 49 V9+Micro unified).
            timestamp_utc: Unix epoch seconds for session encoding.
            s1_confidence: (legacy) Stage 1 model confidence [0, 1].
            features: (legacy) Context features dict.
            atr_percentile: (legacy) Current ATR percentile rank.

        Returns:
            FilterResult with pass/fail decision and p_win estimate.
        """
        if not self.enabled or (self._model is None and self._mlp_model is None):
            return FilterResult(
                passed=True,
                p_win=0.5,
                threshold=self.threshold,
                reason="filter_disabled",
                exhaustion_factor=1.0,
            )

        try:
            # ── Feature resolution ──
            if v9_features is not None and s1_prediction is not None:
                # v4.1: Check if micro features are present (48+ keys implies unified dict)
                has_micro = len(v9_features) >= 45 and any(
                    k in v9_features for k in ("avg_spread", "OIM", "tick_velocity")
                )
                if has_micro:
                    # Micro data gate (Imputation Ghosts Fix): reject if micro features are NaN
                    micro_missing = self._check_micro_data(v9_features)
                    if micro_missing:
                        return FilterResult(
                            passed=False,
                            p_win=0.0,
                            threshold=self.threshold,
                            reason=f"micro_data_unavailable:{','.join(micro_missing)}",
                        )
                    # Apply Z-score normalization to micro features (Scaling Toxicity Fix)
                    v9_features = self._apply_micro_scaler(v9_features)

                # v4.0/v4.1: Runtime meta-feature computation path
                meta_map = self._compute_runtime_meta_features(
                    v9_features, s1_prediction, timestamp_utc
                )
                # Assemble full feature vector: V9 features + meta features
                feat_vec = self._assemble_feature_vector(v9_features, meta_map)
                # Update rolling buffers after feature computation
                self._pred_buffer.append(float(s1_prediction))
                current_atr = float(v9_features.get("M5_ATR_14", 1.0))
                if current_atr > 0:
                    self._atr_buffer.append(current_atr)
                # Update micro rolling buffers (for micro-derived meta features)
                if has_micro:
                    self._update_micro_buffers(v9_features)
            elif features is not None:
                # Legacy path: 15-feature handcrafted mapping
                fmap = self._runtime_feature_map(
                    direction=direction,
                    s1_confidence=s1_confidence or 0.5,
                    features=features,
                    atr_percentile=atr_percentile or 0.5,
                )
                feat_vec = [fmap.get(name, 0.0) for name in self._feature_names]
            else:
                return FilterResult(
                    passed=True,
                    p_win=0.5,
                    threshold=self.threshold,
                    reason="no_features_provided",
                    exhaustion_factor=1.0,
                )

            # ── Ensemble prediction (LGB + optional MLP) + Platt calibration ──
            p_win = self._predict_proba(feat_vec)

            if self.mode == "bandit":
                return FilterResult(
                    passed=True,
                    p_win=round(p_win, 4),
                    threshold=self.threshold,
                    exhaustion_factor=round(p_win, 4),
                )

            # ── Protocol 3: Conformal prediction thresholding ──
            effective_threshold = self.threshold
            now = time.time()
            if self._conformal_mode and self._calibrator is not None:
                # Time-decayed filter: only use predictions within max_age_days
                cutoff = now - self._conformal_max_age_days * 86400.0
                recent_probs = [p for ts, p in self._pred_history if ts >= cutoff]
                if len(recent_probs) >= 100:
                    percentile_threshold = float(
                        np.percentile(recent_probs, self._conformal_percentile)
                    )
                    effective_threshold = max(
                        percentile_threshold, self._min_threshold, self.threshold
                    )
                # else: warm-up — use fixed threshold until 100 recent predictions accumulate

            # Append to history AFTER computing threshold (no lookahead on current prediction)
            # Store (timestamp, probability) tuple for time-decayed conformal filtering
            self._pred_history.append((now, float(p_win)))

            passed = p_win >= effective_threshold
            reason = "" if passed else f"p_win_{p_win:.3f}_below_{effective_threshold:.3f}"
            return FilterResult(
                passed=passed,
                p_win=round(p_win, 4),
                threshold=round(effective_threshold, 4),
                reason=reason,
            )
        except Exception:
            return FilterResult(
                passed=True,
                p_win=0.5,
                threshold=self.threshold,
                reason="filter_error_fallback",
                exhaustion_factor=1.0,
            )

    def filter_arrays(
        self,
        direction: str,
        s1_prediction: float,
        v9_array: Any,
        micro_array: Any,
        timestamp_utc: float | None = None,
    ) -> FilterResult:
        """Convenience wrapper that accepts raw ndarrays from the live pipeline.

        In the live trading path, features arrive as (40,) V9 ndarray and (9,)
        micro ndarray.  This method builds the named dict that :meth:`filter`
        expects, using the model's own ``_feature_names`` as the canonical name
        list (features [0:40] are V9 institutional, [40:49] are microstructure).

        Args:
            direction: ``"long"`` or ``"short"`` (converted to 1/-1).
            s1_prediction: Stage 1 raw prediction in bps.
            v9_array: (40,) array of V9 institutional features.
            micro_array: (9,) array of microstructure features.
            timestamp_utc: Unix epoch seconds for session encoding.
        """
        import numpy as np

        # Convert direction string to int
        dir_int = 1 if direction == "long" else -1 if direction == "short" else 0

        # Build named feature dict from arrays using feature-name-indexed lookup.
        # V9 features are identified by timeframe prefix (M5_/M15_/M30_/H1_);
        # everything else is a micro or meta feature.  This eliminates the
        # hardcoded [:40]/[40:49] positional slices that assume a fixed ordering.
        _V9_PREFIXES = ("M5_", "M15_", "M30_", "H1_")
        v9_indices: list[int] = []
        micro_indices: list[int] = []
        for _i, _name in enumerate(self._feature_names):
            if _name.startswith(_V9_PREFIXES):
                v9_indices.append(_i)
            else:
                micro_indices.append(_i)

        v9_dict: dict[str, float] = {}
        if v9_array is not None:
            arr = np.asarray(v9_array, dtype=np.float64).ravel()
            for _vi, _fi in enumerate(v9_indices):
                if _vi < len(arr):
                    v9_dict[self._feature_names[_fi]] = float(arr[_vi])
        if micro_array is not None:
            arr = np.asarray(micro_array, dtype=np.float64).ravel()
            for _vi, _fi in enumerate(micro_indices):
                if _vi < len(arr):
                    v9_dict[self._feature_names[_fi]] = float(arr[_vi])

        return self.filter(
            direction=dir_int,
            s1_prediction=s1_prediction,
            v9_features=v9_dict,
            timestamp_utc=timestamp_utc,
        )

    # ── State persistence (crash recovery & restart resilience) ──

    def save_state(self, path: str) -> None:
        """Persist rolling buffers so conformal/z-score thresholds survive restarts.

        Writes a JSON dict with ``pred_history`` (timestamped tuples),
        ``pred_buffer``, ``atr_buffer``, and ``micro_spread_buffer``.

        On process crash or MT5 restart, deques are wiped.  Without this,
        the conformal threshold drops to ``min_threshold`` and the system
        runs minimally-defended until 100 new predictions accumulate.
        """
        import json as _json

        state = {
            "pred_history": list(self._pred_history),
            "pred_buffer": list(self._pred_buffer),
            "atr_buffer": list(self._atr_buffer),
            "micro_spread_buffer": list(self._micro_spread_buffer),
        }
        import os as _os

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            _json.dump(state, fh)
        _os.replace(tmp_path, path)

    def load_state(self, path: str) -> None:
        """Restore rolling buffers from a previously-saved state file.

        Missing or corrupt files are silently ignored — the filter will
        cold-start with fresh buffers (warm-up period applies).
        """
        import json as _json

        try:
            with open(path, encoding="utf-8") as fh:
                state = _json.load(fh)
        except Exception:
            return

        try:
            if "pred_history" in state:
                # Restore as (timestamp, probability) tuples
                items = state["pred_history"]
                if items and isinstance(items[0], list):
                    items = [tuple(it) for it in items]
                for item in items[-self._conformal_window :]:
                    self._pred_history.append((float(item[0]), float(item[1])))
        except Exception:
            pass

        try:
            for item in state.get("pred_buffer", [])[-20:]:
                self._pred_buffer.append(float(item))
        except Exception:
            pass

        try:
            for item in state.get("atr_buffer", [])[-100:]:
                self._atr_buffer.append(float(item))
        except Exception:
            pass

        try:
            for item in state.get("micro_spread_buffer", [])[-100:]:
                self._micro_spread_buffer.append(float(item))
        except Exception:
            pass

    def _predict_proba(self, feat_vec: list[float]) -> float:
        """Compute ensemble P(TP|signal) = w_lgb * prob_lgb + w_mlp * prob_mlp.

        Supports LGB-only, MLP-only, and LGB+MLP ensemble configurations.
        """
        if self._model is None and self._mlp_model is None:
            return 0.5

        # Get LGB probability (if available)
        lgb_prob: float | None = None
        if self._model is not None:
            lgb_prob = float(self._model.predict([feat_vec])[0])

        # Get MLP probability (if available)
        mlp_prob: float | None = None
        if self._mlp_model is not None:
            try:
                raw = self._mlp_model.forward_numpy(np.array(feat_vec, dtype=np.float32))
                # Handle both binary (N,2) softmax and single-output sigmoid
                if raw.ndim == 2 and raw.shape[1] == 2:
                    mlp_prob = float(raw[0, 1])  # P(class=1)
                elif raw.ndim == 1 and len(raw) >= 2:
                    mlp_prob = float(raw[1])  # (2,): P(class=1) at index 1
                elif raw.ndim == 1:
                    mlp_prob = float(raw[0])  # single-output sigmoid
                else:
                    mlp_prob = float(raw.ravel()[0])
            except Exception:
                pass

        # Ensemble or single-model fallback
        if lgb_prob is not None and mlp_prob is not None:
            w_lgb, w_mlp = self._ensemble_weights
            raw_prob = w_lgb * lgb_prob + w_mlp * mlp_prob
        elif mlp_prob is not None:
            raw_prob = mlp_prob
        elif lgb_prob is not None:
            raw_prob = lgb_prob
        else:
            return 0.5

        # ── Protocol 2: Platt scaling calibration (log-odds → sigmoid) ──
        if self._calibrator is not None:
            eps = 1e-4  # Conservative: raw_prob outside [1e-4, 1-1e-4] is noise
            clamped = max(min(raw_prob, 1 - eps), eps)
            log_odds = np.log(clamped / (1 - clamped))
            # calibrator.predict_proba returns [[P(0), P(1)]]
            cal_prob = float(self._calibrator.predict_proba([[log_odds]])[0, 1])
            return max(0.0, min(cal_prob, 1.0))  # Ultimate safety clamp

        return raw_prob

    # ── v4.1: Micro data gate + scaling ──

    @staticmethod
    def _check_micro_data(v9_features: dict[str, float]) -> list[str]:
        """Check if micro features are NaN or missing. Returns list of missing names."""
        missing = []
        for name in MICROSTRUCTURE_9_FEATURES:
            val = v9_features.get(name)
            if val is None:
                missing.append(name)
            elif isinstance(val, float) and val != val:  # NaN
                missing.append(name)
        return missing

    def _apply_micro_scaler(self, v9_features: dict[str, float]) -> dict[str, float]:
        """Apply pre-fit StandardScaler to 9 micro features in-place.

        Returns the (possibly modified) feature dict. If no scaler is configured,
        returns the dict unchanged (raw micro features are passed through).
        """
        if self._micro_scaler is None:
            return v9_features

        import numpy as np

        raw = np.array(
            [[float(v9_features.get(name, 0.0)) for name in MICROSTRUCTURE_9_FEATURES]],
            dtype=np.float64,
        )
        scaled = self._micro_scaler.transform(raw)[0]
        result = dict(v9_features)
        for i, name in enumerate(MICROSTRUCTURE_9_FEATURES):
            result[name] = float(scaled[i])
        return result

    def _update_micro_buffers(self, v9_features: dict[str, float]) -> None:
        """Update rolling buffers for micro-derived meta features."""
        avg_spread = v9_features.get("avg_spread", 0.0)
        oim = v9_features.get("OIM", 0.0)
        if avg_spread and avg_spread == avg_spread:  # not NaN
            self._micro_spread_buffer.append(float(avg_spread))
        if oim and oim == oim:
            self._micro_oim_buffer.append(float(oim))

    def get_exhaustion_factor(
        self,
        direction: int,
        s1_confidence: float,
        features: dict[str, float],
        *,
        atr_percentile: float = 0.5,
    ) -> float:
        """v3.1: Convenience method for bandit sizing — returns exhaustion_factor only."""
        result = self.filter(
            direction=direction,
            s1_confidence=s1_confidence,
            features=features,
            atr_percentile=atr_percentile,
        )
        return result.exhaustion_factor

    def is_active(self) -> bool:
        """Return True if the filter is loaded and ready to use."""
        return self.enabled and self._model is not None

    # ── v4.0: Runtime meta-feature computation ──

    def _compute_runtime_meta_features(
        self,
        v9_features: dict[str, float],
        stage1_prediction: float,
        timestamp_utc: float | None,
    ) -> dict[str, float]:
        """Compute runtime meta-features from V9 features + Stage 1 prediction.

        These mirror the meta-features built by build_meta_features.py at training
        time, using only data available at inference (no future returns).

        v4.0 features (7): oof_pred, oof_pred_zscore_20, atr_percentile_100,
            vol_zscore, hurst_m5, session_sin, session_cos

        v4.1 micro-derived features (3): spread_zscore, oim_divergence,
            toxicity_score — computed only when micro data is available.

        Returns:
            Dict mapping meta-feature names to their computed values.
        """
        meta: dict[str, float] = {}

        # 1. OOF prediction (Stage 1 raw prediction)
        # Apply output_unit conversion if needed (Trap 2 fix)
        if self._output_unit == "atr_multiple":
            current_atr = float(v9_features.get("M5_ATR_14", 1.0))
            meta["oof_pred"] = stage1_prediction * max(current_atr, 1e-6)
        else:
            meta["oof_pred"] = float(stage1_prediction)

        # 2. OOF prediction z-score (rolling 20-bar, cold-start guarded — Trap 1 fix)
        # Clipped to [-5, 5] to prevent explosion when OOF variance is near-zero.
        oof_zscore = 0.0
        if len(self._pred_buffer) >= 2:
            buf = np.array(self._pred_buffer, dtype=np.float64)
            buf_std = float(np.std(buf))
            raw_z = (stage1_prediction - float(np.mean(buf))) / max(buf_std, 1e-6)
            oof_zscore = float(np.clip(raw_z, -5.0, 5.0))
        meta["oof_pred_zscore_20"] = oof_zscore

        # 3. ATR percentile (rolling 100-bar)
        atr_percentile = 0.5  # neutral default
        current_atr = float(v9_features.get("M5_ATR_14", 1.0))
        if len(self._atr_buffer) >= 10:
            sorted_buf = sorted(self._atr_buffer)
            rank = sorted_buf.index(current_atr) if current_atr in sorted_buf else 0
            atr_percentile = rank / max(len(sorted_buf) - 1, 1)
        meta["atr_percentile_100"] = atr_percentile

        # 4. Vol regime z-score (direct from V9 features)
        meta["vol_zscore"] = float(v9_features.get("M5_Vol_ZScore", 0.0))

        # 5. Hurst exponent (direct from V9 features)
        meta["hurst_m5"] = float(v9_features.get("M5_Hurst", 0.5))

        # 6-7. Session sin/cos encoding
        session_sin, session_cos = 0.0, 0.0
        if timestamp_utc is not None and timestamp_utc > 0:
            session_sin, session_cos = _compute_session_features(float(timestamp_utc))
        meta["session_sin"] = session_sin
        meta["session_cos"] = session_cos

        # ── v4.1: Micro-derived meta features (Scaling Toxicity Fix — values already Z-scored) ──

        # 8. Spread z-score: current avg_spread deviation from rolling EWMA
        avg_spread = float(v9_features.get("avg_spread", 0.0))
        spread_zscore = 0.0
        if len(self._micro_spread_buffer) >= 10:
            sbuf = np.array(self._micro_spread_buffer, dtype=np.float64)
            s_std = float(np.std(sbuf))
            if s_std > 1e-8:
                spread_zscore = (avg_spread - float(np.mean(sbuf))) / s_std
        meta["spread_zscore"] = spread_zscore

        # 9. OIM divergence: sign mismatch between OIM and price direction
        oim = float(v9_features.get("OIM", 0.0))
        tick_return = float(v9_features.get("tick_return", 0.0))
        oim_divergence = 0.0
        if abs(oim) > 0.01 and abs(tick_return) > 1e-6:
            oim_dir = 1.0 if oim > 0 else -1.0
            price_dir = 1.0 if tick_return > 0 else -1.0
            oim_divergence = -oim_dir * price_dir  # +1 = aligned, -1 = diverging
        meta["oim_divergence"] = oim_divergence

        # 10. Toxicity score: tick_velocity / ATR (mirrors position_manager toxicity veto)
        tick_velocity = float(v9_features.get("tick_velocity", 0.0))
        toxicity_score = 0.0
        if current_atr > 1e-6 and tick_velocity > 0:
            toxicity_score = tick_velocity / max(current_atr, 1e-6)
            toxicity_score = float(np.clip(toxicity_score / 1000.0, 0.0, 10.0))
        meta["toxicity_score"] = toxicity_score

        return meta

    def _assemble_feature_vector(
        self,
        v9_features: dict[str, float],
        meta_map: dict[str, float],
    ) -> list[float]:
        """Assemble the full 47-feature vector in model-trained order.

        The model's _feature_names list defines the exact column order
        (40 V9 institutional names + 7 meta-feature names). Values not
        found in either dict default to 0.0.
        """
        merged = {**v9_features, **meta_map}
        return [float(merged.get(name, 0.0)) for name in self._feature_names]

    # ── Legacy feature mapping (v3.1, 15 features) ──

    @staticmethod
    def _runtime_feature_map(
        direction: int,
        s1_confidence: float,
        features: dict[str, float],
        atr_percentile: float,
    ) -> dict[str, float]:
        """Build the legacy 15-feature map used by pre-v4.0 meta models."""
        rsi = features.get("m5_rsi", 50.0)
        macd = features.get("m5_macd", 0.0)
        h1_ret = features.get("h1_ret", 0.0)
        h1_macd = features.get("h1_macd", 0.0)
        vol_z = features.get("m5_vol_zscore", 0.0)
        ou = features.get("m5_ou_theta", 0.0)
        hurst = features.get("m5_hurst", 0.5)

        rsi_dist = abs(rsi - 50.0)
        h1_trend = abs(h1_ret) / max(atr_percentile, 0.01)

        return {
            "s1_direction": float(direction),
            "s1_confidence": s1_confidence,
            "m5_rsi": rsi,
            "m5_macd": macd,
            "h1_ret": h1_ret,
            "h1_macd": h1_macd,
            "m5_vol_zscore": vol_z,
            "m5_ou_theta": ou,
            "m5_hurst": hurst,
            "atr_percentile": atr_percentile,
            "rsi_distance": rsi_dist,
            "h1_trend_strength": h1_trend,
            "direction_x_rsi": float(direction) * (rsi - 50.0),
            "direction_x_macd": float(direction) * macd,
            "direction_x_h1": float(direction) * h1_ret,
        }


# ── Helpers ──


def _compute_session_features(timestamp_utc: float) -> tuple[float, float]:
    """Compute sin/cos encoding from a UTC Unix timestamp.

    Forex sessions have distinct volatility/regime characteristics.
    Encoding session time as sin/cos preserves the circular nature of
    the 24h cycle.

    Mirrors build_meta_features._compute_session_features().
    """
    import time as _time_module

    st = _time_module.gmtime(timestamp_utc)
    hours = st.tm_hour + st.tm_min / 60.0
    radians = 2.0 * math.pi * hours / 24.0
    return float(math.sin(radians)), float(math.cos(radians))


# ── Factory ──


def create_meta_filter(
    model_path: str | None = None,
    mlp_model_path: str | None = None,
    threshold: float = 0.30,
    enabled: bool = True,
    mode: str = "binary",
    ensemble_weights: tuple[float, float] | None = None,
    micro_scaler_path: str | None = None,
    calibrator_path: str | None = None,
    conformal_mode: bool = False,
    conformal_window: int = 500,
    conformal_percentile: float = 80.0,
    min_threshold: float = 0.50,
) -> MetaSignalFilter | None:
    """Create and load a MetaSignalFilter, returning None if unavailable.

    When the model can't be loaded, returns None so the live system
    can gracefully disable Stage 2 filtering and pass all signals through.
    """
    filt = MetaSignalFilter(
        model_path=model_path,
        mlp_model_path=mlp_model_path,
        threshold=threshold,
        enabled=enabled,
        mode=mode,
        ensemble_weights=ensemble_weights,
        micro_scaler_path=micro_scaler_path,
        calibrator_path=calibrator_path,
        conformal_mode=conformal_mode,
        conformal_window=conformal_window,
        conformal_percentile=conformal_percentile,
        min_threshold=min_threshold,
    )
    if model_path and enabled:
        loaded = filt.load()
        if not loaded:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "meta_filter_unavailable",
                        "model_path": model_path,
                        "action": "disabled_stage2_all_signals_pass",
                    }
                )
                + "\n"
            )
            return None
    return filt
