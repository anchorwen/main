"""Tests for core.brains.schema_versions — schema version constants.

FIX-20260625-XXX: Tier 2 zero-coverage breakout #1.
"""

from __future__ import annotations

from core.brains.schema_versions import SCHEMA_BRAIN_DECISION_PROPOSAL


class TestSchemaVersions:
    def test_brain_decision_proposal_constant(self) -> None:
        assert SCHEMA_BRAIN_DECISION_PROPOSAL == "brain_decision_proposal.v1"
