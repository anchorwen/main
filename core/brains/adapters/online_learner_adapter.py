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
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.schemas.trading_contracts import BrainSignal

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.deployment.brain_alert import emit_brain_alert

logger = logging.getLogger(__name__)

# ── Drift protection constants ──
_MAX_WEIGHT_DELTA = 0.30  # max relative Frobenius norm change per update
_DRIFT_WINDOW = 20  # recent deltas to track
_SNAPSHOT_INTERVAL = 10  # save snapshot every N updates
_MAX_DRIFT_EVENTS = 3  # freeze after this many drift events
_RECENT_SAMPLES = 30  # validation samples to hold for loss check

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
        self._coef: Any = None  # np.ndarray | None (set by _init_zeros / load)
        self._intercept: Any = None  # np.ndarray | None
        self._classes: np.ndarray = LABEL_CLASSES
        self._n_features: int = 40
        self._total_updates: int = 0
        self._recent_updates: list[dict[str, Any]] = []
        self._max_recent: int = 50
        self._mlp: Any = None  # OnlineMLP instance (v2 mode)
        self._use_mlp: bool = False

        # Adam optimizer state (for SGD path learning-rate scheduling)
        self._adam_m: float = 0.0  # first moment (gradient norm)
        self._adam_v: float = 0.0  # second moment (gradient norm²)
        self._adam_beta1: float = 0.9
        self._adam_beta2: float = 0.999
        self._adam_eps: float = 1e-8
        self._adam_alpha: float = 0.001  # base learning rate
        self._adam_t: int = 0  # step counter

        # ── Drift protection ──
        self._drift_weight_deltas: deque[float] = deque(maxlen=_DRIFT_WINDOW)
        self._drift_snapshot: dict[str, np.ndarray] | None = None
        self._drift_snapshot_at: int = 0
        self._drift_event_count: int = 0
        self._drift_frozen: bool = False
        self._recent_samples: deque[tuple[np.ndarray, int]] = deque(maxlen=_RECENT_SAMPLES)
        self._recent_losses: deque[float] = deque(maxlen=_RECENT_SAMPLES)

    # ------------------------------------------------------------------
    # BaseBrainAdapter interface
    # ------------------------------------------------------------------

    def load(self) -> None:
        artifact_path = self._brain_entry.get("artifact_path", "")
        brain_id = self._brain_entry.get("brain_id", "unknown")
        if not artifact_path or not Path(artifact_path).exists():
            self._backend = "online_sgd:zeros"
            self._init_zeros()
            logger.warning("OnlineLearnerAdapter: no artifact, starting from zero weights")
            emit_brain_alert(
                brain_id,
                "model_load_failed",
                {"reason": "artifact not found", "artifact": artifact_path},
            )
            return

        try:
            data = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
        except Exception:
            self._backend = "online_sgd:zeros"
            self._init_zeros()
            emit_brain_alert(
                brain_id,
                "model_load_failed",
                {"reason": "json parse failed", "artifact": artifact_path},
            )
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
                emit_brain_alert(
                    brain_id, "model_load_failed", {"reason": "MLP state dict load failed"}
                )
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
                emit_brain_alert(
                    brain_id, "model_load_failed", {"reason": "SGD weights load failed"}
                )

    def _init_zeros(self) -> None:
        n_classes = len(LABEL_CLASSES)
        self._coef = np.zeros((n_classes, self._n_features), dtype=np.float64)
        self._intercept = np.zeros(n_classes, dtype=np.float64)

    def _init_mlp_zeros(self) -> None:
        from core.brains.online_mlp_model import OnlineMLP

        self._mlp = OnlineMLP(n_features=self._n_features)

    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        t0 = time.perf_counter()

        # ── Zero-vector guard — catches silent FeatureService fallback ──
        vec_arr = np.asarray(feature_vector, dtype=np.float64)
        if np.max(np.abs(vec_arr)) < 1e-10:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "zero_feature_vector",
                {"feature_count": len(feature_vector)},
            )
            return {
                "probs": np.array([0.5, 0.5, 0.5], dtype=np.float32),
                "classes": LABEL_CLASSES.copy(),
                "runtime_ms": 0.0,
                "fallback": True,
                "fallback_reason": "zero_feature_vector",
                "total_updates": self._total_updates,
                "logits": None,
            }

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
        assert self._coef is not None  # _init_zeros() guarantees this

        x = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
        if x.shape[1] != self._n_features:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "feature_dimension_mismatch",
                {
                    "expected": self._n_features,
                    "got": x.shape[1],
                    "action": "truncating",
                },
            )
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

    def get_signal(self, raw_output: dict[str, Any]) -> BrainSignal:
        from core.schemas.trading_contracts import BrainSignal, Direction

        probs = raw_output["probs"]
        classes = raw_output["classes"]
        best_idx = int(np.argmax(probs))
        class_label = int(classes[best_idx])
        confidence = float(probs[best_idx])

        direction: Direction
        if class_label == 1:
            direction = "long"
        elif class_label == -1:
            direction = "short"
        else:
            direction = "neutral"

        runtime_ms = raw_output.get("runtime_ms", 0.0)
        fallback = raw_output.get("fallback", self._backend == "online_sgd:zeros")

        return BrainSignal(
            brain_id=self._brain_entry.get("brain_id", "online_sgd"),
            direction=direction,
            confidence=confidence,
            raw_score=float(probs[best_idx]),
            fallback=fallback,
            runtime_ms=runtime_ms,
            diagnostics={
                k: v for k, v in raw_output.items() if k not in ("runtime_ms", "fallback")
            },
        )

    # ------------------------------------------------------------------
    # Online update API
    # ------------------------------------------------------------------

    def partial_fit(self, feature_vector: np.ndarray, label: int) -> bool:
        """Single-sample update with drift protection.

        - Rejects updates when frozen (too many drift events).
        - Tracks per-update weight delta (|ΔW|/|W|) for drift detection.
        - Takes weight snapshots every _SNAPSHOT_INTERVAL updates.
        - On drift detection: rolls back to last snapshot + increments counter.
        - After _MAX_DRIFT_EVENTS drift events: freezes permanently.
        """
        if self._drift_frozen:
            return False

        if label not in (-1, 0, 1):
            logger.warning("OnlineLearnerAdapter: invalid label %s, skipping update", label)
            return False

        x = np.asarray(feature_vector, dtype=np.float64).reshape(1, -1)
        if x.shape[1] != self._n_features:
            x = x[:, : self._n_features]

        # ── Store pre-update weights for delta tracking ──
        if self._use_mlp and self._mlp is not None:
            before = self._mlp_weights_snapshot()
            ok = self._mlp.partial_fit(x.ravel(), label)
            if not ok:
                return False
            self._total_updates += 1
            weight_delta = self._compute_mlp_weight_delta(before)

        else:
            # Legacy SGD path
            if self._coef is None:
                self._init_zeros()
            assert self._coef is not None
            before_coef = self._coef.copy()
            before_intercept = self._intercept.copy()

            ok = self._sgd_step(x, label)
            if not ok:
                return False
            self._total_updates += 1

            coef_d = self._coef - before_coef
            int_d = self._intercept - before_intercept
            w_norm = float(np.linalg.norm(before_coef))
            d_norm = float(np.sqrt(np.sum(coef_d**2) + np.sum(int_d**2)))
            weight_delta = d_norm / max(w_norm, 1e-8)

        # ── Track weight delta ──
        self._drift_weight_deltas.append(weight_delta)

        # ── Track recent samples for validation ──
        self._recent_samples.append((x.ravel().copy(), int(label)))

        # ── Compute recent loss for drift validation ──
        self._recent_losses.append(self._compute_sample_loss(x.ravel(), int(label)))

        # ── Periodic snapshot ──
        if self._total_updates % _SNAPSHOT_INTERVAL == 0:
            self._drift_snapshot = self._take_weight_snapshot()
            self._drift_snapshot_at = self._total_updates

        # ── Drift check ──
        self._check_drift()

        self._recent_updates.append(
            {
                "label": int(label),
                "total_updates": self._total_updates,
                "backend": "mlp" if self._use_mlp else "sgd",
                "weight_delta": round(weight_delta, 6),
                "drift_frozen": self._drift_frozen,
                "drift_events": self._drift_event_count,
            }
        )
        if len(self._recent_updates) > self._max_recent:
            self._recent_updates = self._recent_updates[-self._max_recent :]

        return True

    # ------------------------------------------------------------------
    # Drift protection helpers
    # ------------------------------------------------------------------

    def _mlp_weights_snapshot(self) -> dict[str, np.ndarray]:
        """Return a shallow copy of all MLP weight arrays."""
        return {
            "W1": self._mlp.W1.copy(),
            "b1": self._mlp.b1.copy(),
            "gamma1": self._mlp.gamma1.copy(),
            "beta1": self._mlp.beta1.copy(),
            "W2": self._mlp.W2.copy(),
            "b2": self._mlp.b2.copy(),
            "gamma2": self._mlp.gamma2.copy(),
            "beta2": self._mlp.beta2.copy(),
            "W3": self._mlp.W3.copy(),
            "b3": self._mlp.b3.copy(),
        }

    def _compute_mlp_weight_delta(self, before: dict[str, np.ndarray]) -> float:
        """Compute relative Frobenius-norm change across all MLP weights."""
        total_norm = 0.0
        total_delta = 0.0
        for key in before:
            old = before[key]
            new = getattr(self._mlp, key)
            total_norm += float(np.sum(old**2))
            total_delta += float(np.sum((new - old) ** 2))
        return np.sqrt(total_delta) / max(np.sqrt(total_norm), 1e-8)

    def _sgd_step(self, x: np.ndarray, label: int) -> bool:
        """Single SGDClassifier step (extracted from partial_fit for clarity)."""
        y = np.array([label], dtype=np.int32)
        from sklearn.linear_model import SGDClassifier

        assert self._coef is not None  # caller ensures this
        self._adam_t += 1
        if self._adam_t == 1:
            lr = self._adam_alpha
        else:
            m_hat = self._adam_m / (1.0 - self._adam_beta1**self._adam_t)
            v_hat = self._adam_v / (1.0 - self._adam_beta2**self._adam_t)
            lr = self._adam_alpha * m_hat / (np.sqrt(v_hat) + self._adam_eps)
            lr = float(max(1e-6, min(0.1, lr)))

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
            logger.error("OnlineLearnerAdapter: SGD step failed: %s", e)
            return False

        self._coef = clf.coef_.copy()
        self._intercept = clf.intercept_.copy()
        return True

    def _take_weight_snapshot(self) -> dict[str, np.ndarray]:
        """Deep-copy current weights for rollback."""
        if self._use_mlp and self._mlp is not None:
            return self._mlp_weights_snapshot()
        return {
            "coef_": self._coef.copy()
            if self._coef is not None
            else np.zeros((3, self._n_features)),
            "intercept_": self._intercept.copy() if self._intercept is not None else np.zeros(3),
        }

    def _rollback_weights(self, snapshot: dict[str, np.ndarray]) -> None:
        """Restore weights from a snapshot."""
        if self._use_mlp and self._mlp is not None:
            for key in ("W1", "b1", "gamma1", "beta1", "W2", "b2", "gamma2", "beta2", "W3", "b3"):
                if key in snapshot:
                    setattr(self._mlp, key, snapshot[key].copy())
        else:
            if "coef_" in snapshot:
                self._coef = snapshot["coef_"].copy()
            if "intercept_" in snapshot:
                self._intercept = snapshot["intercept_"].copy()

    def _compute_sample_loss(self, x: np.ndarray, label: int) -> float:
        """Cross-entropy loss for a single sample."""
        probs = self.forward_numpy(x) if self._use_mlp else self._sgd_probs(x)
        # Map label to class index: -1→0, 0→1, 1→2
        idx = {-1: 0, 0: 1, 1: 2}[label]
        p = max(probs[idx], 1e-12)
        return float(-np.log(p))

    def forward_numpy(self, x: np.ndarray) -> np.ndarray:
        """Forward pass for drift validation (returns probs)."""
        if self._use_mlp and self._mlp is not None:
            return self._mlp.forward_numpy(x)
        return self._sgd_probs(x)

    def _sgd_probs(self, x: np.ndarray) -> np.ndarray:
        """Softmax probs from SGD weights."""
        if self._coef is None:
            return np.array([0.33, 0.34, 0.33])
        logits = x @ self._coef.T + self._intercept
        logits = logits - np.max(logits)
        exp = np.exp(logits)
        return exp / np.sum(exp)

    def _check_drift(self) -> None:
        """Evaluate drift condition and roll back if needed.

        Drift is confirmed when BOTH:
        1. The max recent weight delta exceeds _MAX_WEIGHT_DELTA, AND
        2. Recent losses have degraded (current median > baseline median * 1.5)
        """
        if len(self._drift_weight_deltas) < 3:
            return  # not enough data

        max_delta = max(self._drift_weight_deltas)
        if max_delta <= _MAX_WEIGHT_DELTA:
            return  # weight changes within normal range

        # Check loss degradation on recent samples
        if len(self._recent_losses) < 10:
            return  # not enough validation data

        losses = list(self._recent_losses)
        mid = len(losses) // 2
        baseline_median = float(np.median(losses[:mid]))
        recent_median = float(np.median(losses[mid:]))

        if recent_median <= baseline_median * 1.5:
            return  # loss hasn't degraded significantly

        # ── Drift confirmed — roll back ──
        self._drift_event_count += 1
        logger.warning(
            "OnlineLearnerAdapter DRIFT #%d: max_delta=%.4f baseline_loss=%.4f recent_loss=%.4f",
            self._drift_event_count,
            max_delta,
            baseline_median,
            recent_median,
        )

        if self._drift_snapshot is not None:
            self._rollback_weights(self._drift_snapshot)
            self._total_updates = self._drift_snapshot_at
            logger.info(
                "OnlineLearnerAdapter: rolled back to snapshot at update %d",
                self._drift_snapshot_at,
            )

        # Clear drift state after rollback
        self._drift_weight_deltas.clear()
        self._recent_losses.clear()

        if self._drift_event_count >= _MAX_DRIFT_EVENTS:
            self._drift_frozen = True
            logger.error(
                "OnlineLearnerAdapter: FROZEN after %d drift events — "
                "no further updates accepted",
                self._drift_event_count,
            )

    def save_weights(self, output_path: str | None = None) -> str:
        target = output_path or self._brain_entry.get("artifact_path", "")
        if not target:
            target = "data/models/online_learner_weights.json"
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)

        if self._use_mlp and self._mlp is not None:
            mlp_data = self._mlp.state_dict()
            mlp_data["model_type"] = "online_mlp_v1"
            # Embed drift state
            mlp_data["_drift_event_count"] = self._drift_event_count
            mlp_data["_drift_frozen"] = self._drift_frozen
            p.write_text(json.dumps(mlp_data, indent=2), encoding="utf-8")
        else:
            data: dict[str, Any] = {
                "coef_": self._coef.tolist() if self._coef is not None else [],
                "intercept_": self._intercept.tolist() if self._intercept is not None else [],
                "classes_": self._classes.tolist(),
                "n_features": self._n_features,
                "total_updates": self._total_updates,
                "backend": self._backend,
                "_drift_event_count": self._drift_event_count,
                "_drift_frozen": self._drift_frozen,
            }
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        logger.info(
            "OnlineLearnerAdapter: weights saved to %s (updates=%d frozen=%s)",
            target,
            self._total_updates,
            self._drift_frozen,
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
                "drift": {
                    "frozen": self._drift_frozen,
                    "event_count": self._drift_event_count,
                    "max_recent_delta": round(max(self._drift_weight_deltas), 6)
                    if self._drift_weight_deltas
                    else None,
                    "snapshot_age": self._total_updates - self._drift_snapshot_at
                    if self._drift_snapshot
                    else None,
                    "recent_loss_median": round(float(np.median(list(self._recent_losses))), 4)
                    if self._recent_losses
                    else None,
                },
            }
        )
        return base
