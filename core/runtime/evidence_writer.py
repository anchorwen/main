"""Runtime evidence writer."""
from core.contracts.ids import new_runtime_evidence_id
from core.ledger.stream_names import LEDGER_STREAM_RUNTIME_EVIDENCE
from core.runtime.evidence_contracts import RuntimeEvidenceRecord
from core.runtime.integration_contracts import RuntimePipelineResult


class RuntimeEvidenceWriter:
    """Writes runtime cycle evidence records to the ledger store."""

    def __init__(self, ledger_store, stream_name: str = LEDGER_STREAM_RUNTIME_EVIDENCE):
        self._ledger_store = ledger_store
        self._stream_name = stream_name

    def write_result(self, *, runtime_cycle_id: str, result: RuntimePipelineResult) -> tuple[RuntimeEvidenceRecord, object]:
        record = RuntimeEvidenceRecord.from_pipeline_result(
            evidence_id=new_runtime_evidence_id(),
            runtime_cycle_id=runtime_cycle_id,
            result=result,
        )
        ledger_path = self._ledger_store.append_record(
            date_key=record.generated_at.strftime("%Y-%m-%d"),
            symbol=runtime_cycle_id,
            record=record,
            stream_name=self._stream_name,
        )
        return record, ledger_path
