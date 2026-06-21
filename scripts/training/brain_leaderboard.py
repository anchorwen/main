"""Model performance leaderboard: aggregate decision records by brain_id.

Reads decision records to extract which brain supported each decision, then
cross-references with trade labels (when available) for win-rate attribution.

Usage:
  python scripts/training/brain_leaderboard.py --decisions-dir data/decisions
  python scripts/training/brain_leaderboard.py --decisions-dir data/decisions --labels data/reports/live_labels.jsonl
  python scripts/training/brain_leaderboard.py --decisions-dir data/decisions --output data/reports/leaderboard.json
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "brain_leaderboard.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_decisions(
    decisions_dir: Path,
    *,
    date_filter: str | None = None,
    symbol: str | None = None,
) -> list[dict[str, Any]]:
    """Load decision records from partitioned JSONL files.

    By default matches the canonical MT5 contract symbol ``XAUUSDc`` and
    legacy files (``XAUUSD`` without ``c`` suffix).  Pass ``symbol="BTCUSDc"``
    for BTC data.

    DQAF-20260622-048: The previous hardcoded glob ``XAUUSD.decisions.jsonl``
    (without 'c') silently ignored 95.7% of decision files (45/47),
    causing the leaderboard to show only 1 brain instead of 68.
    """
    records: list[dict[str, Any]] = []
    if not decisions_dir.is_dir():
        return records

    if symbol is None:
        symbol = "XAUUSDc"

    # Canonical pattern first, then legacy (without MT5 contract suffix 'c')
    patterns: list[str] = []
    prefix = f"{date_filter}/" if date_filter else "**/"
    patterns.append(f"{prefix}{symbol}.decisions.jsonl")

    if symbol.endswith("c"):
        legacy_symbol = symbol[:-1]  # "XAUUSDc" → "XAUUSD"
        legacy_pattern = f"{prefix}{legacy_symbol}.decisions.jsonl"
        patterns.append(legacy_pattern)
        # Institutional Polish: explicit protest against non-canonical data
        logger.warning(
            "DEBT: Loading legacy non-canonical decision files via pattern %s",
            legacy_pattern,
        )

    seen_paths: set[Path] = set()
    for pattern in patterns:
        for path in sorted(decisions_dir.glob(pattern)):
            if path in seen_paths:
                continue  # protect against overlap when date_filter is used
            seen_paths.add(path)
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("schema_version") == "decision_record.v1":
                    records.append(rec)
    return records


def load_labels(labels_path: Path) -> list[dict[str, Any]]:
    """Load training labels JSONL."""
    if not labels_path.exists():
        return []
    labels: list[dict[str, Any]] = []
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("schema_version") == "training_label.v1":
            labels.append(rec)
    return labels


def _link_decision_to_label(
    decision_time: str, labels: list[dict[str, Any]], window_minutes: int = 60
) -> dict[str, Any] | None:
    """Find the closest label within a time window of the decision."""
    if not labels or not decision_time:
        return None

    try:
        dt_decision = datetime.fromisoformat(decision_time.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except (ValueError, TypeError):
        return None

    best: dict[str, Any] | None = None
    best_delta = float("inf")

    for lbl in labels:
        open_time = lbl.get("open_recorded_at")
        if not open_time:
            continue
        try:
            dt_label = datetime.fromisoformat(str(open_time).replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except (ValueError, TypeError):
            continue
        delta = abs((dt_decision - dt_label).total_seconds())
        if delta < best_delta and delta <= window_minutes * 60:
            best_delta = delta
            best = lbl

    return best


def aggregate_by_brain(
    decisions: list[dict[str, Any]],
    *,
    labels: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Aggregate decisions by brain_id, computing per-brain metrics."""
    brain_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "brain_id": "",
            "signal_count": 0,
            "long_count": 0,
            "short_count": 0,
            "neutral_count": 0,
            "linked_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
        }
    )

    for rec in decisions:
        supporting = rec.get("attribution", {}).get("supporting_brains", [])
        side = rec.get("labels", {}).get("decision_side", "unknown")
        event_time = rec.get("event_time", "")

        for brain_id in supporting:
            stats = brain_stats[brain_id]
            stats["brain_id"] = brain_id
            stats["signal_count"] += 1
            if side == "long":
                stats["long_count"] += 1
            elif side == "short":
                stats["short_count"] += 1
            else:
                stats["neutral_count"] += 1

            # Link to trade label if available
            if labels:
                matched = _link_decision_to_label(event_time, labels)
                if matched and matched.get("is_closed"):
                    stats["linked_trades"] += 1
                    pnl = matched.get("pnl")
                    if pnl is not None:
                        stats["total_pnl"] += pnl
                    if matched.get("label") == "win":
                        stats["wins"] += 1
                    elif matched.get("label") == "loss":
                        stats["losses"] += 1

    result = []
    for bid, raw in brain_stats.items():
        n = raw["signal_count"]
        entry = {
            "brain_id": bid,
            "signal_count": n,
            "direction_distribution": {
                "long_pct": round(raw["long_count"] / n, 4) if n > 0 else 0.0,
                "short_pct": round(raw["short_count"] / n, 4) if n > 0 else 0.0,
                "neutral_pct": round(raw["neutral_count"] / n, 4) if n > 0 else 0.0,
            },
        }
        linked = raw["linked_trades"]
        if linked > 0:
            entry["trade_performance"] = {
                "linked_trades": linked,
                "win_rate": round(raw["wins"] / linked, 4) if linked > 0 else None,
                "total_pnl": round(raw["total_pnl"], 6),
                "avg_pnl": round(raw["total_pnl"] / linked, 6) if linked > 0 else None,
            }
        else:
            entry["trade_performance"] = None
        result.append(entry)

    result.sort(key=lambda x: x["signal_count"], reverse=True)
    return result


def build_report(
    decisions_dir: Path,
    *,
    date_filter: str | None = None,
    labels_path: Path | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    decisions = load_decisions(decisions_dir, date_filter=date_filter, symbol=symbol)
    labels = load_labels(labels_path) if labels_path else []

    if not decisions:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "decisions_dir": str(decisions_dir),
            "total_decisions": 0,
            "error": "no_decision_records",
        }

    leaderboard = aggregate_by_brain(decisions, labels=labels if labels else None)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "decisions_dir": str(decisions_dir),
        "date_filter": date_filter,
        "total_decisions": len(decisions),
        "total_brains": len(leaderboard),
        "labels_available": len(labels) > 0,
        "leaderboard": leaderboard,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brain_leaderboard")
    p.add_argument(
        "--decisions-dir",
        type=Path,
        default=Path("data/decisions"),
        help="Directory containing partitioned decision JSONL files",
    )
    p.add_argument("--date", default=None, help="Date filter, e.g. 2026-04-26")
    p.add_argument(
        "--labels",
        type=Path,
        default=None,
        help="Path to training_labels.jsonl for trade outcome linking",
    )
    p.add_argument("--output", default=None, help="Write leaderboard JSON to file")
    p.add_argument(
        "--symbol",
        default="XAUUSDc",
        help="Trading symbol (XAUUSDc or BTCUSDc) for decision file matching",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        args.decisions_dir,
        date_filter=args.date,
        labels_path=args.labels,
        symbol=args.symbol,
    )
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"[brain_leaderboard] Leaderboard written to {out}")

    if report.get("error"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
