"""Unit tests for JournalAccepted.entry_context field_validator (DLR-001).

Verifies that the write-boundary Layer 1 defense rejects incomplete
entry_context payloads before they enter the live trade journal.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.contracts.journal_contract import JournalAccepted


class TestEntryContextValidation:
    """Layer 1 write-boundary enforcement: entry_context.vector must be present."""

    def test_valid_entry_context_with_vector(self) -> None:
        """Happy path: entry_context has a non-empty vector list."""
        payload = {
            "action": "open",
            "symbol": "BTCUSDc",
            "side": "long",
            "message_id": "eq_btc_swing_test123",
            "position_ticket": 12345,
            "recorded_at": "2026-06-17T12:00:00Z",
            "entry_context": {
                "schema_version": "v9_institutional",
                "vector": [0.01, 0.02, 0.03],
                "entry_spread": 10.0,
                "bid": 65000.0,
                "ask": 65010.0,
            },
        }
        entry = JournalAccepted(**payload)
        assert entry.entry_context is not None
        assert entry.entry_context["vector"] == [0.01, 0.02, 0.03]

    def test_entry_context_none_is_tolerated(self) -> None:
        """entry_context=None is tolerated for backward compat (pre-DLR-001 entries)."""
        payload = {
            "action": "open",
            "symbol": "BTCUSDc",
            "side": "long",
            "message_id": "eq_btc_swing_test456",
            "position_ticket": 67890,
            "recorded_at": "2026-06-17T12:00:00Z",
            "entry_context": None,
        }
        entry = JournalAccepted(**payload)
        assert entry.entry_context is None

    def test_missing_vector_raises_validation_error(self) -> None:
        """entry_context exists but vector key is missing → ValidationError."""
        payload = {
            "action": "open",
            "symbol": "BTCUSDc",
            "side": "long",
            "message_id": "eq_btc_swing_test789",
            "position_ticket": 11111,
            "recorded_at": "2026-06-17T12:00:00Z",
            "entry_context": {
                "schema_version": "v9_institutional",
                "entry_spread": 10.0,
                # vector key intentionally missing
            },
        }
        with pytest.raises(ValidationError, match="MISSING"):
            JournalAccepted(**payload)

    def test_vector_none_raises_validation_error(self) -> None:
        """entry_context.vector is explicitly None → ValidationError."""
        payload = {
            "action": "open",
            "symbol": "BTCUSDc",
            "side": "long",
            "message_id": "eq_btc_swing_test222",
            "position_ticket": 22222,
            "recorded_at": "2026-06-17T12:00:00Z",
            "entry_context": {
                "schema_version": "v9_institutional",
                "vector": None,
            },
        }
        with pytest.raises(ValidationError, match="MISSING"):
            JournalAccepted(**payload)

    def test_empty_vector_list_raises_validation_error(self) -> None:
        """entry_context.vector is an empty list → ValidationError."""
        payload = {
            "action": "open",
            "symbol": "BTCUSDc",
            "side": "long",
            "message_id": "eq_btc_swing_test333",
            "position_ticket": 33333,
            "recorded_at": "2026-06-17T12:00:00Z",
            "entry_context": {
                "schema_version": "v9_institutional",
                "vector": [],
            },
        }
        with pytest.raises(ValidationError, match="EMPTY"):
            JournalAccepted(**payload)

    def test_entry_context_not_dict_raises_validation_error(self) -> None:
        """entry_context is not a dict → ValidationError."""
        payload = {
            "action": "open",
            "symbol": "BTCUSDc",
            "side": "long",
            "message_id": "eq_btc_swing_test444",
            "position_ticket": 44444,
            "recorded_at": "2026-06-17T12:00:00Z",
            "entry_context": "not_a_dict",
        }
        with pytest.raises(ValidationError, match="valid dictionary"):
            JournalAccepted(**payload)

    def test_full_realistic_entry_passes(self) -> None:
        """A complete realistic open entry (like ticket=3922330113) passes."""
        payload = {
            "action": "open",
            "symbol": "BTCUSDc",
            "strategy": "btc_swing",
            "magic": 90410,
            "side": "long",
            "volume": 0.01,
            "message_id": "eq_btc_swing_abc123def456",
            "position_ticket": 3922330113,
            "recorded_at": "2026-06-17T05:59:53.328825Z",
            "p_win": 0.42,
            "confidence": 0.55,
            "brain_ids": ["BTC_Swing_V12_H1_Survival"],
            "entry_context": {
                "schema_version": "v9_institutional",
                "vector": [0.0077, 1.0, 74.97, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                           0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                           0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                           0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "entry_spread": 10.0,
                "bid": 65773.46,
                "ask": 65783.46,
            },
        }
        entry = JournalAccepted(**payload)
        assert entry.entry_context is not None
        assert len(entry.entry_context["vector"]) == 40
