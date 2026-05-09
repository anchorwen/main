"""Online learner adapter — incremental model updates from live trade outcomes.

Supports two backends:
  - online_sgd: sklearn.linear_model.SGDClassifier (legacy, linear)
  - online_mlp: PyTorch small MLP with LayerNorm/GELU (v2, non-linear)

Initial weights trained from barrier labels; live trade outcomes drive incremental
weight updates without requiring batch retraining.

brain_type: ``online_sgd``
artifact format: JSON — either legacy SGD format (coef_ + intercept_) or MLP format
  (model_type="online_mlp_v1" with W1/b1/gamma1/beta1/W2/b2/gamma2/beta2/W3/b3)
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

LABEL_CLASSES = np.array([-1, 0, 1], dtype=np.int32)
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
    """Streaming learner adapter with dual-backend support (SGD / MLP).

    Auto-detects artifact format on load:
      - model_type="online_mlp_v1" → PyTorch MLP with LayerNorm/GELU
      - Otherwise → sklearn SGDClassifier (legacy)
    """

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry)
        self._feature_adapter = feature_adapter
        self._coef: np.ndarray | None = None
        self._intercept: np.ndarray | None = None
        self._classes: np.ndarray = LABEL_CLASSES
        self._n_features: int = 40
        self._total_updates: int = 0
        self._recent_updates: list[dict[str, Any]] = []
        self._max_recent: int = 50
        self._mlp = None  # OnlineMLP instance (v2 mode)
        self._use_mlp: bool = False

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
        except Exception:
            self._backend = "online_sgd:zeros"
            self._init_zeros()
            return

        # Detect MLP format
        if data.get("model_type") == "online_mlp_v1":
            self._use_mlp = True
            try:
                from core.brains.online_mlp_model import OnlineMLP

                self._mlp = OnlineMLP()
                self._mlp.load_state_dict(data)
                self._n_features = self._mlp.n_features
                self._total_updates = self._mlp._total_updates
                self._backend = "online_mlp:v1"
                logger.info(
                    "OnlineLearnerAdapter loaded MLP: n_features=%d updates=%d",
                    self._n_features,
                    self._total_updates,
                )
            except Exception:
                self._backend = "online_mlp:zeros"
                self._init_mlp_zeros()
        else:
            self._use_mlp = False
            try:
                self._coef = np.array(data["coef_"], dtype=np.float64)
                self._intercept = np.array(data["intercept_"], dtype=np.float64)
                self._classes = np.array(data.get("classes_", LABEL_CLASSES), dtype=np.int32)
                self._n_features = int(data.get("n_features", self._coef.shape[1]))
                self._total_updates = int(data.get("total_updates", 0))
                self._backend = "online_sgd:json"
                logger.info(
                    "OnlineLearnerAdapter loaded SGD: n_features=%d classes=%s updates=%d",
                    self._n_features,
                    self._classes.tolist(),
                    self._total_updates,
                )
            except Exception:
                self._backend = "online_sgd:zeros"
                self._init_zeros()

    def _init_zeros(self) -> None:
        n_classes = len(LABEL_CLASSES)
        self._coef = np.zeros((n_classes, self._n_features), dtype=np.float64)
        self._intercept = np.zeros(n_classes, dtype=np.float64)

    def _init_mlp_zeros(self) -> None:
        from core.brains.online_mlp_model import OnlineMLP

        self._mlp = OnlineMLP(n_features=self._n_features)

    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        t0 = time.perf_counter()

        if self._use_mlp and self._mlp is not None:
            x = np.asarray(feature_vector, dtype=np.float64)
            probs = self._mlp.forward_numpy(x)
            runtime_ms = (time.perf_counter() - t0) * 1000.0
            return {
                "probs": probs.astype(np.float32),
                "classes": LABEL_CLASSES.copy(),
                "runtime_ms": round(runtime_ms, 4),
                "fallback": self._backend == "online_mlp:zeros",
                "total_updates": self._total_updates,
                "logits": None,
            }

        # Legacy SGD path
        if self._coef is None:
            self._init_zeros()

        x = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
        if x.shape[1] != self._n_features:
            x = x[:, : self._n_features]

        logits = x @ self._coef.T + self._intercept
        logits = logits.ravel()
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

        method = "online_mlp_v1" if self._use_mlp else "online_sgd_logistic_regression"

        return BrainDecisionProposal(
            schema_version=SCHEMA_BRAIN_DECISION_PROPOSAL,
            proposal_id=new_proposal_id(),
            snapshot_id="",
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
            rationale={"method": method, "updates": self._total_updates},
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
        if label not in (-1, 0, 1):
            logger.warning("OnlineLearnerAdapter: invalid label %s, skipping update", label)
            return False

        x = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
        if x.shape[1] != self._n_features:
            x = x[:, : self._n_features]

        if self._use_mlp and self._mlp is not None:
            ok = self._mlp.partial_fit(x.ravel(), label)
            if ok:
                self._total_updates += 1
                self._recent_updates.append(
                    {
                        "label": int(label),
                        "total_updates": self._total_updates,
                        "backend": "mlp",
                    }
                )
                if len(self._recent_updates) > self._max_recent:
                    self._recent_updates = self._recent_updates[-self._max_recent :]
            return ok

        # Legacy SGD path
        if self._coef is None:
            self._init_zeros()

        y = np.array([label], dtype=np.int32)
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
        clf.coef_ = self._coef.copy()
        clf.intercept_ = self._intercept.copy()
        clf.classes_ = self._classes.copy()

        try:
            clf.partial_fit(x, y, classes=self._classes)
        except Exception as e:
            logger.error("OnlineLearnerAdapter: partial_fit failed: %s", e)
            return False

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

        return True

    def save_weights(self, output_path: str | None = None) -> str:
        target = output_path or self._brain_entry.get("artifact_path", "")
        if not target:
            target = "data/models/online_learner_weights.json"
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)

        if self._use_mlp and self._mlp is not None:
            self._mlp.save(target)
        else:
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
                "use_mlp": self._use_mlp,
                "coef_norm": round(float(np.linalg.norm(self._coef)), 4)
                if self._coef is not None
                else 0.0,
                "recent_updates": self._recent_updates[-5:],
            }
        )
        return base
