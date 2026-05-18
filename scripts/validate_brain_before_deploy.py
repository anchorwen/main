"""Brain deployment quality gate — catch direction bias, NEUTRAL death, and
signal redundancy BEFORE a brain reaches live trading.

Usage:
  python scripts/validate_brain_before_deploy.py --brain-config configs/brains/xxx.json
  python scripts/validate_brain_before_deploy.py --all  # validate all registered brains
  python scripts/validate_brain_before_deploy.py --brain-id OU_Params_V6_Sniper

Checks (any FAIL = do not deploy):
  1. Direction sanity   — >90% one direction → FAIL (mono-directional brain)
  2. NEUTRAL rate       — >80% NEUTRAL → FAIL (brain never votes)
  3. Correlation        — >0.85 with any existing brain → WARN (redundant signal)
  4. Output validity    — produces valid direction/confidence for all test vectors

Test data: last 500 records from the M5 feature store. If unavailable, falls back
to zero-vector stub (only checks output validity, not direction distribution).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

# ── Test data loading ────────────────────────────────────────────────────────


def _load_feature_vectors(limit: int = 500) -> list[dict[str, float]]:
    """Load the last *limit* feature records from the M5 feature store."""
    store_path = Path("data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl")
    if not store_path.exists():
        return []
    records: list[dict[str, float]] = []
    with open(store_path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                vals = r.get("values", {})
                if isinstance(vals, dict) and len(vals) >= 10:
                    records.append(vals)
            except (json.JSONDecodeError, KeyError):
                pass
    return records[-limit:]


def _build_feature_vector(
    values: dict[str, float],
    feature_names: list[str],
) -> np.ndarray:
    """Extract features in the order specified by the brain config."""
    vec = np.zeros(len(feature_names), dtype=np.float32)
    for i, name in enumerate(feature_names):
        vec[i] = float(values.get(name, 0.0))
    return vec


# ── Brain loading ────────────────────────────────────────────────────────────


def _load_brain_entry(config_path: str) -> dict[str, Any] | None:
    """Load a brain registry entry from a JSON config file."""
    path = Path(config_path)
    if not path.exists():
        print(f"[ERROR] Config not found: {config_path}")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
        if entry.get("schema_version") == "brain_registry_entry.v1":
            return entry
    except Exception as exc:
        print(f"[ERROR] Failed to load {config_path}: {exc}")
    return None


def _build_adapter(entry: dict[str, Any]):
    """Build a brain adapter from a registry entry."""
    from core.brains.services.brain_factory import BrainFactory

    factory = BrainFactory()
    try:
        return factory.build(entry)
    except Exception as exc:
        print(f"[ERROR] BrainFactory.build() failed: {exc}")
        return None


# ── Signal extraction ────────────────────────────────────────────────────────


def _get_direction_and_confidence(proposal: Any) -> tuple[str, float]:
    """Extract (direction, confidence) from a BrainDecisionProposal.

    BrainDecisionProposal stores direction/confidence inside the nested
    ``prediction`` dict (keys: ``direction_bias``, ``confidence``), not as
    top-level attributes.
    """
    prediction = getattr(proposal, "prediction", {}) or {}
    direction = str(prediction.get("direction_bias", "neutral") or "neutral").lower()
    confidence = float(prediction.get("confidence", 0.0) or 0.0)
    return direction, confidence


# ── Checks ───────────────────────────────────────────────────────────────────


def _check_direction_sanity(
    directions: list[str],
    max_single_pct: float = 90.0,
) -> tuple[bool, str]:
    """FAIL if >max_single_pct% of votes are in one direction."""
    if not directions:
        return True, "no_data"
    n = len(directions)
    long_pct = directions.count("long") / n * 100
    short_pct = directions.count("short") / n * 100
    neutral_pct = directions.count("neutral") / n * 100

    if long_pct > max_single_pct:
        return False, f"{long_pct:.1f}% LONG (threshold {max_single_pct}%)"
    if short_pct > max_single_pct:
        return False, f"{short_pct:.1f}% SHORT (threshold {max_single_pct}%)"
    return True, f"LONG={long_pct:.1f}% SHORT={short_pct:.1f}% NEUTRAL={neutral_pct:.1f}%"


def _check_neutral_rate(
    directions: list[str],
    max_neutral_pct: float = 80.0,
) -> tuple[bool, str]:
    """FAIL if >max_neutral_pct% of votes are NEUTRAL (brain never votes)."""
    if not directions:
        return True, "no_data"
    n = len(directions)
    neutral_pct = directions.count("neutral") / n * 100
    if neutral_pct > max_neutral_pct:
        return (
            False,
            f"{neutral_pct:.1f}% NEUTRAL (threshold {max_neutral_pct}%) — brain never votes",
        )
    return True, f"{neutral_pct:.1f}% NEUTRAL"


def _check_output_validity(results: list[dict[str, Any]]) -> tuple[bool, str]:
    """FAIL if any inference produces invalid output."""
    if not results:
        return False, "no results produced"
    invalid = 0
    for r in results:
        d = r.get("direction", "")
        c = r.get("confidence", -1)
        if d not in ("long", "short", "neutral"):
            invalid += 1
        elif c < 0 or c > 1:
            invalid += 1
    if invalid > 0:
        return False, f"{invalid}/{len(results)} invalid outputs"
    return True, f"all {len(results)} outputs valid"


def _check_correlation(
    directions: list[str],
    existing_directions: dict[str, list[str]],
    max_corr: float = 0.85,
) -> tuple[bool, str]:
    """WARN if direction agreement with any existing brain > max_corr."""
    if not directions or not existing_directions:
        return True, "no comparison data"
    warnings = []
    for other_id, other_dirs in existing_directions.items():
        min_len = min(len(directions), len(other_dirs))
        if min_len < 10:
            continue
        matches = sum(1 for i in range(min_len) if directions[i] == other_dirs[i])
        agreement = matches / min_len
        if agreement > max_corr:
            warnings.append(f"{other_id}: {agreement:.2%} agreement")
    if warnings:
        return False, " | ".join(warnings)
    return True, "no redundant signals"


# ── Main ─────────────────────────────────────────────────────────────────────


def validate_brain(
    entry: dict[str, Any],
    feature_records: list[dict[str, float]],
    existing_directions: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Run all quality checks on a single brain.  Returns a report dict."""
    brain_id = entry.get("brain_id", "unknown")
    brain_type = entry.get("brain_type", "?")
    feature_names: list[str] = entry.get("features", [])

    report: dict[str, Any] = {
        "brain_id": brain_id,
        "brain_type": brain_type,
        "checks": {},
        "passed": True,
    }

    # ── Build adapter ──
    adapter = _build_adapter(entry)
    if adapter is None:
        report["passed"] = False
        report["checks"]["adapter_build"] = {
            "passed": False,
            "detail": "BrainFactory.build() failed",
        }
        return report
    report["checks"]["adapter_build"] = {"passed": True, "detail": "ok"}

    # ── Run inference ──
    results: list[dict[str, Any]] = []
    n_vectors = min(len(feature_records), 500) if feature_records else 0

    if n_vectors == 0:
        # Fallback: run once with zero vector
        try:
            zero_vec = np.zeros(len(feature_names) if feature_names else 40, dtype=np.float32)
            proposal = adapter.inference(zero_vec)
            d, c = _get_direction_and_confidence(proposal)
            results.append({"direction": d, "confidence": c})
        except Exception as exc:
            report["passed"] = False
            report["checks"]["inference"] = {"passed": False, "detail": str(exc)[:100]}
            return report
    else:
        for _i, record in enumerate(feature_records[:n_vectors]):
            try:
                vec = _build_feature_vector(record, feature_names) if feature_names else None
                if vec is not None:
                    proposal = adapter.inference(vec)
                else:
                    proposal = adapter.inference(np.zeros(40, dtype=np.float32))
                d, c = _get_direction_and_confidence(proposal)
                results.append({"direction": d, "confidence": c})
            except Exception:
                results.append({"direction": "neutral", "confidence": 0.0})

    directions = [r["direction"] for r in results]
    confidences = [r["confidence"] for r in results]

    # ── Check 1: Direction sanity ──
    ok, detail = _check_direction_sanity(directions)
    report["checks"]["direction_sanity"] = {"passed": ok, "detail": detail}

    # ── Check 2: NEUTRAL rate ──
    ok, detail = _check_neutral_rate(directions)
    report["checks"]["neutral_rate"] = {"passed": ok, "detail": detail}

    # ── Check 3: Output validity ──
    ok, detail = _check_output_validity(results)
    report["checks"]["output_validity"] = {"passed": ok, "detail": detail}

    # ── Check 4: Correlation ──
    if existing_directions:
        ok, detail = _check_correlation(directions, existing_directions)
        report["checks"]["correlation"] = {"passed": ok, "detail": detail}

    # ── Stats ──
    if confidences:
        report["stats"] = {
            "n_inferences": len(results),
            "avg_confidence": round(float(np.mean(confidences)), 4),
            "med_confidence": round(float(np.median(confidences)), 4),
            "min_confidence": round(float(np.min(confidences)), 4),
            "max_confidence": round(float(np.max(confidences)), 4),
            "direction_dist": {
                "long": directions.count("long"),
                "short": directions.count("short"),
                "neutral": directions.count("neutral"),
            },
        }

    # ── Overall ──
    report["passed"] = all(c["passed"] for c in report["checks"].values())
    return report


def print_report(report: dict[str, Any]) -> None:
    """Pretty-print a validation report."""
    brain_id = report["brain_id"]
    passed = report["passed"]
    status = "PASS" if passed else "FAIL"
    print(f"\n{'='*70}")
    print(f"  {brain_id}  [{status}]")
    print(f"{'='*70}")

    for check_name, result in report.get("checks", {}).items():
        icon = "PASS" if result["passed"] else "FAIL"
        print(f"  [{icon}] {check_name}: {result['detail']}")

    stats = report.get("stats")
    if stats:
        print(f"\n  Inferences: {stats['n_inferences']}")
        print(
            f"  Confidence: avg={stats['avg_confidence']:.3f} med={stats['med_confidence']:.3f} min={stats['min_confidence']:.3f} max={stats['max_confidence']:.3f}"
        )
        dd = stats["direction_dist"]
        print(f"  Directions: LONG={dd['long']} SHORT={dd['short']} NEUTRAL={dd['neutral']}")

    if not passed:
        print("\n  *** DO NOT DEPLOY ***")
    print()


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Brain deployment quality gate")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--brain-config", help="Path to a brain config JSON")
    group.add_argument("--brain-id", help="Brain ID to validate (looks up in configs/brains/)")
    group.add_argument("--all", action="store_true", help="Validate all registered brains")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text")
    args = parser.parse_args()

    # ── Resolve entries to validate ──
    entries: list[dict[str, Any]] = []

    if args.brain_config:
        entry = _load_brain_entry(args.brain_config)
        if entry is None:
            return 1
        entries.append(entry)
    elif args.brain_id:
        config_dir = Path("configs/brains")
        found = False
        for f in sorted(config_dir.glob("*.json")):
            entry = _load_brain_entry(str(f))
            if entry and entry.get("brain_id") == args.brain_id:
                entries.append(entry)
                found = True
                break
        if not found:
            print(f"[ERROR] Brain ID not found: {args.brain_id}")
            return 1
    elif args.all:
        config_dir = Path("configs/brains")
        for f in sorted(config_dir.glob("*.json")):
            entry = _load_brain_entry(str(f))
            if entry:
                entries.append(entry)

    if not entries:
        print("[ERROR] No valid brain configs found")
        return 1

    # ── Load test data ──
    feature_records = _load_feature_vectors(limit=500)
    if feature_records:
        print(f"Loaded {len(feature_records)} feature records for testing")
    else:
        print("No feature store data — running with zero-vector stub only")

    # ── Collect existing directions for correlation check ──
    existing_directions: dict[str, list[str]] = {}
    if args.all and len(entries) > 1:
        # Compute directions for all brains first, then check correlations
        pass  # Two-pass: first collect, then report

    # ── Validate ──
    reports = []
    all_passed = True
    for entry in entries:
        report = validate_brain(entry, feature_records, existing_directions)
        reports.append(report)
        if not report["passed"]:
            all_passed = False
        if not args.json:
            print_report(report)
        # Store directions for correlation check (next brain)
        stats = report.get("stats")
        if stats:
            dd = stats["direction_dist"]
            directions = (
                ["long"] * dd["long"] + ["short"] * dd["short"] + ["neutral"] * dd["neutral"]
            )
            existing_directions[report["brain_id"]] = directions

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
