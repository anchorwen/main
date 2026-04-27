"""Alpha budget operator end-to-end smoke tests."""
import json

from apps.engine.cli import main
from core.alpha.schema_versions import SCHEMA_ALPHA_RISK_BUDGET


def test_alpha_budget_operator_workflow_smoke(tmp_path, capsys):
    risk_budget = tmp_path / "alpha_risk_budget.json"
    usage = tmp_path / "alpha_budget_usage.json"

    assert main([
        "--base-dir", str(tmp_path),
        "alpha", "register",
        "--alpha-id", "alpha1",
        "--name", "Alpha One",
        "--state", "active",
    ]) == 0
    assert main([
        "--base-dir", str(tmp_path),
        "alpha", "performance",
        "--alpha-id", "alpha1",
        "--metric", "signal_count=10",
        "--metric", "order_count=10",
        "--metric", "fill_ratio=1.0",
        "--metric", "denied_count=0",
        "--metric", "average_slippage_bps=1.0",
        "--metric", "orders_per_signal=1.0",
    ]) == 0
    capsys.readouterr()

    export_code = main([
        "--base-dir", str(tmp_path),
        "alpha", "export-risk-budget",
        "--total-notional", "100000",
        "--output", str(risk_budget),
    ])
    exported = json.loads(capsys.readouterr().out)
    assert export_code == 0
    assert exported["schema_version"] == SCHEMA_ALPHA_RISK_BUDGET
    assert exported["budgets"]["alpha1"]["enabled"] is True
    assert risk_budget.exists()

    run_code = main([
        "--base-dir", str(tmp_path),
        "runtime", "run-paper",
        "--cycle-id", "cycle_operator_smoke",
        "--feature", "ema_bias=2.0",
        "--price", "2000",
        "--base-quantity", "0.1",
        "--alpha-risk-budget", str(risk_budget),
        "--alpha-budget-usage", str(usage),
    ])
    run_payload = json.loads(capsys.readouterr().out)
    assert run_code == 0
    assert run_payload["completed"] is True
    assert run_payload["order_count"] == 1
    assert run_payload["approval_count"] == 3

    usage_code = main([
        "--base-dir", str(tmp_path),
        "alpha", "budget-usage",
        "--usage-file", str(usage),
    ])
    usage_payload = json.loads(capsys.readouterr().out)
    assert usage_code == 0
    assert usage_payload["counts"] == {"alpha1": 1}

    inspect_code = main([
        "--base-dir", str(tmp_path),
        "runtime", "inspect",
        "--cycle-id", "cycle_operator_smoke",
    ])
    evidence = json.loads(capsys.readouterr().out)
    assert inspect_code == 0
    assert evidence["payload"]["approvals"][0]["gate"] == "alpha_risk_budget"
    assert evidence["payload"]["approvals"][0]["approved"] is True
    assert evidence["payload"]["quality_report"]["order_count"] == 1


def test_alpha_budget_operator_workflow_denies_after_daily_usage(tmp_path, capsys):
    risk_budget = tmp_path / "alpha_risk_budget.json"
    usage = tmp_path / "alpha_budget_usage.json"

    assert main([
        "--base-dir", str(tmp_path),
        "alpha", "register",
        "--alpha-id", "alpha1",
        "--name", "Alpha One",
        "--state", "active",
    ]) == 0
    assert main([
        "--base-dir", str(tmp_path),
        "alpha", "performance",
        "--alpha-id", "alpha1",
        "--metric", "signal_count=10",
        "--metric", "order_count=10",
        "--metric", "fill_ratio=1.0",
        "--metric", "denied_count=0",
        "--metric", "average_slippage_bps=1.0",
        "--metric", "orders_per_signal=1.0",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--base-dir", str(tmp_path),
        "alpha", "export-risk-budget",
        "--total-notional", "10000",
        "--output", str(risk_budget),
    ]) == 0
    budget_payload = json.loads(risk_budget.read_text(encoding="utf-8"))
    budget_payload["budgets"]["alpha1"]["max_daily_orders"] = 1
    risk_budget.write_text(json.dumps(budget_payload, indent=2), encoding="utf-8")
    capsys.readouterr()

    assert main([
        "--base-dir", str(tmp_path),
        "runtime", "run-paper",
        "--cycle-id", "cycle_operator_first",
        "--feature", "ema_bias=2.0",
        "--price", "2000",
        "--base-quantity", "0.1",
        "--alpha-risk-budget", str(risk_budget),
        "--alpha-budget-usage", str(usage),
    ]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["order_count"] == 1

    assert main([
        "--base-dir", str(tmp_path),
        "runtime", "run-paper",
        "--cycle-id", "cycle_operator_second",
        "--feature", "ema_bias=2.0",
        "--price", "2000",
        "--base-quantity", "0.1",
        "--alpha-risk-budget", str(risk_budget),
        "--alpha-budget-usage", str(usage),
    ]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["order_count"] == 0
    assert second["skipped_count"] == 1

    assert main([
        "--base-dir", str(tmp_path),
        "runtime", "inspect",
        "--cycle-id", "cycle_operator_second",
    ]) == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["payload"]["approvals"][0]["reasons"] == ["alpha_daily_order_limit_exceeded(2>1)"]

    assert main([
        "--base-dir", str(tmp_path),
        "alpha", "budget-usage-reset",
        "--usage-file", str(usage),
    ]) == 0
    reset = json.loads(capsys.readouterr().out)
    assert reset["counts"] == {}
