"""Online Adaptive MLP — small PyTorch network for streaming partial_fit learning.

Architecture: Input(40) → Linear(32) → LayerNorm → GELU → Linear(16) → LayerNorm
→ GELU → Linear(3) softmax.  ~2,115 parameters (vs 123 for linear SGD).

Designed for single-sample SGD updates from live trade outcomes.  The small
parameter count ensures stable online learning without catastrophic forgetting,
while the non-linear layers capture feature interactions the linear model misses.

Replaces sklearn.linear_model.SGDClassifier while maintaining the same API:
  - partial_fit(x, y) → bool
  - predict_proba(x) → np.ndarray
  - save(path) / load(path)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


class OnlineMLP:
    """Small 3-class MLP with LayerNorm for stable online learning.

    Weights are stored as numpy arrays so the adapter can inspect / serialize
    them without needing torch at inference time.  Training (partial_fit) does
    require torch, but inference can optionally use a pure-numpy forward pass
    for zero-dependency deployment.
    """

    def __init__(self, n_features: int = 40, n_classes: int = 3, seed: int = 42):
        self.n_features = n_features
        self.n_classes = n_classes
        self.seed = seed
        self._total_updates: int = 0

        # He initialization
        rng = np.random.RandomState(seed)
        self.W1: np.ndarray = rng.randn(n_features, 32).astype(np.float64) * np.sqrt(
            2.0 / n_features
        )
        self.b1: np.ndarray = np.zeros(32, dtype=np.float64)
        self.gamma1: np.ndarray = np.ones(32, dtype=np.float64)
        self.beta1: np.ndarray = np.zeros(32, dtype=np.float64)

        self.W2: np.ndarray = rng.randn(32, 16).astype(np.float64) * np.sqrt(2.0 / 32)
        self.b2: np.ndarray = np.zeros(16, dtype=np.float64)
        self.gamma2: np.ndarray = np.ones(16, dtype=np.float64)
        self.beta2: np.ndarray = np.zeros(16, dtype=np.float64)

        self.W3: np.ndarray = rng.randn(16, n_classes).astype(np.float64) * np.sqrt(2.0 / 16)
        self.b3: np.ndarray = np.zeros(n_classes, dtype=np.float64)

    # ------------------------------------------------------------------
    # Numpy forward pass (zero-dependency inference)
    # ------------------------------------------------------------------

    def _layer_norm(
        self, x: np.ndarray, gamma: np.ndarray, beta: np.ndarray, eps: float = 1e-5
    ) -> np.ndarray:
        mean = x.mean(axis=-1, keepdims=True)
        var = x.var(axis=-1, keepdims=True)
        return gamma * (x - mean) / np.sqrt(var + eps) + beta

    def _gelu(self, x: np.ndarray) -> np.ndarray:
        return 0.5 * x * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (x + 0.044715 * x**3)))

    def forward_numpy(self, x: np.ndarray) -> np.ndarray:
        """Pure-numpy forward pass returning class probabilities.

        Args:
            x: 1-D feature vector (n_features,) or 2-D (batch, n_features).

        Returns:
            Probability array (n_classes,) or (batch, n_classes).
        """
        if x.ndim == 1:
            x = x.reshape(1, -1)
        h = x @ self.W1 + self.b1
        h = self._layer_norm(h, self.gamma1, self.beta1)
        h = self._gelu(h)
        h = h @ self.W2 + self.b2
        h = self._layer_norm(h, self.gamma2, self.beta2)
        h = self._gelu(h)
        logits = h @ self.W3 + self.b3
        # Softmax
        logits_stable = logits - logits.max(axis=-1, keepdims=True)
        exp_logits = np.exp(logits_stable)
        probs = exp_logits / exp_logits.sum(axis=-1, keepdims=True)
        if probs.shape[0] == 1:
            return probs[0]
        return probs

    # ------------------------------------------------------------------
    # Torch-based partial_fit (single-sample SGD)
    # ------------------------------------------------------------------

    def _to_torch(self):
        import torch

        model: Any = _TorchOnlineMLP(self.n_features, self.n_classes)
        with torch.no_grad():
            model.W1.copy_(torch.from_numpy(self.W1))
            model.b1.copy_(torch.from_numpy(self.b1))
            model.gamma1.copy_(torch.from_numpy(self.gamma1))
            model.beta1.copy_(torch.from_numpy(self.beta1))
            model.W2.copy_(torch.from_numpy(self.W2))
            model.b2.copy_(torch.from_numpy(self.b2))
            model.gamma2.copy_(torch.from_numpy(self.gamma2))
            model.beta2.copy_(torch.from_numpy(self.beta2))
            model.W3.copy_(torch.from_numpy(self.W3))
            model.b3.copy_(torch.from_numpy(self.b3))
        return model

    def _from_torch(self, model) -> None:
        import torch

        with torch.no_grad():
            self.W1 = model.W1.detach().cpu().numpy()
            self.b1 = model.b1.detach().cpu().numpy()
            self.gamma1 = model.gamma1.detach().cpu().numpy()
            self.beta1 = model.beta1.detach().cpu().numpy()
            self.W2 = model.W2.detach().cpu().numpy()
            self.b2 = model.b2.detach().cpu().numpy()
            self.gamma2 = model.gamma2.detach().cpu().numpy()
            self.beta2 = model.beta2.detach().cpu().numpy()
            self.W3 = model.W3.detach().cpu().numpy()
            self.b3 = model.b3.detach().cpu().numpy()

    def partial_fit(self, x: np.ndarray, y: int | np.ndarray) -> bool:
        """Single-sample SGD update.

        Args:
            x: 1-D feature vector (n_features,) or (1, n_features).
            y: class label (-1, 0, or 1).

        Returns:
            True if update was applied.
        """
        try:
            import torch
        except ImportError:
            return False

        model = self._to_torch()
        model.train()

        x_t = torch.from_numpy(np.asarray(x, dtype=np.float32).reshape(1, -1))
        y_t = torch.tensor([int(y)], dtype=torch.long)
        # Map -1 → 0, 0 → 1, 1 → 2
        y_t = torch.where(
            y_t == -1, torch.tensor(0), torch.where(y_t == 1, torch.tensor(2), torch.tensor(1))
        )

        lr = 0.001 / (1.0 + 0.0001 * self._total_updates) if self._total_updates > 0 else 0.001
        optimizer = torch.optim.SGD(model.parameters(), lr=lr, momentum=0.9)

        logits = model(x_t)
        loss = torch.nn.functional.cross_entropy(logits, y_t)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()

        self._from_torch(model)
        self._total_updates += 1
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def state_dict(self) -> dict[str, Any]:
        return {
            "n_features": self.n_features,
            "n_classes": self.n_classes,
            "total_updates": self._total_updates,
            "W1": self.W1.tolist(),
            "b1": self.b1.tolist(),
            "gamma1": self.gamma1.tolist(),
            "beta1": self.beta1.tolist(),
            "W2": self.W2.tolist(),
            "b2": self.b2.tolist(),
            "gamma2": self.gamma2.tolist(),
            "beta2": self.beta2.tolist(),
            "W3": self.W3.tolist(),
            "b3": self.b3.tolist(),
        }

    def load_state_dict(self, d: dict[str, Any]) -> None:
        self.n_features = int(d["n_features"])
        self.n_classes = int(d["n_classes"])
        self._total_updates = int(d.get("total_updates", 0))
        self.W1 = np.array(d["W1"], dtype=np.float64)
        self.b1 = np.array(d["b1"], dtype=np.float64)
        self.gamma1 = np.array(d["gamma1"], dtype=np.float64)
        self.beta1 = np.array(d["beta1"], dtype=np.float64)
        self.W2 = np.array(d["W2"], dtype=np.float64)
        self.b2 = np.array(d["b2"], dtype=np.float64)
        self.gamma2 = np.array(d["gamma2"], dtype=np.float64)
        self.beta2 = np.array(d["beta2"], dtype=np.float64)
        self.W3 = np.array(d["W3"], dtype=np.float64)
        self.b3 = np.array(d["b3"], dtype=np.float64)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = self.state_dict()
        data["model_type"] = "online_mlp_v1"
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> OnlineMLP:
        import json

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        model = cls(n_features=data.get("n_features", 40), n_classes=data.get("n_classes", 3))
        model.load_state_dict(data)
        return model


# ═══════════════════════════════════════════════════════════════════════
# Torch module (lazy import — only needed for training)
# ═══════════════════════════════════════════════════════════════════════


class _TorchOnlineMLP:
    """PyTorch nn.Module equivalent of OnlineMLP. Kept as a plain class
    with explicit parameters so we can easily copy weights to/from numpy."""

    def __new__(cls, n_features: int, n_classes: int):
        import torch
        import torch.nn as nn

        class _Module(nn.Module):
            def __init__(self):
                super().__init__()
                self.W1 = nn.Parameter(torch.empty(n_features, 32))
                self.b1 = nn.Parameter(torch.zeros(32))
                self.gamma1 = nn.Parameter(torch.ones(32))
                self.beta1 = nn.Parameter(torch.zeros(32))

                self.W2 = nn.Parameter(torch.empty(32, 16))
                self.b2 = nn.Parameter(torch.zeros(16))
                self.gamma2 = nn.Parameter(torch.ones(16))
                self.beta2 = nn.Parameter(torch.zeros(16))

                self.W3 = nn.Parameter(torch.empty(16, n_classes))
                self.b3 = nn.Parameter(torch.zeros(n_classes))
                self._init_weights()

            def _init_weights(self):
                nn.init.kaiming_uniform_(self.W1, a=np.sqrt(5))
                nn.init.kaiming_uniform_(self.W2, a=np.sqrt(5))
                nn.init.kaiming_uniform_(self.W3, a=np.sqrt(5))

            def forward(self, x):
                h = x @ self.W1 + self.b1
                h = nn.functional.layer_norm(h, [32], self.gamma1, self.beta1)
                h = nn.functional.gelu(h)
                h = h @ self.W2 + self.b2
                h = nn.functional.layer_norm(h, [16], self.gamma2, self.beta2)
                h = nn.functional.gelu(h)
                return h @ self.W3 + self.b3

        return _Module()
