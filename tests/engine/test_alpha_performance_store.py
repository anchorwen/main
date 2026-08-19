"""Alpha performance store tests."""

from typing import Any, cast

import pytest

from apps.engine.cli import main
from core.alpha.performance_store import AlphaPerformanceSnapshot, AlphaPerformanceStore
from core.alpha.schema_versions import (
    SCHEMA_ALPHA_PERFORMANCE_SNAPSHOT,
    SCHEMA_ALPHA_PERFORMANCE_STORE,
    SCHEMA_ALPHA_PERFORMANCE_SUMMARY,
)
from core.runtime.evidence_reader import RuntimeEvidenceReader
from core.runtime.summary_service import RuntimeSummaryService


def _run_paper(base_dir, cycle_id, feature_value):
    return main(
        [
            "--base-dir",
            str(base_dir),
            "runtime",
            "run-paper",
            "--cycle-id",
            cycle_id,
            "--feature",
            f"ema_bias={feature_value}",
            "--price",
            "2000",
        ]
    )


class TestAlphaPerformanceSnapshot:
    def test_snapshot_to_dict(self):
        snapshot = AlphaPerformanceSnapshot(
            "alpha1", {"fill_ratio": 1.0}, source="test", window="1d"
        )
        payload = snapshot.to_dict()
        assert payload["schema_version"] == SCHEMA_ALPHA_PERFORMANCE_SNAPSHOT
        assert payload["alpha_id"] == "alpha1"
        assert payload["metrics"]["fill_ratio"] == 1.0

    def test_snapshot_validation(self):
        with pytest.raises(ValueError):
            AlphaPerformanceSnapshot("", {})
        with pytest.raises(ValueError):
            AlphaPerformanceSnapshot(
                "alpha1", cast(dict[str, Any], [])
            )  # TECH_DEBT-009: 非法输入探针 list→dict 类型层绕过


class TestAlphaPerformanceStore:
    def test_record_latest_history_and_summary(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", {"fill_ratio": 0.5, "order_count": 1})
        latest = store.record_snapshot("alpha1", {"fill_ratio": 1.0, "order_count": 2})
        assert store.latest("alpha1") == latest
        assert len(store.history("alpha1")) == 2
        summary = store.summarize("alpha1")
        assert summary["schema_version"] == SCHEMA_ALPHA_PERFORMANCE_SUMMARY
        assert summary["snapshot_count"] == 2
        assert summary["aggregates"]["fill_ratio"]["average"] == 0.75
        assert summary["aggregates"]["order_count"]["max"] == 2.0

    def test_empty_summary(self):
        summary = AlphaPerformanceStore().summarize("missing")
        assert summary["snapshot_count"] == 0
        assert summary["latest"] is None
        assert summary["aggregates"] == {}

    def test_rank_by_latest_metric(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", {"score": 0.2})
        store.record_snapshot("alpha2", {"score": 0.9})
        store.record_snapshot("alpha3", {"other": 1.0})
        ranked = store.rank("score")
        assert [row["alpha_id"] for row in ranked] == ["alpha2", "alpha1"]
        assert store.rank("score", descending=False)[0]["alpha_id"] == "alpha1"

    def test_to_dict(self):
        store = AlphaPerformanceStore()
        store.record_snapshot("alpha1", {"score": 1.0})
        payload = store.to_dict()
        assert payload["schema_version"] == SCHEMA_ALPHA_PERFORMANCE_STORE
        assert payload["alpha_count"] == 1
        assert payload["summaries"][0]["alpha_id"] == "alpha1"

    def test_ingest_runtime_summary(self, tmp_path, capsys):
        assert _run_paper(tmp_path, "cycle_1", 2.0) == 0
        assert _run_paper(tmp_path, "cycle_2", -2.0) == 0
        capsys.readouterr()
        runtime_summary = RuntimeSummaryService(
            RuntimeEvidenceReader(str(tmp_path / "ledger"))
        ).summarize()
        store = AlphaPerformanceStore()
        snapshots = store.ingest_runtime_summary(runtime_summary, {"alpha1": "alpha_asset_1"})
        assert len(snapshots) == 1
        latest = store.latest("alpha_asset_1")
        assert latest is not None
        assert latest.source == "runtime_summary"
        assert latest.metrics["strategy_id"] == "alpha1"
        assert latest.metrics["signal_count"] == 2
        assert latest.metrics["order_count"] == 2
        assert latest.metrics["filled_order_count"] == 2
        assert latest.metrics["fill_ratio"] == 1.0
        assert latest.metrics["paper_cycles"] == 2
        assert latest.metrics["orders_per_signal"] == 1.0

    def test_ingest_live_bridge_report(self):
        store = AlphaPerformanceStore()
        report = {
            "date_key": "2026-04-28",
            "journal_path": "/tmp/journal.jsonl",
            "total": 4,
            "counts": {"accepted": 2, "acknowledged": 1, "rejected": 1, "other": 0},
            "acceptance_rate": 0.5,
            "rejection_rate": 0.25,
            "rejected_reasons": {"risk": 1},
            "live_consecutive_rejected_tail": 1,
        }
        snap = store.ingest_live_bridge_report(
            "alpha_x", report, journal_source_path="/data/j.jsonl"
        )
        assert snap.source == "live_bridge_report"
        latest = store.latest("alpha_x")
        assert latest is not None
        m = latest.metrics
        assert m["live_bridge"] is True
        assert m["live_total"] == 4
        assert m["live_accepted"] == 2
        assert m["live_rejected"] == 1
        assert m["journal_source_path"] == "/data/j.jsonl"
        assert m["live_rejected_reasons_top"]["risk"] == 1
        assert m["live_consecutive_rejected"] == 1
