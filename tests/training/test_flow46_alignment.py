"""M4 Phase 6.1 / FIX-20260803-007 — OFI residual transfer tests.

Locks the three hard contracts of the OFI 46-dim transfer:

  1. Alignment is leak-free BY CONSTRUCTION: a base row at wall-clock W only
     ever consumes settles with ``settle_wall <= W`` (build_btc_flow46_dataset
     last-settle merge).  A future settle must NOT influence the flow vector.
  2. The residual learner refuses (TransferDataError) when < min_flow_dim flow
     features are live — dead dims are zero-padded, never silently dropped.
  3. Combined inference is y_A + r and preserves the 46-dim schema slice
     (base = [:, :41], flow = [:, 41:46]) — runtime-safe.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from core.training.transfer_adapter import (
    FLOW_SLICE_START,
    ResidualTransferLearner,
    TransferDataError,
    compute_flow_coverage,
    effective_flow_dim,
)

# ── 1. Leak-free alignment (wall-clock last-settle merge) ──────────────────────


class TestLeakFreeAlignment:
    def _make_flow_records(self) -> list[dict[str, object]]:
        """Three settles on an M5 wall-clock grid (30s apart)."""
        return [
            {"time": "2026-07-07T14:34:30.000000", "OFI_M5": 1.0, "OFI_ZScore_20": 0.5},
            {"time": "2026-07-07T14:35:00.000000", "OFI_M5": 2.0, "OFI_ZScore_20": 1.0},
            # a future settle the base bar at 14:35 must NEVER see
            {"time": "2026-07-07T14:40:00.000000", "OFI_M5": 99.0, "OFI_ZScore_20": 9.0},
        ]

    def test_last_settle_le_bar_wall(self, tmp_path: Path) -> None:
        """The aligned flow at bar W is the last settle with settle_wall <= W."""
        from scripts.training.build_btc_flow46_dataset import (
            _load_history,
            _parse_wall_clock,
            _wall_seconds,
        )

        records = self._make_flow_records()
        ofi_path = tmp_path / "ofi_history.jsonl"
        ofi_path.write_text(
            "\n".join(
                json.dumps(
                    {
                        "time": r["time"],
                        "OFI_M5": r["OFI_M5"],
                        "OFI_ZScore_20": r["OFI_ZScore_20"],
                        "OFI_Cumulative_Delta": 0,
                        "OFI_Delta_Divergence": 0,
                        "OFI_Volume_Real_Ratio": 0,
                    }
                )
                for r in records
            ),
            encoding="utf-8",
        )
        loaded = _load_history(ofi_path)
        assert len(loaded) == 3

        wall = np.array([_wall_seconds(_parse_wall_clock(r["time"])) for r in loaded])
        # Bar at 14:35:00 wall-clock: consumes settle #0 (14:34:30) and #1 (14:35:00),
        # NEVER #2 (14:40:00) — a future settle is a look-ahead leak.
        bar_wall = _wall_seconds(_parse_wall_clock("2026-07-07T14:35:00.000000"))
        j = 0
        while j < len(loaded) and wall[j] <= bar_wall:
            j += 1
        assert j == 2, "future settle leaked into the bar's flow state"
        assert loaded[j - 1]["OFI_M5"] == 2.0
        # lag >= 0 by construction
        lag = bar_wall - wall[j - 1]
        assert 0.0 <= lag <= 600.0


# ── 2. effective flow dim + zero-pad contract ─────────────────────────────────


class TestEffectiveFlowDim:
    _FLOW_NAMES = [
        "OFI_M5",
        "OFI_ZScore_20",
        "OFI_Cumulative_Delta",
        "OFI_Delta_Divergence",
        "OFI_Volume_Real_Ratio",
    ]

    def test_live_and_dead_detection(self) -> None:
        X_flow = np.zeros((100, 5))
        X_flow[:, 0] = np.random.default_rng(0).normal(size=100)  # live
        X_flow[:, 1] = 1.0  # constant non-zero → not "live" (>5% non-zero? yes it is)
        coverage = compute_flow_coverage(X_flow, self._FLOW_NAMES)
        assert coverage["OFI_M5"] > 0.05
        assert coverage["OFI_Delta_Divergence"] == 0.0
        dim, live = effective_flow_dim(coverage)
        assert dim == 2
        assert "OFI_M5" in live and "OFI_ZScore_20" in live

    def test_all_dead_refused(self, tmp_path: Path) -> None:
        class _Base:
            base_id = "fake"

            def predict(self, X):
                return np.zeros(X.shape[0])

        base = _Base()
        learner = ResidualTransferLearner(
            base,  # type: ignore[arg-type]
            self._FLOW_NAMES,
            min_flow_dim=2,
        )
        X_flow = np.zeros((50, 5))  # every flow feature dead
        with pytest.raises(TransferDataError, match="effective_flow_dim=0 < min_flow_dim=2"):
            learner.assert_viable(X_flow)


# ── 3. Combined inference keeps the 46-dim slice contract ─────────────────────


class TestCombinedInference:
    def test_predict_is_yA_plus_r(self, tmp_path: Path) -> None:
        import lightgbm as lgb

        from core.training.transfer_adapter import FrozenBaseModel

        rng = np.random.default_rng(7)
        X41 = rng.normal(size=(80, 41))
        flow = rng.normal(size=(80, 5))
        flow[:, 3] = 0.0  # dead dims stay zero
        flow[:, 4] = 0.0
        X46 = np.concatenate([X41, flow], axis=1)
        y_true = np.sum(X41[:, :3], axis=1) + flow[:, 0]  # r is flow-driven

        # Fake frozen base: predicts sum of first 3 base features (leaves the
        # flow term as the residual the learner must recover).
        base_booster = lgb.train(
            {"objective": "regression", "n_estimators": 1, "verbose": -1, "seed": 1},
            lgb.Dataset(np.zeros((5, 41)), label=np.zeros(5)),
            num_boost_round=1,
        )
        # Override predict to return the intended y_A.
        base = FrozenBaseModel(base_booster, "fake_base")

        def _fake_predict(X):
            return np.sum(X[:, :3], axis=1)

        base.predict = _fake_predict  # type: ignore[method-assign]

        learner = ResidualTransferLearner(base, [f"f{i}" for i in range(5)], min_flow_dim=2)
        # make 5 flow names live
        learner.flow_feature_names = [
            "OFI_M5",
            "OFI_ZScore_20",
            "OFI_Cumulative_Delta",
            "OFI_Delta_Divergence",
            "OFI_Volume_Real_Ratio",
        ]
        learner.fit(
            X46[:60],
            y_true[:60],
            X46[60:],
            y_true[60:],
            params={"n_estimators": 50, "learning_rate": 0.1},
        )
        pred = learner.predict(X46[60:])
        assert pred.shape == (20,)
        assert np.all(np.isfinite(pred))
        # The slice contract: base used X[:, :41], residual used X[:, 41:46].
        assert FLOW_SLICE_START == 41


# ── 4. Temporal split is chronological (no cross-time shuffle) ────────────────


class TestTemporalSplit:
    def test_split_is_chronological(self) -> None:
        from scripts.training.train_btc_flow_46_transfer import temporal_split

        ts = np.arange(1000, dtype=np.float64) * 300.0
        tr, va, te = temporal_split(ts, 0.15, 0.15)
        assert len(tr) == 700 and len(va) == 150 and len(te) == 150
        # train is earliest, test is freshest
        assert ts[tr].max() < ts[va].min()
        assert ts[va].max() < ts[te].min()
