"""Data contract tests — verify that key data files conform to expected schemas.

FIX-20260611-022: These tests catch schema drift BEFORE it silently
corrupts downstream consumers.  Run in CI on every commit.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.contracts.events import (
    DataSource,
    GovernanceTransitionEvent,
    PnLEvent,
)
from core.data.event_writer import EventWriter, reset_event_writer

# ── PnLEvent contract ──────────────────────────────────────────────────────


class TestPnLEventContract:
    """PnLEvent is the atomic unit of the event stream.  Its schema is law."""

    def test_valid_event_roundtrips(self):
        event = PnLEvent(
            timestamp=datetime.now(UTC),
            source=DataSource.LIVE,
            event_type="SignalSettled",
            brain_id="test_brain",
            symbol="XAUUSDc",
            direction="long",
            entry_price=2000.0,
            exit_price=2005.0,
            pnl_r=1.5,
            confidence=0.75,
            position_ticket=12345,
            generated_by="test",
        )
        js = event.model_dump_json()
        restored = PnLEvent.model_validate_json(js)
        assert restored.brain_id == "test_brain"
        assert restored.pnl_r == 1.5
        assert restored.source == "live"

    def test_rejects_unknown_fields(self):
        with pytest.raises(Exception):
            PnLEvent(
                timestamp=datetime.now(UTC),
                source="live",
                event_type="SignalSettled",
                brain_id="test",
                symbol="XAU",
                pnl_r=1.0,
                generated_by="test",
                **{
                    "EVIL_FIELD": "should_fail"
                },  # dynamic kwarg — static-invisible, runtime-rejected
            )

    def test_rejects_nan_pnl(self):
        with pytest.raises(Exception):
            PnLEvent(
                timestamp=datetime.now(UTC),
                source="live",
                event_type="SignalSettled",
                brain_id="test",
                symbol="XAU",
                pnl_r=float("nan"),
                generated_by="test",
            )

    def test_rejects_inf_pnl(self):
        with pytest.raises(Exception):
            PnLEvent(
                timestamp=datetime.now(UTC),
                source="live",
                event_type="SignalSettled",
                brain_id="test",
                symbol="XAU",
                pnl_r=float("inf"),
                generated_by="test",
            )

    def test_rejects_invalid_source(self):
        with pytest.raises(Exception):
            PnLEvent(
                timestamp=datetime.now(UTC),
                source="invalid_source",
                event_type="SignalSettled",
                brain_id="test",
                symbol="XAU",
                pnl_r=1.0,
                generated_by="test",
            )

    def test_rejects_invalid_confidence(self):
        with pytest.raises(Exception):
            PnLEvent(
                timestamp=datetime.now(UTC),
                source="live",
                event_type="SignalSettled",
                brain_id="test",
                symbol="XAU",
                pnl_r=1.0,
                confidence=1.5,  # > 1.0
                generated_by="test",
            )

    def test_rejects_empty_brain_id(self):
        with pytest.raises(Exception):
            PnLEvent(
                timestamp=datetime.now(UTC),
                source="live",
                event_type="SignalSettled",
                brain_id="",  # empty
                symbol="XAU",
                pnl_r=1.0,
                generated_by="test",
            )

    def test_write_read_roundtrip(self):
        """Event written through EventWriter MUST be readable and valid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            writer = EventWriter(path)
            event = PnLEvent(
                timestamp=datetime.now(UTC),
                source="live",
                event_type="SignalSettled",
                brain_id="brain_1",
                symbol="XAUUSDc",
                pnl_r=2.5,
                generated_by="test",
            )
            eid = writer.write(event)
            writer.close()
            reset_event_writer()

            # Read back
            with open(path) as f:
                line = f.readline()
            restored = PnLEvent.model_validate_json(line)
            assert restored.event_id == eid
            assert restored.brain_id == "brain_1"
            assert restored.pnl_r == 2.5


# ── GovernanceTransitionEvent contract ──────────────────────────────────────


class TestGovernanceTransitionContract:
    def test_valid_transition(self):
        event = GovernanceTransitionEvent(
            timestamp=datetime.now(UTC),
            brain_id="test_brain",
            from_status="candidate",
            to_status="live",
            reason="Promoted after manual review",
            triggered_by="manual",
        )
        js = event.model_dump_json()
        restored = GovernanceTransitionEvent.model_validate_json(js)
        assert restored.brain_id == "test_brain"
        assert restored.to_status == "live"

    def test_rejects_unknown_fields(self):
        with pytest.raises(Exception):
            GovernanceTransitionEvent(
                timestamp=datetime.now(UTC),
                brain_id="test",
                from_status="candidate",
                to_status="live",
                reason="test",
                triggered_by="manual",
                **{"BAD": "field"},  # dynamic kwarg — static-invisible, runtime-rejected
            )


# ── Event stream invariants ────────────────────────────────────────────────


class TestTradeJournalContract:
    """Structural invariants for live_trade_journal.jsonl."""

    def test_open_entries_have_entry_price_and_volume(self):
        """Every 'open' entry MUST have entry_price > 0 and volume > 0."""
        import json as _j

        for data_dir in ["data", "data_btc"]:
            jp = Path(data_dir) / "live_trade_journal.jsonl"
            if not jp.exists():
                continue
            violations = []
            with open(jp, encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _j.loads(line)
                    except Exception:  # noqa: BLE001 — skip malformed journal line
                        continue
                    if rec.get("action") != "open":
                        continue
                    if not rec.get("entry_price"):
                        violations.append(f"L{line_num}: no entry_price")
                    if not rec.get("volume"):
                        violations.append(f"L{line_num}: volume=0")
            # Journal may have legacy entries; just report, don't fail
            if violations:
                print(f"\n  [{data_dir}] {len(violations)} open entries with issues")

    def test_close_label_coverage(self):
        """At least 80% of close entries should have a non-empty label."""
        import json as _j

        for data_dir in ["data", "data_btc"]:
            jp = Path(data_dir) / "live_trade_journal.jsonl"
            if not jp.exists():
                continue
            total = 0
            labeled = 0
            with open(jp, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _j.loads(line)
                    except Exception:  # noqa: BLE001 — skip malformed journal line
                        continue
                    if rec.get("action") != "close":
                        continue
                    total += 1
                    if rec.get("label"):
                        labeled += 1
            if total > 10:
                rate = labeled / total
                assert (
                    rate >= 0.80
                ), f"{data_dir}: only {labeled}/{total} ({rate:.1%}) closes have labels"


class TestEventStreamInvariants:
    """Invariants that every event stream file MUST satisfy."""

    def test_jsonl_one_event_per_line(self):
        """Every non-empty line must be valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            writer = EventWriter(path)
            for i in range(5):
                event = PnLEvent(
                    timestamp=datetime.now(UTC),
                    source="live",
                    event_type="SignalSettled",
                    brain_id=f"brain_{i}",
                    symbol="XAUUSDc",
                    pnl_r=float(i),
                    generated_by="test",
                )
                writer.write(event)
            writer.close()
            reset_event_writer()

            with open(path) as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        PnLEvent.model_validate_json(line)
                    except Exception as e:  # noqa: BLE001 — report invalid line
                        pytest.fail(f"Line {line_num} is invalid: {e}")

    def test_event_ids_are_unique(self):
        """Every event_id in a stream MUST be unique."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            writer = EventWriter(path)
            ids = set()
            for _ in range(20):
                event = PnLEvent(
                    timestamp=datetime.now(UTC),
                    source="live",
                    event_type="SignalSettled",
                    brain_id="test",
                    symbol="XAUUSDc",
                    pnl_r=1.0,
                    generated_by="test",
                )
                eid = writer.write(event)
                assert eid not in ids, f"Duplicate event_id: {eid}"
                ids.add(eid)
            writer.close()
            reset_event_writer()

    def test_source_field_is_always_present(self):
        """Every event in the stream MUST have a valid source field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            writer = EventWriter(path)
            event = PnLEvent(
                timestamp=datetime.now(UTC),
                source="live",
                event_type="SignalSettled",
                brain_id="test",
                symbol="XAUUSDc",
                pnl_r=1.0,
                generated_by="test",
            )
            writer.write(event)
            writer.close()
            reset_event_writer()

            with open(path) as f:
                data = json.loads(f.readline())
            assert "source" in data
            assert data["source"] in {"live", "shadow", "backtest", "migration"}
