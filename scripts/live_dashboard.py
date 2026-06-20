"""Live dashboard: human-readable daily summary of the entire trading system.

Aggregates data from journal, labels, leaderboard, performance tracker,
governance, and feature store into a single readable report.

Usage:
  python scripts/live_dashboard.py
  python scripts/live_dashboard.py --base-dir data --date 2026-05-04
  python scripts/live_dashboard.py --json
  python scripts/live_dashboard.py --output data/reports/dashboard.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import fail_open_guard

SCHEMA_VERSION = "live_dashboard.v1"

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _today_key() -> str:
    return datetime.now(UTC).replace(tzinfo=None).date().isoformat()


# ── Data collectors (each is independent, failures are non-fatal) ──


def _collect_journal(base_dir: Path, date_key: str) -> dict[str, Any]:
    """Count today's journal entries by status."""
    journal_path = base_dir / "live_trade_journal.jsonl"
    result: dict[str, Any] = {
        "path": str(journal_path),
        "exists": False,
        "total": 0,
        "accepted": 0,
        "rejected": 0,
        "acknowledged": 0,
        "other": 0,
    }
    if not journal_path.exists():
        return result
    result["exists"] = True
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if date_key and not str(rec.get("recorded_at", "")).startswith(date_key):
            continue
        result["total"] += 1
        ack = rec.get("ack_status", "other")
        if ack == "accepted":
            result["accepted"] += 1
        elif ack == "rejected":
            result["rejected"] += 1
        elif ack == "acknowledged":
            result["acknowledged"] += 1
        else:
            result["other"] += 1
    return result


def _collect_labels(base_dir: Path, date_key: str) -> dict[str, Any]:
    """Summarize P&L labels for today."""
    labels_path = base_dir / "reports" / "live_labels.jsonl"
    result: dict[str, Any] = {
        "exists": False,
        "total": 0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "unlabeled": 0,
        "total_pnl": 0.0,
    }
    if not labels_path.exists():
        return result
    result["exists"] = True
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if date_key and not str(rec.get("open_recorded_at", "")).startswith(date_key):
            continue
        result["total"] += 1
        lbl = rec.get("label", "unlabeled")
        if lbl == "win":
            result["wins"] += 1
        elif lbl == "loss":
            result["losses"] += 1
        elif lbl == "breakeven":
            result["breakeven"] += 1
        else:
            result["unlabeled"] += 1
        pnl = rec.get("pnl")
        if pnl is not None:
            result["total_pnl"] += pnl
    result["total_pnl"] = round(result["total_pnl"], 2)
    if result["total"] > 0:
        result["win_rate"] = round(result["wins"] / max(result["wins"] + result["losses"], 1), 4)
    return result


def _collect_tracker(base_dir: Path) -> dict[str, Any]:
    """Load brain performance summaries."""
    tracker_path = base_dir / "brain_performance.json"
    if not tracker_path.exists():
        return {"exists": False, "brains": []}
    try:
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        tracker = BrainPerformanceTracker.load(tracker_path)
        summaries = tracker.get_all_summaries()
        return {
            "exists": True,
            "brains": summaries,
            "path": str(tracker_path),
        }
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_dashboard:_collect_tracker"):
            return {"exists": False, "error": str(exc)[:200]}
def _collect_governance(base_dir: Path) -> dict[str, Any]:
    """Load governance brain states."""
    gov_path = base_dir / "governance_state.json"
    if not gov_path.exists():
        return {"exists": False, "brains": {}}
    try:
        from core.governance.governance_service import GovernanceService

        gov = GovernanceService.load(gov_path)
        states = gov.get_all_states()
        transitions = gov.get_transition_log()
        return {
            "exists": True,
            "brains": states,
            "transition_count": len(transitions),
            "recent_transitions": transitions[-5:] if transitions else [],
            "path": str(gov_path),
        }
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_dashboard:_collect_governance"):
            return {"exists": False, "error": str(exc)[:200]}
def _collect_leaderboard(base_dir: Path, date_key: str) -> dict[str, Any]:
    """Run brain leaderboard aggregation."""
    decisions_dir = base_dir / "decisions"
    labels_path = base_dir / "reports" / "live_labels.jsonl"
    if not decisions_dir.is_dir():
        return {"exists": False, "brains": []}
    try:
        from scripts.training.brain_leaderboard import (
            aggregate_by_brain,
            load_decisions,
            load_labels,
        )

        decisions = load_decisions(decisions_dir, date_filter=date_key)
        labels = load_labels(labels_path) if labels_path.exists() else []
        lb = aggregate_by_brain(decisions, labels=labels if labels else None)
        return {
            "exists": True,
            "total_decisions": len(decisions),
            "total_brains": len(lb),
            "brains": lb,
        }
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_dashboard:_collect_leaderboard"):
            return {"exists": False, "error": str(exc)[:200]}
def _collect_features(base_dir: Path) -> dict[str, Any]:
    """Check feature store status."""
    fs_dir = base_dir / "feature_store"
    if not fs_dir.is_dir():
        return {"exists": False, "row_count": 0}
    try:
        from core.features.local_feature_store import LocalFeatureStore

        store = LocalFeatureStore(str(fs_dir))
        latest = store.latest(symbol="XAUUSD", timeframe="M1")
        # Count total rows by scanning the features file
        features_file = fs_dir / "records" / "symbol=XAUUSD" / "timeframe=M1" / "features.jsonl"
        row_count = 0
        if features_file.exists():
            row_count = sum(
                1 for _ in features_file.read_text(encoding="utf-8").splitlines() if _.strip()
            )
        return {
            "exists": True,
            "row_count": row_count,
            "latest_event_time": getattr(latest, "event_time", "") if latest else "",
        }
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_dashboard:_collect_features"):
            return {"exists": False, "error": str(exc)[:200]}
def _collect_flag(base_dir: Path) -> dict[str, Any]:
    """Check dispatch block flag."""
    flag_path = base_dir / "live_dispatch_block.flag"
    if not flag_path.exists():
        return {"active": False}
    try:
        payload = json.loads(flag_path.read_text(encoding="utf-8"))
        return {"active": True, "payload": payload}
    except (json.JSONDecodeError, OSError):
        return {"active": True, "payload": {}}


# ── Formatting ──


def _fmt_pnl(value: float) -> str:
    if value > 0:
        return f"+${value:,.2f}"
    if value < 0:
        return f"-${abs(value):,.2f}"
    return "$0.00"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "  N/A"
    return f"{value * 100:6.1f}%"


def _bar(value: float, width: int = 20) -> str:
    """Simple ASCII bar chart."""
    filled = int(round(value * width))
    return "#" * filled + "-" * (width - filled)


def _build_text_report(data: dict[str, Any]) -> str:
    """Render the dashboard data dict as a human-readable text report."""
    lines: list[str] = []
    sep = "-" * 60

    # Header
    lines.append("")
    lines.append("=" * 60)
    lines.append("  QUANT OS -- DAILY DASHBOARD")
    lines.append("=" * 60)
    lines.append(
        "  {:20s}  {:>32s}".format(
            f"Date: {data['date_key']}", f"Generated: {data['generated_at'][:16]}"
        )
    )
    lines.append("=" * 60)
    lines.append("")

    # Run state
    flag = data.get("dispatch_flag", {})
    journal = data.get("journal", {})
    lines.append(sep)
    lines.append("  RUN STATE")
    lines.append(sep)
    if flag.get("active"):
        lines.append("  STATUS: [BLOCKED] DISPATCH BLOCKED")
        reason = flag.get("payload", {}).get("reason", "unknown")
        lines.append(f"  Reason: {reason}")
    elif journal.get("total", 0) > 0:
        lines.append("  STATUS: [ACTIVE] (trades today)")
    else:
        lines.append("  STATUS: [IDLE] (no trades today)")
    lines.append(f"  Journal entries today: {journal.get('total', 0)}")
    lines.append(
        f"  Accepted: {journal.get('accepted', 0)}  "
        f"Rejected: {journal.get('rejected', 0)}  "
        f"Acknowledged: {journal.get('acknowledged', 0)}"
    )
    lines.append("")

    # P&L Summary
    labels = data.get("labels", {})
    lines.append(sep)
    lines.append("  P&L SUMMARY")
    lines.append(sep)
    if labels.get("exists"):
        lines.append(f"  Closed trades today: {labels['total']}")
        lines.append(
            f"  Wins: {labels['wins']}  "
            f"Losses: {labels['losses']}  "
            f"Breakeven: {labels['breakeven']}  "
            f"Unlabeled: {labels['unlabeled']}"
        )
        if labels["total"] > 0:
            lines.append(f"  Win rate: {_fmt_pct(labels.get('win_rate'))}")
            lines.append(f"  Total P&L: {_fmt_pnl(labels['total_pnl'])}")
    else:
        lines.append("  (no labels file -- trades not yet closed/labeled)")
    lines.append("")

    # Brain Leaderboard
    lb = data.get("leaderboard", {})
    lines.append(sep)
    lines.append("  BRAIN LEADERBOARD")
    lines.append(sep)
    if lb.get("exists") and lb.get("brains"):
        lines.append(
            f"  Total decisions: {lb['total_decisions']}  |  Brains tracked: {lb['total_brains']}"
        )
        lines.append("")
        # Header row
        lines.append(
            f"  {'Brain':<10s} {'Signals':>7s}  {'Long':>6s}  {'Short':>6s}  "
            f"{'Neutral':>7s}  {'Win Rate':>8s}  {'P&L':>10s}"
        )
        lines.append("  " + "-" * 56)
        for b in lb["brains"][:15]:
            dist = b.get("direction_distribution", {})
            perf = b.get("trade_performance") or {}
            wr = _fmt_pct(perf.get("win_rate"))
            pnl_str = _fmt_pnl(perf.get("total_pnl", 0.0)) if perf else "     N/A"
            lines.append(
                f"  {b['brain_id']:<10s} {b['signal_count']:>7d}  "
                f"{_fmt_pct(dist.get('long_pct', 0))}  "
                f"{_fmt_pct(dist.get('short_pct', 0))}  "
                f"{_fmt_pct(dist.get('neutral_pct', 0))}  "
                f"{wr}  {pnl_str:>10s}"
            )
    else:
        lines.append("  (no decision records -- run shadow ensemble first)")
    lines.append("")

    # Brain Health (from tracker)
    tracker = data.get("tracker", {})
    lines.append(sep)
    lines.append("  BRAIN HEALTH (from Performance Tracker)")
    lines.append(sep)
    if tracker.get("exists") and tracker.get("brains"):
        lines.append(
            f"  {'Brain':<10s} {'Samples':>7s}  {'Mean':>8s}  {'Health':>12s}  Recommendation"
        )
        lines.append("  " + "-" * 56)
        for b in tracker["brains"]:
            health = b.get("health_signal", "?")
            tag = {
                "healthy": "[OK]",
                "stable": "[--]",
                "degraded": "[!!]",
                "critical": "[XX]",
                "insufficient_data": "[..]",
            }.get(health, "[..]")
            lines.append(
                f"  {tag} {b['brain_id']:<7s} {b['sample_count']:>7d}  "
                f"{b['composite_mean']:>8.4f}  {health:>12s}  "
                f"{b.get('recommendation', 'observe')}"
            )
    else:
        lines.append("  (no tracker data -- run live_intent_loop to accumulate)")
    lines.append("")

    # Governance
    gov = data.get("governance", {})
    lines.append(sep)
    lines.append("  GOVERNANCE STATUS")
    lines.append(sep)
    if gov.get("exists") and gov.get("brains"):
        lines.append(f"  Total transitions: {gov.get('transition_count', 0)}")
        lines.append(f"  {'Brain':<10s}  Status       Transitions  Freezes")
        lines.append("  " + "─" * 50)
        for bid, state in gov["brains"].items():
            lines.append(
                f"  {bid:<10s}  {state.get('status', '?'):<12s}  "
                f"{state.get('transition_count', 0):>11d}  "
                f"{state.get('freeze_count', 0):>7d}"
            )
        if gov.get("recent_transitions"):
            lines.append("")
            lines.append("  Recent transitions:")
            for t in gov["recent_transitions"]:
                lines.append(
                    f"    {t.get('brain_id', '?')}: "
                    f"{t.get('from_status', '?')} ->{t.get('to_status', '?')} "
                    f"({t.get('reason', '')})"
                )
    else:
        lines.append("  (no governance state -- run daily_ops to initialize)")
    lines.append("")

    # Feature Store
    fs = data.get("features", {})
    lines.append(sep)
    lines.append("  FEATURE STORE")
    lines.append(sep)
    if fs.get("exists"):
        lines.append(f"  Features rows: {fs['row_count']:,}")
        if fs.get("latest_event_time"):
            lines.append(f"  Latest event:  {fs['latest_event_time']}")
    else:
        lines.append("  (no feature store)")
    lines.append("")

    # Dispatch flag
    lines.append(sep)
    lines.append("  DISPATCH GUARD")
    lines.append(sep)
    if flag.get("active"):
        lines.append("  [BLOCKED] BLOCK ACTIVE -- live dispatch is suppressed")
        lines.append(f"  Flag: {json.dumps(flag.get('payload', {}), default=str)}")
    else:
        lines.append("  [OK] CLEAR -- live dispatch allowed")
    lines.append("")

    # Footer
    lines.append(sep)
    lines.append(f"  Schema: {data['schema_version']}  |  Errors: {len(data.get('errors', []))}")
    lines.append(sep)
    lines.append("")

    return "\n".join(lines)


# ── Main builder ──


def build_dashboard(
    base_dir: str = "data",
    *,
    date_key: str | None = None,
) -> dict[str, Any]:
    """Collect all dashboard sections and return a structured report.

    Args:
        base_dir: Base data directory.
        date_key: UTC date key; defaults to today.

    Returns:
        Structured dict with all sections plus a pre-rendered text block.
    """
    date = date_key or _today_key()
    base = Path(base_dir)
    errors: list[str] = []

    sections: dict[str, Any] = {}

    # Collect each section independently
    sections["journal"] = _collect_journal(base, date)

    sections["labels"] = _collect_labels(base, date)

    try:
        sections["tracker"] = _collect_tracker(base)
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_dashboard:build_dashboard"):
            errors.append(f"tracker: {exc}")
            sections["tracker"] = {"exists": False, "error": str(exc)[:200]}
    try:
        sections["governance"] = _collect_governance(base)
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_dashboard:build_dashboard"):
            errors.append(f"governance: {exc}")
            sections["governance"] = {"exists": False, "error": str(exc)[:200]}
    try:
        sections["leaderboard"] = _collect_leaderboard(base, date)
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_dashboard:build_dashboard"):
            errors.append(f"leaderboard: {exc}")
            sections["leaderboard"] = {"exists": False, "error": str(exc)[:200]}
    try:
        sections["features"] = _collect_features(base)
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_dashboard:build_dashboard"):
            errors.append(f"features: {exc}")
            sections["features"] = {"exists": False, "error": str(exc)[:200]}
    sections["dispatch_flag"] = _collect_flag(base)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "date_key": date,
        "base_dir": str(base),
        "errors": errors,
        **sections,
    }

    # Pre-render text version
    report["text"] = _build_text_report(report)
    return report


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_dashboard")
    p.add_argument("--base-dir", default="data", help="Base data directory")
    p.add_argument("--date", default=None, help="UTC date key; default=today")
    p.add_argument("--json", action="store_true", help="Output JSON instead of text")
    p.add_argument("--output", type=Path, default=None, help="Write output to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    report = build_dashboard(base_dir=args.base_dir, date_key=args.date)

    if args.json:
        # Strip the pre-rendered text for JSON output
        json_report = {k: v for k, v in report.items() if k != "text"}
        output = json.dumps(json_report, indent=2, ensure_ascii=False, default=str)
    else:
        output = report["text"]

    print(output)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output, encoding="utf-8")

    return 0 if len(report.get("errors", [])) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
