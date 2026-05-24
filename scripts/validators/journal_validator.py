"""Schema validator for live_trade_journal.v2 records.

Usage:
  python -m scripts.validators.journal_validator --journal data/live_trade_journal.jsonl
  python -m scripts.validators.journal_validator --journal data/live_trade_journal.jsonl --date 2026-05-04
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "live_trade_journal.v2"

REQUIRED_FIELDS: dict[str, type | tuple] = {
    "schema_version": str,
    "recorded_at": str,
    "message_id": str,
    "target": str,
    "ack_status": str,
    "symbol": str,
    "action": str,
    "side": str,
}

OPTIONAL_FIELDS: dict[str, type | tuple] = {
    "detail": dict,
    "volume": (float, int, type(None)),
    "effective_volume_hint": (float, int, type(None)),
    "position_ticket": (int, type(None)),
    "execution_payload_schema": (str, type(None)),
    "sl": (float, int, type(None)),
    "tp": (float, int, type(None)),
    "outbox_path": str,
    "archive_path": str,
    "receipt_path": str,
}

VALID_ACK_STATUSES = {"accepted", "rejected", "acknowledged", "closed"}
VALID_ACTIONS = {"open", "close", "modify", "modify_sltp"}
VALID_SIDES = {"long", "short"}


def validate_journal_record(rec: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a single journal record against the v2 schema.

    Returns (is_valid, list_of_error_strings).
    """
    errors: list[str] = []

    if not isinstance(rec, dict):
        return False, ["record is not a dict"]

    mid = rec.get("message_id", "?")
    tag = f"[{mid}]"

    # Schema version check
    schema = rec.get("schema_version")
    if schema != SCHEMA_VERSION:
        errors.append(f"{tag} schema_version expected {SCHEMA_VERSION!r}, got {schema!r}")

    # Required fields
    for field, expected_type in REQUIRED_FIELDS.items():
        value = rec.get(field)
        if value is None:
            errors.append(f"{tag} missing required field: {field}")
        elif not isinstance(value, expected_type):
            errors.append(
                f"{tag} {field}: expected {getattr(expected_type, '__name__', str(expected_type))}, got {type(value).__name__}"
            )

    # Optional field type checks
    for field, expected_type in OPTIONAL_FIELDS.items():
        value = rec.get(field)
        if value is not None and not isinstance(value, expected_type):
            errors.append(f"{tag} {field}: expected {expected_type}, got {type(value).__name__}")

    # Enum validations
    ack = rec.get("ack_status")
    if ack and isinstance(ack, str) and ack not in VALID_ACK_STATUSES:
        errors.append(f"{tag} ack_status {ack!r} not in {VALID_ACK_STATUSES}")

    action = rec.get("action")
    if action and isinstance(action, str) and action not in VALID_ACTIONS:
        errors.append(f"{tag} action {action!r} not in {VALID_ACTIONS}")

    side = rec.get("side")
    if side and isinstance(side, str) and side not in VALID_SIDES:
        errors.append(f"{tag} side {side!r} not in {VALID_SIDES}")

    return len(errors) == 0, errors


def validate_journal_file(journal_path: Path, *, date_filter: str | None = None) -> dict[str, Any]:
    if not journal_path.exists():
        return {
            "journal_path": str(journal_path),
            "exists": False,
            "total_records": 0,
            "valid": 0,
            "invalid": 0,
            "errors": [],
        }

    all_errors: list[str] = []
    valid_count = 0
    invalid_count = 0

    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            invalid_count += 1
            all_errors.append("JSON decode error on line")
            continue

        if date_filter:
            recorded = str(rec.get("recorded_at", ""))
            if not recorded.startswith(date_filter):
                continue

        is_valid, errs = validate_journal_record(rec)
        if is_valid:
            valid_count += 1
        else:
            invalid_count += 1
            all_errors.extend(errs)

    return {
        "journal_path": str(journal_path),
        "exists": True,
        "date_filter": date_filter,
        "total_records": valid_count + invalid_count,
        "valid": valid_count,
        "invalid": invalid_count,
        "errors": all_errors[:100],  # cap to avoid massive output
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="journal_validator")
    p.add_argument("--journal", type=Path, required=True, help="Path to live_trade_journal.jsonl")
    p.add_argument("--date", default=None, help="ISO date filter (UTC), e.g. 2026-05-04")
    p.add_argument("--output", default=None, help="Write JSON report to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = validate_journal_file(Path(args.journal), date_filter=args.date)
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    return 1 if report["invalid"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
