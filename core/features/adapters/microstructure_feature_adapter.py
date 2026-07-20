"""Microstructure feature adapter — dict or sequence → normalised model input.

Handles three input formats:
  1. Single-bar dict (backward-compat) → (1, 9)
  2. Sequence (n_bars, 9) → (1, n_bars, 9) for Transformer
  3. Flattened (n_bars*9,) → (1, 288) for XGBoost

All outputs are StandardScaler-normalised when a scaler is configured.

DQAF-20260622-054: replaced ``joblib.load()`` (broken on JSON scalers) with
``_load_scaler_json()`` that reconstructs an sklearn StandardScaler from the
training pipeline's JSON format.  Added ``require_scaler`` flag for fail-closed
safe-loading — when ``True``, missing scaler raises ``DataIntegrityError``
instead of silently degrading to raw features.

DQAF-20260622-058: de-hardcoded ``resolve_scaler_path()`` BTC fallback (now uses
``symbol[:3]`` shorthand so XAU also resolves).  Added ``generate_cold_start_scaler()``
for instruments without historical feature-store data (identity transform: mean=0,
scale=1).  ``_load_scaler_json()`` now logs the ``_comment`` field so operators can
distinguish empirical vs cold-start scalers at a glance.
"""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sklearn.preprocessing import StandardScaler

from core.contracts.exceptions import DataIntegrityError
from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES

_logger = logging.getLogger(__name__)
DEFAULT_SEQ_LEN = 32


class MicrostructureFeatureAdapter:
    """Extracts and normalises microstructure features for model input.

    When a *scaler_path* is provided, loads a JSON-format StandardScaler
    that was fit on per-bar features during training and reconstructs it
    in memory (DQAF-054: replaces broken ``joblib.load()``).

    Parameters
    ----------
    scaler_path:
        Path to a JSON scaler file with ``mean_`` / ``scale_`` / ``var_`` /
        ``n_features_in_`` / ``feature_names_in_`` keys.
    require_scaler:
        If ``True`` (live mode), missing or unreadable scaler raises
        ``DataIntegrityError``.  If ``False`` (shadow/testing), degrades
        gracefully to raw features with a warning.
    """

    def __init__(
        self,
        scaler_path: str | Path | None = None,
        *,
        require_scaler: bool = False,
    ):
        self._scaler: StandardScaler | None = None
        self._scaler_path = Path(scaler_path) if scaler_path else None
        self._scaler_warned: bool = False
        self._require_scaler = require_scaler

        if self._scaler_path is not None:
            if not self._scaler_path.exists():
                if require_scaler:
                    raise DataIntegrityError(
                        f"CRITICAL SKEW PREVENTED: Scaler missing at "
                        f"{self._scaler_path} under require_scaler=True. "
                        f"PROCESS HALTED.",
                        source="adapter:microstructure:init",
                    )
                _logger.warning(
                    "DEGRADE: Scaler file not found at %s. "
                    "Falling back to RAW features. Monitor for drift!",
                    self._scaler_path,
                )
            else:
                self._scaler = self._load_scaler_json(self._scaler_path)
        elif require_scaler:
            raise DataIntegrityError(
                "CRITICAL SKEW PREVENTED: scaler_path is None under "
                "require_scaler=True. PROCESS HALTED.",
                source="adapter:microstructure:init",
            )

    # ── Scaler loading (DQAF-054) ──────────────────────────────────────

    @staticmethod
    def _load_scaler_json(path: Path) -> StandardScaler:
        """Reconstruct a StandardScaler from a JSON file.

        Training pipelines save scalers as JSON with keys ``mean_``,
        ``scale_``, ``var_``, ``n_features_in_``, ``feature_names_in_``.
        This replaces the broken ``joblib.load()`` path that failed on
        JSON-format scaler files.

        sklearn is imported lazily — this module must be importable in
        environments (CI, shadow fixtures) where sklearn is absent.
        """
        from sklearn.preprocessing import StandardScaler as _StandardScaler

        data = json.loads(path.read_text(encoding="utf-8"))
        scaler = _StandardScaler()
        scaler.mean_ = np.asarray(data["mean_"], dtype=np.float64)
        scaler.scale_ = np.asarray(data["scale_"], dtype=np.float64)
        scaler.var_ = np.asarray(data.get("var_", []), dtype=np.float64)
        scaler.n_features_in_ = int(data.get("n_features_in_", len(data["mean_"])))
        scaler.feature_names_in_ = np.asarray(data.get("feature_names_in_", []), dtype=str)
        _comment = data.get("_comment", "")
        _tag = f" [{_comment}]" if _comment else ""
        _logger.info(
            "MicrostructureFeatureAdapter: scaler loaded from %s (%d features)%s",
            path,
            scaler.n_features_in_,
            _tag,
        )
        return scaler

    # ── Scaler auto-discovery (DQAF-20260622-055) ───────────────────────

    @staticmethod
    def resolve_scaler_path(base_dir: str | Path, symbol: str) -> Path | None:
        """Discover the micro scaler for *symbol* under *base_dir*/models/.

        Lookup order (first match wins):

        1. ``{base_dir}/models/{symbol_lower}_micro_scaler.json``
           e.g. ``data_btc/models/btcusdc_micro_scaler.json``
        2. ``{base_dir}/models/{symbol_shorthand}_micro_scaler.json``
           e.g. ``data_btc/models/btc_micro_scaler.json`` (shorthand fallback)

        The shorthand is the first 3 lowercase characters of *symbol*
        (``btcusdc`` → ``btc``, ``xauusdc`` → ``xau``).  This replaces the
        previous hardcoded ``btc_micro_scaler.json`` fallback that only worked
        for BTC and silently returned ``None`` for XAU (DQAF-058).

        Returns the *Path* if found, ``None`` otherwise.
        """
        base = Path(base_dir)
        symbol_lower = symbol.lower()
        symbol_shorthand = symbol_lower[:3]
        candidates = [
            base / "models" / f"{symbol_lower}_micro_scaler.json",
            base / "models" / f"{symbol_shorthand}_micro_scaler.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    # ── Cold-start scaler generation (DQAF-20260622-058) ──────────────

    @staticmethod
    def generate_cold_start_scaler(output_path: Path) -> Path:
        """Generate an identity scaler for cold-start instruments.

        When an instrument has no historical feature-store data to fit
        an empirical scaler, this produces a mean=0 / scale=1 identity
        transform that satisfies ``require_scaler=True`` without halting
        the process.  It is functionally equivalent to raw features but
        allows the system to start and accumulate data for later re-fit.

        The output JSON includes a ``_comment`` marking it as a cold-start
        scaler so operators can distinguish it from empirically-fitted ones.
        """
        from sklearn.preprocessing import StandardScaler as _StandardScaler

        n_features = len(MICROSTRUCTURE_9_FEATURES)
        scaler = _StandardScaler()
        scaler.mean_ = np.zeros(n_features, dtype=np.float64)
        scaler.scale_ = np.ones(n_features, dtype=np.float64)
        scaler.var_ = np.ones(n_features, dtype=np.float64)
        scaler.n_features_in_ = n_features
        scaler.feature_names_in_ = np.array(MICROSTRUCTURE_9_FEATURES, dtype=str)

        data: dict[str, object] = {
            "mean_": scaler.mean_.tolist(),
            "scale_": scaler.scale_.tolist(),
            "var_": scaler.var_.tolist(),
            "n_features_in_": scaler.n_features_in_,
            "feature_names_in_": scaler.feature_names_in_.tolist(),
            "_comment": (
                "COLD_START_IDENTITY_SCALER — all features mean=0 scale=1. "
                "Re-fit when ≥5000 feature store records accumulate. "
                "Generated by MicrostructureFeatureAdapter.generate_cold_start_scaler()."
            ),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        _logger.info(
            "MicrostructureFeatureAdapter: cold-start identity scaler written to %s (%d features)",
            output_path,
            n_features,
        )
        return output_path

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def is_warmed_up(self) -> bool:
        return True

    @property
    def feature_count(self) -> int:
        return len(MICROSTRUCTURE_9_FEATURES)

    # ── Single-bar (backward-compat) ──────────────────────────────────

    def build_raw_vector(self, feature_source: dict) -> np.ndarray:
        """Extract 9 microstructure features in canonical order from a dict."""
        values = [float(feature_source.get(name, 0.0)) for name in MICROSTRUCTURE_9_FEATURES]
        return np.asarray(values, dtype=np.float32)

    def build_model_input(self, feature_source: dict) -> np.ndarray:
        """Single bar: dict → (1, 9) normalised array."""
        raw = self.build_raw_vector(feature_source)
        return self.normalize(raw).reshape(1, -1)

    # ── Sequence input ───────────────────────────────────────────────

    def build_sequence_input(self, seq: np.ndarray) -> np.ndarray:
        """Sequence (n_bars, 9) → (1, n_bars, 9) normalised.

        Applies per-bar normalization (broadcast over bars).
        """
        if seq.ndim != 2 or seq.shape[1] != 9:
            raise ValueError(f"Expected (n, 9) array, got {seq.shape}")
        normalized = self._normalize_2d(seq)
        return normalized.reshape(1, seq.shape[0], 9).astype(np.float32)

    def build_flat_input(self, seq: np.ndarray) -> np.ndarray:
        """Sequence (n_bars, 9) → (1, n_bars*9) normalised flat.

        Row-major (C-order): bar0_f0...bar0_f8, bar1_f0...bar1_f8, ...
        Matches training data built with sliding_window_view.reshape(-1).
        """
        if seq.ndim != 2 or seq.shape[1] != 9:
            raise ValueError(f"Expected (n, 9) array, got {seq.shape}")
        normalized = self._normalize_2d(seq)
        return normalized.ravel().reshape(1, -1).astype(np.float32)

    # ── Normalization ────────────────────────────────────────────────

    def normalize(self, raw_vector: np.ndarray) -> np.ndarray:
        """Apply StandardScaler to a 1-D vector, or return raw."""
        if self._scaler is not None:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="X does not have valid feature names")
                return np.asarray(
                    self._scaler.transform(raw_vector.reshape(1, -1)).ravel(),
                    dtype=np.float32,
                )
        if not self._scaler_warned:
            _logger.warning(
                "MicrostructureFeatureAdapter: no scaler loaded — returning raw features"
            )
            self._scaler_warned = True
        return np.asarray(raw_vector, dtype=np.float32)

    def _normalize_2d(self, arr: np.ndarray) -> np.ndarray:
        """Apply StandardScaler per-row, or return raw if no scaler."""
        if self._scaler is not None:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="X does not have valid feature names")
                return np.asarray(self._scaler.transform(arr), dtype=np.float32)
        if not self._scaler_warned:
            _logger.warning(
                "MicrostructureFeatureAdapter: no scaler loaded — returning raw features"
            )
            self._scaler_warned = True
        return np.asarray(arr, dtype=np.float32)
