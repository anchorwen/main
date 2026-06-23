"""Tests for core.runtime.reconciliation — position reconciliation.

FIX-20260619-039: Tier 1 zero-coverage breakout #10.
Tests label-write path without MT5 dependency.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.runtime.reconciliation import reconcile_closed_positions


class TestReconcileClosedPositions:
    def test_empty_tickets_returns_empty(self) -> None:
        """No known tickets means nothing to reconcile."""
        mock_worker = MagicMock()
        mock_worker.positions_get.return_value = []
        result = reconcile_closed_positions(
            mt5_worker=mock_worker,
            known_tickets={},
            symbol="XAUUSDc",
            journal_path="/fake/path.jsonl",
            state=None,
        )
        assert result == []
