"""Abstract base for all Brain adapters (ModelArtifactAdapter).

Every model-type-specific adapter MUST implement load() + infer() + get_signal().
BrainFactory routes by brain_type; all adapters output a uniform BrainDecisionProposal.

The `inference()` convenience method chains infer() → get_signal() into a single
call that main.py / BrainRunService can consume without knowing adapter internals.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

import numpy as np

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.schemas.trading_contracts import BrainSignal, Direction


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
    def get_signal(self, raw_output: dict[str, Any]) -> BrainSignal:
        """Convert raw inference output into a standard BrainSignal.

        This is the single point where model-specific logic maps raw numbers
        onto the unified ``{direction, confidence, raw_score}`` contract consumed by
        ParliamentService, DecisionCompiler, and the ledger.
        """
        from core.schemas.trading_contracts import BrainSignal

        raw_score = raw_output.get("raw_score", 0.0)
        runtime_ms = raw_output.get("runtime_ms", 0.0)
        fallback = raw_output.get("fallback", False)
        direction: Direction = (
            "long" if raw_score > 0.1 else ("short" if raw_score < -0.1 else "neutral")
        )
        confidence = (
            BaseBrainAdapter._compute_confidence(
                raw_score, self._brain_entry.get("confidence_params")
            )
            if "raw_score" in raw_output
            else 0.5
        )

        # Preserve adapter-specific diagnostics for brain_votes recording.
        _extra = {
            k: v for k, v in raw_output.items() if k not in ("raw_score", "runtime_ms", "fallback")
        }

        return BrainSignal(
            brain_id=self._brain_entry.get("brain_id", ""),
            direction=direction,
            confidence=confidence,
            raw_score=raw_score,
            fallback=fallback,
            runtime_ms=runtime_ms,
            diagnostics=_extra,
            # DQAF-20260731-004: Preserve explicit vote_weight=0.0 (muted/observation-only).
            vote_weight=float(self._brain_entry.get("vote_weight", 1.0)),
        )

    # ------------------------------------------------------------------
    # Convenience — chains infer + get_signal (used by main.py pipeline)
    # ------------------------------------------------------------------

    def inference(self, feature_vector: np.ndarray | None = None) -> BrainSignal:
        """Run the full inference chain: produce features → infer → get_signal.

        If feature_vector is None, subclasses with internal state (e.g. OU buffer)
        may still produce output by calling infer() with a stub vector.

        Returns a BrainSignal ready for ParliamentService / DecisionCompiler.
        """
        if feature_vector is None:
            # Subclasses that maintain internal state (e.g. ParamsBrainAdapter
            # with a rolling price buffer) can still produce a signal.
            # Pass a zero-length vector as a signal to infer().
            dim = getattr(self, "_num_features", None) or self._feature_dimension
            feature_vector = np.zeros(dim, dtype=np.float64)

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

    def run(self, snapshot, feature_source: dict | None = None) -> BrainSignal:
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
                # Legacy fallback: dict order dependent (fragile).
                # FIX-20260612-002: All brain configs should have 'features' populated
                # by BrainFactory at load time. This fallback should never be reached.
                logger.warning(
                    "BaseBrainAdapter: no 'features' in brain_entry for %s — "
                    "falling back to dict.values() positional extraction",
                    self._brain_entry.get("brain_id", "unknown"),
                )
                feature_vector = np.array(list(feature_source.values()), dtype=np.float64)
        return self.inference(feature_vector)

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    # ── Confidence calibration ────────────────────────────────────────────────

    @staticmethod
    def _compute_confidence(
        score: float,
        confidence_params: dict | None = None,
    ) -> float:
        """Compute confidence from a calibrated score using the configured method.

        **Quantile-aware Gaussian decay** (``method == "quantile_gaussian"``):
        Below P95 → linear ramp (model is interpolating inside known distribution).
        Above P95 → Gaussian decay (model is extrapolating — penalise certainty).

        Falls back to ``tanh(abs(score))`` when ``confidence_params`` is None or
        the method is unrecognised (cold-start / backward-compat).

        DQAF-20260704-004 — replaces the classic ``tanh(abs(score))`` anti-pattern
        that assigned *higher* confidence to extreme feature-space extrapolations.
        """
        if confidence_params and confidence_params.get("method") == "quantile_gaussian":
            p95 = float(confidence_params.get("p95", 0.75))
            peak_conf = float(confidence_params.get("peak_conf", 0.64))
            lambda_decay = float(confidence_params.get("lambda_decay", 80.0))

            abs_score = abs(score)
            if abs_score <= p95:
                # Linear ramp: 0 → Peak_Conf  within the known distribution
                return (abs_score / p95) * peak_conf
            else:
                # Gaussian decay: penalise extrapolation
                return peak_conf * float(np.exp(-lambda_decay * (abs_score - p95) ** 2))

        # Fallback: classic tanh (cold-start, backward-compat, non-configured brains)
        return float(np.tanh(abs(score)))

    # ── Shared score-to-direction mapping (avoids duplication across adapters) ──

    @staticmethod
    def _score_to_direction(
        raw_score: float,
        objective: str = "regression",
        threshold: float = 0.1,
        calibration_offset: float = 0.0,
        confidence_params: dict | None = None,
    ) -> tuple[Direction, float, float]:
        """Map model output to (direction_bias, up_prob, down_prob).

        FIX-20260526-030 — Two distinct paths based on model objective.
        FIX-20260630-202 — threshold decoupled from hardcoded ±0.1.
        FIX-20260704-002 — calibration_offset for multiclass prior correction.
        FIX-20260706-015 — binary_directional path with probability deadzone
        for B-path directional classifiers trained without NEUTRAL samples.
        DQAF-20260731-004 — expected_r_long/expected_r_short paths for V4
        Two-Tower Expected R regression.  Single-direction voting with
        excess-based confidence (quantile_gaussian is unsuitable for the
        negatively-skewed E[R] distribution).

        ...

        **Path 4 — Expected R LONG Tower** (expected_r_long):
        - raw_score = E[R_long] ∈ [-1.0, +1.667]
        - E[R] > threshold → LONG (NEVER votes SHORT)
        - Confidence = min(0.90, 0.40 + excess / (2*threshold) * 0.50)
        - Rationale: E[R] distribution is negatively skewed; quantile_gaussian
          fails when p95 is near zero or negative.  Linear ramp from threshold
          is physically meaningful (R-multiples are well-scaled).

        **Path 5 — Expected R SHORT Tower** (expected_r_short):
        - raw_score = E[R_short] ∈ [-1.0, +1.667]
        - E[R] > threshold → SHORT (NEVER votes LONG)
        - Same excess-based confidence as Path 4.

        **Path 3 — Regression / Multiclass** (multi:softprob, multiclass, etc.):
        - raw_score = P(TP-hit | features) ∈ [0, 1]
        - LONG-ONLY quality classifier.  NEVER votes SHORT.
        - P > 0.55 → LONG,  else ABSTAIN (NEUTRAL).

        **Path 2 — Directional binary** (binary_directional):
        - raw_score = P(LONG | features) ∈ [0, 1]
        - Deadzone [0.5−θ, 0.5+θ] restores the NEUTRAL state that was
          stripped during training (binary_mask = y_all != 1).
        - P > 0.5+θ → LONG,  P < 0.5−θ → SHORT,  else NEUTRAL.
        - up/down are direct probabilities — NO confidence scaling.
          Confidence is the caller's responsibility (get_signal).

        **Path 3 — Regression / Multiclass** (multi:softprob, multiclass, etc.):
        - raw_score = signed score (e.g. BPS, or P(LONG)−P(SHORT))
        - calibration_offset applied first: calibrated = raw_score + offset
        - > +θ → LONG,  < −θ → SHORT,  else NEUTRAL.
        - Uses 0.5 ± confidence/2 anchoring so the predicted direction
          always wins the up/down comparison in consensus.
        """

        # ── Path 1: Trade-Quality Classifier (Legacy Binary) ──
        if objective in ("binary_logloss", "binary"):
            # P(TP-hit) for LONG-ONLY strategy. Never votes SHORT.
            if raw_score > 0.55:
                up = float(raw_score)
                down = 1.0 - float(raw_score)
                return "long", up, down
            return "neutral", 0.5, 0.5

        # ── Path 2: Directional Binary Classifier (B-Path / FIX-20260706-015) ──
        if objective == "binary_directional":
            lower = 0.5 - threshold
            upper = 0.5 + threshold

            if raw_score > upper:
                up = float(raw_score)
                down = 1.0 - up
                return "long", up, down
            elif raw_score < lower:
                down = 1.0 - float(raw_score)
                up = 1.0 - down
                return "short", up, down
            else:
                return "neutral", 0.5, 0.5

        # ── Path 4: Expected R LONG Tower (V4 Two-Tower, DQAF-20260731-004) ──
        # raw_score = E[R_long] — expected R-multiple if we go LONG.
        # Positive E[R] → LONG, else NEUTRAL.  NEVER votes SHORT.
        # Confidence derived from excess over threshold (not quantile_gaussian —
        # E[R] distributions are skewed negative and p95-based calibration fails).
        if objective == "expected_r_long":
            if raw_score > threshold:
                excess = raw_score - threshold
                confidence = min(0.90, 0.40 + excess / (2.0 * threshold) * 0.50)
                up = 0.5 + confidence / 2.0
                down = 1.0 - up
                return "long", up, down
            return "neutral", 0.5, 0.5

        # ── Path 5: Expected R SHORT Tower (V4 Two-Tower, DQAF-20260731-004) ──
        # raw_score = E[R_short] — expected R-multiple if we go SHORT.
        # Positive E[R] → SHORT, else NEUTRAL.  NEVER votes LONG.
        if objective == "expected_r_short":
            if raw_score > threshold:
                excess = raw_score - threshold
                confidence = min(0.90, 0.40 + excess / (2.0 * threshold) * 0.50)
                down = 0.5 + confidence / 2.0
                up = 1.0 - down
                return "short", up, down
            return "neutral", 0.5, 0.5

        # ── Path 3: Regression / Multiclass ──
        calibrated = raw_score + calibration_offset
        confidence = BaseBrainAdapter._compute_confidence(calibrated, confidence_params)

        if calibrated > threshold:
            up = 0.5 + confidence / 2.0
            down = 1.0 - up
            return "long", up, down
        elif calibrated < -threshold:
            down = 0.5 + confidence / 2.0
            up = 1.0 - down
            return "short", up, down
        else:
            return "neutral", 0.5, 0.5

    # ── Diagnostics ──────────────────────────────────────────────────────────

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
