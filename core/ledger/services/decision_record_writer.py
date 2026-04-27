from datetime import datetime

from core.contracts.domain.decision_record import DecisionRecord
from core.ledger.schema_versions import SCHEMA_DECISION_RECORD
from core.contracts.ids import new_record_id
from core.ledger.stream_names import LEDGER_STREAM_DECISIONS


class DecisionRecordWriter:
    def __init__(self, ledger_store):
        self._ledger_store = ledger_store

    def seed_record(self, feature_snapshot, proposals, candidate, intent, verdict) -> tuple[DecisionRecord, object]:
        record = DecisionRecord(
            schema_version=SCHEMA_DECISION_RECORD,
            record_id=new_record_id(),
            snapshot_id=feature_snapshot.snapshot_id,
            intent_id=intent.intent_id,
            verdict_id=verdict.verdict_id,
            event_time=feature_snapshot.event_time,
            recorded_at=datetime.utcnow(),
            context={
                "symbol": feature_snapshot.symbol,
                "venue": feature_snapshot.venue,
            },
            inputs={
                "proposal_ids": [p.proposal_id for p in proposals],
                "candidate_id": candidate.candidate_id,
            },
            execution={
                "dispatch_status": "pending",
            },
            outcome={},
            attribution={
                "supporting_brains": list(candidate.supporting_brains),
                "opposing_brains": list(candidate.opposing_brains),
            },
            labels={
                "decision_action": intent.action,
                "decision_side": intent.side,
                "risk_status": verdict.status,
            },
            trace={
                "intent_trace": intent.trace,
                "verdict_trace": verdict.trace,
            },
            extensions={},
        )
        ledger_path = self._ledger_store.append_record(
            date_key=feature_snapshot.event_time.strftime("%Y-%m-%d"),
            symbol=feature_snapshot.symbol,
            record=record,
            stream_name=LEDGER_STREAM_DECISIONS,
        )
        return record, ledger_path
