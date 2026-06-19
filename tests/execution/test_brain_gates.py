"""Unit tests for brain_gates — pure functions extracted from strategy_line.py.

Strangler Fig #17: count_valid_voters + check_min_valid_brains.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.execution.brain_gates import (
    check_min_valid_brains,
    count_valid_voters,
    extract_entry_z_score,
)


def _proposal(direction="long", vote_weight=1.0):
    return SimpleNamespace(direction=direction, vote_weight=vote_weight, prediction={})


def _proposal_via_pred(direction_bias="long", vote_weight=1.0):
    return SimpleNamespace(direction=None, vote_weight=vote_weight, prediction={"direction_bias": direction_bias})


class TestCountValidVoters:
    def test_empty(self):
        assert count_valid_voters([]) == 0

    def test_all_neutral(self):
        proposals = [_proposal("neutral"), _proposal("neutral")]
        assert count_valid_voters(proposals) == 0

    def test_mixed(self):
        proposals = [_proposal("long"), _proposal("neutral"), _proposal("short")]
        assert count_valid_voters(proposals) == 2

    def test_muted_excluded(self):
        proposals = [_proposal("long", vote_weight=0.0), _proposal("short")]
        assert count_valid_voters(proposals) == 1

    def test_legacy_prediction_dict(self):
        proposals = [_proposal_via_pred("long"), _proposal_via_pred("neutral")]
        assert count_valid_voters(proposals) == 1

    def test_zero_weight_neutral_not_counted(self):
        proposals = [_proposal("long", vote_weight=0.0), _proposal("neutral", vote_weight=0.0)]
        assert count_valid_voters(proposals) == 0


class TestCheckMinValidBrains:
    def test_enough_voters(self):
        proposals = [_proposal("long"), _proposal("short"), _proposal("long")]
        assert check_min_valid_brains(proposals, min_valid_brains=2) == 0  # passes

    def test_insufficient_voters(self):
        proposals = [_proposal("long")]
        assert check_min_valid_brains(proposals, min_valid_brains=3) == 1  # blocks

    def test_zero_voters_passes(self):
        """All neutral → zero voters → passes (let consensus decide neutral)."""
        proposals = [_proposal("neutral"), _proposal("neutral")]
        assert check_min_valid_brains(proposals, min_valid_brains=2) == 0

    def test_exact_threshold(self):
        proposals = [_proposal("long"), _proposal("short")]
        assert check_min_valid_brains(proposals, min_valid_brains=2) == 0


# ── extract_entry_z_score ─────────────────────────────────────────────────


class TestExtractEntryZScore:
    def test_empty_proposals(self):
        assert extract_entry_z_score([]) == (0.0, 0.0)

    def test_no_ou_brain(self):
        p = SimpleNamespace(raw_score=0.0, diagnostics={})
        assert extract_entry_z_score([p]) == (0.0, 0.0)

    def test_valid_z_score(self):
        p = SimpleNamespace(raw_score=-2.35, diagnostics={"half_life": 12.0})
        z, hl = extract_entry_z_score([p])
        assert z == -2.35
        assert hl == 12.0

    def test_breaks_on_first_half_life(self):
        p1 = SimpleNamespace(raw_score=-2.0, diagnostics={"half_life": 10.0})
        p2 = SimpleNamespace(raw_score=-3.0, diagnostics={"half_life": 20.0})
        z, hl = extract_entry_z_score([p1, p2])
        assert z == -2.0  # first proposal's z
        assert hl == 10.0  # breaks on p1

    def test_handles_missing_raw_score(self):
        p = SimpleNamespace(raw_score=None, diagnostics={"half_life": 5.0})
        z, hl = extract_entry_z_score([p])
        assert z == 0.0
        assert hl == 5.0
