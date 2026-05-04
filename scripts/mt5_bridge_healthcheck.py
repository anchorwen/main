"""MT5 bridge health check report."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mt5_bridge_healthcheck")
    parser.add_argument("--outbox-dir", default="data/mt5_outbox")
    parser.add_argument("--receipt-dir", default="data/receipts")
    parser.add_argument("--max-pending", type=int, default=10)
    parser.add_argument("--max-rejected", type=int, default=0)
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--output", default=None)
    return parser


def _today_key() -> str:
    return datetime.now(UTC).replace(tzinfo=None).date().isoformat()


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_received_at_utc(value: str | None):
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        return None


def build_report(
    *,
    outbox_dir: str,
    receipt_dir: str,
    max_pending: int,
    max_rejected: int,
    lookback_hours: int = 24,
) -> dict:
    outbox_root = Path(outbox_dir)
    receipt_root = Path(receipt_dir)
    date_key = _today_key()
    pending_files = sorted(outbox_root.rglob("*.mt5.json")) if outbox_root.exists() else []
    receipt_files = (
        sorted((receipt_root / date_key).rglob("*.ack.json"))
        if (receipt_root / date_key).exists()
        else []
    )

    counts = {
        "pending": len(pending_files),
        "acked_total": len(receipt_files),
        "accepted": 0,
        "acknowledged": 0,
        "rejected": 0,
        "other": 0,
    }
    rejected_samples: list[dict] = []
    latest_receipts: list[dict] = []
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    lookback_hours = int(max(1, lookback_hours))
    # Keep v1 behavior for global counters; add recent counters for operational readiness.
    recent_cutoff = now_utc.timestamp() - (lookback_hours * 3600)

    recent_counts = {"acked_total": 0, "accepted": 0, "acknowledged": 0, "rejected": 0, "other": 0}
    for path in receipt_files:
        payload = _load_json(path)
        ack_status = str(payload.get("ack_status", "other")).lower()
        if ack_status in counts:
            counts[ack_status] += 1
        else:
            counts["other"] += 1
        dt = _parse_received_at_utc(payload.get("received_at"))
        if dt and dt.timestamp() >= recent_cutoff:
            recent_counts["acked_total"] += 1
            if ack_status in recent_counts:
                recent_counts[ack_status] += 1
            else:
                recent_counts["other"] += 1
        if ack_status == "rejected" and len(rejected_samples) < 5:
            rejected_samples.append(
                {
                    "message_id": payload.get("message_id"),
                    "received_at": payload.get("received_at"),
                    "detail": payload.get("detail", {}),
                    "path": str(path),
                }
            )
        latest_receipts.append(
            {
                "message_id": payload.get("message_id"),
                "ack_status": ack_status,
                "received_at": payload.get("received_at"),
                "path": str(path),
            }
        )
    latest_receipts = sorted(
        latest_receipts, key=lambda x: str(x.get("received_at", "")), reverse=True
    )[:10]

    checks = {
        "pending_within_limit": counts["pending"] <= max_pending,
        "rejected_within_limit": counts["rejected"] <= max_rejected,
        "has_recent_receipts": recent_counts["acked_total"] > 0 or counts["pending"] == 0,
    }
    return {
        "schema_version": "mt5_bridge_healthcheck.v1",
        "generated_at": datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "date_key": date_key,
        "go_live_ready": all(checks.values()),
        "checks": checks,
        "limits": {"max_pending": max_pending, "max_rejected": max_rejected},
        "counts": counts,
        "recent_window": {"hours": lookback_hours, "counts": recent_counts},
        "rejected_samples": rejected_samples,
        "latest_receipts": latest_receipts,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        outbox_dir=args.outbox_dir,
        receipt_dir=args.receipt_dir,
        max_pending=args.max_pending,
        max_rejected=args.max_rejected,
        lookback_hours=args.lookback_hours,
    )
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    return 0 if report["go_live_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
