"""transfer_adapter.py — freeze-and-residual OFI transfer learner.

M4 Phase 6.1 / IC 终局裁决 (2026-08-03).  Architecture:

    y     = y_A + r
      y_A = frozen 41-dim E[R] base tower (LONG or SHORT) — NEVER re-trained
      r   = LightGBM residual fit on the 5 OFI flow features only (dead dims
            zero-padded so the runtime ``btc_macro_flow_46`` slice stays verbatim)

The base tower's weights are immutable — the OFI residual may only ADD what
the frozen base misses.  This is the difference between transfer learning and
a second opinion: the base is the institutional 41-dim substrate, the residual
is the pure order-flow increment.

Hard gate (fail-closed, mirrors Iron Law #12 — no dead-code accumulation):
    effective_flow_dim >= min_flow_dim  — else TransferDataError.
    A residual learner whose flow inputs are fewer than ``min_flow_dim`` LIVE
    features is a lie (it cannot learn an OFI increment the base does not
    already carry).  The gate refuses with a per-feature DEATH REPORT (coverage
    per feature + why), never a bare exception.

Zero-pad contract:
    Dead flow dims stay as constant-zero COLUMNS (not dropped) so a runtime
    46-dim vector slices to ``X_flow = X_46[:, 41:46]`` verbatim, with no
    re-indexing.  LightGBM never splits on a constant column.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── The 46-dim schema base/flow boundary (btc_macro_flow_46) ──
FLOW_SLICE_START = 41  # flow columns are X_46[:, 41:46]
DEFAULT_MIN_FLOW_DIM = 2  # IC 终局裁决: "MIN_FLOW_SAMPLES >= 2 (或标定 3)"
_DEFAULT_LIVE_COVERAGE = 0.05  # a flow feature is live if >5% bars are non-zero


class TransferDataError(RuntimeError):
    """Hard gate failure — the residual learner is refused, no human waiver."""


def compute_flow_coverage(X_flow: np.ndarray, feature_names: list[str]) -> dict[str, float]:
    """Per-feature non-zero rate of the flow columns (Iron Law #11 evidence)."""
    if X_flow.ndim != 2 or X_flow.shape[1] != len(feature_names):
        raise TransferDataError(
            f"flow matrix shape {X_flow.shape} != feature count {len(feature_names)}"
        )
    return {
        name: float(np.mean(np.abs(X_flow[:, i]) > 1e-12)) for i, name in enumerate(feature_names)
    }


def effective_flow_dim(
    coverage: dict[str, float], min_coverage: float = _DEFAULT_LIVE_COVERAGE
) -> tuple[int, list[str]]:
    """(live_count, live_names) — live = non-zero in > min_coverage of bars."""
    live = [f for f, cov in coverage.items() if cov > min_coverage]
    return len(live), live


def flow_death_report(coverage: dict[str, float]) -> str:
    """Human-readable per-feature coverage + verdict — the auditable gate."""
    lines = []
    for name, cov in coverage.items():
        lines.append(
            f"    {name:26s} {cov * 100:5.1f}% {'LIVE' if cov > _DEFAULT_LIVE_COVERAGE else 'DEAD'}"
        )
    return "\n".join(lines)


class FrozenBaseModel:
    """Immutable 41-dim E[R] base tower — wraps a LightGBM Booster."""

    def __init__(self, booster: Any, base_id: str) -> None:
        self._booster = booster
        self.base_id = base_id

    def predict(self, X_41: np.ndarray) -> np.ndarray:
        if X_41.ndim != 2 or X_41.shape[1] != FLOW_SLICE_START:
            raise TransferDataError(
                f"base model expects 41-dim input, got {X_41.shape} " f"(base_id={self.base_id})"
            )
        pred = np.asarray(self._booster.predict(X_41), dtype=np.float64)
        return pred.ravel()

    @classmethod
    def from_file(cls, path: str | Path, base_id: str) -> FrozenBaseModel:
        import lightgbm as lgb

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"frozen base model not found: {p}")
        booster = lgb.Booster(model_file=str(p))
        return cls(booster, base_id)


class ResidualTransferLearner:
    """Frozen-base + OFI residual — trains r and predicts y = y_A + r."""

    def __init__(
        self,
        base: FrozenBaseModel,
        flow_feature_names: list[str],
        min_flow_dim: int = DEFAULT_MIN_FLOW_DIM,
        residual_path: str | Path | None = None,
    ) -> None:
        self.base = base
        self.flow_feature_names = list(flow_feature_names)
        self.min_flow_dim = min_flow_dim
        self._residual: Any = None
        self._metadata: dict[str, Any] = {}
        if residual_path is not None:
            self.load_residual(residual_path)

    # ── Hard gate ──────────────────────────────────────────────────────────
    def assert_viable(self, X_flow: np.ndarray) -> dict[str, float]:
        """Refuse training when < min_flow_dim flow features are live."""
        coverage = compute_flow_coverage(X_flow, self.flow_feature_names)
        live_dim, live = effective_flow_dim(coverage)
        if live_dim < self.min_flow_dim:
            raise TransferDataError(
                f"effective_flow_dim={live_dim} < min_flow_dim={self.min_flow_dim}: "
                f"refusing to train a residual learner on {live_dim} live flow "
                f"features.  DEATH REPORT:\n{flow_death_report(coverage)}"
            )
        return coverage

    # ── Training ──────────────────────────────────────────────────────────
    def fit(
        self,
        X_46_train: np.ndarray,
        y_train: np.ndarray,
        X_46_val: np.ndarray,
        y_val: np.ndarray,
        sample_weight: np.ndarray | None = None,
        params: dict[str, Any] | None = None,
        seed: int = 42,
        early_stopping_rounds: int = 50,
    ) -> dict[str, Any]:
        """Fit the OFI residual r = y - y_A.  Returns validation metrics."""
        import lightgbm as lgb

        coverage = self.assert_viable(X_46_train[:, FLOW_SLICE_START:])
        self._metadata["flow_coverage"] = coverage
        live_dim, live = effective_flow_dim(coverage)
        self._metadata["effective_flow_dim"] = live_dim
        self._metadata["live_flow_features"] = live

        X_flow_tr = X_46_train[:, FLOW_SLICE_START:]
        X_flow_va = X_46_val[:, FLOW_SLICE_START:]

        y_A_tr = self.base.predict(X_46_train[:, :FLOW_SLICE_START])
        y_A_va = self.base.predict(X_46_val[:, :FLOW_SLICE_START])
        r_train = np.asarray(y_train, dtype=np.float64) - y_A_tr
        r_val = np.asarray(y_val, dtype=np.float64) - y_A_va

        lgb_params = {
            "objective": "huber",
            "alpha": 1.0,
            "num_leaves": 15,
            "max_depth": 4,
            "learning_rate": 0.05,
            "n_estimators": 300,
            "min_data_in_leaf": 30,
            "lambda_l1": 0.1,
            "lambda_l2": 0.5,
            "feature_fraction": 1.0,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "verbosity": -1,
            "seed": seed,
            "num_threads": 4,
        }
        if params:
            lgb_params.update(params)

        dtr = lgb.Dataset(X_flow_tr, label=r_train, weight=sample_weight)
        dva = lgb.Dataset(X_flow_va, label=r_val, reference=dtr)
        booster = lgb.train(
            lgb_params,
            dtr,
            valid_sets=[dva],
            num_boost_round=lgb_params["n_estimators"],
            callbacks=[
                lgb.early_stopping(early_stopping_rounds, verbose=False),
                lgb.log_evaluation(0),
            ],
        )
        self._residual = booster

        # ── Combined evaluation on val: y = y_A + r ──
        pred_va = self.predict(X_46_val)
        from core.training.utils import spearman_rho

        combined_rho = spearman_rho(pred_va, np.asarray(y_val, dtype=np.float64))
        residual_rho = spearman_rho(booster.predict(X_flow_va), r_val)
        self._metadata["val_combined_rho"] = float(combined_rho)
        self._metadata["val_residual_rho"] = float(residual_rho)
        self._metadata["val_mae"] = float(np.mean(np.abs(pred_va - y_val)))
        self._metadata["best_iteration"] = int(booster.best_iteration)
        self._metadata["n_flow_features"] = int(X_flow_tr.shape[1])
        return dict(self._metadata)

    # ── Inference ─────────────────────────────────────────────────────────
    def predict(self, X_46: np.ndarray) -> np.ndarray:
        if self._residual is None:
            raise TransferDataError("residual booster not trained/loaded")
        X_46 = np.asarray(X_46, dtype=np.float64)
        if X_46.ndim == 1:
            X_46 = X_46.reshape(1, -1)
        if X_46.shape[1] != 46:
            raise TransferDataError(f"expected 46-dim input, got {X_46.shape}")
        y_A = self.base.predict(X_46[:, :FLOW_SLICE_START])
        r = np.asarray(self._residual.predict(X_46[:, FLOW_SLICE_START:]), dtype=np.float64)
        return y_A + r.ravel()

    # ── Persistence ───────────────────────────────────────────────────────
    def save_residual(self, path: str | Path) -> Path:
        if self._residual is None:
            raise TransferDataError("nothing to save — residual not trained")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._residual.save_model(str(p))
        meta_path = p.with_suffix(".meta.json")
        meta_path.write_text(
            json.dumps(
                {
                    "base_id": self.base.base_id,
                    "flow_feature_names": self.flow_feature_names,
                    "effective_flow_dim": self._metadata.get("effective_flow_dim"),
                    "val_combined_rho": self._metadata.get("val_combined_rho"),
                    "min_flow_dim": self.min_flow_dim,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return p

    def load_residual(self, path: str | Path) -> None:
        import lightgbm as lgb

        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"residual booster not found: {p}")
        self._residual = lgb.Booster(model_file=str(p))
