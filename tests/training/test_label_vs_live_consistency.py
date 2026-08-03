"""Phase 2 / M1 hard assertions — 标签契约的绝对独裁 (label contract absolutism).

FIX-20260803-003 / IC 最高批准:
  1. Training scripts are DEPRIVED of the power to set SL/TP/spread — the
     ONLY source of truth is ``label_from_live_yaml.py`` (live YAML → training,
     DQAF-20260630-200 direction lesson).
  2. ``validate_label_vs_live.py`` is the hard fuse: any contract whose label
     triple (SL/TP/spread) diverges from the live strategy line raises
     ``LabelLiveMismatchError`` — no model is produced from mismatched labels.
  3. ``build_expected_r`` canon: open-next-bar entry, TP-before-SL, same-bar
     ambiguous → 0.  This is PINNED against the barrier path (close entry,
     SL-before-TP) — the two semantics are intentionally divergent and both
     must be preserved (XAU barrier vs BTC two-tower Expected R).
"""

from __future__ import annotations

import numpy as np
import pytest

from core.contracts.training.label_contract import LabelContract, _compute_atr
from core.contracts.training.label_from_live_yaml import label_params_from_live_yaml
from scripts.training.validate_label_vs_live import (
    LabelLiveMismatchError,
    validate_label_contract_vs_live,
)

LIVE_BTC = "configs/live_btc.yaml"


def _make_contract(
    *,
    sl: float = 2.0,
    tp: float = 2.5,
    horizon: int = 5,
    contract_type: str = "expected_r",
    bar_timeframe: str = "M30",
) -> LabelContract:
    return LabelContract(
        schema_version="label_contract.v1",
        contract_id="test-contract",
        type=contract_type,
        horizon_bars=horizon,
        label_classes={},
        sl_atr_mult=sl,
        tp_atr_mult=tp,
        bar_timeframe=bar_timeframe,
        atr_period=14,
        spread_points=200,
        slippage_points=10,
        tick_size=0.01,
        tick_value=0.01,
    )


# ── 1. live.yaml is the single truth source ───────────────────────────────


class TestLabelParamsFromLiveYaml:
    def test_btc_m30_params(self) -> None:
        p = label_params_from_live_yaml("btc_swing_m30", LIVE_BTC)
        assert p.sl_atr_mult == 2.0
        assert p.tp_atr_mult == 2.5
        assert p.spread_points == 200.0
        assert p.timeframe == "M30"
        assert p.symbol == "BTCUSDc"
        assert p.tick_size == 0.01

    def test_expected_r_m15_params(self) -> None:
        p = label_params_from_live_yaml("btc_expected_r_m15", LIVE_BTC)
        assert p.sl_atr_mult == 1.5
        assert p.tp_atr_mult == 2.5
        assert p.timeframe == "M15"

    def test_missing_strategy_raises(self) -> None:
        with pytest.raises(KeyError):
            label_params_from_live_yaml("btc_nonexistent_line", LIVE_BTC)


# ── 2. Hard gate: aligned passes, mismatched fuses ────────────────────────


class TestValidateLabelVsLive:
    def test_aligned_contract_passes(self) -> None:
        contract = _make_contract(sl=2.0, tp=2.5, bar_timeframe="M30")
        issues = validate_label_contract_vs_live(contract, "btc_swing_m30", LIVE_BTC)
        assert issues == []

    def test_mismatched_sl_fuses(self) -> None:
        contract = _make_contract(sl=3.0, tp=2.5, bar_timeframe="M30")
        issues = validate_label_contract_vs_live(contract, "btc_swing_m30", LIVE_BTC)
        assert any("SL mismatch" in i for i in issues)

    def test_mismatched_tp_fuses(self) -> None:
        contract = _make_contract(sl=2.0, tp=1.5, bar_timeframe="M30")
        issues = validate_label_contract_vs_live(contract, "btc_swing_m30", LIVE_BTC)
        assert any("TP mismatch" in i for i in issues)

    def test_mismatched_timeframe_fuses(self) -> None:
        contract = _make_contract(sl=2.0, tp=2.5, bar_timeframe="H1")
        issues = validate_label_contract_vs_live(contract, "btc_swing_m30", LIVE_BTC)
        assert any("timeframe mismatch" in i for i in issues)

    def test_fuse_exception_is_blocking(self) -> None:
        """The fuse is an exception (blocking), not a warning."""
        contract = _make_contract(sl=9.9, tp=2.5, bar_timeframe="M30")
        issues = validate_label_contract_vs_live(contract, "btc_swing_m30", LIVE_BTC)
        assert issues
        with pytest.raises(LabelLiveMismatchError):
            if issues:
                raise LabelLiveMismatchError("; ".join(issues))


# ── 3. build_expected_r canon semantics (TP-first, open entry) ─────────────


def _entry_ohlc(h31: float, l31: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Deterministic OHLC: flat bars up to entry (ATR=2.0), control bar 31."""
    n = 40
    o = np.full(n, 100.0)
    h = np.full(n, 101.0)
    l = np.full(n, 99.0)
    c = np.full(n, 100.0)
    h[31] = h31
    l[31] = l31
    return o, h, l, c


class TestBuildExpectedR:
    def test_contract_requires_expected_r_type(self) -> None:
        c = _make_contract(contract_type="survival_barrier")
        o, h, l, cc = _entry_ohlc(106.0, 100.0)
        with pytest.raises(ValueError):
            c.build_expected_r(o, h, l, cc, entry_idx=30, side="long")

    def test_atr_deterministic_two(self) -> None:
        o, h, l, c = _entry_ohlc(101.0, 99.0)
        atr = _compute_atr(h[:31], l[:31], c[:31], period=14)
        assert atr == pytest.approx(2.0)  # TR=2 per bar → ATR=2

    def test_tp_hit_returns_positive_r(self) -> None:
        c = _make_contract(sl=2.0, tp=2.0)
        o, h, l, cc = _entry_ohlc(106.0, 100.0)  # high hits TP, low stays above SL
        r = c.build_expected_r(o, h, l, cc, entry_idx=30, side="long")
        # entry = open[31] + half_spread + slippage = 100 + 1 + 0.1 = 101.1
        # sl_dist = 2*2 = 4, tp_dist = max(2*2, 4*0.3) = 4
        # sl_price = 97.1, tp_price = 105.1 → TP hit → R = 4/4 = 1.0
        assert r == pytest.approx(1.0)

    def test_sl_hit_returns_neg_one(self) -> None:
        c = _make_contract(sl=2.0, tp=2.0)
        o, h, l, cc = _entry_ohlc(102.0, 96.0)  # low hits SL, high stays below TP
        r = c.build_expected_r(o, h, l, cc, entry_idx=30, side="long")
        assert r == pytest.approx(-1.0)

    def test_same_bar_ambiguous_returns_zero(self) -> None:
        c = _make_contract(sl=2.0, tp=2.0)
        o, h, l, cc = _entry_ohlc(106.0, 96.0)  # high hits TP AND low hits SL → 0
        r = c.build_expected_r(o, h, l, cc, entry_idx=30, side="long")
        assert r == 0.0

    def test_timeout_returns_partial_r(self) -> None:
        c = _make_contract(sl=2.0, tp=2.0, horizon=5)
        o, h, l, cc = _entry_ohlc(101.0, 99.0)  # no barrier → timeout partial
        r = c.build_expected_r(o, h, l, cc, entry_idx=30, side="long")
        # close_at_end = 100, entry = 101.1 → (100 - 101.1)/4 = -0.275
        assert r == pytest.approx((100.0 - 101.1) / 4.0)

    def test_barrier_vs_expected_r_divergence_pinned(self) -> None:
        """Same-bar TP+SL: expected-r returns 0 (ambiguous, TP-first);
        barrier path returns sl_hit_first (SL-first).  Both preserved.

        Thresholds differ by entry model:
          expected-r: entry=open[31]+costs=101.1 → sl=97.1 / tp=105.1
          barrier:    entry=close[30]=100      → eff_sl=95.9 / eff_tp=102.0
        A bar touching both paths' SL+TP needs h>=105.1 AND l<=95.9.
        """
        o, h, l, cc = _entry_ohlc(106.0, 95.5)
        exp = _make_contract(sl=2.0, tp=2.0, contract_type="expected_r")
        assert exp.build_expected_r(o, h, l, cc, entry_idx=30, side="long") == 0.0

        bar = _make_contract(sl=2.0, tp=2.0, contract_type="survival_barrier")
        result = bar.build_barrier_labels(h, l, cc, entry_idx=30, side="long")
        assert result.label == "sl_hit_first"  # SL-first canon in barrier path


# ── 4. Config barrier_order metadata ───────────────────────────────────────


class TestBarrierOrderMetadata:
    def test_expected_r_config_tp_before_sl(self) -> None:
        c = LabelContract.from_file(
            "configs/training/label_contracts/label-expected-r-btc-m15.json"
        )
        assert c.metadata.get("barrier_order") == "tp_before_sl"
        assert c.type == "expected_r"

    def test_barrier_config_sl_before_tp(self) -> None:
        c = LabelContract.from_file("configs/training/label_contracts/label-barrier-btc-m30.json")
        assert c.metadata.get("barrier_order") == "sl_before_tp"
        assert c.type == "survival_barrier"
