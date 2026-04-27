"""Alpha Factory CLI tests."""
from datetime import date
import json

from apps.engine.cli import main
from core.alpha.schema_versions import SCHEMA_ALPHA_PORTFOLIO_ALLOCATION, SCHEMA_ALPHA_RISK_BUDGET
from core.runtime.schema_versions import (
    SCHEMA_ALPHA_BATCH_EVALUATION,
    SCHEMA_ALPHA_BUDGET_USAGE,
    SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
    SCHEMA_ALPHA_RUNTIME_INGESTION,
)


def test_alpha_register_and_list(tmp_path, capsys):
    code = main([
        "--base-dir", str(tmp_path),
        "alpha", "register",
        "--alpha-id", "alpha1",
        "--name", "Alpha One",
        "--version", "1.0",
        "--strategy-id", "strategy1",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alpha_id"] == "alpha1"
    assert payload["strategy_id"] == "strategy1"

    code = main(["--base-dir", str(tmp_path), "alpha", "list"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alpha_count"] == 1
    assert payload["records"][0]["alpha_id"] == "alpha1"


def test_alpha_register_requires_fields(tmp_path, capsys):
    code = main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["error"] == "--alpha-id and --name are required for alpha register"


def test_alpha_transition_persists_state(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1", "--name", "Alpha One"]) == 0
    capsys.readouterr()
    code = main([
        "--base-dir", str(tmp_path),
        "alpha", "transition",
        "--alpha-id", "alpha1",
        "--to-state", "backtest_passed",
        "--reason", "unit_test",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["record"]["state"] == "backtest_passed"
    assert payload["transitions"][0]["reason"] == "unit_test"

    code = main(["--base-dir", str(tmp_path), "alpha", "list"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["records"][0]["state"] == "backtest_passed"


def test_alpha_performance_records_and_persists(tmp_path, capsys):
    code = main([
        "--base-dir", str(tmp_path),
        "alpha", "performance",
        "--alpha-id", "alpha1",
        "--metric", "fill_ratio=1.0",
        "--metric", "order_count=3",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["snapshot_count"] == 1
    assert payload["latest"]["metrics"]["order_count"] == 3.0

    code = main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha1"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["snapshot_count"] == 1
    assert payload["aggregates"]["fill_ratio"]["latest"] == 1.0


def test_alpha_evaluate_and_apply(tmp_path, capsys):
    assert main([
        "--base-dir", str(tmp_path),
        "alpha", "register",
        "--alpha-id", "alpha1",
        "--name", "Alpha One",
        "--state", "backtest_passed",
    ]) == 0
    assert main([
        "--base-dir", str(tmp_path),
        "alpha", "performance",
        "--alpha-id", "alpha1",
        "--metric", "signal_count=5",
        "--metric", "order_count=5",
        "--metric", "fill_ratio=1.0",
        "--metric", "denied_count=0",
        "--metric", "paper_cycles=3",
        "--metric", "average_slippage_bps=1.0",
    ]) == 0
    capsys.readouterr()

    code = main(["--base-dir", str(tmp_path), "alpha", "evaluate", "--alpha-id", "alpha1"])
    decision = json.loads(capsys.readouterr().out)
    assert code == 0
    assert decision["action"] == "promote"
    assert decision["target_state"] == "paper_trading"

    code = main(["--base-dir", str(tmp_path), "alpha", "evaluate", "--alpha-id", "alpha1", "--apply"])
    decision = json.loads(capsys.readouterr().out)
    assert code == 0
    assert decision["approved"] is True

    code = main(["--base-dir", str(tmp_path), "alpha", "list"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["records"][0]["state"] == "paper_trading"


def test_alpha_evaluate_missing_id(tmp_path, capsys):
    code = main(["--base-dir", str(tmp_path), "alpha", "evaluate"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["error"] == "--alpha-id is required for alpha evaluate"


def test_alpha_output_file(tmp_path, capsys):
    output = tmp_path / "reports" / "alpha_list.json"
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1", "--name", "Alpha One"]) == 0
    capsys.readouterr()
    code = main(["--base-dir", str(tmp_path), "alpha", "list", "--output", str(output)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alpha_count"] == 1
    assert json.loads(output.read_text(encoding="utf-8"))["alpha_count"] == 1



def test_alpha_ingest_runtime_creates_performance_snapshot(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "runtime", "run-paper", "--cycle-id", "cycle_1", "--feature", "ema_bias=2.0", "--price", "2000"]) == 0
    assert main(["--base-dir", str(tmp_path), "runtime", "run-paper", "--cycle-id", "cycle_2", "--feature", "ema_bias=-2.0", "--price", "2000"]) == 0
    capsys.readouterr()
    code = main(["--base-dir", str(tmp_path), "alpha", "ingest-runtime", "--strategy-id", "alpha1", "--alpha-id", "alpha_asset_1"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == SCHEMA_ALPHA_RUNTIME_INGESTION
    assert payload["runtime_cycle_count"] == 2
    assert payload["snapshot_count"] == 1
    assert payload["snapshots"][0]["alpha_id"] == "alpha_asset_1"
    assert payload["snapshots"][0]["metrics"]["order_count"] == 2
    code = main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha_asset_1"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["snapshot_count"] == 1
    assert summary["latest"]["metrics"]["fill_ratio"] == 1.0


def test_alpha_ingest_runtime_without_mapping_uses_strategy_id(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "runtime", "run-paper", "--cycle-id", "cycle_1", "--feature", "ema_bias=2.0", "--price", "2000"]) == 0
    capsys.readouterr()
    code = main(["--base-dir", str(tmp_path), "alpha", "ingest-runtime"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["snapshots"][0]["alpha_id"] == "alpha1"


def test_alpha_ingest_runtime_requires_alpha_id_when_strategy_id_provided(tmp_path, capsys):
    code = main(["--base-dir", str(tmp_path), "alpha", "ingest-runtime", "--strategy-id", "alpha1"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["error"] == "--alpha-id is required when --strategy-id is provided for alpha ingest-runtime"


def test_alpha_ingest_runtime_then_evaluate_apply(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha_asset_1", "--name", "Alpha Asset", "--state", "backtest_passed"]) == 0
    for idx in range(2):
        assert main(["--base-dir", str(tmp_path), "runtime", "run-paper", "--cycle-id", f"cycle_{idx}", "--feature", "ema_bias=2.0", "--price", "2000"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "ingest-runtime", "--strategy-id", "alpha1", "--alpha-id", "alpha_asset_1"]) == 0
    capsys.readouterr()
    code = main(["--base-dir", str(tmp_path), "alpha", "evaluate", "--alpha-id", "alpha_asset_1", "--apply"])
    decision = json.loads(capsys.readouterr().out)
    assert code == 0
    assert decision["target_state"] == "paper_trading"
    code = main(["--base-dir", str(tmp_path), "alpha", "list"])
    registry = json.loads(capsys.readouterr().out)
    assert code == 0
    assert registry["records"][0]["state"] == "paper_trading"



def test_alpha_batch_evaluate_outputs_all_decisions(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1", "--name", "Alpha One", "--state", "backtest_passed"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha2", "--name", "Alpha Two", "--state", "active"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha1", "--metric", "signal_count=5", "--metric", "order_count=5", "--metric", "fill_ratio=1.0", "--metric", "denied_count=0", "--metric", "paper_cycles=3", "--metric", "average_slippage_bps=1.0"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha2", "--metric", "signal_count=5", "--metric", "order_count=5", "--metric", "fill_ratio=0.4", "--metric", "denied_count=0", "--metric", "paper_cycles=3", "--metric", "average_slippage_bps=1.0"]) == 0
    capsys.readouterr()
    code = main(["--base-dir", str(tmp_path), "alpha", "batch-evaluate"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == SCHEMA_ALPHA_BATCH_EVALUATION
    assert payload["alpha_count"] == 2
    assert payload["applied_count"] == 0
    by_alpha = {item["alpha_id"]: item for item in payload["decisions"]}
    assert by_alpha["alpha1"]["target_state"] == "paper_trading"
    assert by_alpha["alpha2"]["target_state"] == "throttled"


def test_alpha_batch_evaluate_apply_persists_lifecycle(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1", "--name", "Alpha One", "--state", "backtest_passed"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha1", "--metric", "signal_count=5", "--metric", "order_count=5", "--metric", "fill_ratio=1.0", "--metric", "denied_count=0", "--metric", "paper_cycles=3", "--metric", "average_slippage_bps=1.0"]) == 0
    capsys.readouterr()
    code = main(["--base-dir", str(tmp_path), "alpha", "batch-evaluate", "--apply"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["applied_count"] == 1
    assert payload["decisions"][0]["target_state"] == "paper_trading"
    code = main(["--base-dir", str(tmp_path), "alpha", "list"])
    registry = json.loads(capsys.readouterr().out)
    assert code == 0
    assert registry["records"][0]["state"] == "paper_trading"


def test_alpha_batch_evaluate_empty_registry(tmp_path, capsys):
    code = main(["--base-dir", str(tmp_path), "alpha", "batch-evaluate"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["alpha_count"] == 0
    assert payload["decisions"] == []



def test_alpha_allocate_cli(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1", "--name", "Alpha One", "--state", "active"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha2", "--name", "Alpha Two", "--state", "probation_live"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha1", "--metric", "signal_count=10", "--metric", "order_count=10", "--metric", "fill_ratio=1.0", "--metric", "denied_count=0", "--metric", "average_slippage_bps=1.0", "--metric", "orders_per_signal=1.0"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha2", "--metric", "signal_count=10", "--metric", "order_count=10", "--metric", "fill_ratio=1.0", "--metric", "denied_count=0", "--metric", "average_slippage_bps=1.0", "--metric", "orders_per_signal=1.0"]) == 0
    capsys.readouterr()
    code = main(["--base-dir", str(tmp_path), "alpha", "allocate", "--total-notional", "1000"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == SCHEMA_ALPHA_PORTFOLIO_ALLOCATION
    assert payload["alpha_count"] == 2
    assert payload["allocatable_count"] == 2
    assert round(sum(row["target_weight"] for row in payload["recommendations"]), 6) == 1.0
    assert round(sum(row["max_notional"] for row in payload["recommendations"]), 2) == 1000.0


def test_alpha_allocate_output_file(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1", "--name", "Alpha One", "--state", "active"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha1", "--metric", "signal_count=10", "--metric", "order_count=10", "--metric", "fill_ratio=1.0", "--metric", "denied_count=0", "--metric", "average_slippage_bps=1.0", "--metric", "orders_per_signal=1.0"]) == 0
    capsys.readouterr()
    output = tmp_path / "reports" / "allocation.json"
    code = main(["--base-dir", str(tmp_path), "alpha", "allocate", "--output", str(output)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["allocatable_count"] == 1
    assert json.loads(output.read_text(encoding="utf-8"))["allocatable_count"] == 1



def test_alpha_export_risk_budget_cli(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1", "--name", "Alpha One", "--state", "active"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha1", "--metric", "signal_count=10", "--metric", "order_count=10", "--metric", "fill_ratio=1.0", "--metric", "denied_count=0", "--metric", "average_slippage_bps=1.0", "--metric", "orders_per_signal=1.0"]) == 0
    capsys.readouterr()
    output = tmp_path / "risk" / "alpha_risk_budget.json"
    code = main(["--base-dir", str(tmp_path), "alpha", "export-risk-budget", "--total-notional", "1000", "--output", str(output)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == SCHEMA_ALPHA_RISK_BUDGET
    assert payload["budget_count"] == 1
    assert payload["budgets"]["alpha1"]["enabled"] is True
    assert payload["budgets"]["alpha1"]["max_notional"] == 1000.0
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == SCHEMA_ALPHA_RISK_BUDGET


def test_alpha_export_risk_budget_retired_disabled(tmp_path, capsys):
    assert main(["--base-dir", str(tmp_path), "alpha", "register", "--alpha-id", "alpha1", "--name", "Alpha One", "--state", "retired"]) == 0
    assert main(["--base-dir", str(tmp_path), "alpha", "performance", "--alpha-id", "alpha1", "--metric", "signal_count=10", "--metric", "order_count=10", "--metric", "fill_ratio=1.0", "--metric", "denied_count=0", "--metric", "average_slippage_bps=1.0", "--metric", "orders_per_signal=1.0"]) == 0
    capsys.readouterr()
    code = main(["--base-dir", str(tmp_path), "alpha", "export-risk-budget"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["budgets"]["alpha1"]["enabled"] is False
    assert payload["budgets"]["alpha1"]["max_notional"] == 0.0



def test_alpha_budget_usage_default_file(tmp_path, capsys):
    code = main(["--base-dir", str(tmp_path), "alpha", "budget-usage"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == SCHEMA_ALPHA_BUDGET_USAGE
    assert payload["counts"] == {}


def test_alpha_budget_usage_custom_file_and_output(tmp_path, capsys):
    usage = tmp_path / "ops" / "usage.json"
    usage.parent.mkdir(parents=True)
    usage.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": date.today().isoformat(),
        "counts": {"alpha1": 2},
    }), encoding="utf-8")
    output = tmp_path / "reports" / "usage_report.json"
    code = main([
        "--base-dir", str(tmp_path),
        "alpha", "budget-usage",
        "--usage-file", str(usage),
        "--output", str(output),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["counts"] == {"alpha1": 2}
    assert json.loads(output.read_text(encoding="utf-8"))["counts"] == {"alpha1": 2}


def test_alpha_budget_usage_reset(tmp_path, capsys):
    usage = tmp_path / "alpha_budget_usage.json"
    usage.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": "2026-01-01",
        "counts": {"alpha1": 2},
    }), encoding="utf-8")
    code = main(["--base-dir", str(tmp_path), "alpha", "budget-usage-reset", "--usage-file", str(usage)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["counts"] == {}
    assert json.loads(usage.read_text(encoding="utf-8"))["counts"] == {}



def test_alpha_budget_usage_with_risk_budget_report(tmp_path, capsys):
    usage = tmp_path / "alpha_budget_usage.json"
    budget = tmp_path / "alpha_risk_budget.json"
    usage.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": date.today().isoformat(),
        "counts": {"alpha1": 2},
    }), encoding="utf-8")
    budget.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
        "budgets": {
            "alpha1": {
                "enabled": True,
                "risk_tier": "standard",
                "max_notional": 10000,
                "max_order_notional": 1000,
                "max_daily_orders": 5,
            }
        },
    }), encoding="utf-8")
    output = tmp_path / "reports" / "usage_report.json"
    code = main([
        "--base-dir", str(tmp_path),
        "alpha", "budget-usage",
        "--usage-file", str(usage),
        "--alpha-risk-budget", str(budget),
        "--output", str(output),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == SCHEMA_ALPHA_BUDGET_USAGE_REPORT
    assert payload["budgets"]["alpha1"]["used_daily_orders"] == 2
    assert payload["budgets"]["alpha1"]["remaining_daily_orders"] == 3
    assert payload["budgets"]["alpha1"]["usage_ratio"] == 0.4
    assert payload["warning_count"] == 0
    assert payload["warnings"] == []
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == SCHEMA_ALPHA_BUDGET_USAGE_REPORT



def test_alpha_budget_usage_warning_returns_nonzero_by_default(tmp_path, capsys):
    usage = tmp_path / "alpha_budget_usage.json"
    budget = tmp_path / "alpha_risk_budget.json"
    usage.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": date.today().isoformat(),
        "counts": {"alpha1": 5},
    }), encoding="utf-8")
    budget.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
        "budgets": {
            "alpha1": {
                "enabled": True,
                "risk_tier": "standard",
                "max_notional": 10000,
                "max_order_notional": 1000,
                "max_daily_orders": 5,
            }
        },
    }), encoding="utf-8")
    code = main([
        "--base-dir", str(tmp_path),
        "alpha", "budget-usage",
        "--usage-file", str(usage),
        "--alpha-risk-budget", str(budget),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["warning_count"] == 1
    assert payload["warnings"][0]["type"] == "daily_usage_exhausted"


def test_alpha_budget_usage_warning_non_strict_returns_success(tmp_path, capsys):
    usage = tmp_path / "alpha_budget_usage.json"
    budget = tmp_path / "alpha_risk_budget.json"
    usage.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": date.today().isoformat(),
        "counts": {"alpha1": 4},
    }), encoding="utf-8")
    budget.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
        "budgets": {
            "alpha1": {
                "enabled": True,
                "risk_tier": "standard",
                "max_notional": 10000,
                "max_order_notional": 1000,
                "max_daily_orders": 5,
            }
        },
    }), encoding="utf-8")
    code = main([
        "--base-dir", str(tmp_path),
        "alpha", "budget-usage",
        "--usage-file", str(usage),
        "--alpha-risk-budget", str(budget),
        "--non-strict",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["warning_count"] == 1
    assert payload["warnings"][0]["type"] == "daily_usage_high"


def test_alpha_budget_usage_raw_counts_ignore_warning_exit(tmp_path, capsys):
    usage = tmp_path / "alpha_budget_usage.json"
    usage.write_text(json.dumps({
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": date.today().isoformat(),
        "counts": {"alpha1": 999},
    }), encoding="utf-8")
    code = main(["--base-dir", str(tmp_path), "alpha", "budget-usage", "--usage-file", str(usage)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == SCHEMA_ALPHA_BUDGET_USAGE
    assert payload["counts"] == {"alpha1": 999}
