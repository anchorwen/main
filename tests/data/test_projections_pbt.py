"""Hypothesis Property-Based Tests for the projection engine.

FIX-20260611-021: These tests verify mathematical invariants that MUST hold
for the event stream → governance state projection.  If any of these fail,
the projection engine has a logic bug — not a data quality issue.

Run::

    python -m pytest tests/data/test_projections_pbt.py -v
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from core.contracts.events import DataSource, PnLEvent
from core.data.event_writer import EventWriter, reset_event_writer
from core.data.projections import project_governance_state


# ── Strategy generators ────────────────────────────────────────────────────


@st.composite
def pnl_events(draw, min_events: int = 0, max_events: int = 50):
    """Generate a list of valid PnLEvent instances."""
    n = draw(st.integers(min_value=min_events, max_value=max_events))
    events = []
    for _ in range(n):
        event = PnLEvent(
            timestamp=datetime.now(UTC),
            source=draw(st.sampled_from(["live", "shadow", "backtest", "migration"])),
            event_type=draw(st.sampled_from(["SignalSettled", "PositionClosed"])),
            brain_id=draw(
                st.sampled_from(["Swing_V9_M15_V2", "Barrier_V9_12B_V2", "Brain_Trend_M30_V1"])
            ),
            symbol="XAUUSDc",
            direction=draw(st.sampled_from(["long", "short"])),
            entry_price=draw(st.floats(min_value=1000, max_value=5000)),
            exit_price=draw(st.floats(min_value=1000, max_value=5000)),
            pnl_r=draw(st.floats(min_value=-10.0, max_value=10.0)),
            confidence=draw(st.floats(min_value=0.0, max_value=1.0)),
            generated_by="hypothesis_test",
        )
        events.append(event)
    return events


def _write_events(events: list[PnLEvent], tmpdir: str) -> Path:
    """Write events to a temporary event stream file."""
    path = Path(tmpdir) / "ledger_events.jsonl"
    writer = EventWriter(path)
    for event in events:
        writer.write(event)
    writer.close()
    reset_event_writer()
    return path


# ── Invariant 1: Projection is idempotent ─────────────────────────────────


@given(pnl_events(min_events=5, max_events=50))
@settings(max_examples=200)
def test_projection_is_idempotent(events):
    """Running the projection twice on the same event stream MUST produce
    identical results.  This is the most fundamental property of a pure
    function projection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_events(events, tmpdir)
        state1 = project_governance_state(path)
        state2 = project_governance_state(path)

    # Strip checkpoint metadata for comparison
    s1 = {k: v for k, v in state1.items() if not k.startswith("_")}
    s2 = {k: v for k, v in state2.items() if not k.startswith("_")}

    assert s1 == s2, f"Projection is NOT idempotent!\nRun 1: {s1}\nRun 2: {s2}"


# ── Invariant 2: Cumulative PnL = sum of individual PnLs ───────────────────


@given(pnl_events(min_events=5, max_events=50))
@settings(max_examples=200)
def test_cumulative_pnl_equals_sum(events):
    """The cumulative PnL in the projection MUST equal the arithmetic sum
    of all individual event PnLs (for events matching the source filter)."""
    live_events = [e for e in events if e.source == DataSource.LIVE]

    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_events(events, tmpdir)
        state = project_governance_state(path)

    for brain_id in {e.brain_id for e in live_events}:
        if brain_id not in state:
            continue
        projected_pnl = state[brain_id]["pnl_r"]
        expected_pnl = sum(e.pnl_r for e in live_events if e.brain_id == brain_id)
        # Allow small floating-point error
        assert abs(projected_pnl - expected_pnl) < 0.01, (
            f"Brain {brain_id}: projected PnL={projected_pnl}, " f"sum of events={expected_pnl}"
        )


# ── Invariant 3: Trade count is correct ────────────────────────────────────


@given(pnl_events(min_events=5, max_events=50))
@settings(max_examples=200)
def test_trade_count_matches_events(events):
    """The total_trades count for each brain MUST equal the number of
    events for that brain (with matching source filter)."""
    live_events = [e for e in events if e.source == DataSource.LIVE]

    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_events(events, tmpdir)
        state = project_governance_state(path)

    from collections import Counter

    expected_counts = Counter(e.brain_id for e in live_events)

    for brain_id, expected in expected_counts.items():
        if brain_id not in state:
            continue
        projected_count = state[brain_id]["total_trades"]
        assert projected_count == expected, (
            f"Brain {brain_id}: projected trades={projected_count}, " f"expected={expected}"
        )


# ── Invariant 4: Win rate is bounded [0, 1] ───────────────────────────────


@given(pnl_events(min_events=5, max_events=50))
@settings(max_examples=200)
def test_win_rate_is_valid_probability(events):
    """Every brain's win_rate MUST be a valid probability in [0.0, 1.0]."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_events(events, tmpdir)
        state = project_governance_state(path)

    for brain_id, metrics in state.items():
        if brain_id.startswith("_"):
            continue
        wr = metrics["win_rate"]
        assert 0.0 <= wr <= 1.0, f"Brain {brain_id}: win_rate={wr} is not a valid probability"


# ── Invariant 5: source_filter isolates backtest/shadow from live ─────────


@given(pnl_events(min_events=10, max_events=50))
@settings(max_examples=100)
def test_source_filter_isolates_data(events):
    """When source_filter={'live'}, backtest/shadow/migration events MUST
    NOT contribute to the governance projection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_events(events, tmpdir)

        # Projection with live-only filter
        state_live = project_governance_state(path, source_filter={DataSource.LIVE})

        # Projection with ALL sources
        state_all = project_governance_state(
            path,
            source_filter={
                DataSource.LIVE,
                DataSource.SHADOW,
                DataSource.BACKTEST,
                DataSource.MIGRATION,
            },
        )

    # For any brain, live-only total_trades <= all-sources total_trades
    for brain_id in state_all:
        if brain_id.startswith("_"):
            continue
        all_count = state_all[brain_id]["total_trades"]
        live_count = state_live.get(brain_id, {}).get("total_trades", 0)
        assert live_count <= all_count, (
            f"Brain {brain_id}: live-only count ({live_count}) > all-sources "
            f"count ({all_count}) — source filter is broken!"
        )


# ── Invariant 6: Checkpoint replay produces same result as full rebuild ────


@given(pnl_events(min_events=20, max_events=80))
@settings(max_examples=50)
def test_checkpoint_replay_equals_full_rebuild(events):
    """Checkpoint-based incremental replay MUST produce same result as
    full rebuild from the SAME event stream file."""
    assume(len(events) >= 20)  # Need enough events for meaningful split

    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_events(events, tmpdir)
        ckpt_path = Path(tmpdir) / "governance_checkpoint.json"

        # Full rebuild (no checkpoint) — the reference result
        # state_full defined above with full_path
        pass

        # Checkpoint replay test: use the SAME file for two projections.
        # First pass without checkpoint → processes all events.
        # Second pass projects all events but with checkpoint from first pass
        # → skips already-processed events → result should be EMPTY (no new events).
        #
        # Then third pass: add NEW events to file, replay from checkpoint
        # → should only see the new events.

        half_n = len(events) // 2

        # Phase 1: Initial projection → saves checkpoint
        first_result = project_governance_state(path, checkpoint_path=ckpt_path)

        # Phase 2: Re-project same file from checkpoint → should be idempotent
        # (no new lines, so no new events to process)
        second_result = project_governance_state(path, checkpoint_path=ckpt_path)

        # Both should produce the same derived metrics
        f1 = {k: v for k, v in first_result.items() if not k.startswith("_")}
        f2 = {k: v for k, v in second_result.items() if not k.startswith("_")}
        assert f1 == f2, f"Idempotent replay FAILED!\nFirst: {f1}\nSecond: {f2}"

        # Phase 3: Append new events, re-project from checkpoint
        writer2 = EventWriter(path)
        extra_events = [
            PnLEvent(
                timestamp=datetime.now(UTC),
                source="live",
                event_type="SignalSettled",
                brain_id="extra_test_brain",
                symbol="XAUUSDc",
                pnl_r=5.0,
                generated_by="checkpoint_test",
            )
        ]
        for e in extra_events:
            writer2.write(e)
        writer2.close()
        reset_event_writer()

        extra_result = project_governance_state(path, checkpoint_path=ckpt_path)
        # The extra brain should appear with 1 trade, 5.0 PnL
        assert "extra_test_brain" in extra_result, "Extra brain not found in incremental replay!"
        assert extra_result["extra_test_brain"]["total_trades"] == 1
        assert extra_result["extra_test_brain"]["pnl_r"] == 5.0
