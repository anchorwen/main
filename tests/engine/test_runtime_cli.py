"""Runtime CLI operator surface tests."""

import json

from apps.engine.cli import main
from core.alpha.schema_versions import SCHEMA_ALPHA_RISK_BUDGET
from core.execution.paper_gateway import PaperExecutionGateway
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.runtime.evidence_writer import RuntimeEvidenceWriter
from core.runtime.execution_gates import (
    RuntimeExecutionApprovalChain,
    RuntimeGovernanceGate,
    RuntimeRiskGate,
)
from core.runtime.execution_gateway_router import ExecutionGatewayRouter
from core.runtime.execution_pipeline import RuntimeExecutionPipeline
from core.runtime.integration_contracts import OrderSizingPolicy
from core.runtime.schema_versions import SCHEMA_RUNTIME_CYCLE_LIST
from core.runtime.signal_order_builder import SignalOrderRequestBuilder
from core.strategies.examples import ThresholdAlphaAgent
from core.strategies.registry import StrategyPluginRegistry, StrategyPluginRunner


def _write_cycle(base_dir, cycle_id="cycle_cli"):
    ledger_dir = base_dir / "ledger"
    store = JsonlLedgerStore(str(ledger_dir))
    writer = RuntimeEvidenceWriter(store)
    registry = StrategyPluginRegistry()
    agent = ThresholdAlphaAgent("alpha1", "ema_bias", 1.0, -1.0)
    registry.register(agent)
    runner = StrategyPluginRunner(registry)
    runner.warmup_all({})
    router = ExecutionGatewayRouter()
    router.register("PAPER", PaperExecutionGateway())
    chain = RuntimeExecutionApprovalChain(
        [
            RuntimeRiskGate(max_quantity=100, allowed_symbols={"XAUUSD"}, max_notional=50_000),
            RuntimeGovernanceGate(allowed_strategy_ids={"alpha1"}, allowed_venues={"PAPER"}),
        ]
    )
    pipeline = RuntimeExecutionPipeline(
        strategy_runner=runner,
        order_builder=SignalOrderRequestBuilder(
            OrderSizingPolicy(base_quantity=10), default_venue="PAPER"
        ),
        gateway_router=router,
        approval_chain=chain,
        evidence_writer=writer,
    )
    pipeline.run({"ema_bias": 2.0}, {"price": 2000.0}, {"runtime_cycle_id": cycle_id})
    return ledger_dir


class TestRuntimeCLI:
    def test_runtime_list_cycles(self, tmp_path, capsys):
        _write_cycle(tmp_path, "cycle_cli")
        code = main(["--base-dir", str(tmp_path), "runtime", "list-cycles"])
        captured = capsys.readouterr().out
        payload = json.loads(captured)
        assert code == 0
        assert payload["schema_version"] == SCHEMA_RUNTIME_CYCLE_LIST
        assert payload["cycle_ids"] == ["cycle_cli"]

    def test_runtime_replay_cycle(self, tmp_path, capsys):
        _write_cycle(tmp_path, "cycle_cli")
        code = main(["--base-dir", str(tmp_path), "runtime", "replay", "--cycle-id", "cycle_cli"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["runtime_cycle_id"] == "cycle_cli"
        assert payload["replayable"] is True
        assert payload["order_count"] == 1

    def test_runtime_inspect_cycle(self, tmp_path, capsys):
        _write_cycle(tmp_path, "cycle_cli")
        code = main(["--base-dir", str(tmp_path), "runtime", "inspect", "--cycle-id", "cycle_cli"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["runtime_cycle_id"] == "cycle_cli"
        assert payload["payload"]["quality_report"]["order_count"] == 1

    def test_runtime_replay_requires_cycle_id(self, tmp_path, capsys):
        code = main(["--base-dir", str(tmp_path), "runtime", "replay"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["error"] == "--cycle-id is required for runtime replay"

    def test_runtime_inspect_missing_cycle(self, tmp_path, capsys):
        code = main(["--base-dir", str(tmp_path), "runtime", "inspect", "--cycle-id", "missing"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["error"] == "runtime cycle evidence not found"

    def test_runtime_output_file(self, tmp_path, capsys):
        _write_cycle(tmp_path, "cycle_cli")
        output = tmp_path / "reports" / "runtime_replay.json"
        code = main(
            [
                "--base-dir",
                str(tmp_path),
                "runtime",
                "replay",
                "--cycle-id",
                "cycle_cli",
                "--output",
                str(output),
            ]
        )
        capsys.readouterr()
        assert code == 0
        assert json.loads(output.read_text(encoding="utf-8"))["replayable"] is True

    def test_runtime_custom_ledger_dir(self, tmp_path, capsys):
        ledger_dir = _write_cycle(tmp_path, "cycle_cli")
        code = main(
            [
                "--base-dir",
                str(tmp_path / "unused"),
                "runtime",
                "list-cycles",
                "--ledger-dir",
                str(ledger_dir),
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["cycle_ids"] == ["cycle_cli"]

    def test_runtime_run_paper_writes_evidence_and_can_replay(self, tmp_path, capsys):
        code = main(
            [
                "--base-dir",
                str(tmp_path),
                "runtime",
                "run-paper",
                "--cycle-id",
                "cycle_manual",
                "--feature",
                "ema_bias=2.0",
                "--price",
                "2000",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["completed"] is True
        assert payload["runtime_cycle_id"] == "cycle_manual"
        assert payload["signal_count"] == 1
        assert payload["order_count"] == 1
        assert payload["approval_count"] == 2

        replay_code = main(
            ["--base-dir", str(tmp_path), "runtime", "replay", "--cycle-id", "cycle_manual"]
        )
        replay = json.loads(capsys.readouterr().out)
        assert replay_code == 0
        assert replay["replayable"] is True
        assert replay["order_count"] == 1

    def test_runtime_run_paper_requires_market_price(self, tmp_path, capsys):
        code = main(
            [
                "--base-dir",
                str(tmp_path),
                "runtime",
                "run-paper",
                "--cycle-id",
                "cycle_manual",
                "--feature",
                "ema_bias=2.0",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["completed"] is False
        assert payload["error"] == "--price or both --bid/--ask are required"

    def test_runtime_run_paper_sell_signal_with_bid_ask(self, tmp_path, capsys):
        code = main(
            [
                "--base-dir",
                str(tmp_path),
                "runtime",
                "run-paper",
                "--cycle-id",
                "cycle_sell",
                "--feature",
                "ema_bias=-2.0",
                "--bid",
                "1999",
                "--ask",
                "2000",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["completed"] is True
        assert payload["order_count"] == 1
        assert payload["quality_summary"]["filled_order_count"] == 1

    def test_runtime_run_paper_with_alpha_risk_budget_allows(self, tmp_path, capsys):
        budget = tmp_path / "alpha_risk_budget.json"
        budget.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
                    "budgets": {
                        "alpha1": {
                            "enabled": True,
                            "risk_tier": "standard",
                            "max_notional": 100000,
                            "max_order_notional": 50000,
                            "max_daily_orders": 10,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        code = main(
            [
                "--base-dir",
                str(tmp_path),
                "runtime",
                "run-paper",
                "--cycle-id",
                "cycle_budget_allow",
                "--feature",
                "ema_bias=2.0",
                "--price",
                "2000",
                "--alpha-risk-budget",
                str(budget),
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["completed"] is True
        assert payload["order_count"] == 1
        assert payload["approval_count"] == 3
        inspect_code = main(
            ["--base-dir", str(tmp_path), "runtime", "inspect", "--cycle-id", "cycle_budget_allow"]
        )
        evidence = json.loads(capsys.readouterr().out)
        assert inspect_code == 0
        assert evidence["payload"]["approvals"][0]["gate"] == "alpha_risk_budget"
        assert evidence["payload"]["approvals"][0]["approved"] is True

    def test_runtime_run_paper_with_alpha_risk_budget_denies(self, tmp_path, capsys):
        budget = tmp_path / "alpha_risk_budget.json"
        budget.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
                    "budgets": {
                        "alpha1": {
                            "enabled": True,
                            "risk_tier": "standard",
                            "max_notional": 100000,
                            "max_order_notional": 1,
                            "max_daily_orders": 10,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        code = main(
            [
                "--base-dir",
                str(tmp_path),
                "runtime",
                "run-paper",
                "--cycle-id",
                "cycle_budget_deny",
                "--feature",
                "ema_bias=2.0",
                "--price",
                "2000",
                "--alpha-risk-budget",
                str(budget),
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["completed"] is True
        assert payload["order_count"] == 0
        assert payload["skipped_count"] == 1
        assert payload["approval_count"] == 1
        inspect_code = main(
            ["--base-dir", str(tmp_path), "runtime", "inspect", "--cycle-id", "cycle_budget_deny"]
        )
        evidence = json.loads(capsys.readouterr().out)
        assert inspect_code == 0
        approval = evidence["payload"]["approvals"][0]
        assert approval["gate"] == "alpha_risk_budget"
        assert approval["approved"] is False
        assert "alpha_order_notional_exceeded" in approval["reasons"][0]

    def test_runtime_run_paper_alpha_budget_usage_persists_across_cli_calls(self, tmp_path, capsys):
        budget = tmp_path / "alpha_risk_budget.json"
        usage = tmp_path / "alpha_budget_usage.json"
        budget.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
                    "budgets": {
                        "alpha1": {
                            "enabled": True,
                            "risk_tier": "standard",
                            "max_notional": 100000,
                            "max_order_notional": 50000,
                            "max_daily_orders": 1,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        first = main(
            [
                "--base-dir",
                str(tmp_path),
                "runtime",
                "run-paper",
                "--cycle-id",
                "cycle_usage_1",
                "--feature",
                "ema_bias=2.0",
                "--price",
                "2000",
                "--alpha-risk-budget",
                str(budget),
                "--alpha-budget-usage",
                str(usage),
            ]
        )
        first_payload = json.loads(capsys.readouterr().out)
        assert first == 0
        assert first_payload["order_count"] == 1
        assert json.loads(usage.read_text(encoding="utf-8"))["counts"] == {"alpha1": 1}
        second = main(
            [
                "--base-dir",
                str(tmp_path),
                "runtime",
                "run-paper",
                "--cycle-id",
                "cycle_usage_2",
                "--feature",
                "ema_bias=2.0",
                "--price",
                "2000",
                "--alpha-risk-budget",
                str(budget),
                "--alpha-budget-usage",
                str(usage),
            ]
        )
        second_payload = json.loads(capsys.readouterr().out)
        assert second == 0
        assert second_payload["order_count"] == 0
        assert second_payload["skipped_count"] == 1
        inspect_code = main(
            ["--base-dir", str(tmp_path), "runtime", "inspect", "--cycle-id", "cycle_usage_2"]
        )
        evidence = json.loads(capsys.readouterr().out)
        assert inspect_code == 0
        assert evidence["payload"]["approvals"][0]["reasons"] == [
            "alpha_daily_order_limit_exceeded(2>1)"
        ]
