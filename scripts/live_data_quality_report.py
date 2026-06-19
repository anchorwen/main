"""Cross-verify journal, receipt, archive, and outbox for data integrity.

Run from repo root:
  python scripts/live_data_quality_report.py --base-dir data --symbol XAUUSDc
  python scripts/live_data_quality_report.py --base-dir data --date 2026-04-29

Checks:
  - Journal → receipt match (every journal entry has a receipt, status consistent)
  - Receipt → journal match (every receipt has a journal entry)
  - Archive → journal match (every archived handoff has a journal entry)
  - Outbox staleness
  - Unmatched orphans
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "live_data_quality_report.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _read_journal_entries(
    journal_path: Path, date_filter: str | None = None
) -> list[dict[str, Any]]:
    """Parse .jsonl journal file, optionally filtered by date key (from recorded_at ISO prefix)."""
    entries: list[dict[str, Any]] = []
    if not journal_path.exists():
        return entries
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if date_filter:
            recorded = str(rec.get("recorded_at", ""))
            if not recorded.startswith(date_filter):
                continue
        entries.append(rec)
    return entries


def _collect_receipt_map(
    receipt_root: Path, date_filter: str | None = None
) -> dict[str, dict[str, Any]]:
    """Build {message_id: receipt_payload} from all .ack.json files."""
    receipt_map: dict[str, dict[str, Any]] = {}
    if not receipt_root.exists():
        return receipt_map
    pattern = "**/*.ack.json"
    if date_filter:
        pattern = f"{date_filter}/**/*.ack.json"
    for p in receipt_root.glob(pattern):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        mid = data.get("message_id", p.stem.removesuffix(".ack"))
        receipt_map[mid] = data
    return receipt_map


def _collect_archive_map(archive_root: Path, date_filter: str | None = None) -> dict[str, Path]:
    """Build {filename_stem: archive_path} from .mt5.json files."""
    archive_map: dict[str, Path] = {}
    if not archive_root.exists():
        return archive_map
    pattern = "**/*.mt5.json"
    if date_filter:
        pattern = f"{date_filter}/**/*.mt5.json"
    for p in archive_root.glob(pattern):
        key = p.stem  # message_id is the stem
        archive_map[key] = p
    return archive_map


def _collect_outbox_map(outbox_root: Path) -> dict[str, Path]:
    """Build {filename_stem: outbox_path} from pending .mt5.json files."""
    outbox_map: dict[str, Path] = {}
    if not outbox_root.exists():
        return outbox_map
    for p in outbox_root.rglob("*.mt5.json"):
        key = p.stem
        outbox_map[key] = p
    return outbox_map


def _stale_outbox_report(outbox_map: dict[str, Path], max_age_minutes: int) -> dict[str, Any]:
    cutoff = datetime.now(UTC).timestamp() - (max_age_minutes * 60)
    stale: list[str] = []
    for mid, p in outbox_map.items():
        try:
            if p.stat().st_mtime < cutoff:
                stale.append(mid)
        except OSError:
            stale.append(mid)
    return {
        "stale_count": len(stale),
        "stale_message_ids": sorted(stale),
        "max_age_minutes": max_age_minutes,
    }


def _status_consistency(journal_status: str | None, receipt_status: str | None) -> str:
    """Compare journal ack_status vs receipt ack_status."""
    if journal_status is None and receipt_status is None:
        return "both_missing"
    if journal_status is None:
        return "journal_missing"
    if receipt_status is None:
        return "receipt_missing"
    if journal_status != receipt_status:
        return f"mismatch(journal={journal_status},receipt={receipt_status})"
    return f"ok({journal_status})"


def _parse_iso_to_epoch(iso_str: str | None) -> float | None:
    """Parse ISO timestamp to epoch seconds, returning None on failure."""
    if not iso_str:
        return None
    try:
        from datetime import UTC, datetime

        # Handle 'Z' suffix
        s = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(UTC).replace(tzinfo=None)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _latency_check(
    journal_recorded_at: str | None,
    receipt_received_at: str | None,
    max_latency_seconds: float = 30.0,
) -> dict[str, Any]:
    """Check latency between journal recorded_at and receipt received_at."""
    j_epoch = _parse_iso_to_epoch(journal_recorded_at)
    r_epoch = _parse_iso_to_epoch(receipt_received_at)
    result: dict[str, Any] = {
        "journal_recorded_at": journal_recorded_at,
        "receipt_received_at": receipt_received_at,
        "latency_seconds": None,
        "exceeds_threshold": False,
        "max_latency_seconds": max_latency_seconds,
    }
    if j_epoch is not None and r_epoch is not None:
        latency = abs(r_epoch - j_epoch)
        result["latency_seconds"] = round(latency, 3)
        result["exceeds_threshold"] = latency > max_latency_seconds
    elif j_epoch is None and r_epoch is not None:
        result["latency_seconds"] = None
        result["parse_error"] = "journal_recorded_at unparseable"
    elif r_epoch is None and j_epoch is not None:
        result["latency_seconds"] = None
        result["parse_error"] = "receipt_received_at unparseable"
    return result


def _receipt_format_issues(
    receipt_rec: dict[str, Any] | None,
) -> list[str]:
    """Check for format issues in receipt record."""
    issues: list[str] = []
    if receipt_rec is None:
        return issues
    if "error" in receipt_rec:
        issues.append(f"receipt_has_error_field: {receipt_rec['error']}")
    if "ack_status" not in receipt_rec and receipt_rec:
        issues.append("receipt_missing_ack_status")
    return issues


def build_report(
    base_dir: Path,
    *,
    date_filter: str | None = None,
    stale_max_age_minutes: int = 10,
) -> dict[str, Any]:
    journal_path = base_dir / "live_trade_journal.jsonl"
    receipt_root = base_dir / "receipts"
    archive_root = base_dir / "mt5_outbox_processed"
    outbox_root = base_dir / "mt5_outbox"

    journals = _read_journal_entries(journal_path, date_filter=date_filter)

    # Schema validation
    try:
        from scripts.validators.journal_validator import validate_journal_file

        journal_schema_check = validate_journal_file(journal_path, date_filter=date_filter)
    except Exception:  # BLE001:REVIEWED
        journal_schema_check = {"error": "journal_validator_import_failed"}

    receipt_map = _collect_receipt_map(receipt_root, date_filter=date_filter)
    archive_map = _collect_archive_map(archive_root, date_filter=date_filter)
    outbox_map = _collect_outbox_map(outbox_root)

    journal_ids = set()
    matched: list[dict[str, Any]] = []

    journal_without_receipt: list[dict[str, Any]] = []
    receipt_without_journal: list[dict[str, Any]] = []
    status_mismatches: list[dict[str, Any]] = []
    journal_without_archive: list[
        str
    ] = []  # message_ids referenced in journal but no archive found
    latency_exceeded: list[dict[str, Any]] = []
    receipt_format_issues: list[dict[str, Any]] = []

    for j_rec in journals:
        mid = str(j_rec.get("message_id", ""))
        if not mid:
            continue
        journal_ids.add(mid)

        j_status = j_rec.get("ack_status")
        r_rec = receipt_map.get(mid)
        r_status = r_rec.get("ack_status") if r_rec else None

        cross = {
            "message_id": mid,
            "journal_status": j_status,
            "receipt_status": r_status,
            "status_match": _status_consistency(j_status, r_status),
            "has_receipt": r_rec is not None,
            "journal_recorded_at": j_rec.get("recorded_at"),
        }

        if r_rec is None:
            journal_without_receipt.append(j_rec)
        elif j_status is not None and r_status is not None and j_status != r_status:
            status_mismatches.append(cross)

        # Latency check (journal recorded_at vs receipt received_at)
        if r_rec is not None:
            lat = _latency_check(
                j_rec.get("recorded_at"),
                r_rec.get("received_at"),
            )
            if lat.get("exceeds_threshold") or lat.get("parse_error"):
                lat["message_id"] = mid
                latency_exceeded.append(lat)

        # Receipt format issues
        fmt_issues = _receipt_format_issues(r_rec)
        if fmt_issues:
            receipt_format_issues.append({"message_id": mid, "issues": fmt_issues})

        # Check archive coverage
        ref_archive = j_rec.get("archive_path", j_rec.get("outbox_path", ""))
        if ref_archive and not archive_map.get(mid):
            journal_without_archive.append(mid)

        matched.append(cross)

    # Receipts without journal entries
    for rid, r_rec in receipt_map.items():
        if rid not in journal_ids:
            receipt_without_journal.append(r_rec)

    # Archive files without journal entry
    archive_without_journal = sorted(set(archive_map.keys()) - journal_ids)

    staleness = _stale_outbox_report(outbox_map, stale_max_age_minutes)

    schema_issues = (
        journal_schema_check.get("invalid", 0) if isinstance(journal_schema_check, dict) else 0
    )

    issues_count = (
        len(journal_without_receipt)
        + len(receipt_without_journal)
        + len(status_mismatches)
        + len(journal_without_archive)
        + len(archive_without_journal)
        + len(latency_exceeded)
        + len(receipt_format_issues)
        + staleness["stale_count"]
        + schema_issues
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "base_dir": str(base_dir.resolve()),
        "date_filter": date_filter,
        "journal_schema_validation": journal_schema_check,
        "summary": {
            "total_journal_entries": len(journals),
            "total_receipts": len(receipt_map),
            "total_archives": len(archive_map),
            "total_pending_outbox": len(outbox_map),
            "issues_count": issues_count,
        },
        "cross_verify": {
            "journal_without_receipt": journal_without_receipt,
            "receipt_without_journal": receipt_without_journal,
            "status_mismatches": status_mismatches,
            "journal_without_archive": journal_without_archive,
            "archive_without_journal": archive_without_journal,
            "latency_exceeded": latency_exceeded,
            "receipt_format_issues": receipt_format_issues,
        },
        "outbox_staleness": staleness,
        "matched_cross_checks": matched,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_data_quality_report")
    p.add_argument("--base-dir", default="data")
    p.add_argument("--date", default=None, help="ISO date filter (UTC), e.g. 2026-04-29")
    p.add_argument("--stale-max-age-minutes", type=int, default=10)
    p.add_argument("--output", default=None, help="Write JSON report to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = Path(args.base_dir)
    report = build_report(
        base_dir=base,
        date_filter=args.date,
        stale_max_age_minutes=args.stale_max_age_minutes,
    )
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    issues = report["cross_verify"]
    any_critical = bool(
        issues["journal_without_receipt"]
        or issues["receipt_without_journal"]
        or issues["status_mismatches"]
    )
    return 1 if any_critical else 0


try:
    from core.deployment.scheduled_task_registry import register

    register("data_quality_report", build_report)
except ImportError:
    pass

if __name__ == "__main__":
    raise SystemExit(main())
