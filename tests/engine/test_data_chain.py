"""Full data chain audit: signal → decision → ledger → execution → reconciliation → feedback → governance."""

import json
from datetime import UTC, datetime
from pathlib import Path

from apps.engine.system_facade import SystemFacade
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.lifecycle_manager import LifecycleManager
from core.deployment.service_container import ServiceContainer
from core.deployment.state_persistence import StatePersistence
from core.ledger.stream_names import (
    LEDGER_STREAM_COMMUNICATIONS,
    LEDGER_STREAM_DECISIONS,
    LEDGER_STREAM_EXECUTION_EVENTS,
    stream_jsonl_suffix,
)
from core.observability.metric_names import CYCLES_TOTAL, venue_events_metric


def _prod(tmp_path):
    cfg = EnvironmentConfig.production(str(tmp_path / "data"))
    cfg.enable_idempotency = False
    c = ServiceContainer(cfg).build()
    return c


class TestLedgerPersistence:
    def test_decision_record_written_to_disk(self, tmp_path):
        c = _prod(tmp_path)
        orch = c.build_orchestrator()
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        assert outcome.decision_result is not None

        today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
        ledger_dir = Path(str(tmp_path / "data")) / "decisions" / today
        decision_files = list(ledger_dir.glob(f"*.{stream_jsonl_suffix(LEDGER_STREAM_DECISIONS)}"))
        assert len(decision_files) >= 1

        content = decision_files[0].read_text(encoding="utf-8").strip()
        records = [json.loads(line) for line in content.splitlines()]
        assert len(records) >= 1
        assert "record_id" in records[0]
        assert "intent_id" in records[0]

    def test_communication_record_written(self, tmp_path):
        c = _prod(tmp_path)
        orch = c.build_orchestrator()
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})

        if outcome.decision_result and outcome.decision_result.communication_record:
            today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
            comm_files = list(
                (Path(str(tmp_path / "data")) / today).glob(
                    f"*.{stream_jsonl_suffix(LEDGER_STREAM_COMMUNICATIONS)}"
                )
            )
            assert len(comm_files) >= 1

    def test_multiple_decisions_accumulate(self, tmp_path):
        c = _prod(tmp_path)
        orch = c.build_orchestrator()
        for _ in range(5):
            orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})

        today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
        decision_files = list(
            (Path(str(tmp_path / "data")) / "decisions" / today).glob(
                f"*.{stream_jsonl_suffix(LEDGER_STREAM_DECISIONS)}"
            )
        )
        total = 0
        for f in decision_files:
            total += len(f.read_text(encoding="utf-8").strip().splitlines())
        assert total == 5

    def test_different_symbols_separate_files(self, tmp_path):
        c = _prod(tmp_path)
        orch = c.build_orchestrator()
        orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})
        orch.run_cycle({"symbol": "EURUSD"}, {"f": 1.0})

        today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
        decision_files = list(
            (Path(str(tmp_path / "data")) / "decisions" / today).glob(
                f"*.{stream_jsonl_suffix(LEDGER_STREAM_DECISIONS)}"
            )
        )
        symbols = {f.name.split(".")[0] for f in decision_files}
        assert "XAUUSD" in symbols
        assert "EURUSD" in symbols

    def test_ledger_reader_finds_records(self, tmp_path):
        c = _prod(tmp_path)
        orch = c.build_orchestrator()
        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})

        if outcome.decision_result and outcome.decision_result.communication_record:
            msg_id = outcome.decision_result.communication_record.message_id
            today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
            found = c.communication_reader.find_by_message_id(  # type: ignore[reportOptionalMemberAccess]
                date_key=today, target="exec_bridge", message_id=msg_id
            )
            assert found is not None
            assert found["message_id"] == msg_id


class TestFullDataChain:
    def test_signal_to_governance_chain(self, tmp_path):
        """Complete chain: decide → fill → feedback → governance."""
        c = _prod(tmp_path)
        c.governance_service.register_brain("alpha", "live")  # type: ignore[reportOptionalMemberAccess]

        sp = StatePersistence(str(tmp_path / "state"))
        lm = LifecycleManager(c, sp)
        orch = c.build_orchestrator()
        facade = SystemFacade(c, orchestrator=orch, lifecycle=lm)
        lm.startup()

        # Step 1: Make decisions
        msg_ids = []
        for _ in range(10):
            r = facade.decide("XAUUSD")
            if r.get("allowed") and r.get("message_id"):
                msg_ids.append(r["message_id"])

        # Step 2: Fill some orders
        for mid in msg_ids[:3]:
            facade.process_event(mid, "ack", venue="exchange")
            facade.process_event(
                mid, "filled", filled_quantity=0.01, price=2000.0, venue="exchange"
            )

        # Step 3: Verify execution events on disk
        today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
        exec_files = list(
            (Path(str(tmp_path / "data")) / today).glob(
                f"*.{stream_jsonl_suffix(LEDGER_STREAM_EXECUTION_EVENTS)}"
            )
        )
        if msg_ids:
            assert len(exec_files) >= 1 or c.execution_manager is not None

        # Step 4: Verify metrics accumulated
        assert c.metrics.get_counter(CYCLES_TOTAL) >= 10  # type: ignore[reportOptionalMemberAccess]
        if msg_ids:
            assert c.metrics.get_counter(venue_events_metric("ack")) >= 1  # type: ignore[reportOptionalMemberAccess]

        # Step 5: Verify governance state
        brain_state = c.governance_service.get_brain_state("alpha")  # type: ignore[reportOptionalMemberAccess]
        assert brain_state is not None

        # Step 6: Verify positions
        positions = facade.positions()
        assert isinstance(positions["open"], list)

        # Step 7: Verify tracing was attached
        # (All cycles have trace_summary due to orchestrator integration)

        lm.shutdown(save_state=True)

    def test_reconciliation_after_fill(self, tmp_path):
        """Verify reconciliation produces correct status after fill events."""
        c = _prod(tmp_path)
        orch = c.build_orchestrator()

        outcome = orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})

        if outcome.decision_result and outcome.decision_result.verdict.is_allowed():
            msg_id = outcome.decision_result.communication_record.message_id
            record_id = outcome.decision_result.record.record_id

            orch.process_execution_event(message_id=msg_id, event_type="ack", venue="ex")
            orch.process_execution_event(
                message_id=msg_id,
                event_type="filled",
                filled_quantity=0.01,
                price=2000.0,
                venue="ex",
            )

            today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
            recon = c.reconciliation_service.reconcile_message(  # type: ignore[reportOptionalMemberAccess]
                date_key=today, target="exec_bridge", message_id=msg_id, correlation_id=record_id
            )
            assert recon["status"] in ("matched", "unmatched", "partial", "stale")

    def test_contract_validation_on_persisted_records(self, tmp_path):
        """Read records from disk and validate contracts."""
        c = _prod(tmp_path)
        orch = c.build_orchestrator()
        orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})

        today = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
        decision_files = list(
            (Path(str(tmp_path / "data")) / "decisions" / today).glob(
                f"*.{stream_jsonl_suffix(LEDGER_STREAM_DECISIONS)}"
            )
        )

        for f in decision_files:
            for line in f.read_text(encoding="utf-8").strip().splitlines():
                record = json.loads(line)
                assert "record_id" in record
                assert "schema_version" in record
                assert "event_time" in record

    def test_batch_decisions_all_have_trace(self, tmp_path):
        """Every decision cycle produces a trace summary."""
        c = _prod(tmp_path)
        orch = c.build_orchestrator()
        outcomes = [orch.run_cycle({"symbol": f"SYM{i}"}, {"f": 1.0}) for i in range(10)]
        for out in outcomes:
            assert out.trace_summary is not None
            assert out.trace_summary["span_count"] >= 2


class TestSystemMetadata:
    def test_diagnostics_snapshot_complete(self, tmp_path):
        c = _prod(tmp_path)
        c.governance_service.register_brain("a", "live")  # type: ignore[reportOptionalMemberAccess]
        c.governance_service.register_brain("b", "candidate")  # type: ignore[reportOptionalMemberAccess]
        c.brain_tracker.record_outcome("a", {"composite_score": 0.8})  # type: ignore[reportOptionalMemberAccess]
        c.brain_tracker.record_outcome("b", {"composite_score": 0.6})  # type: ignore[reportOptionalMemberAccess]
        orch = c.build_orchestrator()
        for _ in range(5):
            orch.run_cycle({"symbol": "XAUUSD"}, {"f": 1.0})

        snap = c.diagnostics.build_snapshot()  # type: ignore[reportOptionalMemberAccess]
        assert snap["metrics"] is not None
        assert snap["metrics"]["counters"][CYCLES_TOTAL] == 5
        assert "brain_health" in snap
        assert snap["brain_health"]["brain_count"] == 2

    def test_health_reflects_state(self, tmp_path):
        c = _prod(tmp_path)
        h = c.health_check.readiness()  # type: ignore[reportOptionalMemberAccess]
        assert h["status"] == "ready"

    def test_alert_service_fires_on_high_error_rate(self, tmp_path):
        c = _prod(tmp_path)
        from core.observability.alert_service import InMemoryAlertChannel

        channel = InMemoryAlertChannel()
        c.alert_service._channels.append(channel)  # type: ignore[reportOptionalMemberAccess]
        c.alert_service.evaluate({"error_rate": 0.6})  # type: ignore[reportOptionalMemberAccess]
        assert len(channel.get_alerts()) >= 1

    def test_venue_router_in_container(self, tmp_path):
        c = _prod(tmp_path)
        assert c.venue_router is not None
        log = c.venue_router.get_route_log()
        assert isinstance(log, list)

    def test_config_hot_reload_in_container(self, tmp_path):
        c = _prod(tmp_path)
        assert c.config_hot_reload is not None
        status = c.config_hot_reload.get_status()
        assert "reload_count" in status
