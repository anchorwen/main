"""Unit tests for ofi_gate.py — OFI Toxicity Gate for statarb strategies.

Covers:
  - Pass-through for non-statarb strategies
  - Pass-through when micro_feature_dict is None/missing OFI
  - Block short when OFI_Z > 2.0
  - Block long when OFI_Z < -2.0
  - Pass when OFI is neutral
  - Boundary values at ±2.0
"""

from __future__ import annotations

from typing import Any

from core.execution.ofi_gate import apply_ofi_toxicity_gate

# ── Mock make_decision ────────────────────────────────────────────────────


def _mock_make_decision(**kwargs: Any) -> dict[str, Any]:
    """Capture call arguments for assertion."""
    return {"_called": True, **kwargs}


# ═══════════════════════════════════════════════════════════════════════════
# apply_ofi_toxicity_gate
# ═══════════════════════════════════════════════════════════════════════════


class TestApplyOfiToxicityGate:
    """Pure function: OFI toxicity gate."""

    # ── Pass-through: non-statarb strategies ──

    def test_non_statarb_passes_through(self) -> None:
        """Strategies outside statarb_dynamic/statarb_m15 are never blocked."""
        result = apply_ofi_toxicity_gate(
            strategy_name="btc_swing",
            micro_feature_dict={"OFI": 5.0},
            direction="short",
            confidence=0.8,
            brain_ids=["brain_1"],
            support_count=2,
            total_count=3,
            regime_gate_mode="cold_explore",
            make_decision=_mock_make_decision,
        )
        assert result is None

    def test_statarb_dynamic_is_checked(self) -> None:
        """statarb_dynamic is subject to OFI gate."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 5.0},
            direction="short",
            confidence=0.8,
            brain_ids=["brain_1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="cold_explore",
            make_decision=_mock_make_decision,
        )
        assert result is not None
        assert result["should_trade"] is False

    def test_statarb_m15_is_checked(self) -> None:
        """statarb_m15 is subject to OFI gate."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_m15",
            micro_feature_dict={"OFI": -5.0},
            direction="long",
            confidence=0.8,
            brain_ids=["brain_1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="cold_explore",
            make_decision=_mock_make_decision,
        )
        assert result is not None
        assert result["should_trade"] is False

    # ── Pass-through: missing/None micro features ──

    def test_none_micro_features_passes_through(self) -> None:
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict=None,
            direction="short",
            confidence=0.8,
            brain_ids=["brain_1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="cold_explore",
            make_decision=_mock_make_decision,
        )
        assert result is None

    def test_missing_ofi_key_passes_through(self) -> None:
        """If OFI key missing from dict, treated as 0.0 → passes."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"SOME_OTHER_FEATURE": 99.0},
            direction="short",
            confidence=0.8,
            brain_ids=["brain_1"],
            support_count=1,
            total_count=1,
            regime_gate_mode="cold_explore",
            make_decision=_mock_make_decision,
        )
        assert result is None

    # ── Block: short + toxic OFI ──

    def test_blocks_short_when_ofi_z_above_2(self) -> None:
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 3.5},
            direction="short",
            confidence=0.75,
            brain_ids=["brain_a"],
            support_count=2,
            total_count=3,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is not None
        assert result["should_trade"] is False
        assert result["direction"] == "short"
        assert result["volume"] == 0.0
        assert "ofi_toxicity_blocked_short" in result["reason"]

    def test_blocks_short_when_ofi_z_barely_above_2(self) -> None:
        """OFI = 2.0001 → blocked (strict > 2.0)."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 2.0001},
            direction="short",
            confidence=0.5,
            brain_ids=["b"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is not None
        assert result["should_trade"] is False

    # ── Block: long + toxic OFI ──

    def test_blocks_long_when_ofi_z_below_minus_2(self) -> None:
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_m15",
            micro_feature_dict={"OFI": -4.0},
            direction="long",
            confidence=0.6,
            brain_ids=["brain_c"],
            support_count=3,
            total_count=4,
            regime_gate_mode="warm",
            make_decision=_mock_make_decision,
        )
        assert result is not None
        assert result["should_trade"] is False
        assert result["direction"] == "long"
        assert "ofi_toxicity_blocked_long" in result["reason"]

    def test_blocks_long_when_ofi_z_barely_below_minus_2(self) -> None:
        """OFI = -2.0001 → blocked (strict <)."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_m15",
            micro_feature_dict={"OFI": -2.0001},
            direction="long",
            confidence=0.5,
            brain_ids=["d"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is not None
        assert result["should_trade"] is False

    # ── Pass: neutral OFI ──

    def test_passes_when_ofi_neutral_for_short(self) -> None:
        """OFI = 1.0 → below 2.0 threshold → no block for short."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 1.0},
            direction="short",
            confidence=0.8,
            brain_ids=["brain_e"],
            support_count=1,
            total_count=1,
            regime_gate_mode="cold_explore",
            make_decision=_mock_make_decision,
        )
        assert result is None

    def test_passes_when_ofi_neutral_for_long(self) -> None:
        """OFI = -1.0 → above -2.0 threshold → no block for long."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_m15",
            micro_feature_dict={"OFI": -1.0},
            direction="long",
            confidence=0.8,
            brain_ids=["brain_f"],
            support_count=1,
            total_count=1,
            regime_gate_mode="cold_explore",
            make_decision=_mock_make_decision,
        )
        assert result is None

    def test_passes_when_ofi_zero(self) -> None:
        """OFI = 0.0 → perfectly neutral."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 0.0},
            direction="short",
            confidence=0.8,
            brain_ids=["brain_g"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is None

    # ── Boundary: exactly at threshold ──

    def test_passes_short_when_ofi_exactly_2(self) -> None:
        """OFI == 2.0 → NOT > 2.0 → passes (strict inequality)."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 2.0},
            direction="short",
            confidence=0.8,
            brain_ids=["brain_h"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is None

    def test_passes_long_when_ofi_exactly_minus_2(self) -> None:
        """OFI == -2.0 → NOT < -2.0 → passes (strict inequality)."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_m15",
            micro_feature_dict={"OFI": -2.0},
            direction="long",
            confidence=0.8,
            brain_ids=["brain_i"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is None

    # ── Direction/Opposite checks ──

    def test_long_not_blocked_by_high_ofi(self) -> None:
        """High OFI (positive) blocks short, not long."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 5.0},
            direction="long",
            confidence=0.8,
            brain_ids=["brain_j"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is None

    def test_short_not_blocked_by_low_ofi(self) -> None:
        """Low OFI (negative) blocks long, not short."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_m15",
            micro_feature_dict={"OFI": -5.0},
            direction="short",
            confidence=0.8,
            brain_ids=["brain_k"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is None

    # ── Result propagation ──

    def test_decision_propagates_all_fields(self) -> None:
        """The blocked decision carries forward all input parameters."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 5.0},
            direction="short",
            confidence=0.77,
            brain_ids=["b1", "b2"],
            support_count=2,
            total_count=3,
            regime_gate_mode="cold_explore",
            make_decision=_mock_make_decision,
        )
        assert result is not None
        assert result["direction"] == "short"
        assert result["confidence"] == 0.77
        assert result["volume"] == 0.0
        assert result["sl"] == 0.0
        assert result["tp"] == 0.0
        assert result["hard_sl"] == 0.0
        assert result["brain_ids"] == ["b1", "b2"]
        assert result["supporting_count"] == 2
        assert result["total_count"] == 3
        assert result["regime_mode"] == "cold_explore"

    def test_reason_includes_ofi_value(self) -> None:
        """Block reason contains the OFI_Z value for audit trail."""
        result = apply_ofi_toxicity_gate(
            strategy_name="statarb_dynamic",
            micro_feature_dict={"OFI": 4.56},
            direction="short",
            confidence=0.5,
            brain_ids=["b"],
            support_count=1,
            total_count=1,
            regime_gate_mode="active",
            make_decision=_mock_make_decision,
        )
        assert result is not None  # TECH_DEBT-009: apply_ofi_toxicity_gate 返回 dict|None 类型收窄
        assert "4.56" in result["reason"]
