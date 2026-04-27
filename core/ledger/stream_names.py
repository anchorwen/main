"""Shared ledger stream names and JSONL filename helpers."""

LEDGER_STREAM_DECISIONS = "decisions"
LEDGER_STREAM_COMMUNICATIONS = "communications"
LEDGER_STREAM_REPLAYS = "replays"
LEDGER_STREAM_EXECUTION_EVENTS = "execution_events"
LEDGER_STREAM_RUNTIME_EVIDENCE = "runtime_evidence"


def stream_jsonl_suffix(stream_name: str) -> str:
    return f"{stream_name}.jsonl"


def stream_jsonl_filename(symbol: str, stream_name: str) -> str:
    return f"{symbol}.{stream_jsonl_suffix(stream_name)}"
