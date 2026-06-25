"""Tests for core.brains.services.brain_attribution_service — P&L attribution.

FIX-20260625-XXX: Tier 2 zero-coverage breakout #4.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.brains.services.brain_attribution_service import (
    AttributionReport,
    BrainAttribution,
    BrainAttributionService,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_journal_entry(**overrides) -> dict:
    """Build a minimal journal entry."""
    entry: dict = {
        "action": "open",
        "message_id": "msg_001",
        "brain_ids": ["brain_a"],
        "label": "",
        "pnl": 0.0,
        "side": "long",
    }
    entry.update(overrides)
    return entry


def _write_journal(path: Path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ── BrainAttribution dataclass ─────────────────────────────────────────────


class TestBrainAttribution:
    def test_to_dict_structure(self) -> None:
        attr = BrainAttribution(
            brain_id="brain_x",
            total_trades=10,
            winning_trades=6,
            losing_trades=4,
            total_pnl=150.0,
            avg_pnl_per_trade=15.0,
            win_rate=0.6,
            label_distribution={"tp_hit": 4, "sl_hit": 2},
            sponsor_count=8,
            dissenter_count=2,
        )
        d = attr.to_dict()
        assert d["brain_id"] == "brain_x"
        assert d["total_trades"] == 10
        assert d["total_pnl"] == 150.0
        assert d["avg_pnl_per_trade"] == 15.0
        assert d["win_rate"] == 0.6
        assert d["label_distribution"] == {"tp_hit": 4, "sl_hit": 2}
        assert d["sponsor_count"] == 8
        assert d["dissenter_count"] == 2

    def test_to_dict_defaults(self) -> None:
        attr = BrainAttribution()
        d = attr.to_dict()
        assert d["brain_id"] == ""
        assert d["total_trades"] == 0
        assert d["total_pnl"] == 0.0


# ── AttributionReport dataclass ────────────────────────────────────────────


class TestAttributionReport:
    def test_to_dict(self) -> None:
        attr = BrainAttribution(brain_id="b1", total_trades=5, total_pnl=100.0)
        report = AttributionReport(
            layer_1_counterfactual={"b1": {"signals": 5}},
            layer_2_attributed=[attr],
            layer_3_realized={"by_brain": {"b1": {"trades": 5}}},
            total_labeled_trades=5,
            total_realized_pnl=100.0,
        )
        d = report.to_dict()
        assert d["total_labeled_trades"] == 5
        assert d["total_realized_pnl"] == 100.0
        assert len(d["layer_2_attributed"]) == 1


# ── _effective_pnl ─────────────────────────────────────────────────────────


class TestEffectivePnl:
    def test_pnl_field_direct(self) -> None:
        assert BrainAttributionService._effective_pnl({"pnl": 15.5}) == 15.5

    def test_pnl_field_nested(self) -> None:
        assert BrainAttributionService._effective_pnl({"detail": {"pnl": 20.0}}) == 20.0

    def test_pnl_direct_takes_priority(self) -> None:
        assert (
            BrainAttributionService._effective_pnl({"pnl": 10.0, "detail": {"pnl": 20.0}}) == 10.0
        )

    def test_no_pnl_returns_zero(self) -> None:
        assert BrainAttributionService._effective_pnl({}) == 0.0

    def test_none_pnl_returns_zero(self) -> None:
        assert BrainAttributionService._effective_pnl({"pnl": None}) == 0.0


# ── _split_sponsors_dissenters ─────────────────────────────────────────────


class TestSplitSponsorsDissenters:
    def test_sponsor_matches_direction(self) -> None:
        votes = [{"brain_id": "b1", "direction_bias": "long", "confidence": 0.8}]
        sponsors, dissenters = BrainAttributionService._split_sponsors_dissenters(votes, "long")
        assert len(sponsors) == 1
        assert len(dissenters) == 0
        assert sponsors[0]["brain_id"] == "b1"

    def test_dissenter_mismatches_direction(self) -> None:
        votes = [{"brain_id": "b1", "direction_bias": "long", "confidence": 0.8}]
        sponsors, dissenters = BrainAttributionService._split_sponsors_dissenters(votes, "short")
        assert len(sponsors) == 0
        assert len(dissenters) == 1
        assert dissenters[0]["brain_id"] == "b1"

    def test_neutral_excluded_from_both(self) -> None:
        votes = [{"brain_id": "b1", "direction_bias": "neutral", "confidence": 0.5}]
        sponsors, dissenters = BrainAttributionService._split_sponsors_dissenters(votes, "long")
        assert len(sponsors) == 0
        assert len(dissenters) == 0

    def test_mixed_votes(self) -> None:
        votes = [
            {"brain_id": "b1", "direction_bias": "long", "confidence": 0.8},
            {"brain_id": "b2", "direction_bias": "short", "confidence": 0.6},
            {"brain_id": "b3", "direction_bias": "neutral", "confidence": 0.5},
            {"brain_id": "b4", "direction_bias": "long", "confidence": 0.7},
        ]
        sponsors, dissenters = BrainAttributionService._split_sponsors_dissenters(votes, "long")
        assert len(sponsors) == 2
        assert len(dissenters) == 1
        assert {s["brain_id"] for s in sponsors} == {"b1", "b4"}

    def test_legacy_direction_field(self) -> None:
        votes = [{"brain_id": "b1", "direction": "long", "confidence": 0.8}]
        sponsors, dissenters = BrainAttributionService._split_sponsors_dissenters(votes, "long")
        assert len(sponsors) == 1

    def test_empty_votes(self) -> None:
        sponsors, dissenters = BrainAttributionService._split_sponsors_dissenters([], "long")
        assert sponsors == []
        assert dissenters == []


# ── _attribute_trades ──────────────────────────────────────────────────────


class TestAttributeTrades:
    def test_basic_even_split_attribution(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(
                    action="open", message_id="m1", brain_ids=["brain_a", "brain_b"], side="long"
                ),
                _make_journal_entry(
                    action="close",
                    open_message_id="m1",
                    pnl=100.0,
                    label="tp_hit",
                    brain_ids=["brain_a", "brain_b"],
                    side="long",
                ),
            ],
        )
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        assert report.total_realized_pnl == pytest.approx(100.0)
        # Even split: 50 each (legacy path — no brain_votes)
        totals = {a.brain_id: a.total_pnl for a in report.layer_2_attributed}
        assert totals.get("brain_a") == pytest.approx(50.0)
        assert totals.get("brain_b") == pytest.approx(50.0)

    def test_confidence_weighted_attribution(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(
                    action="open", message_id="m1", brain_ids=["brain_a", "brain_b"]
                ),
                _make_journal_entry(
                    action="close",
                    open_message_id="m1",
                    pnl=100.0,
                    label="tp_hit",
                    side="long",
                    brain_votes=[
                        {"brain_id": "brain_a", "direction_bias": "long", "confidence": 0.9},
                        {"brain_id": "brain_b", "direction_bias": "long", "confidence": 0.3},
                    ],
                ),
            ],
        )
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        # brain_a gets 0.9/(0.9+0.3)=0.75, brain_b gets 0.3/1.2=0.25
        totals = {a.brain_id: a.total_pnl for a in report.layer_2_attributed}
        assert totals.get("brain_a") == pytest.approx(75.0, abs=1.0)
        assert totals.get("brain_b") == pytest.approx(25.0, abs=1.0)

    def test_unknown_brain_fallback(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(
                    action="close", pnl=50.0, label="tp_hit", brain_ids=[], brain_votes=[]
                ),
            ],
        )
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        unknown = {a.brain_id: a.total_pnl for a in report.layer_2_attributed}
        assert unknown.get("_unknown_") == pytest.approx(50.0)

    def test_legacy_path_no_brain_votes(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(action="open", message_id="m1", brain_ids=["brain_x"]),
                _make_journal_entry(action="close", open_message_id="m1", pnl=75.0, label="tp_hit"),
            ],
        )
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        assert report.total_realized_pnl == pytest.approx(75.0)

    def test_skips_orphan_labels(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(action="close", pnl=10.0, label="auto_orphan_timeout"),
            ],
        )
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        assert report.total_labeled_trades == 0

    def test_all_dissenters_even_split(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(
                    action="open", message_id="m1", brain_ids=["brain_a", "brain_b"], side="long"
                ),
                _make_journal_entry(
                    action="close",
                    open_message_id="m1",
                    pnl=60.0,
                    label="sl_hit",
                    side="long",
                    brain_ids=["brain_a", "brain_b"],
                    brain_votes=[
                        {"brain_id": "brain_a", "direction_bias": "short", "confidence": 0.5},
                        {"brain_id": "brain_b", "direction_bias": "short", "confidence": 0.5},
                    ],
                ),
            ],
        )
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        totals = {a.brain_id: a.total_pnl for a in report.layer_2_attributed}
        # All dissenters → no sponsors → even split fallback
        assert totals.get("brain_a") == pytest.approx(30.0)
        assert totals.get("brain_b") == pytest.approx(30.0)

    def test_sponsor_dissenter_counting(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(
                    action="open", message_id="m1", brain_ids=["sponsor_brain", "dissenter_brain"]
                ),
                _make_journal_entry(
                    action="close",
                    open_message_id="m1",
                    pnl=50.0,
                    label="tp_hit",
                    side="long",
                    brain_votes=[
                        {"brain_id": "sponsor_brain", "direction_bias": "long", "confidence": 0.8},
                        {
                            "brain_id": "dissenter_brain",
                            "direction_bias": "short",
                            "confidence": 0.6,
                        },
                    ],
                ),
            ],
        )
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        for a in report.layer_2_attributed:
            if a.brain_id == "sponsor_brain":
                assert a.sponsor_count == 1
            elif a.brain_id == "dissenter_brain":
                assert a.dissenter_count == 1

    def test_empty_journal(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(journal_path, [])
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        assert report.total_labeled_trades == 0
        assert report.total_realized_pnl == 0.0

    def test_no_journal_file(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "nonexistent.jsonl"
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        assert report.total_labeled_trades == 0


# ── _load_counterfactual ───────────────────────────────────────────────────


class TestLoadCounterfactual:
    def test_loads_pnl_ledger(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(journal_path, [])
        pnl_ledger_path = tmp_path / "pnl_ledger.json"
        pnl_ledger_path.write_text(
            json.dumps(
                {
                    "settled": {
                        "brain_a": [
                            {"pnl_per_unit": 10.0},
                            {"pnl_per_unit": -5.0},
                        ],
                    },
                }
            )
        )
        svc = BrainAttributionService(journal_path, pnl_ledger_path)
        report = svc.build_report()
        cf = report.layer_1_counterfactual
        assert "brain_a" in cf
        assert cf["brain_a"]["signals"] == 2
        assert cf["brain_a"]["winning_signals"] == 1
        assert cf["brain_a"]["total_pnl"] == 5.0

    def test_no_pnl_ledger(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(journal_path, [])
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        assert report.layer_1_counterfactual == {}


# ── quick_summary ──────────────────────────────────────────────────────────


class TestQuickSummary:
    def test_compact_output(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(
                    action="open", message_id="m1", brain_ids=["brain_a"], side="long"
                ),
                _make_journal_entry(
                    action="close",
                    open_message_id="m1",
                    pnl=42.5,
                    label="tp_hit",
                    brain_ids=["brain_a"],
                    side="long",
                ),
            ],
        )
        svc = BrainAttributionService(journal_path)
        summary = svc.quick_summary()
        assert "brains" in summary
        assert "total_labeled_trades" in summary
        assert "total_realized_pnl" in summary
        assert "brain_a" in summary["brains"]

    def test_skips_unknown_brain(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(action="close", pnl=10.0, label="tp_hit"),
            ],
        )
        svc = BrainAttributionService(journal_path)
        summary = svc.quick_summary()
        assert "_unknown_" not in summary.get("brains", {})


# ── _compute_realized ──────────────────────────────────────────────────────


class TestComputeRealized:
    def test_aggregates_all_attributions(self, tmp_path: Path) -> None:
        journal_path = tmp_path / "journal.jsonl"
        _write_journal(
            journal_path,
            [
                _make_journal_entry(action="open", message_id="m1", brain_ids=["brain_a"]),
                _make_journal_entry(action="open", message_id="m2", brain_ids=["brain_b"]),
                _make_journal_entry(
                    action="close", open_message_id="m1", pnl=100.0, label="tp_hit"
                ),
                _make_journal_entry(
                    action="close", open_message_id="m2", pnl=-30.0, label="sl_hit"
                ),
            ],
        )
        svc = BrainAttributionService(journal_path)
        report = svc.build_report()
        assert report.total_realized_pnl == pytest.approx(70.0)
        assert report.total_labeled_trades == 2
