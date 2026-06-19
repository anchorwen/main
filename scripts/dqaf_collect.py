#!/usr/bin/env python
"""
ECoL — Evidence Collection Station (证据采集站)
DQAF Component A: Automated evidence packaging for incident diagnosis.

Usage:
  python scripts/dqaf_collect.py --hours 2 --docket-id DQAF-20260606-001
  python scripts/dqaf_collect.py --hours 6  # auto-generates docket ID

Output:
  data/dqaf/evidence/<docket-id>_<timestamp>.zip
  Contains: evidence files + MANIFEST.txt with truncation audit trail.

Constraints:
  - Python stdlib only (zipfile, json, argparse, pathlib, datetime, gzip)
  - Enforces per-source truncation rules to prevent memory bombs
  - Total zip size > 5MB triggers WARNING; single source > 50MB flagged HIGH_RISK
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOTS = [PROJECT_ROOT / "data", PROJECT_ROOT / "data_btc"]
EVIDENCE_DIR = PROJECT_ROOT / "data" / "dqaf" / "evidence"

# Truncation limits (per source type)
MAX_JOURNAL_LINES = 5_000  # journal JSONL hard cap
JOURNAL_HEAD = 500  # lines to keep from head when truncating
JOURNAL_TAIL = 500  # lines to keep from tail when truncating
MAX_LOG_FILE_BYTES = 2 * 1024 * 1024  # 2MB per text log file
MAX_GOLDEN_MASTER_LINES = 200  # last N golden master records
MAX_ZIP_SIZE_WARN = 5 * 1024 * 1024  # 5MB — warn if zip exceeds
HIGH_RISK_SOURCE_BYTES = 50 * 1024 * 1024  # 50MB — flag individual source files

# Log level filter: only collect WARNING and above
LOG_LEVEL_KEYWORDS = ["WARNING", "ERROR", "CRITICAL", "FATAL", "EXCEPTION", "Traceback"]

# Evidence source definitions
SOURCE_SPECS: list[dict[str, Any]] = [
    {
        "name": "journal",
        "description": "Live trade journal slice (time-filtered)",
        "glob_patterns": ["live_trade_journal.jsonl"],
        "file_type": "jsonl",
        "truncation": "line_cap",
        "max_lines": MAX_JOURNAL_LINES,
        "head_lines": JOURNAL_HEAD,
        "tail_lines": JOURNAL_TAIL,
    },
    {
        "name": "alert_audit",
        "description": "Alert audit log slice (time-filtered)",
        "glob_patterns": ["alert_audit.jsonl", "alert_undelivered.jsonl"],
        "file_type": "jsonl",
        "truncation": "line_cap",
        "max_lines": MAX_JOURNAL_LINES,
        "head_lines": JOURNAL_HEAD,
        "tail_lines": JOURNAL_TAIL,
    },
    {
        "name": "text_logs",
        "description": "Bridge and system text logs (time-filtered, tail-capped)",
        "glob_patterns": ["bridge_*.log", "*.log"],
        "file_type": "text",
        "truncation": "tail_bytes",
        "max_bytes": MAX_LOG_FILE_BYTES,
    },
    {
        "name": "execution_state",
        "description": "Current execution state snapshot",
        "glob_patterns": ["execution_state.json", "active_position.json"],
        "file_type": "json_snapshot",
        "truncation": "none",  # small files, no truncation needed
    },
    {
        "name": "golden_master",
        "description": "Golden Master recent cycle records",
        "glob_patterns": ["golden_master.jsonl"],
        "file_type": "jsonl",
        "truncation": "line_cap",
        "max_lines": MAX_GOLDEN_MASTER_LINES,
        "head_lines": 0,
        "tail_lines": MAX_GOLDEN_MASTER_LINES,  # tail only for GM
    },
    {
        "name": "gate_audit",
        "description": "Gate audit records (time-filtered)",
        "glob_patterns": ["gate_audit*.jsonl", "gate_audit*.json"],
        "file_type": "jsonl",
        "truncation": "line_cap",
        "max_lines": MAX_JOURNAL_LINES,
        "head_lines": JOURNAL_HEAD,
        "tail_lines": JOURNAL_TAIL,
    },
    {
        "name": "state_snapshots",
        "description": "Additional state files",
        "glob_patterns": [
            "daily_ops_state.json",
            "data_health_state.json",
            "system_mode.json",
            "last_good_state.json",
        ],
        "file_type": "json_snapshot",
        "truncation": "none",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(UTC)


def parse_iso_timestamp(ts: str) -> datetime | None:
    """Parse an ISO-format timestamp string, returning naive UTC datetime or None."""
    if not ts:
        return None
    ts = ts.strip().rstrip("Z")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            continue
    return None


def find_timestamp_in_line(line: str) -> datetime | None:
    """Try to extract a timestamp from a JSON line or log line."""
    # JSONL: try common timestamp keys
    try:
        obj = json.loads(line)
        for key in ("timestamp", "time", "recorded_at", "created_at", "ts", "datetime", "date"):
            if key in obj:
                dt = parse_iso_timestamp(str(obj[key]))
                if dt:
                    return dt
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    # Text log: try ISO prefix or common patterns
    # e.g., "2026-06-06T10:30:00Z ..." or "[2026-06-06 10:30:00] ..."
    if len(line) >= 19:
        dt = parse_iso_timestamp(line[:26].strip("[]"))
        if dt:
            return dt

    return None


def glob_files(data_root: Path, patterns: list[str], cutoff: datetime | None = None) -> list[Path]:
    """Find files matching any of the given patterns under data_root.

    If cutoff is provided, only include files modified after cutoff.
    """
    results: list[Path] = []
    for pattern in patterns:
        # Search in logs/, state/, and root of data dir
        for subdir in ("logs", "state", ""):
            search_dir = data_root / subdir if subdir else data_root
            if search_dir.exists():
                for p in search_dir.glob(pattern):
                    if p.is_file():
                        # Time filter by modification time (use UTC timestamps)
                        if cutoff is not None:
                            try:
                                file_ts = p.stat().st_mtime
                                cutoff_ts = cutoff.timestamp()
                                if file_ts < cutoff_ts:
                                    continue
                            except OSError:
                                pass
                        results.append(p)
    return sorted(set(results), key=lambda p: p.name)


def filter_jsonl_by_time(
    path: Path, cutoff: datetime, max_lines: int, head: int, tail: int
) -> tuple[str, int, int, bool]:
    """Filter a JSONL file by timestamp, with head+tail truncation.

    Returns (content_str, original_lines, kept_lines, was_truncated).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return "", 0, 0, False

    original_count = len(all_lines)

    # Time-filter: keep lines within the window
    time_filtered: list[str] = []
    for line in all_lines:
        dt = find_timestamp_in_line(line)
        if dt is None or dt >= cutoff:
            time_filtered.append(line)

    filtered_count = len(time_filtered)

    # Line-cap truncation (head + tail)
    if filtered_count <= max_lines:
        content = "".join(time_filtered)
        return content, original_count, filtered_count, False

    head_lines = time_filtered[:head]
    tail_lines = time_filtered[-tail:]
    truncated_marker = (
        f"\n...[TRUNCATED {filtered_count - head - tail} lines "
        f"(original file: {original_count} lines, time-filtered: {filtered_count})]...\n"
    )
    content = "".join(head_lines) + truncated_marker + "".join(tail_lines)
    kept = head + tail + 1  # +1 for marker line
    return content, original_count, kept, True


def filter_jsonl_tail_only(path: Path, tail: int) -> tuple[str, int, int, bool]:
    """Take only the last N lines of a JSONL file (no time filter)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError:
        return "", 0, 0, False

    original_count = len(all_lines)
    if original_count <= tail:
        return "".join(all_lines), original_count, original_count, False

    tail_lines = all_lines[-tail:]
    content = f"...[TRUNCATED: showing last {tail} of {original_count} lines]...\n" + "".join(
        tail_lines
    )
    return content, original_count, tail + 1, True


def filter_text_log_tail(path: Path, max_bytes: int) -> tuple[str, int, int, bool]:
    """Read a text log file, keeping only the last max_bytes.

    Returns (content_str, original_bytes, kept_bytes, was_truncated).
    """
    try:
        file_size = path.stat().st_size
    except OSError:
        return "", 0, 0, False

    if file_size <= max_bytes:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(), file_size, file_size, False
        except OSError:
            return "", file_size, 0, False

    # Read only the tail portion
    try:
        with open(path, "rb") as f:
            f.seek(max(0, file_size - max_bytes))
            raw = f.read()
    except OSError:
        return "", file_size, 0, False

    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # BLE001:REVIEWED
        text = str(raw)

    # Try to find a clean line boundary start
    first_newline = text.find("\n")
    if first_newline > 0 and first_newline < 200:
        text = text[first_newline + 1 :]

    content = (
        f"[TRUNCATED: showing last ~{max_bytes // 1024}KB of {file_size} bytes "
        f"({file_size // 1024}KB) original]\n" + text
    )
    return content, file_size, len(content.encode("utf-8")), True


def read_json_snapshot(path: Path) -> tuple[str, int, int, bool]:
    """Read a small JSON file in full (no truncation)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        size = len(content.encode("utf-8"))
        return content, size, size, False
    except OSError:
        return "", 0, 0, False


def filter_log_by_level(content: str) -> tuple[str, int, int]:
    """Filter text log content to WARNING+ lines only.

    Returns (filtered_content, original_lines, kept_lines).
    """
    lines = content.split("\n")
    original = len(lines)
    filtered = [l for l in lines if any(kw in l for kw in LOG_LEVEL_KEYWORDS)]
    return "\n".join(filtered), original, len(filtered)


def generate_docket_id() -> str:
    """Auto-generate a docket ID from current date."""
    now = utc_now()
    date_str = now.strftime("%Y%m%d")
    # Scan existing evidence dirs for today's dockets to get next NNN
    evidence_dir = EVIDENCE_DIR
    existing = []
    if evidence_dir.exists():
        for item in evidence_dir.iterdir():
            if item.is_dir() and item.name.startswith(f"DQAF-{date_str}-"):
                try:
                    nnn = int(item.name.split("-")[-1])
                    existing.append(nnn)
                except ValueError:
                    pass
    next_nnn = max(existing) + 1 if existing else 1
    return f"DQAF-{date_str}-{next_nnn:03d}"


# ---------------------------------------------------------------------------
# Main collection logic
# ---------------------------------------------------------------------------


def collect_evidence(hours: int, docket_id: str) -> Path:
    """Collect all evidence sources and package into a zip file.

    Returns the path to the generated zip file.
    """
    cutoff = (utc_now() - timedelta(hours=hours)).replace(tzinfo=None)
    timestamp_str = utc_now().strftime("%Y%m%dT%H%M%SZ")
    zip_name = f"{docket_id}_{timestamp_str}.zip"

    evidence_dir = EVIDENCE_DIR / docket_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    zip_path = EVIDENCE_DIR / zip_name

    manifest_lines: list[str] = [
        "DQAF Evidence Collection Manifest",
        "=================================",
        f"Docket ID:    {docket_id}",
        f"Collected at: {utc_now().isoformat()}",
        f"Time window:  {hours}h (since {cutoff.isoformat()})",
        "",
        f"{'Source':<20} {'File':<50} {'Orig Size':>10} {'Kept Size':>10} {'Trunc?':>6}",
        f"{'-'*20} {'-'*50} {'-'*10} {'-'*10} {'-'*6}",
    ]

    total_zip_size_estimate = 0
    high_risk_sources: list[str] = []

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for spec in SOURCE_SPECS:
            source_name = spec["name"]
            file_type = spec["file_type"]
            truncation = spec["truncation"]

            for data_root in DATA_ROOTS:
                if not data_root.exists():
                    continue

                files = glob_files(data_root, spec["glob_patterns"], cutoff)
                # Skip files in evidence dir itself (avoid self-inclusion)
                files = [f for f in files if "dqaf" not in str(f)]

                for file_path in files:
                    orig_size = 0
                    kept_size = 0
                    was_truncated = False
                    content = ""
                    risk_flag = ""

                    # Check for high-risk source
                    try:
                        real_size = file_path.stat().st_size
                        if real_size > HIGH_RISK_SOURCE_BYTES:
                            high_risk_sources.append(
                                f"{file_path.name} ({real_size // 1024 // 1024}MB)"
                            )
                            risk_flag = " [HIGH_RISK_SOURCE]"
                    except OSError:
                        pass

                    # Read and truncate based on file type
                    if file_type == "jsonl":
                        if truncation == "line_cap":
                            if source_name == "golden_master":
                                content, orig_count, kept, was_truncated = filter_jsonl_tail_only(
                                    file_path, spec["tail_lines"]
                                )
                                orig_size = orig_count
                                kept_size = kept
                            else:
                                content, orig_count, kept, was_truncated = filter_jsonl_by_time(
                                    file_path,
                                    cutoff,
                                    spec["max_lines"],
                                    spec["head_lines"],
                                    spec["tail_lines"],
                                )
                                orig_size = orig_count
                                kept_size = kept
                        else:
                            content, orig_bytes, kept_bytes, _ = read_json_snapshot(file_path)
                            orig_size = orig_bytes
                            kept_size = kept_bytes

                    elif file_type == "text":
                        content, orig_bytes, kept_bytes, was_truncated = filter_text_log_tail(
                            file_path, spec["max_bytes"]
                        )
                        # Also filter by log level
                        content, _, _ = filter_log_by_level(content)
                        kept_bytes = len(content.encode("utf-8"))
                        orig_size = orig_bytes
                        kept_size = kept_bytes

                    elif file_type == "json_snapshot":
                        content, orig_bytes, kept_bytes, _ = read_json_snapshot(file_path)
                        orig_size = orig_bytes
                        kept_size = kept_bytes

                    # Write to zip (use relative path from data root)
                    rel_path = file_path.relative_to(data_root)
                    arcname = f"{data_root.name}/{rel_path}"
                    zf.writestr(arcname, content)
                    kept_bytes = len(content.encode("utf-8"))
                    total_zip_size_estimate += kept_bytes

                    # Format sizes for manifest
                    if isinstance(orig_size, int) and orig_size > 10_000_000:
                        orig_display = f"{orig_size // 1024 // 1024}MB"
                    elif isinstance(orig_size, int) and orig_size > 10_000:
                        orig_display = f"{orig_size // 1024}KB"
                    else:
                        orig_display = str(orig_size)

                    if kept_bytes > 10_000_000:
                        kept_display = f"{kept_bytes // 1024 // 1024}MB"
                    elif kept_bytes > 10_000:
                        kept_display = f"{kept_bytes // 1024}KB"
                    else:
                        kept_display = str(kept_bytes)

                    trunc_marker = "YES" if was_truncated else "no"
                    manifest_lines.append(
                        f"{source_name:<20} {arcname:<50} {orig_display:>10} "
                        f"{kept_display:>10} {trunc_marker:>6}{risk_flag}"
                    )

        # Add MANIFEST.txt as the last file in the zip
        if high_risk_sources:
            manifest_lines.append("")
            manifest_lines.append("⚠️  HIGH_RISK SOURCES (original > 50MB):")
            for src in high_risk_sources:
                manifest_lines.append(f"  - {src}")
            manifest_lines.append(
                "  ACTION: Manually inspect these files before feeding to AI agent."
            )

        manifest_lines.append("")
        manifest_lines.append(
            f"Total estimated uncompressed size: " f"{total_zip_size_estimate // 1024}KB"
        )
        if total_zip_size_estimate > MAX_ZIP_SIZE_WARN:
            manifest_lines.append(
                "⚠️  WARNING: Total evidence size exceeds 5MB. "
                "Consider narrowing --hours or manually trimming before analysis."
            )

        manifest_content = "\n".join(manifest_lines) + "\n"
        zf.writestr("MANIFEST.txt", manifest_content)

    # Post-zip size check
    try:
        actual_zip_size = zip_path.stat().st_size
        if actual_zip_size > MAX_ZIP_SIZE_WARN:
            print(
                f"⚠️  WARNING: Generated zip file is "
                f"{actual_zip_size // 1024}KB (>5MB limit). "
                f"Consider using a narrower --hours window.",
                file=sys.stderr,
            )
    except OSError:
        pass

    return zip_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DQAF ECoL — Evidence Collection Station",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/dqaf_collect.py --hours 2 --docket-id DQAF-20260606-001
  python scripts/dqaf_collect.py --hours 6
  python scripts/dqaf_collect.py --hours 1 --docket-id DQAF-20260606-002
        """,
    )
    parser.add_argument(
        "--hours",
        type=float,
        required=True,
        help="Time window in hours to collect evidence for (e.g., 2, 0.5)",
    )
    parser.add_argument(
        "--docket-id",
        type=str,
        default=None,
        help="Docket ID (e.g., DQAF-20260606-001). Auto-generated if omitted.",
    )
    args = parser.parse_args()

    docket_id = args.docket_id or generate_docket_id()
    if not args.docket_id:
        print(f"Auto-generated docket ID: {docket_id}")

    print(f"Collecting evidence for {docket_id} (last {args.hours}h)...")
    zip_path = collect_evidence(args.hours, docket_id)

    try:
        zip_size_kb = zip_path.stat().st_size // 1024
        print(f"Evidence package created: {zip_path} ({zip_size_kb}KB)")
    except OSError:
        print(f"Evidence package created: {zip_path}")

    # Print manifest summary
    print("\nContents (see MANIFEST.txt for details):")
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in sorted(zf.namelist()):
            info = zf.getinfo(name)
            print(f"  {info.filename:<60} {info.file_size // 1024:>5}KB")

    print("\nTo use: attach this zip to the DQAF diagnostic prompt.")


if __name__ == "__main__":
    main()
