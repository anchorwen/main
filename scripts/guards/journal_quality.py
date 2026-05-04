"""Journal-based execution quality thresholds (live_trade_journal aggregates)."""

from __future__ import annotations

from typing import Any


def consecutive_rejected_tail(latest_records: list[dict]) -> int:
    c = 0
    for rec in reversed(latest_records):
        if str(rec.get("ack_status", "")).lower() == "rejected":
            c += 1
        else:
            break
    return c


def evaluate_guard(
    *,
    report: dict[str, Any],
    max_rejection_rate: float,
    max_rejections: int,
    max_consecutive_rejected: int,
    min_samples: int,
) -> tuple[bool, list[str]]:
    """Return (triggered, reasons)."""
    reasons: list[str] = []
    total = int(report.get("total", 0))
    rejected = int((report.get("counts") or {}).get("rejected", 0))
    rejection_rate = float(report.get("rejection_rate", 0.0))
    consecutive_rejected = consecutive_rejected_tail(list(report.get("latest_records") or []))
    if total >= min_samples and rejection_rate > max_rejection_rate:
        reasons.append(f"rejection_rate_exceeded({rejection_rate:.3f}>{max_rejection_rate:.3f})")
    if rejected > max_rejections:
        reasons.append(f"rejections_exceeded({rejected}>{max_rejections})")
    if consecutive_rejected > max_consecutive_rejected:
        reasons.append(
            f"consecutive_rejected_exceeded({consecutive_rejected}>{max_consecutive_rejected})"
        )
    return (len(reasons) > 0, reasons)
