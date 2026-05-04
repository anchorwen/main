import json
from pathlib import Path

from core.deployment.domain_keys import (
    PAYLOAD_KEY_CORRELATION_ID,
    PAYLOAD_KEY_EVENT_COUNT,
    PAYLOAD_KEY_EVENT_TYPE,
    PAYLOAD_KEY_EVENT_TYPES,
    PAYLOAD_KEY_ID,
    PAYLOAD_KEY_IS_TERMINAL,
    PAYLOAD_KEY_LATEST_EVENT_TYPE,
    PAYLOAD_KEY_MESSAGE_ID,
    PAYLOAD_KEY_QUANTITY,
    PAYLOAD_KEY_TERMINAL_EVENT_ID,
    PAYLOAD_KEY_TERMINAL_EVENT_TYPE,
    PAYLOAD_KEY_TOTAL_FILLED_QUANTITY,
    TERMINAL_EVENT_CANCELLED,
    TERMINAL_EVENT_EXPIRED,
    TERMINAL_EVENT_FILLED,
    TERMINAL_EVENT_PARTIALLY_FILLED,
    TERMINAL_EVENT_REJECTED,
)


class ExecutionEventReader:
    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir)

    def list_events(self, *, date_key: str, correlation_id: str) -> list[dict]:
        path = self._build_path(date_key=date_key, correlation_id=correlation_id)
        if not path.exists():
            return []
        return self._read_jsonl(path)

    def find_by_message_id(
        self, *, date_key: str, correlation_id: str, message_id: str
    ) -> list[dict]:
        return [
            event
            for event in self.list_events(date_key=date_key, correlation_id=correlation_id)
            if event.get(PAYLOAD_KEY_MESSAGE_ID) == message_id
        ]

    def find_terminal_event(
        self, *, date_key: str, correlation_id: str, message_id: str
    ) -> dict | None:
        terminal_types = {
            TERMINAL_EVENT_REJECTED,
            TERMINAL_EVENT_FILLED,
            TERMINAL_EVENT_CANCELLED,
            TERMINAL_EVENT_EXPIRED,
        }
        events = self.find_by_message_id(
            date_key=date_key, correlation_id=correlation_id, message_id=message_id
        )
        for event in reversed(events):
            if event.get(PAYLOAD_KEY_EVENT_TYPE) in terminal_types:
                return event
        return None

    def find_latest_event(
        self, *, date_key: str, correlation_id: str, message_id: str
    ) -> dict | None:
        events = self.find_by_message_id(
            date_key=date_key, correlation_id=correlation_id, message_id=message_id
        )
        return events[-1] if events else None

    def build_execution_timeline(
        self, *, date_key: str, correlation_id: str, message_id: str
    ) -> dict:
        events = self.find_by_message_id(
            date_key=date_key, correlation_id=correlation_id, message_id=message_id
        )
        total_filled_qty = sum(
            event.get(PAYLOAD_KEY_QUANTITY, {}).get(TERMINAL_EVENT_FILLED, 0)
            for event in events
            if event.get(PAYLOAD_KEY_EVENT_TYPE)
            in {TERMINAL_EVENT_PARTIALLY_FILLED, TERMINAL_EVENT_FILLED}
        )
        terminal = None
        for event in reversed(events):
            if event.get(PAYLOAD_KEY_EVENT_TYPE) in {
                TERMINAL_EVENT_REJECTED,
                TERMINAL_EVENT_FILLED,
                TERMINAL_EVENT_CANCELLED,
                TERMINAL_EVENT_EXPIRED,
            }:
                terminal = event
                break

        return {
            PAYLOAD_KEY_MESSAGE_ID: message_id,
            PAYLOAD_KEY_CORRELATION_ID: correlation_id,
            PAYLOAD_KEY_EVENT_COUNT: len(events),
            PAYLOAD_KEY_EVENT_TYPES: [e.get(PAYLOAD_KEY_EVENT_TYPE) for e in events],
            PAYLOAD_KEY_TOTAL_FILLED_QUANTITY: total_filled_qty,
            PAYLOAD_KEY_TERMINAL_EVENT_TYPE: terminal.get(PAYLOAD_KEY_EVENT_TYPE)
            if terminal
            else None,
            PAYLOAD_KEY_TERMINAL_EVENT_ID: terminal.get(PAYLOAD_KEY_ID) if terminal else None,
            PAYLOAD_KEY_IS_TERMINAL: terminal is not None,
            PAYLOAD_KEY_LATEST_EVENT_TYPE: events[-1].get(PAYLOAD_KEY_EVENT_TYPE)
            if events
            else None,
        }

    def _build_path(self, *, date_key: str, correlation_id: str) -> Path:
        return self._base_dir / date_key / f"{correlation_id}.execution_events.jsonl"

    def _read_jsonl(self, path: Path) -> list[dict]:
        records = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
        return records
