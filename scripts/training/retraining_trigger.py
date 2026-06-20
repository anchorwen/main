"""Retraining trigger: detect model degradation and orchestrate retraining.

Reads brain leaderboard reports (current + optional baseline), identifies
brains whose performance has degraded, and optionally executes the full
retraining pipeline: dataset rebuild → batch train → register.

Usage:
  # Dry-run: check for degradation without retraining
  python scripts/training/retraining_trigger.py \\
    --leaderboard data/reports/leaderboard.json

  # Compare against a previous baseline
  python scripts/training/retraining_trigger.py \\
    --leaderboard data/reports/leaderboard.json \\
    --baseline data/reports/leaderboard_prev.json

  # Auto-execute retraining for degraded lanes
  python scripts/training/retraining_trigger.py \\
    --leaderboard data/reports/leaderboard.json \\
    --execute \\
    --feature-store-dir data/feature_store \\
    --output-dir data/training
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from core.runtime.fault_handler import fail_open_guard

SCHEMA_VERSION = "retraining_signal.v1"

# ── degradation thresholds ──

WIN_RATE_MIN = 0.40  # win rate below this is degraded
WIN_RATE_DROP_WARN = 0.10  # 10pp drop vs baseline = warning
WIN_RATE_DROP_CRITICAL = 0.20  # 20pp drop vs baseline = critical
MIN_LINKED_TRADES = 5  # need at least 5 linked trades to assess win rate
DIRECTION_COLLAPSE_THRESHOLD = 0.85  # >85% in one direction = lost discrimination
MIN_SIGNAL_COUNT = 3  # fewer signals than this = starvation

# brain_id prefix → lane mapping (for retraining)
BRAIN_TO_LANE: dict[str, str] = {
    "V9": "sur",
    "CRT": "sur",
    "XGBoost": "boost",
    "XGB": "boost",
    "LightGBM": "boost",
    "OU": "arb",
    "OU_Params": "arb",
    "Microstructure_Transformer": "mtx",
    "DeepResMLP": "dl",
    "Online_SGD": "online_sgd",
    "Online_MLP": "online_sgd",
}


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ── degradation detection ──


def _assess_brain(
    entry: dict[str, Any],
    baseline_entry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Assess a single brain leaderboard entry for degradation.

    Returns a signal dict if degradation is detected, None otherwise.
    """
    brain_id = entry.get("brain_id", "unknown")
    signal_count = entry.get("signal_count", 0)
    direction = entry.get("direction_distribution", {})
    trade_perf = entry.get("trade_performance") or {}
    linked_trades = trade_perf.get("linked_trades", 0)
    win_rate = trade_perf.get("win_rate")

    issues: list[dict[str, Any]] = []
    urgency = "low"

    # 1. Signal starvation
    if signal_count < MIN_SIGNAL_COUNT:
        issues.append(
            {
                "type": "signal_starvation",
                "severity": "warning",
                "detail": f"Only {signal_count} signals (min {MIN_SIGNAL_COUNT})",
            }
        )

    # 2. Direction collapse
    long_pct = direction.get("long_pct", 0)
    short_pct = direction.get("short_pct", 0)
    if max(long_pct, short_pct) > DIRECTION_COLLAPSE_THRESHOLD and signal_count >= MIN_SIGNAL_COUNT:
        issues.append(
            {
                "type": "direction_collapse",
                "severity": "warning",
                "detail": f"Long {long_pct:.0%} / Short {short_pct:.0%} (threshold {DIRECTION_COLLAPSE_THRESHOLD:.0%})",
            }
        )

    # 3. Win rate degradation (absolute)
    if win_rate is not None and linked_trades >= MIN_LINKED_TRADES:
        if win_rate < WIN_RATE_MIN:
            severity = "critical" if win_rate <= 0.30 else "warning"
            issues.append(
                {
                    "type": "win_rate_low",
                    "severity": severity,
                    "detail": f"Win rate {win_rate:.1%} (min {WIN_RATE_MIN:.0%}) over {linked_trades} trades",
                }
            )

        # 4. Win rate drop vs baseline
        if baseline_entry is not None:
            baseline_perf = baseline_entry.get("trade_performance") or {}
            baseline_wr = baseline_perf.get("win_rate")
            if baseline_wr is not None:
                drop = baseline_wr - win_rate
                if drop >= WIN_RATE_DROP_CRITICAL:
                    issues.append(
                        {
                            "type": "win_rate_drop_vs_baseline",
                            "severity": "critical",
                            "detail": f"Win rate dropped from {baseline_wr:.1%} to {win_rate:.1%} (Δ -{drop:.1%})",
                        }
                    )
                elif drop >= WIN_RATE_DROP_WARN:
                    issues.append(
                        {
                            "type": "win_rate_drop_vs_baseline",
                            "severity": "warning",
                            "detail": f"Win rate dropped from {baseline_wr:.1%} to {win_rate:.1%} (Δ -{drop:.1%})",
                        }
                    )

    if not issues:
        return None

    # Determine overall urgency
    severities = [i["severity"] for i in issues]
    if "critical" in severities:
        urgency = "critical"
    elif "warning" in severities:
        urgency = "warning"

    lane = _guess_lane(brain_id)
    return {
        "brain_id": brain_id,
        "lane": lane,
        "urgency": urgency,
        "issues": issues,
        "current_stats": {
            "signal_count": signal_count,
            "linked_trades": linked_trades,
            "win_rate": win_rate,
            "direction_long_pct": long_pct,
            "direction_short_pct": short_pct,
        },
    }


# Import shared _guess_lane from champion_challenger (single source of truth)
try:
    from scripts.training.champion_challenger import _guess_lane as _guess_lane
except ImportError:
    # Fallback for environments where champion_challenger isn't available
    def _guess_lane(brain_id: str, *, configs_dir: str | None = None) -> str:
        upper = brain_id.upper()
        for prefix, lane in BRAIN_TO_LANE.items():
            if upper.startswith(prefix.upper()):
                return lane
        for key, lane in BRAIN_TO_LANE.items():
            if key.upper() in upper:
                return lane
        return "unclassified"


def detect_degradation(
    leaderboard: dict[str, Any],
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scan leaderboard entries and produce retraining signals.

    Args:
        leaderboard: Current leaderboard report (brain_leaderboard.v1).
        baseline: Optional previous leaderboard for trend comparison.

    Returns:
        Retraining signal report with per-brain degradation assessments.
    """
    entries = leaderboard.get("leaderboard", [])
    baseline_map: dict[str, dict[str, Any]] = {}
    if baseline is not None:
        for e in baseline.get("leaderboard", []):
            baseline_map[e["brain_id"]] = e

    signals: list[dict[str, Any]] = []
    for entry in entries:
        baseline_entry = baseline_map.get(entry.get("brain_id", ""))
        signal = _assess_brain(entry, baseline_entry)
        if signal is not None:
            signals.append(signal)

    overall_urgency = "ok"
    if any(s["urgency"] == "critical" for s in signals):
        overall_urgency = "critical"
    elif any(s["urgency"] == "warning" for s in signals):
        overall_urgency = "warning"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "leaderboard_generated_at": leaderboard.get("generated_at", ""),
        "baseline_generated_at": baseline.get("generated_at", "") if baseline else None,
        "total_brains_assessed": len(entries),
        "degraded_count": len(signals),
        "overall_urgency": overall_urgency,
        "signals": signals,
    }


# ── retraining orchestration ──


def execute_retraining(
    signals: list[dict[str, Any]],
    *,
    feature_store_dir: Path,
    output_dir: Path,
    labels_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute the retraining pipeline for each degraded lane.

    Pipeline: dataset_builder → run_train_batch → register_brain
    """
    results: list[dict[str, Any]] = []
    lanes = list({s["lane"] for s in signals if s["lane"] != "unknown"})

    if not lanes:
        return {"executed": False, "reason": "no_actionable_lanes", "results": []}

    for lane in lanes:
        lane_signals = [s for s in signals if s["lane"] == lane]
        urgency = "critical" if any(s["urgency"] == "critical" for s in lane_signals) else "warning"

        result: dict[str, Any] = {
            "lane": lane,
            "brain_ids": [s["brain_id"] for s in lane_signals],
            "urgency": urgency,
            "steps": [],
        }

        if dry_run:
            result["steps"].append({"step": "dataset_builder", "status": "dry_run"})
            result["steps"].append({"step": "run_train_batch", "status": "dry_run"})
            result["steps"].append({"step": "register_brain", "status": "dry_run"})
            results.append(result)
            continue

        # Step 1: Build dataset
        resolved_labels = labels_path or Path("data/reports/live_labels.jsonl")
        step1 = _run_step(
            [
                sys.executable,
                "scripts/training/dataset_builder.py",
                "--labels",
                str(resolved_labels),
                "--feature-store-dir",
                str(feature_store_dir),
                "--output-dir",
                str(output_dir),
                "--format",
                "parquet",
            ]
        )
        result["steps"].append(step1)

        if step1["status"] != "ok":
            result["status"] = "failed"
            results.append(result)
            continue

        # Step 2: Run batch training for this lane
        step2 = _run_step(
            [
                sys.executable,
                "scripts/training/run_train_batch.py",
                "--execute",
                "--lane",
                lane,
                "--limit",
                "1",
            ]
        )
        result["steps"].append(step2)

        if step2["status"] != "ok":
            result["status"] = "failed"
            results.append(result)
            continue

        # Step 3: Register new brain (find latest manifest)
        manifests_dir = Path("batch_plans")
        latest_manifest = _find_latest_manifest(manifests_dir, lane)
        if latest_manifest:
            step3 = _run_step(
                [
                    sys.executable,
                    "scripts/training/register_brain.py",
                    "--manifest",
                    str(latest_manifest),
                ]
            )
        else:
            step3 = {"step": "register_brain", "status": "skipped", "reason": "no_manifest_found"}
        result["steps"].append(step3)

        result["status"] = "ok" if step3.get("status") == "ok" else "partial"
        results.append(result)

    executed = any(r.get("status") in ("ok", "partial") for r in results)
    return {"executed": executed, "results": results}


def _run_step(cmd: list[str]) -> dict[str, Any]:
    """Run a subprocess step and return structured result."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = proc.returncode in (0, 2)  # exit code 2 = warnings only in dataset_builder
        return {
            "step": Path(cmd[1]).stem if len(cmd) > 1 else "unknown",
            "status": "ok" if ok else "failed",
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout.strip()[-500:] if proc.stdout else "",
            "stderr_tail": proc.stderr.strip()[-500:] if proc.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"step": Path(cmd[1]).stem if len(cmd) > 1 else "unknown", "status": "timeout"}
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("retraining_trigger:_run_step"):
            return {
                "step": Path(cmd[1]).stem if len(cmd) > 1 else "unknown",
                "status": "error",
                "error": str(exc),
            }
def _find_latest_manifest(manifests_dir: Path, lane: str) -> Path | None:
    """Find the most recent manifest for a given lane."""
    if not manifests_dir.is_dir():
        return None
    # Prefer structured layout: manifests_dir / lane / ... / manifest.json
    lane_dir = manifests_dir / lane
    if lane_dir.is_dir():
        candidates = list(lane_dir.rglob("manifest.json"))
    else:
        candidates = list(manifests_dir.rglob("manifest.json"))
    # Filter to paths where lane appears as a directory component
    candidates = [p for p in candidates if lane in p.parent.parts]
    if not candidates:
        # Fallback: any manifest, sorted by mtime
        candidates = list(manifests_dir.rglob("manifest.json"))
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="retraining_trigger")
    p.add_argument(
        "--leaderboard",
        type=Path,
        required=True,
        help="Path to current leaderboard JSON (brain_leaderboard.py output)",
    )
    p.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Path to previous leaderboard JSON for trend comparison",
    )
    p.add_argument(
        "--labels",
        type=Path,
        default=None,
        help="Path to live_labels.jsonl for dataset rebuild (default: data/reports/live_labels.jsonl)",
    )
    p.add_argument(
        "--feature-store-dir",
        type=Path,
        default=Path("data/feature_store"),
        help="Feature store directory (default: data/feature_store)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/training"),
        help="Training data output directory (default: data/training)",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Execute retraining pipeline (default: dry-run detection only)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write retraining signal JSON to file",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Load leaderboard
    if not args.leaderboard.exists():
        print(json.dumps({"error": "leaderboard_not_found", "path": str(args.leaderboard)}))
        return 2

    leaderboard = json.loads(args.leaderboard.read_text(encoding="utf-8"))
    baseline = None
    if args.baseline and args.baseline.exists():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))

    # Detect degradation
    report = detect_degradation(leaderboard, baseline)
    report["execute_mode"] = args.execute

    # Execute retraining if requested
    if args.execute and report["signals"]:
        exec_result = execute_retraining(
            report["signals"],
            feature_store_dir=args.feature_store_dir,
            output_dir=args.output_dir,
            labels_path=args.labels,
            dry_run=False,
        )
        report["execution"] = exec_result
    elif args.execute:
        report["execution"] = {"executed": False, "reason": "no_degradation_signals"}

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    if report["overall_urgency"] == "critical":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
