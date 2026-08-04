"""Phase 1 / M1 hard assertions — feature computation graph absolute unification.

FIX-20260803-XXX (BTC 机构级训练管线重建 — 战役一):
    Historical replay and live inference MUST flow through the SAME pure
    assembly.  These tests HARD-ASSERT:

      1. Slot mapping is exact (golden vector for known inputs).
      2. ``assemble_41_series`` == loop of ``assemble_41_vector`` with
         prev-from-sequence  (bit-identical, maxdiff == 0).
      3. Live ``BTCFeatureAugmenter.augment()`` (stateful, live cross-asset
         sources) == the pure ``assemble_41_vector`` given the same values.
      4. Replay end-to-end over synthetic OHLC: NaN-free, schema-conformant.
      5. Subset-schema extraction by NAME (position-independent, DQAF-20260801-006).

These tests were historically the FIRST recurring failure class (FIX-20260625-137
three-Order divergence, FIX-20260616-091 name-lie, FIX-20260531-022 three
assembly points).  A regression here is a Sev-1 class defect.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from core.features.computers.btc_feature_augmenter import (
    BTCFeatureAugmenter,
    assemble_41_series,
    assemble_41_vector,
)
from core.features.local_feature_store import LocalFeatureStore
from core.features.schemas.registry import get_schema_feature_names
from core.features.store_contracts import FeatureRecord
from core.training.feature_replay import (
    compute_replay_components,
    replay_features,
    replay_features_41,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _mock_live_sources(
    xau: list[float], audjpy: list[float], ratio: list[float], roc: list[float]
) -> tuple[MagicMock, MagicMock, dict[str, int]]:
    """Return (store, worker, counter) mocks that return the given per-bar
    cross-asset values — exactly what ``assemble_41_series`` would consume."""
    import time

    state = {"i": 0}
    # spec=LocalFeatureStore: real store contract only — DQAF-20260804-002.
    store = MagicMock(spec=LocalFeatureStore)

    def _latest(symbol, timeframe):
        i = state["i"]
        return FeatureRecord(
            schema_name="test_schema",
            schema_version="1",
            symbol=symbol,
            timeframe=timeframe,
            event_time=datetime.fromtimestamp(time.time()),
            values={"M5_Ret_1": xau[i]},
        )

    store.latest.side_effect = _latest

    worker = MagicMock()

    def _copy_rates(symbol, _tf, _pos, _count, timeout=3.0):
        i = state["i"]
        if symbol == "AUDJPYc":
            prev = 100.0
            return [{"close": prev}, {"close": prev * (1.0 + audjpy[i])}]
        if symbol == "XAUUSDc":
            if ratio[i] <= 0 or not np.isfinite(ratio[i]):
                return None  # unavailable → augmenter zero-fills
            btc = 60000.0
            xau_curr = btc / ratio[i]
            xau_prev = btc * (1.0 + roc[i]) / ratio[i]
            if xau_curr <= 0 or xau_prev <= 0:
                return None
            return [{"close": xau_prev}, {"close": xau_curr}]
        return None

    worker.copy_rates_from_pos.side_effect = _copy_rates
    return store, worker, state


def _synth_ohlc(n: int = 300, seed: int = 42) -> pd.DataFrame:
    """Synthetic aligned OHLC frame with cross-asset columns (self-contained test)."""
    rng = np.random.RandomState(seed)
    close = 40000 + np.cumsum(rng.randn(n) * 20)
    close = np.maximum(close, 1000.0)
    o = close - rng.rand(n) * 10
    h = np.maximum(o, close) + rng.rand(n) * 10
    l = np.minimum(o, close) - rng.rand(n) * 10
    v = rng.randint(50, 500, n).astype(float)
    df = pd.DataFrame(
        {
            "time": pd.date_range("2026-02-07", periods=n, freq="5min"),
            "open": o,
            "high": h,
            "low": l,
            "close": close,
            "tick_volume": v,
            "spread": 1800.0,
            "XAUUSDc_close": 2500.0 + rng.rand(n) * 10,
            "XAUUSDc_return": rng.randn(n) * 1e-3,
            "AUDJPYc_return": rng.randn(n) * 1e-3,
            "EURUSDc_return": rng.randn(n) * 1e-3,
            "USDJPYc_return": rng.randn(n) * 1e-3,
        }
    )
    return df


# ── 1. Slot mapping golden test ─────────────────────────────────────────────


class TestAssemblyGoldenVector:
    def test_slot_mapping_exact(self) -> None:
        daily = np.arange(24, dtype=np.float64) + 1.0
        micro = np.arange(9, dtype=np.float64) + 1.0
        fv = assemble_41_vector(
            daily,
            micro,
            xau_return=0.01,
            audjpy_return=0.02,
            btc_xau_ratio=23.0,
            btc_xau_ratio_roc=0.05,
            tf_ou=0.3,
            tf_hurst=0.4,
            prev_ou=0.1,
            prev_hurst=0.2,
        )
        assert fv.shape == (41,)
        # [0-11] = daily[0:12]
        np.testing.assert_array_equal(fv[0:12], daily[0:12])
        # [12] = xau_return
        assert fv[12] == pytest.approx(0.01)
        # [13-24] = daily[13:24]
        np.testing.assert_array_equal(fv[13:24], daily[13:24])
        # [24-30] = micro[0:6]
        np.testing.assert_array_equal(fv[24:30], micro[0:6])
        # [30] = audjpy_return
        assert fv[30] == pytest.approx(0.02)
        # [31-33] = micro[7:9]
        np.testing.assert_array_equal(fv[31:33], micro[7:9])
        # [33-34] = tf_ou / tf_hurst
        assert fv[33] == pytest.approx(0.3)
        assert fv[34] == pytest.approx(0.4)
        # regime derivatives
        assert fv[35] == pytest.approx(0.3 - 0.1)  # TF_delta_OU
        assert fv[36] == pytest.approx(0.4 - 0.2)  # TF_delta_Hurst
        assert fv[37] == pytest.approx(0.3 * (1.0 - 0.4))  # TF_OU_x_Hurst
        assert fv[38] == pytest.approx(0.3 / max(daily[7], 1.0))  # TF_OU_div_ADX
        # BTC/XAU ratio
        assert fv[39] == pytest.approx(23.0)
        assert fv[40] == pytest.approx(0.05)

    def test_order_b_matches_schema_names(self) -> None:
        """The assembly output must equal the canonical name list in Schema Order B."""
        names = get_schema_feature_names("btc_macro_enhanced_41_v2")
        assert len(names) == 41
        # canonical regime-derivative ordering (FIX-20260625-137 Order B)
        assert names[35] == "TF_delta_OU"
        assert names[36] == "TF_delta_Hurst"
        assert names[37] == "TF_OU_x_Hurst"
        assert names[38] == "TF_OU_div_ADX"
        assert names[39] == "Cross_BTC_Gold_Ratio"
        assert names[40] == "Cross_BTC_Gold_Ratio_ROC"


# ── 2. Series == sequential pure (bit-identical) ─────────────────────────────


class TestSeriesBitIdentical:
    def test_series_equals_sequential_pure(self) -> None:
        rng = np.random.RandomState(7)
        n = 120
        daily = rng.randn(n, 24)
        micro = rng.randn(n, 9)
        xau = rng.randn(n) * 1e-3
        audjpy = rng.randn(n) * 1e-3
        ratio = 20 + rng.rand(n) * 5
        roc = rng.randn(n) * 0.1
        ou = rng.rand(n)
        hurst = 0.2 + rng.rand(n) * 0.6

        seq = assemble_41_series(
            daily,
            micro,
            xau_return_series=xau,
            audjpy_return_series=audjpy,
            btc_xau_ratio_series=ratio,
            btc_xau_ratio_roc_series=roc,
            tf_ou_series=ou,
            tf_hurst_series=hurst,
        )
        manual = np.zeros((n, 41))
        for i in range(n):
            manual[i] = assemble_41_vector(
                daily[i],
                micro[i],
                xau_return=float(xau[i]),
                audjpy_return=float(audjpy[i]),
                btc_xau_ratio=float(ratio[i]),
                btc_xau_ratio_roc=float(roc[i]),
                tf_ou=float(ou[i]),
                tf_hurst=float(hurst[i]),
                prev_ou=None if i == 0 else float(ou[i - 1]),
                prev_hurst=None if i == 0 else float(hurst[i - 1]),
            )
        np.testing.assert_array_equal(seq, manual)

    def test_series_first_bar_cold_start(self) -> None:
        """Bar 0 delta slots are 0 (cold start), matching a fresh live augmenter."""
        daily = np.zeros((2, 24), dtype=np.float64)
        micro = np.zeros((2, 9), dtype=np.float64)
        out = assemble_41_series(daily, micro, tf_ou_series=[0.5, 0.7], tf_hurst_series=[0.6, 0.4])
        assert out[0, 35] == 0.0  # delta_OU cold start
        assert out[0, 36] == 0.0  # delta_Hurst cold start
        assert out[1, 35] == pytest.approx(0.2)  # 0.7 - 0.5
        assert out[1, 36] == pytest.approx(-0.2)  # 0.4 - 0.6


# ── 3. Live augment() == pure (same code path) ──────────────────────────────


class TestLivePathEqualsPure:
    def test_single_bar_live_equals_pure(self) -> None:
        daily = np.arange(24, dtype=np.float64) + 1.0
        micro = np.arange(9, dtype=np.float64) + 1.0
        xau, audjpy, ratio, roc = 0.005, -0.003, 24.0, 0.01
        store, worker, state = _mock_live_sources([xau], [audjpy], [ratio], [roc])
        aug = BTCFeatureAugmenter(feature_store=store, mt5_worker=worker)
        fv_live = aug.augment(daily, micro, btc_price=60000.0, tf_ou=0.3, tf_hurst=0.4)
        fv_pure = assemble_41_vector(
            daily,
            micro,
            xau_return=xau,
            audjpy_return=audjpy,
            btc_xau_ratio=ratio,
            btc_xau_ratio_roc=roc,
            tf_ou=0.3,
            tf_hurst=0.4,
        )
        # allclose (rtol=1e-12): the LIVE source fetch recomputes audjpy/ratio via
        # division, introducing IEEE 1e-16 rounding vs the exact pure inputs.  The
        # ASSEMBLY is identical; the source-value float artifact is not a divergence.
        np.testing.assert_allclose(fv_live, fv_pure, rtol=1e-12, atol=1e-12)

    def test_multi_bar_live_equals_series(self) -> None:
        """augment() N times (stateful) == assemble_41_series — the replay
        guarantee that live and historical replay are the SAME code path."""
        rng = np.random.RandomState(11)
        n = 8
        daily = rng.randn(n, 24)
        micro = rng.randn(n, 9)
        xau = rng.randn(n) * 1e-3
        audjpy = rng.randn(n) * 1e-3
        ratio = 20 + rng.rand(n) * 5
        roc = rng.randn(n) * 0.1
        ou = rng.rand(n)
        hurst = 0.2 + rng.rand(n) * 0.6

        store, worker, state = _mock_live_sources(list(xau), list(audjpy), list(ratio), list(roc))
        aug = BTCFeatureAugmenter(feature_store=store, mt5_worker=worker)
        live = np.zeros((n, 41))
        for i in range(n):
            state["i"] = i
            live[i] = aug.augment(
                daily[i],
                micro[i],
                btc_price=60000.0,
                tf_ou=float(ou[i]),
                tf_hurst=float(hurst[i]),
            )

        seq = assemble_41_series(
            daily,
            micro,
            xau_return_series=xau,
            audjpy_return_series=audjpy,
            btc_xau_ratio_series=ratio,
            btc_xau_ratio_roc_series=roc,
            tf_ou_series=ou,
            tf_hurst_series=hurst,
        )
        # allclose (rtol=1e-12): live source-fetch division rounding (see
        # test_single_bar_live_equals_pure).  Assembly identical.
        np.testing.assert_allclose(live, seq, rtol=1e-12, atol=1e-12)


# ── 4. Replay end-to-end ────────────────────────────────────────────────────


class TestReplayEndToEnd:
    def test_replay_shape_and_nan_free(self) -> None:
        df = _synth_ohlc(n=300)
        comp = compute_replay_components(df, tf_minutes=5.0)
        assert comp.daily.shape == (300, 24)
        assert comp.micro.shape == (300, 9)
        x41 = replay_features_41(comp)
        assert x41.shape == (300, 41)
        assert not np.isnan(x41).any()

    def test_replay_schema_conformant(self) -> None:
        df = _synth_ohlc(n=300)
        X, meta = replay_features(df, tf_minutes=5.0, schema_name="btc_macro_enhanced_41_v2")
        assert X.shape[1] == 41
        assert meta["n_features"] == 41
        assert meta["schema_id"] == "btc_macro_enhanced_41_v2"
        assert len(meta["feature_names"]) == 41
        assert not np.isnan(X).any()


# ── 5. Subset-schema extraction by name ─────────────────────────────────────


class TestSchemaSubset:
    def test_expected_r_37_by_name(self) -> None:
        """btc_expected_r_37 = 41-dim minus the 4 H4 placeholders (indices 8-11),
        extracted by NAME — never by hardcoded index."""
        from core.training.feature_replay import extract_schema_subset

        rng = np.random.RandomState(3)
        x41 = rng.randn(10, 41)
        x37 = extract_schema_subset(x41, "btc_expected_r_37")
        assert x37.shape == (10, 37)
        canonical = get_schema_feature_names("btc_macro_enhanced_41_v2")
        want = get_schema_feature_names("btc_expected_r_37")
        # names must be a subset, and values must come from the canonical columns
        assert set(want) <= set(canonical)
        for j, name in enumerate(want):
            k = canonical.index(name)
            np.testing.assert_array_equal(x37[:, j], x41[:, k])
