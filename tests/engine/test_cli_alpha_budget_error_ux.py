"""CLI error UX tests for Alpha budget artifacts."""

import json

from apps.engine.cli import main
from core.runtime.schema_versions import SCHEMA_CLI_ERROR


def test_runtime_run_paper_missing_alpha_risk_budget_returns_json_error(tmp_path, capsys):
    missing = tmp_path / "missing_alpha_risk_budget.json"
    code = main(
        [
            "--base-dir",
            str(tmp_path),
            "runtime",
            "run-paper",
            "--feature",
            "ema_bias=2.0",
            "--price",
            "2000",
            "--alpha-risk-budget",
            str(missing),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["schema_version"] == SCHEMA_CLI_ERROR
    assert payload["error"] == "file_not_found"
    assert payload["path"] == str(missing)


def test_runtime_run_paper_malformed_alpha_risk_budget_returns_json_error(tmp_path, capsys):
    malformed = tmp_path / "malformed_alpha_risk_budget.json"
    malformed.write_text("{not-json", encoding="utf-8")
    code = main(
        [
            "--base-dir",
            str(tmp_path),
            "runtime",
            "run-paper",
            "--feature",
            "ema_bias=2.0",
            "--price",
            "2000",
            "--alpha-risk-budget",
            str(malformed),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["schema_version"] == SCHEMA_CLI_ERROR
    assert payload["error"] == "json_decode_error"
    assert payload["path"] == str(malformed)


def test_alpha_budget_usage_malformed_file_returns_json_error(tmp_path, capsys):
    malformed = tmp_path / "malformed_usage.json"
    malformed.write_text("{not-json", encoding="utf-8")
    code = main(
        ["--base-dir", str(tmp_path), "alpha", "budget-usage", "--usage-file", str(malformed)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["schema_version"] == SCHEMA_CLI_ERROR
    assert payload["error"] == "json_decode_error"
    assert payload["path"] == str(malformed)
