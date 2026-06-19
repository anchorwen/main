"""Unit tests for brain_gates — pure functions extracted from strategy_line.py.

Strangler Fig #17: count_valid_voters + check_min_valid_brains.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.execution.brain_gates import check_min_valid_brains, count_valid_voters


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
