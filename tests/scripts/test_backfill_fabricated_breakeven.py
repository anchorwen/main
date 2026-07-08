"""Tests for scripts.backfill_fabricated_breakeven — DQAF-20260708-003 remediation.

Locks the fabrication fingerprint that gates writes to the immutable journal,
and the append-only correction record builder.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location(
    "backfill_fabricated_breakeven",
    Path(__file__).resolve().parents[2] / "scripts" / "backfill_fabricated_breakeven.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_bf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_bf)


def _close(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "close",
        "label": "breakeven",
        "pnl": 0.0,
        "entry_price": 63514.66,
        "detail": {"close_price": 63514.66},
        "message_id": "close_1_2",
        "position_ticket": 1,
        "symbol": "BTCUSDc",
        "side": "long",
        "volume": 0.1,
    }
    base.update(kw)
    return base


class TestFingerprint:
    def test_ground_truth_fabrication_detected(self) -> None:
        # ticket 3947528377 — the proven fabrication
        assert _bf.is_fabricated_breakeven(_close()) is True

    def test_real_loss_not_flagged(self) -> None:
        assert (
            _bf.is_fabricated_breakeven(
                _close(label="loss", pnl=-50.0, detail={"close_price": 63000.0})
            )
            is False
        )

    def test_genuine_breakeven_with_different_close_not_flagged(self) -> None:
        # a real breakeven where close != entry is NOT the fabrication signature
        assert _bf.is_fabricated_breakeven(_close(detail={"close_price": 63520.0})) is False

    def test_already_ssot_resolved_not_flagged(self) -> None:
        assert _bf.is_fabricated_breakeven(_close(_close_price_source="mt5_exit_deal")) is False

    def test_nonzero_pnl_not_flagged(self) -> None:
        assert _bf.is_fabricated_breakeven(_close(pnl=1084.0)) is False

    def test_open_action_not_flagged(self) -> None:
        assert _bf.is_fabricated_breakeven(_close(action="open")) is False

    def test_missing_entry_price_not_flagged(self) -> None:
        assert _bf.is_fabricated_breakeven(_close(entry_price=None)) is False

    def test_null_pnl_with_equal_prices_flagged(self) -> None:
        assert _bf.is_fabricated_breakeven(_close(pnl=None)) is True


class TestCorrectionBuilder:
    def test_correction_is_append_only_and_carries_lineage(self) -> None:
        orig = _close()
        corr = _bf._build_correction(
            orig,
            close_price=64598.99,
            pnl=1084.0,
            close_reason=5,
            close_price_source="mt5_exit_deal",
            pnl_status="verified_from_mt5_deal",
            now_iso="2026-07-08T00:00:00",
        )
        assert corr["_source"] == "mt5_reconciliation_backfill"
        assert corr["_corrects"] == "close_1_2"
        assert corr["pnl"] == 1084.0
        assert corr["label"] == "tp_hit_first"
        assert corr["detail"]["close_price"] == 64598.99
        assert corr["_close_price_source"] == "mt5_exit_deal"
        # never reuses the original message_id (would be a dedup no-op)
        assert corr["message_id"] != orig["message_id"]

    def test_loss_correction_labeled_loss(self) -> None:
        corr = _bf._build_correction(
            _close(),
            close_price=63000.0,
            pnl=-500.0,
            close_reason=4,
            close_price_source="mt5_exit_deal",
            pnl_status="verified_from_mt5_deal",
            now_iso="2026-07-08T00:00:00",
        )
        assert corr["label"] == "sl_hit_first"
        assert corr["pnl"] == -500.0


class TestDetectList:
    def test_detect_filters_only_fabricated(self) -> None:
        entries = [
            _close(),  # fabricated
            _close(label="loss", pnl=-10.0, detail={"close_price": 100.0}),  # real loss
            {"action": "open", "position_ticket": 9},  # open
        ]
        found = _bf.detect(entries)
        assert len(found) == 1
        assert found[0]["position_ticket"] == 1
