from datetime import datetime, timedelta
import json

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import CommunicationMessageType, CommunicationPriority, DispatchStatus, ReplayGateDecision
from core.ledger.services.communication_replay_gate import build_governance_summary
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_record_writer import CommunicationRecordWriter
from core.ledger.services.communication_replay_gate import CommunicationReplayGate
from core.ledger.services.communication_replay_service import CommunicationReplayService
from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE, SCHEMA_DISPATCH_RESULT


def build_envelope(message_id: str, correlation_id: str, target: str = "exec_bridge"):
    return CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id=message_id,
        correlation_id=correlation_id,
        causation_id=None,
        event_time=datetime(2026, 4, 24, 12, 0, 0),
        producer="decision_engine",
        target=target,
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload={"intent_id": message_id},
        deadline_at=datetime(2026, 4, 24, 12, 0, 0) + timedelta(seconds=10),
    )


def build_result(message_id: str, *, status=DispatchStatus.PROTOCOL_VALIDATED, attempts=None, fallback_adapter_name=None, recorded_at=None):
    return DispatchResult(
        schema_version=SCHEMA_DISPATCH_RESULT,
        dispatch_id=f"dispatch_{message_id}",
        message_id=message_id,
        status=status,
        recorded_at=recorded_at or datetime(2026, 4, 24, 12, 0, 1),
        target="exec_bridge",
        adapter_name="stub_adapter",
        fallback_adapter_name=fallback_adapter_name,
        attempts=attempts or [{"adapter_name": "stub_adapter", "status": "succeeded", "reason": None}],
        degrade_reason="primary down" if status == DispatchStatus.DEGRADED else None,
        failure_reason="hard failure" if status == DispatchStatus.FAILED else None,
    )


def write_receipt(receipt_dir, *, date_key: str = "2026-04-24", message_id: str, ack_status: str = "acknowledged", received_at: str = "2026-04-24T12:00:03"):
    receipt_path = receipt_dir / date_key / "exec_bridge"
    receipt_path.mkdir(parents=True, exist_ok=True)
    target_file = receipt_path / f"{message_id}.ack.json"
    target_file.write_text(
        json.dumps(
            {
                "message_id": message_id,
                "ack_status": ack_status,
                "received_at": received_at,
            }
        ),
        encoding="utf-8",
    )
    return target_file


def build_services(tmp_path, receipt_dir=None):
    store = JsonlLedgerStore(str(tmp_path))
    writer = CommunicationRecordWriter(ledger_store=store)
    reader = CommunicationRecordReader(base_dir=str(tmp_path))
    receipt_reader = FileQueueReceiptReader(receipt_dir=str(receipt_dir)) if receipt_dir else None
    inspection = CommunicationInspectionService(record_reader=reader, receipt_reader=receipt_reader)
    replay = CommunicationReplayService(inspection_service=inspection)
    gate = CommunicationReplayGate()
    return writer, replay, gate


def build_correlation_gate_fixture(tmp_path, *, correlation_id: str, message_specs: list[dict]):
    receipt_dir = tmp_path / "receipts"
    writer, replay, gate = build_services(tmp_path, receipt_dir=receipt_dir)

    for spec in message_specs:
        writer.write_record(
            build_envelope(spec["message_id"], correlation_id),
            build_result(
                spec["message_id"],
                status=DispatchStatus.TRANSPORT_DELIVERED,
                recorded_at=spec.get("recorded_at", datetime(2026, 4, 24, 12, 0, 1)),
            ),
        )
        receipt = spec.get("receipt")
        if receipt is not None:
            write_receipt(
                receipt_dir,
                date_key=receipt.get("date_key", "2026-04-24"),
                message_id=spec["message_id"],
                ack_status=receipt.get("ack_status", "acknowledged"),
                received_at=receipt.get("received_at", "2026-04-24T12:00:03"),
            )

    plan = replay.build_correlation_replay_plan(
        date_key="2026-04-24",
        target="exec_bridge",
        correlation_id=correlation_id,
    )
    decision = gate.evaluate_correlation_plan(plan)
    return plan, decision


def test_replay_gate_allows_clean_message_replay_plan(tmp_path):
    writer, replay, gate = build_services(tmp_path)
    writer.write_record(build_envelope("message_001", "corr_001"), build_result("message_001"))

    plan = replay.build_message_replay_plan(date_key="2026-04-24", target="exec_bridge", message_id="message_001")
    decision = gate.evaluate_message_plan(plan)

    assert decision["decision"] == ReplayGateDecision.ALLOW
    assert decision["reasons"] == ["clean_replay_candidate"]
    assert decision["governance_summary"] == {
        "decision": ReplayGateDecision.ALLOW,
        "posture": "auto_replay",
        "recommended_strategy": "direct_replay_candidate",
        "target_issue_codes": ["dispatch_pending"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible"],
    }
    assert build_governance_summary(plan, decision) == {
        "decision": ReplayGateDecision.ALLOW,
        "posture": "auto_replay",
        "recommended_strategy": "direct_replay_candidate",
        "target_issue_codes": ["dispatch_pending"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible"],
    }


def test_replay_gate_reviews_degraded_message_replay_plan(tmp_path):
    writer, replay, gate = build_services(tmp_path)
    writer.write_record(
        build_envelope("message_002", "corr_002"),
        build_result(
            "message_002",
            status=DispatchStatus.DEGRADED,
            attempts=[
                {"adapter_name": "exec_adapter", "status": "failed", "reason": "primary down"},
                {"adapter_name": "backup_adapter", "status": "degraded", "reason": "fallback_success"},
            ],
            fallback_adapter_name="backup_adapter",
        ),
    )

    plan = replay.build_message_replay_plan(date_key="2026-04-24", target="exec_bridge", message_id="message_002")
    decision = gate.evaluate_message_plan(plan)

    assert decision["decision"] == ReplayGateDecision.REVIEW
    assert "requires_governance_review" in decision["governance_tags"]


def test_replay_gate_detects_stale_receipt_across_next_day_lookup_for_message_replay(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay, gate = build_services(tmp_path, receipt_dir=receipt_dir)
    writer.write_record(
        build_envelope("message_stale", "corr_stale"),
        build_result("message_stale", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, date_key="2026-04-25", message_id="message_stale", received_at="2026-04-24T12:00:11")

    plan = replay.build_message_replay_plan(date_key="2026-04-24", target="exec_bridge", message_id="message_stale")
    decision = gate.evaluate_message_plan(plan)

    assert plan["delivery_state"]["phase"] == "stale_receipt"
    assert plan["recommended_strategy"] == "review_stale_receipt_before_replay"
    assert decision["decision"] == ReplayGateDecision.REVIEW
    assert decision["reasons"] == ["stale_receipt_detected"]


def test_replay_gate_denies_missing_message_plan():
    gate = CommunicationReplayGate()

    decision = gate.evaluate_message_plan(None)

    assert decision["decision"] == ReplayGateDecision.DENY
    assert decision["reasons"] == ["missing_replay_plan"]


def test_replay_gate_reviews_correlation_with_failed_message(tmp_path):
    writer, replay, gate = build_services(tmp_path)
    writer.write_record(build_envelope("message_101", "corr_shared"), build_result("message_101"))
    writer.write_record(
        build_envelope("message_102", "corr_shared"),
        build_result(
            "message_102",
            status=DispatchStatus.FAILED,
            attempts=[
                {"adapter_name": "exec_adapter", "status": "failed", "reason": "hard failure"},
            ],
        ),
    )

    plan = replay.build_correlation_replay_plan(date_key="2026-04-24", target="exec_bridge", correlation_id="corr_shared")
    decision = gate.evaluate_correlation_plan(plan)

    assert decision["decision"] == ReplayGateDecision.REVIEW
    assert "failed_history" in decision["governance_tags"]


def test_replay_gate_allows_clean_correlation_plan(tmp_path):
    writer, replay, gate = build_services(tmp_path)
    writer.write_record(build_envelope("message_201", "corr_clean"), build_result("message_201"))
    writer.write_record(build_envelope("message_202", "corr_clean"), build_result("message_202"))

    plan = replay.build_correlation_replay_plan(date_key="2026-04-24", target="exec_bridge", correlation_id="corr_clean")
    decision = gate.evaluate_correlation_plan(plan)

    assert decision["decision"] == ReplayGateDecision.ALLOW
    assert decision["reasons"] == ["clean_correlation_replay_candidate"]


def test_replay_gate_supports_targeted_timeout_replay_when_next_day_receipt_is_found(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay, gate = build_services(tmp_path, receipt_dir=receipt_dir)
    writer.write_record(
        build_envelope("message_301", "corr_timeout"),
        build_result(
            "message_301",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 20),
        ),
    )
    writer.write_record(
        build_envelope("message_302", "corr_timeout"),
        build_result(
            "message_302",
            status=DispatchStatus.TRANSPORT_DELIVERED,
            recorded_at=datetime(2026, 4, 24, 12, 0, 2),
        ),
    )
    write_receipt(receipt_dir, date_key="2026-04-25", message_id="message_302")

    plan = replay.build_correlation_replay_plan(date_key="2026-04-24", target="exec_bridge", correlation_id="corr_timeout")
    decision = gate.evaluate_correlation_plan(plan)

    assert plan["recommended_strategy"] == "replay_only_timed_out_messages"
    assert plan["target_issue_codes"] == ["receipt_timeout"]
    assert plan["target_message_ids"] == ["message_301"]
    assert decision["decision"] == ReplayGateDecision.ALLOW
    assert decision["reasons"] == ["targeted_timeout_replay_candidate"]
    assert decision["governance_summary"] == {
        "decision": ReplayGateDecision.ALLOW,
        "posture": "targeted_replay",
        "recommended_strategy": "replay_only_timed_out_messages",
        "target_issue_codes": ["receipt_timeout"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible", "timeout_targeted_replay"],
    }
    assert build_governance_summary(plan, decision) == {
        "decision": ReplayGateDecision.ALLOW,
        "posture": "targeted_replay",
        "recommended_strategy": "replay_only_timed_out_messages",
        "target_issue_codes": ["receipt_timeout"],
        "review_issue_codes": [],
        "governance_tags": ["auto_replay_eligible", "timeout_targeted_replay"],
    }


def test_replay_gate_reviews_rejected_message_receipt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay, gate = build_services(tmp_path, receipt_dir=receipt_dir)
    writer.write_record(
        build_envelope("message_rejected", "corr_rejected"),
        build_result("message_rejected", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_rejected", ack_status="rejected")

    plan = replay.build_message_replay_plan(date_key="2026-04-24", target="exec_bridge", message_id="message_rejected")
    decision = gate.evaluate_message_plan(plan)

    assert plan["recommended_strategy"] == "review_rejected_receipt_before_replay"
    assert decision["decision"] == ReplayGateDecision.REVIEW
    assert decision["reasons"] == ["rejected_receipt_detected"]
    assert decision["governance_summary"] == {
        "decision": ReplayGateDecision.REVIEW,
        "posture": "review_required",
        "recommended_strategy": "review_rejected_receipt_before_replay",
        "target_issue_codes": ["receipt_rejected"],
        "review_issue_codes": ["receipt_rejected"],
        "governance_tags": ["requires_governance_review", "receipt_rejected"],
    }



def test_replay_gate_denies_terminal_filled_message_receipt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay, gate = build_services(tmp_path, receipt_dir=receipt_dir)
    writer.write_record(
        build_envelope("message_filled", "corr_filled"),
        build_result("message_filled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_filled", ack_status="filled")

    plan = replay.build_message_replay_plan(date_key="2026-04-24", target="exec_bridge", message_id="message_filled")
    decision = gate.evaluate_message_plan(plan)

    assert plan["recommended_strategy"] == "do_not_replay_terminal_receipt"
    assert decision["decision"] == ReplayGateDecision.DENY
    assert decision["reasons"] == ["terminal_receipt_already_recorded"]
    assert decision["governance_summary"] == {
        "decision": ReplayGateDecision.DENY,
        "posture": "blocked",
        "recommended_strategy": "do_not_replay_terminal_receipt",
        "target_issue_codes": ["receipt_filled"],
        "review_issue_codes": [],
        "governance_tags": ["replay_not_required", "terminal_receipt"],
    }



def test_replay_gate_denies_terminal_accepted_message_receipt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay, gate = build_services(tmp_path, receipt_dir=receipt_dir)
    writer.write_record(
        build_envelope("message_accepted", "corr_accepted"),
        build_result("message_accepted", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_accepted", ack_status="accepted")

    plan = replay.build_message_replay_plan(date_key="2026-04-24", target="exec_bridge", message_id="message_accepted")
    decision = gate.evaluate_message_plan(plan)

    assert plan["recommended_strategy"] == "do_not_replay_terminal_receipt"
    assert decision["decision"] == ReplayGateDecision.DENY
    assert decision["reasons"] == ["terminal_receipt_already_recorded"]
    assert decision["governance_summary"] == {
        "decision": ReplayGateDecision.DENY,
        "posture": "blocked",
        "recommended_strategy": "do_not_replay_terminal_receipt",
        "target_issue_codes": ["receipt_accepted"],
        "review_issue_codes": [],
        "governance_tags": ["replay_not_required", "terminal_receipt"],
    }



def test_replay_gate_reviews_cancelled_correlation_receipt(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay, gate = build_services(tmp_path, receipt_dir=receipt_dir)
    writer.write_record(
        build_envelope("message_cancelled", "corr_cancelled"),
        build_result("message_cancelled", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_cancelled", ack_status="cancelled")

    plan = replay.build_correlation_replay_plan(date_key="2026-04-24", target="exec_bridge", correlation_id="corr_cancelled")
    decision = gate.evaluate_correlation_plan(plan)

    assert plan["recommended_strategy"] == "review_cancelled_receipts_before_replay"
    assert decision["decision"] == ReplayGateDecision.REVIEW
    assert decision["reasons"] == ["correlation_contains_cancelled_receipt"]
    assert decision["governance_summary"] == {
        "decision": ReplayGateDecision.REVIEW,
        "posture": "review_required",
        "recommended_strategy": "review_cancelled_receipts_before_replay",
        "target_issue_codes": ["receipt_cancelled"],
        "review_issue_codes": ["receipt_cancelled"],
        "governance_tags": ["sequenced_review_required", "receipt_cancelled"],
    }



def test_replay_gate_denies_terminal_correlation_receipts(tmp_path):
    receipt_dir = tmp_path / "receipts"
    writer, replay, gate = build_services(tmp_path, receipt_dir=receipt_dir)
    writer.write_record(
        build_envelope("message_terminal", "corr_terminal"),
        build_result("message_terminal", status=DispatchStatus.TRANSPORT_DELIVERED),
    )
    write_receipt(receipt_dir, message_id="message_terminal", ack_status="accepted")

    plan = replay.build_correlation_replay_plan(date_key="2026-04-24", target="exec_bridge", correlation_id="corr_terminal")
    decision = gate.evaluate_correlation_plan(plan)

    assert plan["recommended_strategy"] == "do_not_replay_terminal_receipts"
    assert decision["decision"] == ReplayGateDecision.DENY
    assert decision["reasons"] == ["correlation_contains_terminal_receipts"]
    assert decision["governance_summary"] == {
        "decision": ReplayGateDecision.DENY,
        "posture": "blocked",
        "recommended_strategy": "do_not_replay_terminal_receipts",
        "target_issue_codes": ["receipt_accepted"],
        "review_issue_codes": [],
        "governance_tags": ["replay_not_required", "terminal_receipt"],
    }



def test_replay_gate_terminal_priority_contract_matrix(tmp_path):
    cases = [
        {
            "correlation_id": "corr_gate_timeout_only",
            "message_specs": [
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
                {
                    "message_id": "message_acked",
                    "receipt": {"ack_status": "acknowledged"},
                },
            ],
            "recommended_strategy": "replay_only_timed_out_messages",
            "target_issue_codes": ["receipt_timeout"],
            "target_message_ids": ["message_timeout"],
            "decision": ReplayGateDecision.ALLOW,
            "reasons": ["targeted_timeout_replay_candidate"],
            "governance_tags": ["auto_replay_eligible", "timeout_targeted_replay"],
            "posture": "targeted_replay",
        },
        {
            "correlation_id": "corr_gate_cancelled_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_cancelled",
                    "receipt": {"ack_status": "cancelled"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
            ],
            "recommended_strategy": "review_cancelled_receipts_before_replay",
            "target_issue_codes": ["receipt_cancelled"],
            "target_message_ids": ["message_cancelled"],
            "review_issue_codes": ["receipt_cancelled"],
            "decision": ReplayGateDecision.REVIEW,
            "reasons": ["correlation_contains_cancelled_receipt"],
            "governance_tags": ["sequenced_review_required", "receipt_cancelled"],
            "posture": "review_required",
        },
        {
            "correlation_id": "corr_gate_terminal_accepted_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_accepted",
                    "receipt": {"ack_status": "accepted"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
                {
                    "message_id": "message_acked",
                    "receipt": {"ack_status": "acknowledged"},
                },
            ],
            "recommended_strategy": "do_not_replay_terminal_receipts",
            "target_issue_codes": ["receipt_timeout"],
            "target_message_ids": ["message_timeout"],
            "terminal_message_ids": ["message_accepted"],
            "decision": ReplayGateDecision.DENY,
            "reasons": ["correlation_contains_terminal_receipts"],
            "governance_tags": ["replay_not_required", "terminal_receipt"],
            "posture": "blocked",
        },
        {
            "correlation_id": "corr_gate_terminal_partial_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_partial",
                    "receipt": {"ack_status": "partially_filled"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
            ],
            "recommended_strategy": "do_not_replay_terminal_receipts",
            "target_issue_codes": ["receipt_timeout"],
            "target_message_ids": ["message_timeout"],
            "review_issue_codes": ["receipt_partially_filled"],
            "terminal_message_ids": ["message_partial"],
            "decision": ReplayGateDecision.DENY,
            "reasons": ["correlation_contains_terminal_receipts"],
            "governance_tags": ["replay_not_required", "terminal_receipt"],
            "posture": "blocked",
        },
        {
            "correlation_id": "corr_gate_terminal_filled_over_timeout",
            "message_specs": [
                {
                    "message_id": "message_filled",
                    "receipt": {"ack_status": "filled"},
                },
                {
                    "message_id": "message_timeout",
                    "recorded_at": datetime(2026, 4, 24, 12, 0, 20),
                },
            ],
            "recommended_strategy": "do_not_replay_terminal_receipts",
            "target_issue_codes": ["receipt_timeout"],
            "target_message_ids": ["message_timeout"],
            "terminal_message_ids": ["message_filled"],
            "decision": ReplayGateDecision.DENY,
            "reasons": ["correlation_contains_terminal_receipts"],
            "governance_tags": ["replay_not_required", "terminal_receipt"],
            "posture": "blocked",
        },
    ]

    for case in cases:
        plan, decision = build_correlation_gate_fixture(
            tmp_path,
            correlation_id=case["correlation_id"],
            message_specs=case["message_specs"],
        )

        assert plan["recommended_strategy"] == case["recommended_strategy"]
        assert plan["target_issue_codes"] == case["target_issue_codes"]
        assert plan["target_message_ids"] == case["target_message_ids"]
        assert plan["review_issue_codes"] == case.get("review_issue_codes", [])
        if "terminal_message_ids" in case:
            issue_message_ids = plan["delivery_summary"]["issue_message_ids"]
            assert sorted(
                issue_message_ids.get("receipt_accepted", [])
                + issue_message_ids.get("receipt_partially_filled", [])
                + issue_message_ids.get("receipt_filled", [])
            ) == case["terminal_message_ids"]
        assert decision["decision"] == case["decision"]
        assert decision["reasons"] == case["reasons"]
        assert decision["governance_summary"] == {
            "decision": case["decision"],
            "posture": case["posture"],
            "recommended_strategy": case["recommended_strategy"],
            "target_issue_codes": case["target_issue_codes"],
            "review_issue_codes": case.get("review_issue_codes", []),
            "governance_tags": case["governance_tags"],
        }


