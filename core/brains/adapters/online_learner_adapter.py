"""Online SGD learner adapter — incremental model updates from live trade outcomes.

Uses sklearn.linear_model.SGDClassifier with partial_fit for streaming updates.
Initial weights trained from barrier labels; live trade outcomes drive incremental
weight updates without requiring batch retraining.

brain_type: ``online_sgd``
artifact format: JSON weight file (coef_ + intercept_ + classes_)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.brains.schema_versions import SCHEMA_BRAIN_DECISION_PROPOSAL
from core.contracts.domain.brain_decision_proposal import BrainDecisionProposal
from core.contracts.ids import new_proposal_id

logger = logging.getLogger(__name__)

# Label encoding: barrier / trade outcome → class index
LABEL_CLASSES = np.array([-1, 0, 1], dtype=np.int32)  # short/loss, neutral/timeout, long/win

# Label text → int mapping
LABEL_TO_INT: dict[str, int] = {
    "sl_hit_first": -1,
    "loss": -1,
    "short": -1,
    "timeout": 0,
    "breakeven": 0,
    "neutral": 0,
    "tp_hit_first": 1,
    "win": 1,
    "long": 1,
}


class OnlineLearnerAdapter(BaseBrainAdapter):
    """Streaming logistic-regression adapter with partial_fit support.

    Loads initial weights from a JSON artifact (trained via barrier labels)
    and updates them incrementally as live trades close.

    Artifact JSON schema::

        {
          "coef_": [[...], [...], [...]],   // (n_classes, n_features)
          "intercept_": [...],              // (n_classes,)
          "classes_": [-1, 0, 1],
          "n_features": 40,
          "feature_names": ["M5_Ret_1", ...],
          "train_samples": 6813,
          "train_accuracy": 0.62
        }
    """

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry)
        self._feature_adapter = feature_adapter  # reserved for V9 normalization
        self._coef: np.ndarray | None = None
        self._intercept: np.ndarray | None = None
        self._classes: np.ndarray = LABEL_CLASSES
        self._n_features: int = 40
        self._total_updates: int = 0
        self._recent_updates: list[dict[str, Any]] = []  # rolling log of recent partial_fits
        self._max_recent: int = 50

    # ------------------------------------------------------------------
    # BaseBrainAdapter interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        artifact_path = self._brain_entry.get("artifact_path", "")
        if not artifact_path or not Path(artifact_path).exists():
            self._backend = "online_sgd:zeros"
            self._init_zeros()
            logger.warning("OnlineLearnerAdapter: no artifact, starting from zero weights")
            return

        try:
            data = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            self._coef = np.array(data["coef_"], dtype=np.float64)
            self._intercept = np.array(data["intercept_"], dtype=np.float64)
            self._classes = np.array(data.get("classes_", LABEL_CLASSES), dtype=np.int32)
            self._n_features = int(data.get("n_features", self._coef.shape[1]))
            self._total_updates = int(data.get("total_updates", 0))
            self._backend = "online_sgd:json"
            logger.info(
                "OnlineLearnerAdapter loaded: n_features=%d classes=%s updates=%d",
                self._n_features,
                self._classes.tolist(),
                self._total_updates,
            )
        except Exception:
            self._backend = "online_sgd:zeros"
            self._init_zeros()
            logger.warning(
                "OnlineLearnerAdapter: failed to load artifact, starting from zero weights"
            )

    def _init_zeros(self) -> None:
        n_classes = len(LABEL_CLASSES)
        self._coef = np.zeros((n_classes, self._n_features), dtype=np.float64)
        self._intercept = np.zeros(n_classes, dtype=np.float64)

    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        t0 = time.perf_counter()
        if self._coef is None:
            self._init_zeros()

        x = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
        if x.shape[1] != self._n_features:
            x = x[:, : self._n_features]

        # Linear scores: z = x @ W^T + b
        logits = x @ self._coef.T + self._intercept  # (1, n_classes)
        logits = logits.ravel()

        # Softmax probabilities
        logits_stable = logits - np.max(logits)
        probs = np.exp(logits_stable) / np.sum(np.exp(logits_stable))

        runtime_ms = (time.perf_counter() - t0) * 1000.0

        return {
            "logits": logits.astype(np.float32),
            "probs": probs.astype(np.float32),
            "classes": self._classes.copy(),
            "runtime_ms": round(runtime_ms, 4),
            "fallback": self._backend == "online_sgd:zeros",
            "total_updates": self._total_updates,
        }

    def get_signal(self, raw_output: dict[str, Any]) -> BrainDecisionProposal:
        probs = raw_output["probs"]
        classes = raw_output["classes"]
        best_idx = int(np.argmax(probs))
        class_label = int(classes[best_idx])
        confidence = float(probs[best_idx])

        direction_map = {-1: "short", 0: "neutral", 1: "long"}
        direction = direction_map.get(class_label, "neutral")

        up_prob = float(probs[classes.tolist().index(1)] if 1 in classes else 0.0)
        down_prob = float(probs[classes.tolist().index(-1)] if -1 in classes else 0.0)

        return BrainDecisionProposal(
            schema_version=SCHEMA_BRAIN_DECISION_PROPOSAL,
            proposal_id=new_proposal_id(),
            snapshot_id="",  # filled by BrainRunService
            brain_id=self._brain_entry.get("brain_id", "online_sgd"),
            brain_role=self._brain_entry.get("brain_role", "alpha_brain"),
            brain_status=self._brain_entry.get("status", "shadow"),
            model_version=self._backend,
            event_time=datetime.now(UTC).replace(tzinfo=None),
            generated_at=datetime.now(UTC).replace(tzinfo=None),
            prediction={
                "direction_bias": direction,
                "up_probability": up_prob,
                "down_probability": down_prob,
                "confidence": confidence,
                "uncertainty": round(1.0 - confidence, 3),
                "expected_edge_bps": None,
                "expected_hold_seconds": None,
            },
            applicability={
                "regime_tags": self._brain_entry.get("deployment_scope", {}).get("regimes", []),
                "symbol_tags": self._brain_entry.get("deployment_scope", {}).get("symbols", []),
            },
            rationale={"method": "online_sgd_logistic_regression", "updates": self._total_updates},
            health={
                "input_ok": True,
                "fallback_used": raw_output.get("fallback", False),
                "runtime_ms": raw_output.get("runtime_ms", 0.0),
                "risk_score": 0.0,
                "volatility_score": 0.0,
                "backend": self._backend,
            },
        )

    # ------------------------------------------------------------------
    # Online update API
    # ------------------------------------------------------------------

    def partial_fit(self, feature_vector: np.ndarray, label: int) -> bool:
        """Update model weights with a single labeled sample.

        Args:
            feature_vector: 1-D numpy array of 40 V9 institutional features.
            label: -1 (loss/short), 0 (neutral/timeout), or 1 (win/long).

        Returns:
            True if the update was applied, False if skipped (invalid label).
        """
        if label not in (-1, 0, 1):
            logger.warning("OnlineLearnerAdapter: invalid label %s, skipping update", label)
            return False

        if self._coef is None:
            self._init_zeros()

        x = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
        if x.shape[1] != self._n_features:
            x = x[:, : self._n_features]

        y = np.array([label], dtype=np.int32)

        # SGD update with learning rate decay
        # η_t = η_0 / (1 + alpha * t)  where t = total_updates
        from sklearn.linear_model import SGDClassifier

        lr = 0.01 / (1.0 + 0.0001 * self._total_updates) if self._total_updates > 0 else 0.01

        clf = SGDClassifier(
            loss="log_loss",
            penalty="l2",
            alpha=0.0001,
            max_iter=1,
            tol=None,
            learning_rate="constant",
            eta0=lr,
            random_state=42,
        )
        # Inject current weights
        clf.coef_ = self._coef.copy()
        clf.intercept_ = self._intercept.copy()
        clf.classes_ = self._classes.copy()

        try:
            clf.partial_fit(x, y, classes=self._classes)
        except Exception as e:
            logger.error("OnlineLearnerAdapter: partial_fit failed: %s", e)
            return False

        # Pull updated weights back
        self._coef = clf.coef_.copy()
        self._intercept = clf.intercept_.copy()
        self._total_updates += 1

        self._recent_updates.append(
            {
                "label": int(label),
                "lr": round(lr, 6),
                "coef_norm": round(float(np.linalg.norm(self._coef)), 4),
                "total_updates": self._total_updates,
            }
        )
        if len(self._recent_updates) > self._max_recent:
            self._recent_updates = self._recent_updates[-self._max_recent :]

        logger.debug(
            "OnlineLearnerAdapter: partial_fit label=%d lr=%.6f updates=%d",
            label,
            lr,
            self._total_updates,
        )
        return True

    def save_weights(self, output_path: str | None = None) -> str:
        """Persist current weights to JSON artifact file.

        Args:
            output_path: Target path. Defaults to the artifact_path from brain_entry.

        Returns:
            The path the weights were saved to.
        """
        target = output_path or self._brain_entry.get("artifact_path", "")
        if not target:
            target = "data/models/online_learner_weights.json"

        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {
            "coef_": self._coef.tolist() if self._coef is not None else [],
            "intercept_": self._intercept.tolist() if self._intercept is not None else [],
            "classes_": self._classes.tolist(),
            "n_features": self._n_features,
            "total_updates": self._total_updates,
            "backend": self._backend,
        }
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(
            "OnlineLearnerAdapter: weights saved to %s (updates=%d)", target, self._total_updates
        )
        return str(p)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base.update(
            {
                "total_updates": self._total_updates,
                "n_features": self._n_features,
                "coef_norm": round(float(np.linalg.norm(self._coef)), 4)
                if self._coef is not None
                else 0.0,
                "recent_updates": self._recent_updates[-5:],
            }
        )
        return base
