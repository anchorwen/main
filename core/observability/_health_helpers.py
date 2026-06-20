"""Shared helpers for data_health_service.py and health_checks.py.

Extracted to avoid circular imports after the HealthCheckMethods mixin split
(Strangler Fig #28).
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _age_minutes(iso_ts: str | None) -> float:
    """Compute age in minutes from an ISO timestamp string."""
    if not iso_ts:
        return -1.0
    try:
        s = str(iso_ts)[:19]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - dt).total_seconds() / 60.0
    except (ValueError, TypeError, OSError):
        return -1.0


def _safe_json_load(path: str) -> dict[str, Any] | None:
    """Load a JSON file; return None on any failure (Iron Law #1)."""
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # BLE001:REVIEWED — Iron Law #1: never crash on bad data
        return None


def _safe_jsonl_count(path: str) -> int | None:
    """Count lines in a JSONL file; return None on failure."""
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except Exception:  # BLE001:REVIEWED
        return None


def _safe_jsonl_last(path: str, tail_bytes: int = 8192) -> dict[str, Any] | None:
    """Read the last line of a JSONL file using tail-read; return None on failure."""
    try:
        if not os.path.exists(path):
            return None
        fsize = os.path.getsize(path)
        if fsize == 0:
            return None
        with open(path, encoding="utf-8") as f:
            if fsize <= tail_bytes:
                last = None
                for line in f:
                    line = line.strip()
                    if line:
                        last = line
                return json.loads(last) if last else None
            f.seek(max(0, fsize - tail_bytes))
            chunk = f.read(tail_bytes)
            lines = chunk.strip().split("\n")
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            f.seek(max(0, fsize - tail_bytes * 2))
            chunk = f.read(tail_bytes * 2)
            lines = chunk.strip().split("\n")
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
            return None
    except Exception:  # BLE001:REVIEWED
        return None


def _safe_jsonl_tail_stats(path: str, max_scan: int = 500) -> dict[str, Any]:
    """Scan the last N lines of a JSONL for basic stats."""
    try:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        tail = lines[-max_scan:] if total > max_scan else lines

        pnl_null = 0
        close_count = 0
        open_count = 0
        retry_count = 0
        labels: dict[str, int] = {}

        for line in tail:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = rec.get("action", "")
            if action == "close":
                close_count += 1
                if rec.get("pnl") is None:
                    pnl_null += 1
                label = rec.get("label", "unknown")
                labels[label] = labels.get(label, 0) + 1
                ack = rec.get("ack_status", "")
                if ack == "rejected":
                    retry_count += 1
            elif action == "open":
                open_count += 1

        pnl_null_rate = pnl_null / close_count if close_count > 0 else 0.0

        return {
            "total_lines": total,
            "tail_scanned": len(tail),
            "close_count_tail": close_count,
            "open_count_tail": open_count,
            "pnl_null_count": pnl_null,
            "pnl_null_rate": round(pnl_null_rate, 4),
            "retry_count": retry_count,
            "label_distribution": labels,
        }
    except Exception:  # BLE001:REVIEWED
        return {}
