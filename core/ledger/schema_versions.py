"""Schema version constants for ledger stream records."""

SCHEMA_DECISION_RECORD = "decision_record.v1"
SCHEMA_EXECUTION_EVENT = "execution_event.v1"

# Convenience alias — the canonical name used by shadow recorder and decision code
SCHEMA_VERSION = SCHEMA_DECISION_RECORD
