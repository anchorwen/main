"""Tests for resolve_identity — the canonical immutable-identity join key.

FIX-20260708-001 (Canonical Immutable-Identity Join Authority): open<->close
lifecycle legs must be paired on the IMMUTABLE ``position_identifier`` (stable
across MT5 re-ticketing on partial-close/netting), NOT the mutable
``position_ticket``.  These tests pin that contract so a future edit cannot
silently regress the join key back to the mutable ticket.
"""

from __future__ import annotations

from core.data.ticket_resolver import resolve, resolve_identity


def test_prefers_identifier_over_ticket() -> None:
    # Re-ticketed close: new mutable ticket, but identity == original open ticket.
    rec = {"position_identifier": 3946545788, "position_ticket": 3946550905}
    assert resolve_identity(rec) == 3946545788
    # resolve() (broker-facing) still yields the mutable ticket — roles stay split.
    assert resolve(rec) == 3946550905


def test_falls_back_to_ticket_for_legacy_records() -> None:
    # Legacy / pre-DQAF-033 record with no identifier → degrade to the ticket.
    assert resolve_identity({"position_ticket": 3946550905}) == 3946550905


def test_falls_back_through_detail_order() -> None:
    # No identifier and no top-level ticket → resolve()'s detail.order path.
    assert resolve_identity({"detail": {"order": 12345}}) == 12345


def test_zero_or_invalid_identifier_falls_back() -> None:
    # identifier defaulted to 0 (adapter fallback) must not shadow a real ticket.
    assert resolve_identity({"position_identifier": 0, "position_ticket": 777}) == 777
    assert resolve_identity({"position_identifier": -1, "position_ticket": 777}) == 777


def test_returns_none_when_nothing_resolvable() -> None:
    assert resolve_identity({"other": "data"}) is None
    assert resolve_identity({}) is None


def test_open_and_reticketed_close_resolve_to_same_key() -> None:
    # The core invariant: an open (identity == its ticket) and its later
    # re-ticketed close resolve to the SAME join key → no false orphan.
    open_leg = {"action": "open", "position_ticket": 100, "position_identifier": 100}
    close_leg = {"action": "close", "position_ticket": 205, "position_identifier": 100}
    assert resolve_identity(open_leg) == resolve_identity(close_leg) == 100


def test_open_without_identifier_still_matches_close_identity() -> None:
    # Adapter-written opens historically lacked position_identifier; they fall
    # back to their ticket (== original ticket == the close's identifier).
    open_leg = {"action": "open", "position_ticket": 100}
    close_leg = {"action": "close", "position_ticket": 205, "position_identifier": 100}
    assert resolve_identity(open_leg) == resolve_identity(close_leg) == 100
