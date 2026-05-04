import uuid


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def new_snapshot_id() -> str:
    return _new_id("snapshot")


def new_proposal_id() -> str:
    return _new_id("proposal")


def new_candidate_id() -> str:
    return _new_id("candidate")


def new_intent_id() -> str:
    return _new_id("intent")


def new_verdict_id() -> str:
    return _new_id("verdict")


def new_record_id() -> str:
    return _new_id("record")


def new_message_id() -> str:
    return _new_id("message")


def new_dispatch_id() -> str:
    return _new_id("dispatch")


def new_communication_record_id() -> str:
    return _new_id("communication_record")


def new_replay_execution_id() -> str:
    return _new_id("replay")


def new_execution_event_id() -> str:
    return _new_id("exec_event")


def new_runtime_cycle_id() -> str:
    return _new_id("runtime_cycle")


def new_runtime_evidence_id() -> str:
    return _new_id("runtime_evidence")
