"""Regression lock for the trade_journal trail-telemetry probe.

FIX-20260805-009 (DQAF-20260805-003, Sev 4): check_trade_journal probed for an
exact bare "trail" label key, but since FIX-20260612-003 the reconciliation
layer records trail-active exits as "sl_hit_trailed".  The exact-key probe
never matched → TRAIL_TELEMETRY_BLINDSPOT fired on every cycle despite healthy
telemetry.

Contract under test:
  - Any trail-related label (e.g. sl_hit_trailed) in the tail → NO warning.
  - No trail-related label at all → warning retained (probe stays honest).
  - Probe stays gated above 10 closes (unchanged).
"""

from __future__ import annotations

import json

from core.observability.data_health_schema import SourceCheckResult, SourceStatus
from core.observability.data_health_service import DataHealthService


def _close(ticket: int, label: str, pnl: float = 1.0) -> dict:
    """Close row shaped for _safe_jsonl_tail_stats (top-level label/pnl)."""
    return {
        "action": "close",
        "recorded_at": "2026-07-01T00:00:00Z",
        "position_ticket": ticket,
        "ack_status": "closed",
        "label": label,
        "pnl": pnl,
        "detail": {"reason": "sl_hit", "close_price": 100.0, "deal_id": 100 + ticket},
    }


def _closes(labels: list[str]) -> list[dict]:
    return [_close(i + 1, lab) for i, lab in enumerate(labels)]


def _write_journal(tmp_path, close_entries: list[dict]) -> None:
    """Write a live_trade_journal.jsonl under tmp_path (LF bytes only)."""
    lines = [json.dumps(e, ensure_ascii=False) for e in close_entries]
    (tmp_path / "live_trade_journal.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _run(tmp_path) -> SourceCheckResult:
    """Run only the trade_journal check against the fixture dir."""
    svc = DataHealthService(base_dir=str(tmp_path), symbol="BTCUSDc")
    return svc.check_trade_journal()


class TestTrailProbeInclusiveMatch:
    """sl_hit_trailed (the FIX-20260612-003 label) satisfies the probe."""

    def test_sl_hit_trailed_present_suppresses_warning(self, tmp_path) -> None:
        _write_journal(tmp_path, _closes(["sl_hit_first"] * 11 + ["sl_hit_trailed"]))
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert "TRAIL_TELEMETRY_BLINDSPOT" not in result.message

    def test_mixed_trail_labels_suppress_warning(self, tmp_path) -> None:
        _write_journal(
            tmp_path,
            _closes(["sl_hit_first", "sl_hit_trailed", "tp_hit_first"] * 5),
        )
        result = _run(tmp_path)

        assert "TRAIL_TELEMETRY_BLINDSPOT" not in result.message


class TestTrailProbeStillHonest:
    """Absence of ANY trail label keeps the warning (probe stays honest)."""

    def test_no_trail_label_keeps_warning(self, tmp_path) -> None:
        _write_journal(tmp_path, _closes(["sl_hit_first"] * 12))
        result = _run(tmp_path)

        assert result.status == SourceStatus.PASS
        assert "TRAIL_TELEMETRY_BLINDSPOT" in result.message

    def test_warning_gated_on_close_count(self, tmp_path) -> None:
        _write_journal(tmp_path, _closes(["sl_hit_first"] * 5))
        result = _run(tmp_path)

        assert "TRAIL_TELEMETRY_BLINDSPOT" not in result.message
