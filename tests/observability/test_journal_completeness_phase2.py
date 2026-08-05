"""Regression lock for check_journal_completeness Phase 2 semantics.

FIX-20260805-008 (DQAF-20260805-002): the temporary FIX-20260611-005 patch
is retired.  Dup detection now keys on the Phase 2 idempotent event identity
(position_identifier, deal_id) written by PositionClosed.to_journal_entry().

Contract under test:
  - Identical rewrites of the same event (retry residue) are metrics-only,
    NEVER a FAIL — a journal of purely historical double-writes must PASS.
  - >=2 distinct NON-ZERO deal_ids for the same position = event-stream
    ambiguity → FAIL (JOURNAL_SLA_VIOLATION / AMBIGUOUS_EVENTS).
  - deal_id=0 (rejected attempts / engine-side no-deal) is an absence, not a
    deal event — rejected-then-confirmed must NOT be flagged ambiguous.
  - close_price < 50% and trail < 10% thresholds retained as FAIL conditions.
  - The "[EXPIRES ...]" framing and "expires" metric are removed entirely.
"""

from __future__ import annotations

import json

from core.observability.data_health_schema import SourceCheckResult, SourceStatus
from core.observability.data_health_service import DataHealthService

RECORDED = "2026-07-01T00:00:00Z"


def _close(
    ticket: int,
    *,
    deal_id: int = 0,
    ack: str = "closed",
    close_price: float = 100.0,
    reason: str = "take_profit",
    pid: int | None = None,
) -> dict:
    """Build a v2 close journal entry (PositionClosed.to_journal_entry shape)."""
    return {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": RECORDED,
        "message_id": f"close_{ticket}_{deal_id}",
        "target": "exec_bridge",
        "ack_status": ack,
        "detail": {"reason": reason, "close_price": close_price, "deal_id": deal_id},
        "symbol": "BTCUSDc",
        "action": "close",
        "side": "buy",
        "volume": 0.01,
        "pnl": 1.0,
        "position_ticket": ticket,
        "position_identifier": ticket if pid is None else pid,
    }


def _modify() -> dict:
    """Build a modify_sltp entry (contributes to trail coverage)."""
    return {
        "action": "modify_sltp",
        "recorded_at": RECORDED,
        "position_ticket": 1,
        "message_id": f"mod_{RECORDED}",
    }


def _write_journal(tmp_path, close_entries: list[dict], modify_entries: int = 3) -> None:
    """Write a live_trade_journal.jsonl under tmp_path (LF bytes only)."""
    lines = []
    for _ in range(modify_entries):
        lines.append(json.dumps(_modify(), ensure_ascii=False))
    for entry in close_entries:
        lines.append(json.dumps(entry, ensure_ascii=False))
    (tmp_path / "live_trade_journal.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _run(tmp_path) -> SourceCheckResult:
    """Run only the journal_completeness check against the fixture dir."""
    svc = DataHealthService(base_dir=str(tmp_path), symbol="BTCUSDc")
    return svc.check_journal_completeness()


class TestResidueNeverFails:
    """Identical re-writes of the same event are metrics-only, no FAIL."""

    def test_double_write_same_deal_is_residue_not_fail(self, tmp_path) -> None:
        """Two identical close rows (same pid + same deal_id) → PASS."""
        _write_journal(
            tmp_path,
            [_close(1, deal_id=100), _close(1, deal_id=100)],
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.primary_code == "JOURNAL_SLA_OK"
        assert result.metrics["duplicates"] == 1
        assert result.metrics["ambiguous_events"] == 0

    def test_deal_zero_rejected_retries_are_residue_not_ambiguous(self, tmp_path) -> None:
        """Rejected retries (deal_id=0) + a real deal → NOT ambiguous."""
        _write_journal(
            tmp_path,
            [
                _close(1, deal_id=0, ack="rejected", close_price=0.0),
                _close(1, deal_id=500, close_price=50.0),
                _close(2, deal_id=600),
            ],
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.metrics["duplicates"] == 0
        assert result.metrics["ambiguous_events"] == 0

    def test_orphan_rows_excluded_from_dedup(self, tmp_path) -> None:
        """Synthetic auto_orphan_* rows never count toward residue/ambiguity."""
        _write_journal(
            tmp_path,
            [
                _close(1, deal_id=100, reason="auto_orphan_closed"),
                _close(1, deal_id=100, reason="auto_orphan_closed"),
                _close(2, deal_id=200),
            ],
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.metrics["duplicates"] == 0
        assert result.metrics["ambiguous_events"] == 0

    def test_unidentifiable_rows_metric_only(self, tmp_path) -> None:
        """Rows with no position identity are counted, not fatal."""
        unidentifiable = {
            "action": "close",
            "recorded_at": RECORDED,
            "ack_status": "closed",
            "detail": {"reason": "take_profit", "close_price": 50.0, "deal_id": 300},
            "position_ticket": None,
        }
        _write_journal(
            tmp_path,
            [_close(1, deal_id=100), unidentifiable],
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.metrics["unidentifiable_rows"] == 1


class TestAmbiguityFails:
    """>=2 distinct non-zero deal_ids for the same position → FAIL."""

    def test_two_distinct_deals_same_position_fails(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [_close(1, deal_id=100), _close(1, deal_id=200)],
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.FAIL
        assert result.primary_code == "JOURNAL_SLA_VIOLATION"
        assert "AMBIGUOUS_EVENTS=1" in result.message
        assert result.metrics["ambiguous_events"] == 1
        assert result.metrics["duplicates"] == 0


class TestRetainedThresholds:
    """close_price / trail FAIL conditions unchanged."""

    def test_low_close_price_rate_fails(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [
                _close(1, deal_id=100, close_price=0.0),
                _close(2, deal_id=200, close_price=0.0),
            ],
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.FAIL
        assert "CLOSE_PRICE_RATE" in result.message

    def test_low_trail_rate_fails(self, tmp_path) -> None:
        _write_journal(tmp_path, [_close(1, deal_id=100), _close(2, deal_id=200)], modify_entries=0)
        result = _run(tmp_path)

        assert result.status == SourceStatus.FAIL
        assert "TRAIL_RATE" in result.message


class TestExpiryRemoved:
    """The 2026-07-11 temporary-patch framing is fully gone."""

    def test_fail_message_has_no_expires(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [_close(1, deal_id=100), _close(1, deal_id=200)],
        )
        result = _run(tmp_path)

        assert "EXPIRES" not in result.message
        assert "expires" not in result.metrics

    def test_pass_message_has_no_expires(self, tmp_path) -> None:
        _write_journal(tmp_path, [_close(1, deal_id=100), _close(2, deal_id=200)])
        result = _run(tmp_path)

        assert "EXPIRES" not in result.message
        assert "expires" not in result.metrics


class TestCleanJournal:
    def test_healthy_journal_passes(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            [_close(1, deal_id=100), _close(2, deal_id=200)],
        )
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert result.primary_code == "JOURNAL_SLA_OK"
        assert result.metrics["duplicates"] == 0
        assert result.metrics["ambiguous_events"] == 0
