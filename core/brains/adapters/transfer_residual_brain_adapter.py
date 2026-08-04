"""TransferResidualBrainAdapter — freeze-and-residual runtime evaluator (T30②).

M4 Phase 6.2 / FIX-20260804-005.  Runtime twin of
``core/training/transfer_adapter.py::ResidualTransferLearner``:

    y     = y_A + r
      y_A = frozen 41-dim E[R] base tower (LightGBM, NEVER re-trained)
      r   = LightGBM residual on the 5 OFI flow features (dead dims zero-padded)

IC architectural ruling (2026-08-04) splits concerns across metadata:
  * ``brain_type`` keeps its SIGNAL semantics (``expected_r_short`` → Path 5,
    SHORT-only voting).  No new brain_type is invented.
  * the ``transfer`` block describes the PHYSICAL structure — ``kind ==
    freeze_and_residual`` means base+residual composition.  BrainFactory reads
    the transfer block and instantiates this adapter; nothing else changes.

Bit-identical contract: load()/infer() reproduce ``ResidualTransferLearner``
slice-for-slice.  The base slice is the base booster's own ``num_feature()``
(41 for the btc_expected_r_v5_m15 towers) and the residual slice is the
remainder of the 46-dim ``btc_macro_flow_46`` vector.  This is the anti-
train-serve-fork immunity: ``test_transfer_residual_adapter.py`` asserts the
two code paths emit identical raw scores on the same boosters and the same
46-dim input.

Fail-closed: any load error or slice/dimension mismatch degrades to the same
stub fallback as LightGBMBrainAdapter (brain excluded from inference + alert).
"""

from __future__ import annotations

from time import perf_counter
from typing import TYPE_CHECKING, Any

import numpy as np

from core.brains.adapters.lightgbm_brain_adapter import LightGBMBrainAdapter
from core.deployment.brain_alert import emit_brain_alert
from core.features.schemas.registry import SCHEMA_DIMENSIONS

if TYPE_CHECKING:
    pass


class TransferResidualBrainAdapter(LightGBMBrainAdapter):
    """Base+residual composition: y = y_A(frozen base) + r(OFI residual).

    Extends LightGBMBrainAdapter — inherits the metadata-driven 3-defense-line
    ``run()``, ``get_signal()`` (objective ``expected_r_short`` → Path 5
    SHORT-only) and ``inference()`` chain.  Only ``load()`` / ``infer()`` are
    overridden to compose two boosters instead of one.
    """

    def __init__(self, brain_entry: dict, feature_adapter=None):
        super().__init__(brain_entry, feature_adapter)
        self._base_booster = None

    # ------------------------------------------------------------------
    # load — two boosters: frozen base (transfer.frozen_base_artifact_path)
    #        + residual (artifact_path).  Sets _num_features = base+residual.
    # ------------------------------------------------------------------

    def load(self) -> None:
        transfer = self._brain_entry.get("transfer") or {}
        base_path = transfer.get("frozen_base_artifact_path")
        residual_path = self._brain_entry.get("artifact_path")
        if not base_path or not residual_path:
            self._backend = "stub:no_artifact_path"
            return

        try:
            import lightgbm as lgb

            self._lgb = lgb
            # FIX-20260713-006: model_str= parse — LightGBM 4.6.0 C-library
            # file-parser bug (loses tree sync on large .txt).  model_str=
            # parses from memory and is the only robust path for both towers.
            with open(residual_path, encoding="utf-8") as _fh:
                residual = lgb.Booster(model_str=_fh.read())
            with open(base_path, encoding="utf-8") as _fh:
                base = lgb.Booster(model_str=_fh.read())

            base_n = base.num_feature()
            res_n = residual.num_feature()
            total_n = base_n + res_n
            # Hard gate: the two boosters must tile the schema dimension exactly
            # (46 for btc_macro_flow_46).  Any mismatch = fail-closed stub.
            schema_id = self._brain_entry.get("feature_schema_id", "")
            expected = SCHEMA_DIMENSIONS.get(schema_id)
            if expected is not None and total_n != expected:
                raise ValueError(
                    f"base({base_n}) + residual({res_n}) = {total_n} != "
                    f"schema {schema_id} expected {expected}"
                )

            self._base_booster = base
            self._booster = residual
            self._num_features = total_n
            self._backend = "transfer:freeze_and_residual"
        except Exception as exc:  # noqa: BLE001  # BLE001:FOG
            self._backend = f"stub:{type(exc).__name__}"
            self._booster = None
            self._base_booster = None
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "model_load_failed",
                {
                    "artifact": residual_path,
                    "base_artifact": base_path,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    # ------------------------------------------------------------------
    # infer — compose y = y_A(X[:, :base_n]) + r(X[:, base_n:])
    # ------------------------------------------------------------------

    def infer(self, feature_vector: np.ndarray) -> dict[str, Any]:
        if self._base_booster is None or self._booster is None:
            return {
                "raw_score": 0.0,
                "feature_count": len(feature_vector),
                "fallback": True,
                "fallback_reason": "model_not_loaded",
            }

        vec_arr = np.asarray(feature_vector, dtype=np.float64)

        # ── Zero-vector guard — catches silent FeatureService fallback ──
        if np.max(np.abs(vec_arr)) < 1e-10:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "zero_feature_vector",
                {"feature_count": len(feature_vector)},
            )
            return {
                "raw_score": 0.0,
                "feature_count": len(feature_vector),
                "runtime_ms": 0.0,
                "fallback": True,
                "fallback_reason": "zero_feature_vector",
            }

        # ── Dimension guard — must equal schema dim (46 for btc_macro_flow_46) ──
        n_cols = vec_arr.shape[0] if vec_arr.ndim == 1 else vec_arr.shape[1]
        if self._num_features and n_cols != self._num_features:
            emit_brain_alert(
                self._brain_entry.get("brain_id", "unknown"),
                "feature_dimension_mismatch",
                {"expected": self._num_features, "got": n_cols},
            )
            return {
                "raw_score": 0.0,
                "feature_count": n_cols,
                "runtime_ms": 0.0,
                "fallback": True,
                "fallback_reason": (f"dim_mismatch_expected_{self._num_features}_got_{n_cols}"),
            }

        # ── Composition: y = y_A + r (bit-identical to ResidualTransferLearner) ──
        started = perf_counter()
        X = vec_arr.reshape(1, -1)
        base_n = self._base_booster.num_feature()
        y_A = float(np.asarray(self._base_booster.predict(X[:, :base_n]), dtype=np.float64)[0])
        r = float(np.asarray(self._booster.predict(X[:, base_n:]), dtype=np.float64)[0])
        raw_score = y_A + r
        runtime_ms = (perf_counter() - started) * 1000.0

        return {
            "raw_score": raw_score,
            "feature_count": n_cols,
            "runtime_ms": runtime_ms,
            "fallback": False,
            # Decomposition for repairability (flows into BrainSignal.diagnostics).
            "base_score": y_A,
            "residual_score": r,
        }

    # ------------------------------------------------------------------
    # describe — expose both booster load states (Repairability ↑)
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base["num_features"] = self._num_features
        base["base_booster_loaded"] = self._base_booster is not None
        base["residual_booster_loaded"] = self._booster is not None
        base["composition"] = "y = y_A + r"
        return base
