"""Abstract base for all Brain adapters (ModelArtifactAdapter).

Every model-type-specific adapter MUST implement load() + infer() + get_signal().
BrainFactory routes by brain_type; all adapters output a uniform BrainDecisionProposal.

The `inference()` convenience method chains infer() → get_signal() into a single
call that main.py / BrainRunService can consume without knowing adapter internals.
"""

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal


class BaseBrainAdapter(ABC):
    """Uniform interface for all model formats (ONNX / XGBoost JSON / OU Params / LightGBM / ...).

    Subclasses implement format-specific loading and inference; the factory and
    BrainRunService consume only this interface.
    """

    def __init__(self, brain_entry: dict):
        self._brain_entry = brain_entry
        self._backend = "stub:not_loaded"
        self._feature_dimension: int = 40

    # ── Pluggable-brain metadata (read from brain JSON, no hardcoding) ──

    @property
    def brain_id(self) -> str:
        """Unique brain identifier from the registry entry."""
        return self._brain_entry.get("brain_id", "")

    @property
    def contract_group(self) -> str:
        """Contract group this brain belongs to (e.g. 'barrier_12bar')."""
        return self._brain_entry.get("contract_group", "")

    @property
    def training_horizon(self) -> int:
        """Training horizon in M5 cycles (e.g. 12 for barrier, 3 for micro)."""
        return self._brain_entry.get("training_horizon", 12)

    @property
    def feature_schema(self) -> str:
        """Feature schema identifier (e.g. 'v9_40dim', 'micro_9dim')."""
        return self._brain_entry.get("feature_schema", "")

    # ------------------------------------------------------------------
    # Abstract — concrete adapters must implement
    # ------------------------------------------------------------------

    @abstractmethod
    def load(self) -> None:
        """Load the model artifact from disk and prepare for inference.

        Called once during initialisation.  Must set self._backend
        to a descriptive string (e.g. "xgboost:json").
        """
        ...

    @abstractmethod
    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        """Run raw inference on a 1-D feature vector.

        Returns a dict whose keys are adapter-specific but that downstream
        ``get_signal()`` knows how to consume.  Typical keys:
          * xgboost: {"raw_score": float, "feature_count": int, "runtime_ms": float,
              "fallback": bool}
          * ou_params: {"z_score": float, "theta": float, "mu": float, "half_life": float, ...}
          * onnx: {"logits": np.ndarray, "risk_score": float, "vol_score": float}
        """
        ...

    @abstractmethod
    def get_signal(self, raw_output: dict[str, Any]) -> BrainDecisionProposal:
        """Convert raw inference output into a standard BrainDecisionProposal.

        This is the single point where model-specific logic maps raw numbers
        onto the unified ``{direction_bias, confidence, reason}`` contract consumed by
        ParliamentService, DecisionCompiler, and the ledger.
        """
        ...

    # ------------------------------------------------------------------
    # Convenience — chains infer + get_signal (used by main.py pipeline)
    # ------------------------------------------------------------------

    def inference(self, feature_vector: np.ndarray | None = None) -> BrainDecisionProposal:
        """Run the full inference chain: produce features → infer → get_signal.

        If feature_vector is None, subclasses with internal state (e.g. OU buffer)
        may still produce output by calling infer() with a stub vector.

        Returns a BrainDecisionProposal ready for ParliamentService / DecisionCompiler.
        """
        if feature_vector is None:
            # Subclasses that maintain internal state (e.g. ParamsBrainAdapter
            # with a rolling price buffer) can still produce a signal.
            # Pass a zero-length vector as a signal to infer().
            feature_vector = np.zeros(self._feature_dimension or 40, dtype=np.float64)

        raw_output = self.infer(feature_vector)
        return self.get_signal(raw_output)

    # ------------------------------------------------------------------
    # num_features (for load-time validation) ──────────────────────────

    @property
    def num_features(self) -> int | None:
        """Number of features the model expects, or None if unknown.

        Subclasses should set ``self._num_features`` during ``load()``.
        The BrainConfigValidator uses this to check against schema dimension.
        """
        return getattr(self, "_num_features", None)

    # ------------------------------------------------------------------
    # run — full pipeline (snapshot + feature_source → feature_vector → infer → signal)
    # ------------------------------------------------------------------

    def run(self, snapshot, feature_source: dict | None = None) -> BrainDecisionProposal:
        """Full pipeline: feature_source dict → feature_vector → infer → get_signal.

        Metadata-driven extraction when ``features`` is present in the brain config:
        extracts values in the exact order specified by the config, with missing
        keys defaulting to 0.0.  Falls back to dict-values-to-array when no
        ``features`` field exists.

        Adapters with a feature_adapter (e.g. V9OnnxBrainAdapter) override this
        to use their own normalization pipeline.
        """
        feature_vector: np.ndarray | None = None
        if feature_source is not None and feature_source:
            # Metadata-driven extraction: use brain config's features list
            feature_names = self._brain_entry.get("features")
            if feature_names:
                feature_vector = np.array(
                    [float(feature_source.get(name, 0.0)) for name in feature_names],
                    dtype=np.float64,
                )
            else:
                # Legacy fallback: dict order dependent (fragile)
                feature_vector = np.array(list(feature_source.values()), dtype=np.float64)
        return self.inference(feature_vector)

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Return adapter metadata for diagnostics and governance."""
        return {
            "adapter_class": self.__class__.__name__,
            "brain_type": self._brain_entry.get("brain_type"),
            "brain_id": self.brain_id,
            "contract_group": self.contract_group,
            "training_horizon": self.training_horizon,
            "feature_schema": self.feature_schema,
            "backend": self._backend,
            "artifact_path": self._brain_entry.get("artifact_path"),
        }
