"""Alpha budget artifact contract validation tests."""

import json

import pytest

from apps.engine.cli import main
from core.alpha.schema_versions import SCHEMA_ALPHA_RISK_BUDGET
from core.runtime.alpha_budget_contracts import (
    AlphaBudgetContractError,
    AlphaBudgetUsageContractValidator,
    AlphaRiskBudgetContractValidator,
)
from core.runtime.alpha_budget_usage_store import AlphaBudgetUsageStore
from core.runtime.alpha_risk_budget_gate import AlphaRiskBudgetGate
from core.runtime.schema_versions import (
    SCHEMA_ALPHA_BUDGET_USAGE,
    SCHEMA_CLI_ERROR,
)


def _valid_risk_budget():
    return {
        "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
        "budgets": {
            "alpha1": {
                "enabled": True,
                "risk_tier": "standard",
                "max_notional": 1000.0,
                "max_order_notional": 100.0,
                "max_daily_orders": 1,
            }
        },
    }


def _valid_usage():
    return {
        "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
        "usage_date": "2026-01-01",
        "counts": {"alpha1": 1},
    }


class TestAlphaBudgetArtifactContracts:
    def test_valid_risk_budget(self):
        payload = _valid_risk_budget()
        assert AlphaRiskBudgetContractValidator.validate(payload) is payload

    @pytest.mark.parametrize(
        "payload",
        [
            {"schema_version": "wrong", "budgets": {}},
            {"schema_version": SCHEMA_ALPHA_RISK_BUDGET, "budgets": []},
            {"schema_version": SCHEMA_ALPHA_RISK_BUDGET, "budgets": {"alpha1": {"enabled": "yes"}}},
            {
                "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
                "budgets": {"alpha1": {"enabled": True, "max_order_notional": -1}},
            },
            {
                "schema_version": SCHEMA_ALPHA_RISK_BUDGET,
                "budgets": {"alpha1": {"enabled": True, "max_daily_orders": 1.5}},
            },
        ],
    )
    def test_invalid_risk_budget(self, payload):
        with pytest.raises(AlphaBudgetContractError):
            AlphaRiskBudgetContractValidator.validate(payload)
        with pytest.raises(AlphaBudgetContractError):
            AlphaRiskBudgetGate(payload)

    def test_valid_usage(self):
        payload = _valid_usage()
        assert AlphaBudgetUsageContractValidator.validate(payload) is payload

    @pytest.mark.parametrize(
        "payload",
        [
            {"schema_version": "wrong", "usage_date": "2026-01-01", "counts": {}},
            {"schema_version": SCHEMA_ALPHA_BUDGET_USAGE, "usage_date": "not-date", "counts": {}},
            {"schema_version": SCHEMA_ALPHA_BUDGET_USAGE, "usage_date": "2026-01-01", "counts": []},
            {
                "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
                "usage_date": "2026-01-01",
                "counts": {"alpha1": -1},
            },
            {
                "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
                "usage_date": "2026-01-01",
                "counts": {"alpha1": 1.5},
            },
        ],
    )
    def test_invalid_usage(self, payload, tmp_path):
        with pytest.raises(AlphaBudgetContractError):
            AlphaBudgetUsageContractValidator.validate(payload)
        path = tmp_path / "usage.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(AlphaBudgetContractError):
            AlphaBudgetUsageStore(path)

    def test_runtime_cli_rejects_invalid_risk_budget(self, tmp_path, capsys):
        budget = tmp_path / "bad_risk_budget.json"
        budget.write_text(json.dumps({"schema_version": "wrong", "budgets": {}}), encoding="utf-8")
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
                str(budget),
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["schema_version"] == SCHEMA_CLI_ERROR
        assert payload["error"] == "alpha_budget_contract_error"
        assert payload["path"] == str(budget)

    def test_alpha_cli_rejects_invalid_usage_file(self, tmp_path, capsys):
        usage = tmp_path / "bad_usage.json"
        usage.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_ALPHA_BUDGET_USAGE,
                    "usage_date": "2026-01-01",
                    "counts": {"alpha1": -1},
                }
            ),
            encoding="utf-8",
        )
        code = main(
            ["--base-dir", str(tmp_path), "alpha", "budget-usage", "--usage-file", str(usage)]
        )
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["schema_version"] == SCHEMA_CLI_ERROR
        assert payload["error"] == "alpha_budget_contract_error"
        assert payload["path"] == str(usage)
