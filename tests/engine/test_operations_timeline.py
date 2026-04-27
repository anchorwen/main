"""Operations timeline service and CLI tests."""
import json

from apps.engine.cli import main
from core.deployment.domain_keys import (
    PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE,
    TIMELINE_EVENT_EVIDENCE_BUNDLE,
    TIMELINE_EVENT_RELEASE_GATE,
    TIMELINE_EVENT_ROLLBACK_DRILL,
    TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE,
)
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.operations_timeline import OperationsTimelineService
from core.deployment.schema_versions import (
    SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
    SCHEMA_OPERATIONS_TIMELINE_EXPORT,
    SCHEMA_OPERATIONS_TIMELINE_SUMMARY,
)
from core.deployment.service_container import ServiceContainer
from core.runtime.schema_versions import SCHEMA_ALPHA_BUDGET_USAGE_REPORT


def _container(tmp_path):
    return ServiceContainer(EnvironmentConfig.development(str(tmp_path))).build()


class TestOperationsTimelineService:
    def test_record_and_list_event(self, tmp_path):
        tl = OperationsTimelineService(str(tmp_path))
        event = tl.record("custom", {"status": "ok"}, actor="tester")
        assert event["id"] == "evt_000001"
        assert event["actor"] == "tester"
        assert tl.list_events()[0]["event_type"] == "custom"

    def test_record_release_gate_summary(self, tmp_path):
        c = _container(tmp_path)
        report = c.release_gate.evaluate()
        event = c.operations_timeline.record_release_gate(report)
        assert event["event_type"] == TIMELINE_EVENT_RELEASE_GATE
        assert event["status"] == "passed"
        assert event["summary"]["decision"] == "allow"

    def test_record_deployment_execution_summary(self, tmp_path):
        c = _container(tmp_path)
        result = c.deployment_executor.execute()
        event = c.operations_timeline.record_deployment_execution(result)
        assert event["event_type"] == "deployment_execution"
        assert event["summary"]["strategy"] == "standard"

    def test_record_rollback_and_evidence(self, tmp_path):
        c = _container(tmp_path / "data")
        rb = c.rollback_drill.run()
        ev = c.evidence_bundle.build_bundle(str(tmp_path / "evidence"), label="tl")
        c.operations_timeline.record_rollback_drill(rb)
        c.operations_timeline.record_evidence_bundle(ev)
        summary = c.operations_timeline.summarize()
        assert summary["event_type_counts"][TIMELINE_EVENT_ROLLBACK_DRILL] == 1
        assert summary["event_type_counts"][TIMELINE_EVENT_EVIDENCE_BUNDLE] == 1

    def test_list_filter_and_limit(self, tmp_path):
        tl = OperationsTimelineService(str(tmp_path))
        tl.record("a", {"status": "ok"})
        tl.record("b", {"status": "ok"})
        tl.record("a", {"status": "ok"})
        assert len(tl.list_events(event_type="a")) == 2
        assert len(tl.list_events(limit=1)) == 1


    def test_record_alpha_budget_governance_summary(self, tmp_path):
        tl = OperationsTimelineService(str(tmp_path))
        payload = {
            "schema_version": SCHEMA_ALPHA_BUDGET_GOVERNANCE_EVENT,
            "source": "test",
            "status": "warning",
            "record_count": 1,
            "evidence_count": 1,
            "missing_evidence_count": 0,
            "warning_total": 1,
            "warning_release_count": 1,
        }
        event = tl.record_alpha_budget_governance(payload, actor="qa")
        assert event["event_type"] == TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE
        assert event["status"] == "warning"
        assert event["summary"]["warning_total"] == 1
        assert event["summary"]["source"] == "test"

    def test_release_pipeline_records_alpha_budget_governance_event(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 0,
            "warnings": [],
        }
        result = c.release_pipeline.run(
            output_dir=str(tmp_path / "pipeline"),
            alpha_budget_usage_report=alpha_report,
            actor="ci",
        )
        events = c.operations_timeline.list_events(event_type=TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE)
        assert len(events) == 1
        assert events[0]["actor"] == "ci"
        assert events[0]["status"] == "passed"
        assert events[0]["summary"]["evidence_count"] == 1
        assert result["summary"]["alpha_budget_status"] == "passed"
        assert result[PAYLOAD_KEY_ALPHA_BUDGET_GOVERNANCE]["evidence_count"] == 1

    def test_release_pipeline_records_alpha_budget_warning_event(self, tmp_path):
        c = _container(tmp_path / "data")
        alpha_report = {
            "schema_version": SCHEMA_ALPHA_BUDGET_USAGE_REPORT,
            "usage_date": "2026-01-01",
            "alpha_count": 1,
            "warning_count": 1,
            "warnings": [{"alpha_id": "alpha1", "type": "daily_usage_high", "usage_ratio": 0.8, "threshold": 0.8}],
        }
        result = c.release_pipeline.run(
            output_dir=str(tmp_path / "pipeline"),
            strict_gate=False,
            alpha_budget_usage_report=alpha_report,
        )
        events = c.operations_timeline.list_events(event_type=TIMELINE_EVENT_ALPHA_BUDGET_GOVERNANCE)
        assert events[0]["status"] == "warning"
        assert events[0]["summary"]["warning_total"] == 1
        assert result["summary"]["alpha_budget_warning_total"] == 1

    def test_summary_empty(self, tmp_path):
        tl = OperationsTimelineService(str(tmp_path))
        summary = tl.summarize()
        assert summary["event_count"] == 0
        assert summary["first_event_at"] is None

    def test_export(self, tmp_path):
        tl = OperationsTimelineService(str(tmp_path))
        tl.record("a", {"status": "ok"})
        out = tmp_path / "export.json"
        saved = tl.export(str(out))
        assert saved == str(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["schema_version"] == SCHEMA_OPERATIONS_TIMELINE_EXPORT
        assert len(payload["events"]) == 1

    def test_clear(self, tmp_path):
        tl = OperationsTimelineService(str(tmp_path))
        tl.record("a", {"status": "ok"})
        result = tl.clear()
        assert result["cleared"] == 1
        assert tl.list_events() == []

    def test_container_has_timeline(self, tmp_path):
        c = _container(tmp_path)
        assert c.operations_timeline is not None


class TestOperationsTimelineCLI:
    def test_cli_summary(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "ops-timeline", "summary"])
        assert rc == 0

    def test_cli_summary_test_env_force_metrics(self, tmp_path, capsys):
        rc = main([
            "--base-dir", str(tmp_path), "--env", "test", "--force-metrics",
            "ops-timeline", "summary",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["schema_version"] == SCHEMA_OPERATIONS_TIMELINE_SUMMARY
        assert rc == 0

    def test_cli_record_gate(self, tmp_path):
        c = _container(tmp_path / "data")
        report = c.release_gate.evaluate()
        inp = tmp_path / "gate.json"
        inp.write_text(json.dumps(report), encoding="utf-8")
        rc = main(["--base-dir", str(tmp_path / "data"), "ops-timeline", "record-gate",
                   "--input", str(inp), "--actor", "ci"])
        assert rc == 0
        events = c.operations_timeline.list_events()
        assert events[0]["actor"] == "ci"

    def test_cli_record_requires_input(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "ops-timeline", "record-gate"])
        assert rc == 1

    def test_cli_list_and_export_clear(self, tmp_path):
        c = _container(tmp_path / "data")
        c.operations_timeline.record("x", {"status": "ok"})
        rc_list = main(["--base-dir", str(tmp_path / "data"), "ops-timeline", "list", "--limit", "1"])
        out = tmp_path / "timeline_export.json"
        rc_export = main(["--base-dir", str(tmp_path / "data"), "ops-timeline", "export", "--output", str(out)])
        rc_clear = main(["--base-dir", str(tmp_path / "data"), "ops-timeline", "clear"])
        assert rc_list == 0
        assert rc_export == 0
        assert rc_clear == 0
        assert out.exists()

    def test_cli_export_requires_output(self, tmp_path):
        rc = main(["--base-dir", str(tmp_path), "ops-timeline", "export"])
        assert rc == 1
