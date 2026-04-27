"""Runtime summary service and CLI tests."""
import json

from apps.engine.cli import main
from core.runtime.evidence_reader import RuntimeEvidenceReader
from core.runtime.schema_versions import SCHEMA_RUNTIME_SUMMARY
from core.runtime.summary_service import RuntimeSummaryService


def _run_paper(base_dir, cycle_id, feature_value):
    return main([
        "--base-dir", str(base_dir),
        "runtime", "run-paper",
        "--cycle-id", cycle_id,
        "--feature", f"ema_bias={feature_value}",
        "--price", "2000",
    ])


class TestRuntimeSummaryService:
    def test_summarize_runtime_cycles(self, tmp_path):
        assert _run_paper(tmp_path, "cycle_1", 2.0) == 0
        assert _run_paper(tmp_path, "cycle_2", -2.0) == 0
        summary = RuntimeSummaryService(RuntimeEvidenceReader(str(tmp_path / "ledger"))).summarize()
        assert summary["schema_version"] == SCHEMA_RUNTIME_SUMMARY
        assert summary["cycle_count"] == 2
        assert summary["totals"]["signals"] == 2
        assert summary["totals"]["orders"] == 2
        assert summary["totals"]["approvals"] == 4
        assert summary["totals"]["filled_orders"] == 2
        assert summary["averages"]["fill_ratio"] == 1.0
        assert summary["per_strategy"]["alpha1"]["signals"] == 2
        assert summary["per_strategy"]["alpha1"]["orders"] == 2
        assert summary["per_venue"]["PAPER"]["orders"] == 2

    def test_summary_limit(self, tmp_path):
        assert _run_paper(tmp_path, "cycle_1", 2.0) == 0
        assert _run_paper(tmp_path, "cycle_2", 2.0) == 0
        summary = RuntimeSummaryService(RuntimeEvidenceReader(str(tmp_path / "ledger"))).summarize(limit=1)
        assert summary["cycle_count"] == 1
        assert len(summary["cycles"]) == 1

    def test_empty_summary(self, tmp_path):
        summary = RuntimeSummaryService(RuntimeEvidenceReader(str(tmp_path / "ledger"))).summarize()
        assert summary["cycle_count"] == 0
        assert summary["latest_cycle_id"] is None
        assert summary["totals"]["orders"] == 0
        assert summary["averages"]["fill_ratio"] is None


class TestRuntimeSummaryCLI:
    def test_runtime_summary_cli(self, tmp_path, capsys):
        assert _run_paper(tmp_path, "cycle_1", 2.0) == 0
        capsys.readouterr()
        code = main(["--base-dir", str(tmp_path), "runtime", "summary"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["schema_version"] == SCHEMA_RUNTIME_SUMMARY
        assert payload["cycle_count"] == 1
        assert payload["totals"]["orders"] == 1

    def test_runtime_summary_cli_output_and_limit(self, tmp_path, capsys):
        assert _run_paper(tmp_path, "cycle_1", 2.0) == 0
        assert _run_paper(tmp_path, "cycle_2", 2.0) == 0
        capsys.readouterr()
        output = tmp_path / "reports" / "runtime_summary.json"
        code = main([
            "--base-dir", str(tmp_path),
            "runtime", "summary",
            "--limit", "1",
            "--output", str(output),
        ])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["cycle_count"] == 1
        assert json.loads(output.read_text(encoding="utf-8"))["cycle_count"] == 1
