#!/usr/bin/env python
"""Shadow Brain Prediction Quality Auditor — Iron Law #11 compliant.

Analyses per-cycle brain_votes JSONL data to evaluate shadow brain
prediction quality across multiple dimensions: direction distribution,
confidence calibration, consensus alignment, signal stability, and
raw score statistics.

Usage:
  python scripts/analyze_shadow_predictions.py --data-dir data_btc
  python scripts/analyze_shadow_predictions.py --data-dir data_btc --days 3
  python scripts/analyze_shadow_predictions.py --data-dir data_btc --brain-id BTC_Swing_V12_H1_15

Statistical conventions (declared upfront per Iron Law #11):
  - Dedup: brain_votes has one line per brain per cycle — no dedup needed.
  - Direction: from "direction" field (long/short/neutral), as written by record_brain_votes().
  - Confidence: from "confidence" field (float 0.0–1.0).
  - Agreement: two brains agree when direction strings match exactly.
  - Stability: flip rate = proportion of consecutive pairs with direction change.
  - Reference brain: V4 (only live/probation brain, vote_weight>0.0).
  - All statistics computed from script stdout only — no manual supplemental inference.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

# ── Constants ─────────────────────────────────────────────────────────────────

BUCKET_EDGES = [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
BUCKET_LABELS = [
    "0.0-0.3",
    "0.3-0.4",
    "0.4-0.5",
    "0.5-0.6",
    "0.6-0.7",
    "0.7-0.8",
    "0.8-0.9",
    "0.9-1.0",
]


# ── Data Loading ──────────────────────────────────────────────────────────────


def load_brain_votes(
    data_dir: str,
    days: int = 2,
    brain_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Load brain_votes JSONL for the last `days` days, optionally filtered by brain_id."""
    votes_dir = Path(data_dir) / "brain_votes"
    if not votes_dir.is_dir():
        print(f"[ERROR] brain_votes directory not found: {votes_dir}", file=sys.stderr)
        sys.exit(1)

    cutoff_date = datetime.now(UTC).date() - timedelta(days=days)
    records: list[dict[str, Any]] = []

    for fpath in sorted(votes_dir.glob("*.jsonl")):
        date_str = fpath.stem  # "2026-06-25"
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff_date:
            continue

        with open(fpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                bid = rec.get("brain_id", "")
                if brain_ids and bid not in brain_ids:
                    continue
                records.append(rec)

    return records


# ── Metrics ───────────────────────────────────────────────────────────────────


def compute_direction_distribution(records: list[dict]) -> dict[str, Any]:
    """Direction distribution: long/short/neutral counts and percentages."""
    counter = Counter(r.get("direction", "neutral") for r in records)
    total = len(records)
    return {
        "long": counter.get("long", 0),
        "short": counter.get("short", 0),
        "neutral": counter.get("neutral", 0),
        "total": total,
        "long_pct": round(counter.get("long", 0) / total * 100, 1) if total > 0 else 0.0,
        "short_pct": round(counter.get("short", 0) / total * 100, 1) if total > 0 else 0.0,
        "neutral_pct": round(counter.get("neutral", 0) / total * 100, 1) if total > 0 else 0.0,
        "bias": _direction_bias(counter, total),
    }


def _direction_bias(counter: Counter, total: int) -> str:
    """Classify direction bias."""
    if total == 0:
        return "no_data"
    long_pct = counter.get("long", 0) / total
    short_pct = counter.get("short", 0) / total
    neutral_pct = counter.get("neutral", 0) / total
    if neutral_pct > 0.8:
        return "strong_neutral"
    if long_pct > 0.7:
        return "strong_long"
    if short_pct > 0.7:
        return "strong_short"
    if long_pct > 0.55:
        return "slight_long"
    if short_pct > 0.55:
        return "slight_short"
    return "balanced"


def compute_confidence_stats(records: list[dict]) -> dict[str, Any]:
    """Confidence distribution statistics."""
    confs = [r.get("confidence", 0.5) for r in records]
    if not confs:
        return {"mean": 0.0, "median": 0.0, "std": 0.0, "n": 0}
    arr = np.array(confs, dtype=np.float64)
    return {
        "mean": round(float(np.mean(arr)), 4),
        "median": round(float(np.median(arr)), 4),
        "std": round(float(np.std(arr, ddof=1)), 4),
        "min": round(float(np.min(arr)), 4),
        "max": round(float(np.max(arr)), 4),
        "p25": round(float(np.percentile(arr, 25)), 4),
        "p75": round(float(np.percentile(arr, 75)), 4),
        "n": len(confs),
    }


def compute_confidence_histogram(records: list[dict]) -> list[dict[str, Any]]:
    """Confidence histogram buckets."""
    confs = [r.get("confidence", 0.5) for r in records]
    if not confs:
        return []
    hist, _ = np.histogram(confs, bins=BUCKET_EDGES)
    return [
        {
            "bucket": BUCKET_LABELS[i],
            "count": int(hist[i]),
            "pct": round(int(hist[i]) / len(confs) * 100, 1),
        }
        for i in range(len(hist))
    ]


def compute_agreement(records: list[dict], ref_records: list[dict]) -> dict[str, Any]:
    """Compute direction agreement between shadow brain and reference brain (V4).

    Agreement means both brains have the same direction (long/short/neutral) in the same cycle.
    Joins on 'cycle' + 'strategy' to match corresponding votes.
    """
    if not ref_records:
        return {"agreement_pct": 0.0, "matched_cycles": 0, "note": "no_reference_data"}

    # Build lookup: (strategy, cycle) → direction
    ref_map: dict[tuple[str, int], str] = {}
    for r in ref_records:
        key = (r.get("strategy", ""), r.get("cycle", 0))
        ref_map[key] = r.get("direction", "neutral")

    matched = 0
    agreed = 0
    direction_match: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for r in records:
        key = (r.get("strategy", ""), r.get("cycle", 0))
        ref_dir = ref_map.get(key)
        if ref_dir is None:
            continue
        matched += 1
        shadow_dir = r.get("direction", "neutral")
        if shadow_dir == ref_dir:
            agreed += 1
        direction_match[ref_dir][shadow_dir] += 1

    return {
        "agreement_pct": round(agreed / matched * 100, 1) if matched > 0 else 0.0,
        "matched_cycles": matched,
        "agreed_cycles": agreed,
        "direction_matrix": {ref_dir: dict(counts) for ref_dir, counts in direction_match.items()},
    }


def compute_signal_stability(records: list[dict]) -> dict[str, Any]:
    """Signal flip rate: how often direction changes between consecutive cycles.

    Groups records by strategy, sorts by cycle, counts direction transitions.
    """
    # Group by strategy
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_strategy[r.get("strategy", "unknown")].append(r)

    results: dict[str, Any] = {}
    for strat, recs in by_strategy.items():
        sorted_recs = sorted(recs, key=lambda r: r.get("cycle", 0))
        if len(sorted_recs) < 2:
            results[strat] = {"flip_rate": 0.0, "n_pairs": 0, "note": "too_few_cycles"}
            continue

        flips = 0
        pairs = 0
        for i in range(1, len(sorted_recs)):
            pairs += 1
            if sorted_recs[i].get("direction") != sorted_recs[i - 1].get("direction"):
                flips += 1

        results[strat] = {
            "flip_rate": round(flips / pairs * 100, 1) if pairs > 0 else 0.0,
            "n_pairs": pairs,
            "n_flips": flips,
        }

    return results


def compute_consensus_alignment(records: list[dict]) -> dict[str, Any]:
    """How often does the shadow brain agree with the final consensus direction?"""
    agreed = 0
    disagreed = 0
    total = 0
    for r in records:
        cons_dir = r.get("consensus_direction", "")
        brain_dir = r.get("direction", "neutral")
        if not cons_dir:
            continue
        total += 1
        if brain_dir == cons_dir:
            agreed += 1
        else:
            disagreed += 1

    return {
        "consensus_agreement_pct": round(agreed / total * 100, 1) if total > 0 else 0.0,
        "matched_cycles": total,
        "agreed": agreed,
        "disagreed": disagreed,
    }


def compute_raw_score_stats(records: list[dict]) -> dict[str, Any]:
    """Raw score (model output) distribution."""
    scores = []
    for r in records:
        ro = r.get("raw_outputs", {}) or {}
        rs = ro.get("raw_score")
        if rs is not None:
            scores.append(float(rs))

    if not scores:
        return {"n": 0, "note": "no_raw_scores"}

    arr = np.array(scores, dtype=np.float64)
    return {
        "mean": round(float(np.mean(arr)), 4),
        "median": round(float(np.median(arr)), 4),
        "std": round(float(np.std(arr, ddof=1)), 4),
        "min": round(float(np.min(arr)), 4),
        "max": round(float(np.max(arr)), 4),
        "p25": round(float(np.percentile(arr, 25)), 4),
        "p75": round(float(np.percentile(arr, 75)), 4),
        "n": len(scores),
    }


def compute_temporal_summary(records: list[dict]) -> dict[str, Any]:
    """Summarise records by recorded_at hour for temporal pattern detection."""
    by_hour: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        ts = r.get("recorded_at", "")
        if ts and len(ts) >= 13:
            hour_key = ts[:13]  # "2026-06-25T10"
            by_hour[hour_key].append(r)

    hourly: list[dict[str, Any]] = []
    for hk in sorted(by_hour.keys()):
        recs = by_hour[hk]
        dirs = Counter(r.get("direction", "neutral") for r in recs)
        confs = [r.get("confidence", 0.5) for r in recs]
        hourly.append(
            {
                "hour": hk,
                "n": len(recs),
                "dominant_direction": dirs.most_common(1)[0][0] if dirs else "none",
                "mean_confidence": round(float(np.mean(confs)), 4) if confs else 0.0,
            }
        )
    return {"hourly": hourly}


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Shadow Brain Prediction Quality Auditor")
    parser.add_argument("--data-dir", default="data_btc", help="Data directory root")
    parser.add_argument("--days", type=int, default=2, help="Number of recent days to analyse")
    parser.add_argument(
        "--brain-id",
        nargs="*",
        default=["BTC_Swing_V12_H1_Survival", "BTC_Swing_V12_H1_15"],
        help="Brain IDs to analyse (default: V12 shadow brains)",
    )
    parser.add_argument(
        "--ref-brain-id",
        default="BTC_Swing_V4",
        help="Reference brain for agreement comparison (default: V4, the live brain)",
    )
    args = parser.parse_args()

    target_brains = set(args.brain_id) if args.brain_id else set()
    ref_brain = args.ref_brain_id

    print(f"=== Shadow Brain Prediction Quality Audit ===")
    print(f"Data dir : {args.data_dir}")
    print(f"Date range: last {args.days} days")
    print(f"Target brains: {sorted(target_brains)}")
    print(f"Reference brain: {ref_brain}")
    print()

    # ── Load ──
    all_records = load_brain_votes(args.data_dir, days=args.days + 1)  # +1 for timezone safety
    ref_records = [r for r in all_records if r.get("brain_id") == ref_brain]

    if not ref_records:
        print(f"[WARN] No reference brain ({ref_brain}) records found in date range.")
    else:
        print(f"Reference brain ({ref_brain}): {len(ref_records)} records")

    print()

    # ── Per-brain analysis ──
    for brain_id in sorted(target_brains):
        records = [r for r in all_records if r.get("brain_id") == brain_id]
        if not records:
            print(f"--- {brain_id}: NO DATA in date range ---")
            print()
            continue

        print(f"{'='*60}")
        print(f"  {brain_id}")
        print(f"  Records: {len(records)}")
        print(f"{'='*60}")

        # Date span
        timestamps = sorted(r.get("recorded_at", "") for r in records)
        if timestamps:
            print(f"  Time span: {timestamps[0][:19]} → {timestamps[-1][:19]}")
        print()

        # 1. Direction Distribution
        dir_dist = compute_direction_distribution(records)
        print("  [DIR] Direction Distribution:")
        print(f"    LONG:   {dir_dist['long']:4d}  ({dir_dist['long_pct']:5.1f}%)")
        print(f"    SHORT:  {dir_dist['short']:4d}  ({dir_dist['short_pct']:5.1f}%)")
        print(f"    NEUTRAL:{dir_dist['neutral']:4d}  ({dir_dist['neutral_pct']:5.1f}%)")
        print(f"    Bias:   {dir_dist['bias']}")
        print()

        # 2. Confidence Statistics
        conf_stats = compute_confidence_stats(records)
        print("  [CONF] Confidence Statistics:")
        print(f"    Mean:   {conf_stats['mean']:.4f}")
        print(f"    Median: {conf_stats['median']:.4f}")
        print(f"    Std:    {conf_stats['std']:.4f}")
        print(f"    Min/Max:{conf_stats['min']:.4f} / {conf_stats['max']:.4f}")
        print(f"    P25/P75:{conf_stats['p25']:.4f} / {conf_stats['p75']:.4f}")
        print()

        # 3. Confidence Histogram
        hist = compute_confidence_histogram(records)
        if hist:
            print("  [DIST] Confidence Histogram:")
            max_count = max(h["count"] for h in hist)
            for h in hist:
                bar = "█" * max(1, int(h["count"] / max(max_count, 1) * 30))
                print(f"    {h['bucket']:>9}: {bar} {h['count']:3d} ({h['pct']:4.1f}%)")
        print()

        # 4. Agreement with Reference Brain (V4)
        agreement = compute_agreement(records, ref_records)
        print("  [AGREE] Direction Agreement with V4 (Reference):")
        print(f"    Matched cycles: {agreement['matched_cycles']}")
        print(f"    Agreed:         {agreement.get('agreed_cycles', 0)}")
        print(f"    Agreement %:    {agreement['agreement_pct']:.1f}%")
        dm = agreement.get("direction_matrix", {})
        if dm:
            print("    Direction matrix (V4→Shadow):")
            for ref_dir, counts in sorted(dm.items()):
                parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"      V4={ref_dir:>7} → {parts}")
        print()

        # 5. Signal Stability (Flip Rate)
        stability = compute_signal_stability(records)
        print("  [STABLE] Signal Stability (Flip Rate):")
        for strat, info in stability.items():
            if "note" in info:
                print(f"    {strat}: {info['note']}")
            else:
                print(
                    f"    {strat}: {info['flip_rate']:.1f}% flips ({info['n_flips']}/{info['n_pairs']} pairs)"
                )
        print()

        # 6. Consensus Alignment
        cons_align = compute_consensus_alignment(records)
        print("  [ALIGN] Consensus Alignment:")
        print(
            f"    Aligned with consensus:   {cons_align['agreed']}/{cons_align['matched_cycles']} ({cons_align['consensus_agreement_pct']:.1f}%)"
        )
        print(f"    Disagreed with consensus: {cons_align['disagreed']}")
        print()

        # 7. Raw Score Statistics
        raw_stats = compute_raw_score_stats(records)
        print("  [RAW] Raw Score (Model Output) Statistics:")
        if raw_stats.get("note"):
            print(f"    {raw_stats['note']}")
        else:
            print(f"    N:      {raw_stats['n']}")
            print(f"    Mean:   {raw_stats['mean']:.4f}")
            print(f"    Median: {raw_stats['median']:.4f}")
            print(f"    Std:    {raw_stats['std']:.4f}")
            print(f"    Min/Max:{raw_stats['min']:.4f} / {raw_stats['max']:.4f}")
        print()

        # 8. Temporal Summary
        temporal = compute_temporal_summary(records)
        if temporal.get("hourly"):
            print("  [TIME] Hourly Activity:")
            for h in temporal["hourly"]:
                print(
                    f"    {h['hour']}: n={h['n']:3d}, dir={h['dominant_direction']:>7}, conf={h['mean_confidence']:.4f}"
                )
        print()

    # ── Cross-Brain Comparison ──
    if len(target_brains) >= 2:
        print(f"{'='*60}")
        print("  Cross-Brain Comparison")
        print(f"{'='*60}")
        print()
        comparisons = []
        for brain_id in sorted(target_brains):
            records = [r for r in all_records if r.get("brain_id") == brain_id]
            if not records:
                continue
            dir_dist = compute_direction_distribution(records)
            conf_stats = compute_confidence_stats(records)
            agreement = compute_agreement(records, ref_records)
            cons_align = compute_consensus_alignment(records)
            stability = compute_signal_stability(records)
            raw_stats = compute_raw_score_stats(records)
            overall_flip = stability.get("btc_swing_h1", stability.get("btc_swing", {}))
            comparisons.append(
                {
                    "brain_id": brain_id,
                    "n": len(records),
                    "long_pct": dir_dist["long_pct"],
                    "short_pct": dir_dist["short_pct"],
                    "neutral_pct": dir_dist["neutral_pct"],
                    "bias": dir_dist["bias"],
                    "conf_mean": conf_stats["mean"],
                    "conf_std": conf_stats["std"],
                    "v4_agreement": agreement["agreement_pct"],
                    "consensus_agreement": cons_align["consensus_agreement_pct"],
                    "flip_rate": overall_flip.get("flip_rate", 0.0),
                    "raw_mean": raw_stats.get("mean", 0.0),
                    "raw_std": raw_stats.get("std", 0.0),
                }
            )

        # Summary table
        header = f"  {'Brain':<30} {'N':>5} {'Long%':>7} {'Short%':>7} {'Neut%':>7} {'Bias':>15} {'Confμ':>7} {'Confσ':>7} {'V4Agr%':>7} {'Flip%':>7} {'Rawμ':>7}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for c in comparisons:
            print(
                f"  {c['brain_id']:<30} {c['n']:5d} "
                f"{c['long_pct']:6.1f}% {c['short_pct']:6.1f}% {c['neutral_pct']:6.1f}% "
                f"{c['bias']:>15} "
                f"{c['conf_mean']:7.4f} {c['conf_std']:7.4f} "
                f"{c['v4_agreement']:6.1f}% {c['flip_rate']:6.1f}% {c['raw_mean']:7.4f}"
            )
        print()

        # Quality Assessment
        print("  [QUALITY] Quality Assessment:")
        print()
        for c in comparisons:
            issues = []
            ok = []

            # Direction bias check
            if c["neutral_pct"] > 70:
                issues.append(
                    f"[!!] Overly neutral ({c['neutral_pct']:.0f}%) — brain not discriminating"
                )
            elif c["neutral_pct"] < 5:
                ok.append(f"[OK] Low neutrality ({c['neutral_pct']:.0f}%) — decisive signals")

            if c["long_pct"] > 80 or c["short_pct"] > 80:
                issues.append(
                    f"[!] Strong directional bias ({c['bias']}) — may overfit to recent trend"
                )

            # Confidence check
            if c["conf_std"] < 0.03:
                issues.append(
                    f"[!!] Confidence too flat (σ={c['conf_std']:.4f}) — no signal differentiation"
                )
            elif c["conf_std"] > 0.15:
                ok.append(f"[OK] Good confidence spread (σ={c['conf_std']:.4f})")

            if c["conf_mean"] < 0.55:
                issues.append(f"[!] Mean confidence low ({c['conf_mean']:.3f}) — weak conviction")

            # V4 agreement check (informational only, not inherently good/bad)
            if c["v4_agreement"] > 90:
                ok.append(
                    f"[!] V4 agreement very high ({c['v4_agreement']:.0f}%) — possible herding"
                )
            elif c["v4_agreement"] < 30:
                ok.append(
                    f"[OK] Low V4 agreement ({c['v4_agreement']:.0f}%) — independent signal source"
                )

            # Flip rate
            if c["flip_rate"] > 30:
                issues.append(f"[!] High flip rate ({c['flip_rate']:.0f}%) — unstable signals")
            elif c["flip_rate"] < 10 and c["n"] > 20:
                ok.append(f"[OK] Low flip rate ({c['flip_rate']:.0f}%) — stable signals")

            print(f"  {c['brain_id']}:")
            for issue in issues:
                print(f"    {issue}")
            for o in ok:
                print(f"    {o}")
            if not issues and not ok:
                print("    [-] No significant patterns detected")
            print()

    print("=== Audit Complete ===")


if __name__ == "__main__":
    main()
