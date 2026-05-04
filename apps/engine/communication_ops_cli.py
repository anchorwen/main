import argparse
import json

from apps.engine.communication_summary_contract import (
    build_summary_mirror_fields_from_operations_summary,
)
from core.deployment.domain_keys import (
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
    PAYLOAD_KEY_OPERATIONS_POSTURE,
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_POSTURE_SOURCES,
)
from core.ledger.services.communication_inspection_service import CommunicationInspectionService
from core.ledger.services.communication_operations_service import CommunicationOperationsService
from core.ledger.services.communication_record_reader import CommunicationRecordReader
from core.ledger.services.communication_replay_gate import CommunicationReplayGate
from core.ledger.services.communication_replay_service import CommunicationReplayService
from core.ledger.services.replay_execution_reader import ReplayExecutionReader
from core.protocol.services.file_queue_receipt_reader import FileQueueReceiptReader

STABLE_COMMUNICATION_SUMMARY_FIELDS = (
    PAYLOAD_KEY_OPERATIONS_SUMMARY,
    PAYLOAD_KEY_OPERATIONS_POSTURE,
    PAYLOAD_KEY_POSTURE_SOURCES,
    PAYLOAD_KEY_GOVERNANCE_SOURCES,
)


def build_stable_summary_contract(result: dict | None) -> dict | None:
    if result is None:
        return None
    return build_summary_mirror_fields_from_operations_summary(
        result, stable_fields=STABLE_COMMUNICATION_SUMMARY_FIELDS
    )


def build_operations_service(
    base_dir: str, receipt_dir: str | None = None
) -> CommunicationOperationsService:
    communication_reader = CommunicationRecordReader(base_dir=base_dir)
    replay_reader = ReplayExecutionReader(base_dir=base_dir)
    receipt_reader = FileQueueReceiptReader(receipt_dir=receipt_dir) if receipt_dir else None
    inspection_service = CommunicationInspectionService(
        record_reader=communication_reader, receipt_reader=receipt_reader
    )
    replay_service = CommunicationReplayService(inspection_service=inspection_service)
    replay_gate = CommunicationReplayGate()
    return CommunicationOperationsService(
        communication_reader=communication_reader,
        inspection_service=inspection_service,
        replay_service=replay_service,
        replay_gate=replay_gate,
        replay_reader=replay_reader,
        receipt_reader=receipt_reader,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Communication operations CLI")
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--receipt-dir")

    subparsers = parser.add_subparsers(dest="command", required=True)

    message_parser = subparsers.add_parser("message")
    message_parser.add_argument("--date", required=True)
    message_parser.add_argument("--target", required=True)
    message_parser.add_argument("--message-id", required=True)

    correlation_parser = subparsers.add_parser("correlation")
    correlation_parser.add_argument("--date", required=True)
    correlation_parser.add_argument("--target", required=True)
    correlation_parser.add_argument("--correlation-id", required=True)

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--date", required=True)
    replay_parser.add_argument("--target", required=True)
    replay_parser.add_argument("--replay-id", required=True)

    return parser.parse_args(argv)


def run_cli(argv=None):
    args = parse_args(argv)
    operations = build_operations_service(args.base_dir, args.receipt_dir)

    if args.command == "message":
        result = operations.get_message_operations_view(
            date_key=args.date,
            target=args.target,
            message_id=args.message_id,
        )
    elif args.command == "correlation":
        result = operations.get_correlation_operations_view(
            date_key=args.date,
            target=args.target,
            correlation_id=args.correlation_id,
        )
    else:
        result = operations.get_replay_operations_view(
            date_key=args.date,
            target=args.target,
            replay_id=args.replay_id,
        )

    result = _prefer_operations_summary(result)
    return json.dumps(result, ensure_ascii=False, default=str)


def _prefer_operations_summary(result: dict | None) -> dict | None:
    if result is None:
        return None

    stable_contract = build_stable_summary_contract(result)
    if stable_contract is None:
        return result

    return {
        **result,
        **stable_contract,
    }


def extract_stable_summary_fields(result: dict | None) -> dict | None:
    return build_stable_summary_contract(result)


def main(argv=None):
    print(run_cli(argv))


if __name__ == "__main__":
    main()
