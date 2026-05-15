"""Champion/Challenger promotion: compare shadow brains against live champions.

Groups brains by training lane, compares tracked performance, and promotes
challengers that consistently outperform the incumbent champion.

Usage:
  # Dry-run: show what would be promoted
  python scripts/training/champion_challenger.py --dry-run

  # Apply promotions
  python scripts/training/champion_challenger.py --base-dir data
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.governance.governance_service import GovernanceService

SCHEMA_VERSION = "champion_challenger.v1"

# brain_id prefix → lane mapping (comprehensive, covers all registered brains)
BRAIN_ID_PREFIX_TO_LANE: dict[str, str] = {
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

# brain_type → lane fallback (from registry entries)
BRAIN_TYPE_TO_LANE: dict[str, str] = {
    "onnx_v9": "sur",
    "xgboost_v10": "boost",
    "xgboost_v4.5": "boost",
    "lightgbm_v1": "boost",
    "ou_params_v6": "arb",
    "online_sgd_v1": "online_sgd",
    "deep_res_mlp_v1": "dl",
    "microstructure_transformer_v5": "mtx",
}

# Promotion criteria
MIN_CHALLENGER_SAMPLES = 20  # need enough data to trust challenger
MIN_COMPOSITE_DELTA = 0.10  # challenger must beat champion by this margin
MIN_CHAMPION_SAMPLES = 10  # champion must also have enough data


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _lookup_brain_type_from_config(brain_id: str) -> str | None:
    """Try to read brain_type from a config file in configs/brains/."""
    brains_dir = Path("configs/brains")
    if not brains_dir.exists():
        return None
    for config_path in sorted(brains_dir.glob("*.json")):
        if config_path.name.endswith(".normalization.json"):
            continue
        try:
            entry = json.loads(config_path.read_text(encoding="utf-8"))
            if entry.get("brain_id") == brain_id:
                return entry.get("brain_type")
        except (json.JSONDecodeError, FileNotFoundError):
            continue
    return None


def _guess_lane(brain_id: str, *, configs_dir: str | None = None) -> str:
    """Map brain_id to training lane. Shared with retraining_trigger.

    1. Case-insensitive prefix match against BRAIN_ID_PREFIX_TO_LANE.
    2. Fallback: read brain config JSON → brain_type → BRAIN_TYPE_TO_LANE.
    3. Fallback: substring match on brain_id against prefix dict.
    4. Unknown brains return "unclassified" (not silently skipped).
    """
    upper = brain_id.upper()
    # 1. Prefix match (case-insensitive)
    for prefix, lane in BRAIN_ID_PREFIX_TO_LANE.items():
        if upper.startswith(prefix.upper()):
            return lane

    # 2. Config fallback
    brain_type = _lookup_brain_type_from_config(brain_id)
    if brain_type and brain_type in BRAIN_TYPE_TO_LANE:
        return BRAIN_TYPE_TO_LANE[brain_type]

    if brain_type:
        # Fuzzy match brain_type against known types
        for known, lane in BRAIN_TYPE_TO_LANE.items():
            if known in brain_type or brain_type in known:
                return lane

    # 3. Substring match on brain_id prefix table
    for prefix, lane in BRAIN_ID_PREFIX_TO_LANE.items():
        if prefix.upper() in upper:
            return lane

    # 4. Not silently skipped — grouped for visibility
    return "unclassified"


def run_promotion_cycle(
    tracker: BrainPerformanceTracker,
    governance: GovernanceService,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compare champions vs challengers and promote where warranted.

    Args:
        tracker: Populated BrainPerformanceTracker.
        governance: GovernanceService with registered brains.
        dry_run: If True, assess but don't apply transitions.

    Returns:
        Report dict with promotions applied and comparisons.
    """
    summaries = tracker.get_all_summaries()
    if not summaries:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "brains_assessed": 0,
            "comparisons": [],
            "promotions": [],
        }

    # Group brains by lane
    lanes: dict[str, list[dict[str, Any]]] = {}
    for s in summaries:
        brain_id = s["brain_id"]
        lane = _guess_lane(brain_id)
        lanes.setdefault(lane, []).append(s)

    unclassified: list[str] = []
    comparisons: list[dict[str, Any]] = []
    promotions: list[dict[str, Any]] = []

    for lane, brains in lanes.items():
        if lane == "unclassified":
            unclassified = [b["brain_id"] for b in brains]
            continue

        # Find champion (live) and challengers (non-live) in this lane
        champion = None
        challengers = []
        for b in brains:
            state = governance.get_brain_state(b["brain_id"]) or {}
            status = state.get("status", "candidate")
            if status == "live":
                champion = b
            elif status in ("candidate", "probation"):
                challengers.append(b)

        if champion is None:
            continue

        champ_composite = champion.get("composite_mean", 0.0)
        champ_samples = champion.get("sample_count", 0)

        for challenger in challengers:
            chal_composite = challenger.get("composite_mean", 0.0)
            chal_samples = challenger.get("sample_count", 0)
            delta = round(chal_composite - champ_composite, 4)

            eligible = (
                chal_samples >= MIN_CHALLENGER_SAMPLES
                and champ_samples >= MIN_CHAMPION_SAMPLES
                and delta >= MIN_COMPOSITE_DELTA
            )

            comp = {
                "lane": lane,
                "champion": {
                    "brain_id": champion["brain_id"],
                    "composite_mean": champ_composite,
                    "sample_count": champ_samples,
                },
                "challenger": {
                    "brain_id": challenger["brain_id"],
                    "composite_mean": chal_composite,
                    "sample_count": chal_samples,
                },
                "delta": delta,
                "eligible": eligible,
                "reason": None
                if eligible
                else _ineligibility_reason(chal_samples, champ_samples, delta),
            }
            comparisons.append(comp)

            if not eligible:
                continue

            # Apply promotion
            if not dry_run:
                # Demote champion
                demote_result = governance.apply_recommendation(
                    champion["brain_id"],
                    "demote_to_probation",
                    reason=f"challenger:{challenger['brain_id']}",
                )
                # Promote challenger directly to live
                promote_result = governance.transition(
                    challenger["brain_id"],
                    "live",
                    reason=f"promoted_over:{champion['brain_id']}",
                )
            else:
                demote_result = {"action": "would_demote", "brain_id": champion["brain_id"]}
                promote_result = {"action": "would_promote", "brain_id": challenger["brain_id"]}

            promotions.append(
                {
                    "lane": lane,
                    "demoted_champion": demote_result,
                    "promoted_challenger": promote_result,
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "brains_assessed": len(summaries),
        "comparisons": comparisons,
        "promotions": promotions,
        "unclassified_brains": unclassified,
    }


def _ineligibility_reason(chal_samples: int, champ_samples: int, delta: float) -> str:
    parts = []
    if chal_samples < MIN_CHALLENGER_SAMPLES:
        parts.append(f"challenger_samples={chal_samples}<{MIN_CHALLENGER_SAMPLES}")
    if champ_samples < MIN_CHAMPION_SAMPLES:
        parts.append(f"champion_samples={champ_samples}<{MIN_CHAMPION_SAMPLES}")
    if delta < MIN_COMPOSITE_DELTA:
        parts.append(f"delta={delta:.3f}<{MIN_COMPOSITE_DELTA}")
    return "; ".join(parts) if parts else "unknown"


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="champion_challenger")
    p.add_argument(
        "--base-dir",
        default="data",
        help="Base data directory (default: data)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Assess promotions without applying transitions",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write promotion report JSON to file",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    tracker = BrainPerformanceTracker(window_size=100)
    governance = GovernanceService()

    report = run_promotion_cycle(tracker, governance, dry_run=args.dry_run)

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    if report["promotions"]:
        return 1  # non-zero signals ops attention
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
