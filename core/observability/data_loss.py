"""Thread-safe JSONL appender for the Institutional Data Loss Register.

DLR-001 (2026-06-17): 34 real BTC opens lost because ``entry_context.vector``
was missing from journal entries.  This module provides the single shared
utility for Layer 2 (entry_context_guard) and Layer 3 (cross-symbol audit)
to record confirmed data loss events in ``data_loss_register.jsonl``.

Usage::

    from core.observability.data_loss import append_loss_record

    append_loss_record(
        base_dir=Path("data_btc"),
        detector="L2",
        data_type="missing_entry_context.vector",
        affected_count=3,
        sample_ids=["3911586864", "3911814994"],
    )
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def append_loss_record(
    *,
    base_dir: Path,
    detector: str,
    data_type: str,
    affected_count: int,
    sample_ids: list[str] | None = None,
    severity: str = "Sev 2",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Append a data loss event to the institutional register.

    Thread/process-safe via atomic exclusive-create lock pattern,
    compatible with Windows (no ``fcntl`` dependency).

    Args:
        base_dir: Symbol-specific data directory (``data_btc`` or ``data``).
        detector: ``"L1"``, ``"L2"``, or ``"L3"`` — which defense layer caught it.
        data_type: Slug describing what was lost, e.g.
            ``"missing_entry_context.vector"``.
        affected_count: Number of affected records.
        sample_ids: Up to 3 representative ticket/message IDs.
        severity: Severity label for the register entry.
        extra: Arbitrary additional diagnostic fields.

    Returns:
        Path to the register file written.
    """
    register_path = base_dir / "state" / "data_loss_register.jsonl"
    register_path.parent.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
        "detector": detector,
        "severity": severity,
        "data_type": data_type,
        "affected_count": affected_count,
        "sample_ticket_ids": (sample_ids or [])[:3],
        "root_cause": None,  # Human fills in later
        "preventive_measure": None,  # Human fills in later
    }
    if extra:
        record["diagnostic"] = extra

    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    encoded = line.encode("utf-8")

    # Atomic exclusive-create append: each writer creates a unique temp
    # file, then we use a brief spin-lock to serialize the append.
    # On Windows os.O_CREAT|O_EXCL on the register itself would block
    # concurrent readers, so we use a sidecar .lock file instead.
    lock_path = register_path.with_suffix(register_path.suffix + ".loss_lock")
    deadline = time.time() + 5.0
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.time() > deadline:
                # Degrade gracefully: append without lock rather than lose the record
                break
            time.sleep(0.02)

    try:
        with open(register_path, "ab") as fh:
            fh.write(encoded)
    finally:
        import contextlib
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()

    return register_path
