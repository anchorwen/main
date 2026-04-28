"""Tests for unified CLI and exception integration."""
import json
from types import SimpleNamespace

import pytest

from apps.engine.cli import main, build_parser
from core.deployment.domain_keys import ENGINE_CONFIG_KEY_RUNTIME_METRICS, EVIDENCE_SECTION_ENGINE_CONFIG
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.schema_versions import SCHEMA_ENGINE_CONFIG_EVIDENCE, SCHEMA_ENGINE_CONFIG_STATUS
from core.runtime.schema_versions import SCHEMA_ENGINE_STATUS
from core.deployment.service_container import ServiceContainer
from core.governance.governance_service import GovernanceService
from core.execution.execution_manager import ExecutionManager
from core.market.position_tracker import PositionTracker
from core.observability.metric_names import ENGINE_CONFIG_RELOAD_TOTAL
from core.observability.metrics_collector import MetricsCollector
from core.contracts.exceptions import (
    InvalidTransitionError, BrainNotFoundError,
    OrderNotFoundError,
)


class TestCLISelftest:
    def test_selftest_passes(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "selftest"])
        assert rc == 0

    def test_validate_passes(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "validate"])
        assert rc == 0

    def test_config_status(self, tmp_path, capsys):
        d = tmp_path / "data"
        d.mkdir()
        (d / "engine_config.json").write_text(
            json.dumps({"ops_maturity_min_score": 44.0}),
            encoding="utf-8",
        )
        rc = main(["--base-dir", str(d), "config", "status"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_ENGINE_CONFIG_STATUS
        assert out["effective"]["ops_maturity_min_score"] == 44.0

    def test_config_reload_cli_unchanged(self, tmp_path, capsys):
        d = tmp_path / "data"
        d.mkdir()
        (d / "engine_config.json").write_text(
            json.dumps({"ops_maturity_min_score": 40.0}),
            encoding="utf-8",
        )
        rc = main(["--base-dir", str(d), "config", "reload"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["reloaded"] is False
        assert out["effective"]["ops_maturity_min_score"] == 40.0

    def test_status(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "status"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_ENGINE_STATUS
        assert out[EVIDENCE_SECTION_ENGINE_CONFIG]["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE

    def test_status_test_env_no_collector_omits_runtime_metrics(self, tmp_path, capsys):
        """--env test defaults enable_metrics=False; engine_config has no runtime_metrics."""
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "status"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_ENGINE_STATUS
        assert out[EVIDENCE_SECTION_ENGINE_CONFIG]["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE
        assert out["metrics"] == {}
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS not in out[EVIDENCE_SECTION_ENGINE_CONFIG]

    def test_status_no_metrics_flag_omits_runtime_metrics_in_development(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--no-metrics", "status"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["metrics"] == {}
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS not in out[EVIDENCE_SECTION_ENGINE_CONFIG]

    def test_status_no_metrics_flag_overrides_production(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "production", "--no-metrics", "status"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["metrics"] == {}
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS not in out[EVIDENCE_SECTION_ENGINE_CONFIG]

    def test_validate_no_metrics_passes(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--no-metrics", "validate"])
        assert rc == 0

    def test_validate_production_no_metrics_emits_config_warning(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "production", "--no-metrics", "validate",
        ])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out.get("valid") is True
        assert any("metrics" in w.lower() for w in out.get("warnings", []))

    def test_validate_development_no_metrics_has_no_production_only_warning(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--no-metrics", "validate"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out.get("valid") is True
        assert not any("metrics disabled in production" in w for w in out.get("warnings", []))

    def test_status_force_metrics_enables_runtime_metrics_in_test_env(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "--force-metrics", "status"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS in out[EVIDENCE_SECTION_ENGINE_CONFIG]
        assert out[EVIDENCE_SECTION_ENGINE_CONFIG][ENGINE_CONFIG_KEY_RUNTIME_METRICS][ENGINE_CONFIG_RELOAD_TOTAL] >= 0.0

    def test_cli_rejects_both_no_metrics_and_force_metrics(self):
        with pytest.raises(SystemExit):
            main(["--no-metrics", "--force-metrics", "--base-dir", ".", "status"])

    def test_validate_force_metrics_in_test_env_passes(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "--force-metrics", "validate"])
        assert rc == 0

    def test_validate_test_env_prints_valid_config_result(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "validate"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out.get("valid") is True

    def test_selftest_test_env_with_force_metrics_passes(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "--force-metrics", "selftest"])
        assert rc == 0

    def test_diagnose_health(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "diagnose", "health"])
        assert rc == 0

    def test_diagnose_metrics(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "diagnose", "metrics"])
        assert rc == 0

    def test_diagnose_metrics_test_env_reports_not_enabled(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "diagnose", "metrics"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out.get("error") == "metrics not enabled"

    def test_diagnose_metrics_test_env_force_metrics_returns_snapshot(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "test", "--force-metrics", "diagnose", "metrics",
        ])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert "counters" in out
        assert "gauges" in out
        assert "timestamp" in out

    def test_diagnose_snapshot(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "diagnose", "snapshot"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out[EVIDENCE_SECTION_ENGINE_CONFIG]["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE

    def test_diagnose_snapshot_test_env_no_runtime_metrics_in_engine_config(self, tmp_path, capsys):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "diagnose", "snapshot"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out[EVIDENCE_SECTION_ENGINE_CONFIG]["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS not in out[EVIDENCE_SECTION_ENGINE_CONFIG]

    def test_diagnose_snapshot_test_env_force_metrics_includes_runtime_metrics(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "test", "--force-metrics",
            "diagnose", "snapshot",
        ])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out[EVIDENCE_SECTION_ENGINE_CONFIG]["schema_version"] == SCHEMA_ENGINE_CONFIG_EVIDENCE
        assert ENGINE_CONFIG_KEY_RUNTIME_METRICS in out[EVIDENCE_SECTION_ENGINE_CONFIG]
        assert out[EVIDENCE_SECTION_ENGINE_CONFIG][ENGINE_CONFIG_KEY_RUNTIME_METRICS][ENGINE_CONFIG_RELOAD_TOTAL] >= 0.0

    def test_diagnose_positions(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "diagnose", "positions"])
        assert rc == 0

    def test_no_command_returns_zero(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path)])
        assert rc == 0

    def test_backtest_missing_file(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "backtest",
                    "--scenarios", str(tmp_path / "nope.json")])
        assert rc == 1

    def test_backtest_with_file(self, tmp_path):
        scenarios = [
            {"trigger": {"symbol": "XAUUSD"}, "features": {"f": 1.0}},
        ]
        sf = tmp_path / "scenarios.json"
        sf.write_text(json.dumps(scenarios))
        rc = main(["--base-dir", str(tmp_path), "backtest", "--scenarios", str(sf)])
        assert rc == 0

    def test_backtest_with_output(self, tmp_path):
        scenarios = [{"trigger": {"symbol": "XAUUSD"}, "features": {}}]
        sf = tmp_path / "s.json"
        sf.write_text(json.dumps(scenarios))
        out = str(tmp_path / "report.json")
        rc = main(["--base-dir", str(tmp_path), "backtest",
                    "--scenarios", str(sf), "--output", out])
        assert rc == 0
        assert (tmp_path / "report.json").exists()

    def test_production_env(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "--env", "production", "selftest"])
        assert rc == 0

    def test_test_env(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "--env", "test", "validate"])
        assert rc == 0


class TestGovernanceStrictTransition:
    def test_strict_transition_success(self):
        gs = GovernanceService()
        gs.register_brain("a", "live")
        result = gs.strict_transition("a", "frozen", "test")
        assert result["action"] == "transitioned"

    def test_strict_transition_brain_not_found(self):
        gs = GovernanceService()
        with pytest.raises(BrainNotFoundError) as exc_info:
            gs.strict_transition("missing", "live")
        assert exc_info.value.detail["brain_id"] == "missing"

    def test_strict_transition_invalid(self):
        gs = GovernanceService()
        gs.register_brain("x", "retired")
        with pytest.raises(InvalidTransitionError) as exc_info:
            gs.strict_transition("x", "live")
        assert exc_info.value.detail["from"] == "retired"
        assert exc_info.value.detail["to"] == "live"


class TestBuildParser:
    def test_parses_global_flags_before_subcommand(self):
        p = build_parser()
        a = p.parse_args([
            "--env", "test", "--force-metrics", "--base-dir", "C:\\data", "status",
        ])
        assert a.command == "status"
        assert a.env == "test"
        assert a.force_metrics is True
        assert a.no_metrics is False
        assert a.base_dir == "C:\\data"

    def test_global_options_can_reorder_before_subcommand(self):
        p = build_parser()
        a = p.parse_args([
            "--env", "production", "--base-dir", "/data/run", "--no-metrics", "readiness",
        ])
        assert a.command == "readiness"
        assert a.env == "production"
        assert a.base_dir == "/data/run"
        assert a.no_metrics is True
        assert a.force_metrics is False

    def test_parses_live_read_only_and_mt5_terminal_path_flags(self):
        p = build_parser()
        a = p.parse_args([
            "--live-read-only",
            "--mt5-terminal-path", "D:\\MetaTrader 5\\terminal64.exe",
            "status",
        ])
        assert a.command == "status"
        assert a.live_read_only is True
        assert a.mt5_terminal_path == "D:\\MetaTrader 5\\terminal64.exe"

    def test_global_validation_mode_applies_when_subcommand_not_overridden(self):
        p = build_parser()
        a = p.parse_args([
            "--validation-mode", "fast", "--base-dir", "C:\\data", "readiness",
        ])
        assert a.validation_mode == "fast"

    def test_subcommand_validation_mode_overrides_global(self):
        p = build_parser()
        a = p.parse_args([
            "--validation-mode", "fast", "--base-dir", "C:\\data",
            "readiness", "--validation-mode", "deep",
        ])
        assert a.validation_mode == "deep"

    def test_mutually_exclusive_metrics_flags_rejected(self):
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["--no-metrics", "--force-metrics", "--base-dir", ".", "validate"])

    def test_subcommand_before_globals_is_rejected(self):
        """Global options must appear before the subcommand (argparse + subparsers)."""
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["status", "--base-dir", "C:\\data"])


class TestEnvironmentConfigForArgs:
    def test_test_env_force_metrics_enables_collector(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(
            base_dir=str(tmp_path), env="test", no_metrics=False, force_metrics=True,
        )
        assert _environment_config_for_args(a).enable_metrics is True

    def test_production_no_metrics_disables_collector(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(
            base_dir=str(tmp_path), env="production", no_metrics=True, force_metrics=False,
        )
        assert _environment_config_for_args(a).enable_metrics is False

    def test_development_default_unchanged(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(
            base_dir=str(tmp_path), env="development", no_metrics=False, force_metrics=False,
        )
        assert _environment_config_for_args(a).enable_metrics is True

    def test_omitted_flags_use_env_factory_defaults(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(base_dir=str(tmp_path), env="test")
        assert _environment_config_for_args(a).enable_metrics is False

    def test_omitted_flags_production_keeps_metrics_enabled(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(base_dir=str(tmp_path), env="production")
        assert _environment_config_for_args(a).enable_metrics is True

    def test_development_no_metrics_disables_collector(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(
            base_dir=str(tmp_path), env="development", no_metrics=True, force_metrics=False,
        )
        assert _environment_config_for_args(a).enable_metrics is False

    def test_production_force_metrics_stays_enabled(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(
            base_dir=str(tmp_path), env="production", no_metrics=False, force_metrics=True,
        )
        assert _environment_config_for_args(a).enable_metrics is True

    def test_no_metrics_takes_precedence_over_force_metrics(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(
            base_dir=str(tmp_path), env="test", no_metrics=True, force_metrics=True,
        )
        assert _environment_config_for_args(a).enable_metrics is False

    def test_validation_mode_propagates_to_environment_config(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(
            base_dir=str(tmp_path), env="development", no_metrics=False, force_metrics=False, validation_mode="fast",
        )
        assert _environment_config_for_args(a).validation_mode == "fast"

    def test_live_read_only_flag_propagates_to_environment_config(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        a = SimpleNamespace(
            base_dir=str(tmp_path),
            env="development",
            no_metrics=False,
            force_metrics=False,
            live_read_only=True,
        )
        assert _environment_config_for_args(a).live_read_only is True

    def test_mt5_terminal_path_missing_raises_file_not_found(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        missing = tmp_path / "missing-terminal64.exe"
        a = SimpleNamespace(
            base_dir=str(tmp_path),
            env="development",
            no_metrics=False,
            force_metrics=False,
            mt5_terminal_path=str(missing),
        )
        with pytest.raises(FileNotFoundError):
            _environment_config_for_args(a)

    def test_mt5_terminal_path_populates_environment_extensions(self, tmp_path):
        from apps.engine.cli import _environment_config_for_args

        terminal = tmp_path / "terminal64.exe"
        terminal.write_text("", encoding="utf-8")
        a = SimpleNamespace(
            base_dir=str(tmp_path),
            env="development",
            no_metrics=False,
            force_metrics=False,
            mt5_terminal_path=str(terminal),
        )
        cfg = _environment_config_for_args(a)
        assert cfg.extensions["mt5_terminal_path"] == str(terminal)


class TestExecutionStrictGet:
    def test_get_order_strict_success(self):
        em = ExecutionManager(position_tracker=PositionTracker(), metrics=MetricsCollector())
        em.register_order(message_id="m1", correlation_id="c1",
                          symbol="X", side="long", quantity=1.0)
        order = em.get_order_strict("m1")
        assert order["symbol"] == "X"

    def test_get_order_strict_not_found(self):
        em = ExecutionManager(position_tracker=PositionTracker(), metrics=MetricsCollector())
        with pytest.raises(OrderNotFoundError) as exc_info:
            em.get_order_strict("nonexistent")
        assert exc_info.value.detail["message_id"] == "nonexistent"
