#!/usr/bin/env python
"""XAU Signal Generation Probe — traces the exact breakpoint(s) in the XAU
decision pipeline.

Usage:
    python scripts/probe_xau_signal_generation.py
    python scripts/probe_xau_signal_generation.py --brain-id Barrier_V9_12B_V2  # single brain

This script is the diagnostic scalpel for understanding why XAU produces
ABSTAIN/FLAT decisions across all 21 brains for 45+ consecutive days.

Iron Law #11 compliant: stdout is the sole source of truth.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BRAINS_DIR = PROJECT_ROOT / "configs" / "brains"
FEATURE_STORE_DIR = DATA_DIR / "feature_store"
DECISIONS_DIR = DATA_DIR / "decisions"


# ── Feature schema ──────────────────────────────────────────────────────

def load_feature_vector(symbol: str = "XAUUSDc") -> tuple[np.ndarray, str]:
    """Load latest feature vector from LocalFeatureStore."""
    sys.path.insert(0, str(PROJECT_ROOT))
    from core.features.local_feature_store import LocalFeatureStore
    from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

    store = LocalFeatureStore(str(FEATURE_STORE_DIR))
    record = store.latest(symbol, "M5", schema_name="v9_institutional_40")
    if record is None or not record.values:
        return np.zeros(40, dtype=np.float64), "stub:no_record"
    vec = np.array(
        [float(record.values.get(name, 0.0)) for name in V9_INSTITUTIONAL_40_FEATURES],
        dtype=np.float64,
    )
    return vec, "store"


# ── Probe functions ─────────────────────────────────────────────────────

def probe_feature_store() -> dict[str, Any]:
    """Check feature store health for XAU."""
    result: dict[str, Any] = {"status": "unknown", "schemas": [], "latest_record": None}

    schemas_file = FEATURE_STORE_DIR / "schemas.json"
    if schemas_file.exists():
        schemas = json.loads(schemas_file.read_text(encoding="utf-8"))
        xau_schemas = {k: v for k, v in schemas.items() if "XAU" in k}
        result["schemas"] = list(xau_schemas.keys())

    vec, source = load_feature_vector()
    result["feature_source"] = source
    result["feature_dim"] = len(vec)
    result["nonzero_count"] = int(np.count_nonzero(vec))
    result["feature_sample"] = {
        k: round(float(v), 6)
        for i, (k, v) in enumerate(
            zip(
                [
                    "M5_Ret_1",
                    "M5_Body_Ratio",
                    "M5_ATR_14",
                    "M5_RSI_14",
                    "M5_MACD",
                ],
                vec[:5], strict=False,
            )
        )
    }

    if source == "stub:no_record":
        result["status"] = "DEAD: no feature records for XAU M5"
    elif np.max(np.abs(vec)) < 1e-10:
        result["status"] = "DEAD: all-zero feature vector (stub fallback)"
    else:
        result["status"] = "HEALTHY: real feature values present"

    return result


def probe_single_brain(brain_config: dict, feature_vector: np.ndarray) -> dict[str, Any]:
    """Run inference on one brain and trace the full signal path."""
    from core.brains.services.brain_factory import BrainFactory

    bid = brain_config.get("brain_id", "?")
    btype = brain_config.get("brain_type", "?")

    result: dict[str, Any] = {
        "brain_id": bid,
        "brain_type": btype,
        "config_features": len(brain_config.get("features", [])),
    }

    # Step 1: Build adapter
    try:
        factory = BrainFactory()
        adapter = factory.build(brain_config)
        desc = adapter.describe()
        result["num_features"] = desc.get("num_features", "?")
        result["booster_loaded"] = desc.get("booster_loaded", False)
        result["backend"] = desc.get("backend", "?")
    except Exception as exc:
        result["build_error"] = f"{type(exc).__name__}: {exc}"
        return result

    # Step 2: Infer
    try:
        raw = adapter.infer(feature_vector)
        result["raw_score"] = round(float(raw.get("raw_score", 0.0)), 6)
        result["fallback"] = raw.get("fallback", False)
        result["fallback_reason"] = raw.get("fallback_reason", "")
        result["runtime_ms"] = round(float(raw.get("runtime_ms", 0.0)), 4)
    except Exception as exc:
        result["infer_error"] = f"{type(exc).__name__}: {exc}"
        return result

    # Step 3: get_signal → BrainSignal
    try:
        signal = adapter.get_signal(raw)
        result["signal_direction"] = getattr(signal, "direction", "?")
        result["signal_confidence"] = round(float(getattr(signal, "confidence", 0.0)), 6)
        result["signal_has_prediction_attr"] = hasattr(signal, "prediction")
    except Exception as exc:
        result["signal_error"] = f"{type(exc).__name__}: {exc}"
        return result

    # Step 4: Simulate how _run_single_brain() extracts direction_bias (THE BUG)
    try:
        signal = adapter.get_signal(raw)
        pred = signal.prediction if hasattr(signal, "prediction") else {}
        result["_extracted_direction_bias"] = pred.get("direction_bias", "neutral")
        result["_api_correct"] = (
            result["signal_direction"] == result["_extracted_direction_bias"]
        )
    except Exception as exc:
        result["_extracted_direction_bias"] = f"error: {exc}"

    return result


def probe_all_brains(feature_vector: np.ndarray) -> list[dict[str, Any]]:
    """Run probe on all XAU brain configs."""
    results = []
    for bf in sorted(BRAINS_DIR.glob("*.json")):
        # Skip meta filters (not brains)
        cfg = json.loads(bf.read_text(encoding="utf-8"))
        if "brain_type" not in cfg:
            continue
        r = probe_single_brain(cfg, feature_vector)
        results.append(r)
    return results


def probe_decision_files() -> dict[str, Any]:
    """Analyze recent XAU decision files for signal patterns."""
    result: dict[str, Any] = {"dirs_checked": 0, "records": 0, "consensus_dist": Counter()}

    dirs = sorted(DECISIONS_DIR.glob("2026-*"), reverse=True)
    for d in dirs[:5]:  # last 5 days
        dec_file = d / "XAUUSDc.decisions.jsonl"
        if not dec_file.exists():
            continue
        result["dirs_checked"] += 1
        for line in dec_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                consensus = (
                    rec.get("attribution", {})
                    .get("consensus", {})
                    .get("consensus", "?")
                )
                result["consensus_dist"][consensus] += 1
                result["records"] += 1
            except json.JSONDecodeError:
                pass

    return result


# ── Output ──────────────────────────────────────────────────────────────

def render_report(
    feature_result: dict,
    brain_results: list[dict],
    decision_result: dict,
) -> str:
    """Render probe report to stdout."""
    lines = []
    sep = "=" * 72

    lines.append(sep)
    lines.append("  XAU SIGNAL GENERATION PROBE")
    lines.append(sep)

    # ── Section 1: Feature Store ──
    lines.append("\n  [1] FEATURE STORE")
    lines.append(f"      Status:   {feature_result['status']}")
    lines.append(f"      Source:   {feature_result['feature_source']}")
    lines.append(f"      Dim:      {feature_result['feature_dim']}")
    lines.append(f"      NonZero:  {feature_result['nonzero_count']}")
    for name, val in feature_result.get("feature_sample", {}).items():
        lines.append(f"        {name}: {val}")

    # ── Section 2: Per-Brain Inference ──
    lines.append(f"\n  [2] BRAIN INFERENCE ({len(brain_results)} brains)")

    # API fracture detection
    api_broken = sum(
        1 for r in brain_results if not r.get("_api_correct", True) and "build_error" not in r
    )
    lines.append(f"      API fracture (signal.prediction missing): {api_broken}/{len(brain_results)} brains")

    # Fallback distribution
    fallback_brains = [r for r in brain_results if r.get("fallback")]
    dim_mismatch = [
        r for r in fallback_brains
        if "dim_mismatch" in r.get("fallback_reason", "")
    ]
    lines.append(f"      Fallback (inference): {len(fallback_brains)}/{len(brain_results)} brains")
    if dim_mismatch:
        lines.append(f"        dim_mismatch: {len(dim_mismatch)} brains (model vs feature vector)")
        for r in dim_mismatch:
            lines.append(
                f"          {r['brain_id']:45s} "
                f"model={r.get('num_features','?')}dim  "
                f"input={feature_result['feature_dim']}dim"
            )

    # Direction distribution (what _run_single_brain would extract)
    extracted_dirs = Counter(
        r.get("_extracted_direction_bias", "error")
        for r in brain_results
        if "build_error" not in r
    )
    lines.append("\n      Direction distribution (via broken _run_single_brain API):")
    for d, c in extracted_dirs.most_common():
        lines.append(f"        {d}: {c} brains")

    # Actual signal directions (correct API)
    actual_dirs = Counter(
        r.get("signal_direction", "error")
        for r in brain_results
        if "build_error" not in r
    )
    lines.append("\n      Direction distribution (via correct signal.direction API):")
    for d, c in actual_dirs.most_common():
        lines.append(f"        {d}: {c} brains")

    # Score distribution for non-fallback brains
    real_brains = [r for r in brain_results if not r.get("fallback") and "build_error" not in r]
    if real_brains:
        scores = [r["raw_score"] for r in real_brains]
        lines.append(f"\n      Non-fallback brains ({len(real_brains)}):")
        lines.append(f"        raw_score range: [{min(scores):.4f}, {max(scores):.4f}]")
        for r in real_brains:
            lines.append(
                f"        {r['brain_id']:45s} "
                f"score={r['raw_score']:+.4f}  "
                f"dir={r['signal_direction']:7s}  "
                f"confidence={r['signal_confidence']:.4f}"
            )

    # ── Section 3: Decision File History ──
    lines.append("\n  [3] DECISION FILE HISTORY")
    lines.append(f"      Records analyzed: {decision_result['records']}")
    lines.append("      Consensus distribution:")
    for consensus, count in decision_result["consensus_dist"].most_common():
        pct = count / decision_result["records"] * 100 if decision_result["records"] else 0
        lines.append(f"        {consensus:10s}: {count:4d}  ({pct:.1f}%)")

    # ── Section 4: Root Cause Verdict ──
    lines.append("\n  [4] ROOT CAUSE VERDICT")
    lines.append(f"{'─' * 72}")

    if api_broken > 0:
        lines.append("")
        lines.append("  🔴 ROOT CAUSE 1 (PRIMARY — L1 Logic Defect):")
        lines.append("     File:  scripts/live_shadow_ensemble.py:106")
        lines.append("     Bug:   signal.prediction — BrainSignal has NO .prediction attr")
        lines.append("     Fix:   Use signal.direction (Direction Literal) directly")
        lines.append(f"     Impact: {api_broken}/{len(brain_results)} brains' direction discarded")
        lines.append("")

    if dim_mismatch:
        lines.append("  🟡 ROOT CAUSE 2 (SECONDARY — L2 Logic Defect):")
        lines.append("     File:  core/brains/adapters/xgboost_brain_adapter.py:215-227")
        lines.append("     Bug:   No recovery path for 35-dim model ← 40-dim input")
        lines.append("     Fix:   Add 35←40 feature mapping (swing_enhanced_35 ⊂ v9_institutional_40)")
        lines.append(f"     Impact: {len(dim_mismatch)}/{len(brain_results)} brains use fallback zero-score")
        lines.append("")

    lines.append(f"  After L1 fix: {len(real_brains)} brains produce real signal")
    lines.append(f"  After L1+L2 fix: all {len(brain_results)} brains produce real signal")
    lines.append(sep)

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="XAU Signal Generation Probe")
    parser.add_argument(
        "--brain-id",
        default=None,
        help="Probe a single brain by ID (default: all brains)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output",
    )
    args = parser.parse_args()

    # Ensure project root is on sys.path
    sys.path.insert(0, str(PROJECT_ROOT))

    feature_result = probe_feature_store()
    feature_vec, _ = load_feature_vector()

    if args.brain_id:
        cfg_path = BRAINS_DIR / f"{args.brain_id}.json"
        if not cfg_path.exists():
            print(f"Brain config not found: {cfg_path}")
            sys.exit(1)
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        brain_results = [probe_single_brain(cfg, feature_vec)]
    else:
        brain_results = probe_all_brains(feature_vec)

    decision_result = probe_decision_files()

    if args.json:
        output = {
            "feature_store": feature_result,
            "brains": brain_results,
            "decision_history": {
                "records": decision_result["records"],
                "consensus_distribution": dict(decision_result["consensus_dist"]),
            },
        }
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    else:
        print(render_report(feature_result, brain_results, decision_result))


if __name__ == "__main__":
    main()
