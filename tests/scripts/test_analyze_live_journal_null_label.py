"""Regression tests for scripts.analyze_live_journal — DQAF-20260709-001.

Locks the ingestion-boundary null-coalescing fix (FIX-20260709-001).  Live
journal closes legitimately carry ``label: null`` (the no-ticket orphan branch,
pre FIX-20260626-144 write-side hardening).  A bare ``dict.get(k, default)``
substitutes *default* ONLY when the key is absent — a present ``None`` value
flowed through as ``None``, became a ``pnl_by_label`` dict KEY, and crashed the
Section-3 report at ``format(None, '<55s')`` (TypeError).

These tests assert the fix at the ROOT (``analyze_journal`` output), not the
print site: no categorical field may reach a consumer as ``None``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

_SPEC = importlib.util.spec_from_file_location(
    "analyze_live_journal",
    Path(__file__).resolve().parents[2] / "scripts" / "analyze_live_journal.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_alj = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_alj)


class TestCoalesce:
    """The single root-cause primitive: present-but-null == missing."""

    def test_present_null_uses_default(self) -> None:
        assert _alj._coalesce({"label": None}, "label", "(unlabeled)") == "(unlabeled)"

    def test_missing_key_uses_default(self) -> None:
        assert _alj._coalesce({}, "label", "(unlabeled)") == "(unlabeled)"

    def test_present_value_preserved(self) -> None:
        assert _alj._coalesce({"label": "take_profit"}, "label", "(unlabeled)") == "take_profit"

    def test_falsy_non_null_preserved(self) -> None:
        # 0 / "" are legitimate values, NOT nulls — must not be coalesced.
        assert _alj._coalesce({"side": ""}, "side", "?") == ""


def _open(ticket: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "open",
        "position_ticket": ticket,
        "position_identifier": ticket,
        "side": "long",
        "recorded_at": "2026-05-08T10:00:00Z",
        "brain_ids": ["brain_X"],
    }
    base.update(kw)
    return base


def _close(ticket: int, pnl: float, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "close",
        "position_ticket": ticket,
        "position_identifier": ticket,
        "side": "long",
        "pnl": pnl,
        "ack_status": "filled",
        "recorded_at": "2026-05-08T11:00:00Z",
    }
    base.update(kw)
    return base


def _write_journal(data_dir: Path, records: list[dict[str, Any]]) -> None:
    with open(data_dir / "live_trade_journal.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class TestNullLabelReachesReportSafely:
    def test_null_label_bucketed_as_unlabeled(self, tmp_path: Path) -> None:
        _write_journal(
            tmp_path,
            [
                _open(4098814806),
                _close(4098814806, 0.68, label=None),  # present-but-null → the crash trigger
            ],
        )
        result = _alj.analyze_journal(tmp_path)

        labels = result["pnl_by_label"]
        # Root assertion: None must NEVER be a key (that is the :559 format crash precondition).
        assert None not in labels
        assert "(unlabeled)" in labels
        assert labels["(unlabeled)"]["count"] == 1
        assert labels["(unlabeled)"]["pnl_usd"] == 0.68

    def test_all_label_keys_are_str(self, tmp_path: Path) -> None:
        _write_journal(
            tmp_path,
            [
                _open(1001),
                _close(1001, 0.68, label=None),
                _open(1002),
                _close(1002, -5.0, label="stop_loss"),
                _open(1003),
                _close(1003, 3.0),  # label key entirely absent
            ],
        )
        result = _alj.analyze_journal(tmp_path)
        # Guards the ``{lbl:<55s}`` format precondition for every bucket.
        assert all(isinstance(k, str) for k in result["pnl_by_label"])
        assert "stop_loss" in result["pnl_by_label"]
        assert result["pnl_by_label"]["(unlabeled)"]["count"] == 2  # null + missing merge
