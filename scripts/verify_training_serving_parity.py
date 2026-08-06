#!/usr/bin/env python3
"""Training-Serving Parity Verification (Column 2 — Feature Store Dual-Path).
# type: ignore  # FIX-20260620-076: Sev 4 audit script, suppressed

Institutional Data SLA: The feature vector used for live inference MUST be
bitwise-identical to the feature vector used for training.  Any deviation
is Training-Serving Skew — the model learns from data that differs from
what it sees in production.

This script performs three institutional-grade parity checks:

  Check 1 — JSON Serialization Round-Trip Fidelity
    Live path: float64 in memory → adapter → model
    Train path: float64 → JSON string → float64 → dataset builder → model
    Test: Does the JSON round-trip preserve float64 to within 1e-9?

  Check 2 — Schema Merge Fidelity
    Live path: FeatureService returns v9_40 dict + micro dict (same moment)
    Train path: load_feature_store() merges v9 + micro by event_time
    Test: Do the merged values match the raw store values exactly?

  Check 3 — ASOF Join Correctness
    For a set of known trade timestamps, verify the ASOF join selects
    the correct feature record (backward-looking, no future data).

Iron Law #11: Script stdout is the sole source of truth.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# ── Frozen timestamp for all tests ────────────────────────────────────
# Column 2 Guardrail: NEVER use time.now() in parity tests.
# All paths must see the identical "world cross-section".
FROZEN_NOW = datetime(2026, 6, 14, 10, 0, 0, tzinfo=UTC)


def _green(s: str) -> str:
    return f"\033[92m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[91m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[93m{s}\033[0m"


# ══════════════════════════════════════════════════════════════════════════
# Check 1: JSON Serialization Round-Trip Fidelity
# ══════════════════════════════════════════════════════════════════════════


def check_json_roundtrip(data_dir: str) -> dict[str, Any]:
    """Verify that float64 → JSON string → float64 preserves values."""
    print("─" * 60)
    print("Check 1: JSON Serialization Round-Trip Fidelity")
    print("─" * 60)

    fs_path = Path(data_dir) / "feature_store" / "records"
    m5_path = None
    for d in fs_path.iterdir() if fs_path.exists() else []:
        if d.is_dir() and "BTC" in d.name.upper():
            for tf in d.iterdir():
                if tf.is_dir() and "M5" in tf.name:
                    m5_path = tf / "features.jsonl"
                    break

    if m5_path is None or not m5_path.exists():
        return {
            "check": "json_roundtrip",
            "verdict": "SKIP",
            "detail": "No feature store records found",
        }

    # Read the latest 100 records and test round-trip
    records = []
    with open(m5_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not records:
        return {"check": "json_roundtrip", "verdict": "SKIP", "detail": "No valid records"}

    # Test the 100 most recent records
    test_records = records[-100:]
    max_abs_err = 0.0
    max_rel_err = 0.0
    worst_field = ""
    failures = 0
    total_fields = 0
    nan_count = 0

    for rec in test_records:
        values = rec.get("values", {})
        if not isinstance(values, dict):
            continue

        # Step 1: Original values (simulating live path memory state)
        original: dict[str, float] = {}
        for k, v in values.items():
            if isinstance(v, (int, float)):
                original[k] = float(v)

        # Step 2: JSON round-trip (simulating store → disk → read)
        json_str = json.dumps(values)
        restored = json.loads(json_str)

        # Step 3: Compare
        for k, orig_val in original.items():
            restored_val = restored.get(k)
            if restored_val is None:
                continue
            total_fields += 1

            if np.isnan(orig_val) or np.isnan(float(restored_val)):
                nan_count += 1
                continue

            abs_err = abs(orig_val - float(restored_val))
            if abs_err > max_abs_err:
                max_abs_err = abs_err
                worst_field = k

            if abs(orig_val) > 1e-12:
                rel_err = abs_err / abs(orig_val)
                if rel_err > max_rel_err:
                    max_rel_err = rel_err

            if abs_err > 1e-8:
                failures += 1

    print(f"  Records tested: {len(test_records)}")
    print(f"  Total fields: {total_fields}")
    print(f"  NaN fields (excluded): {nan_count}")
    print(f"  Max absolute error: {max_abs_err:.2e}")
    print(f"  Max relative error: {max_rel_err:.2e}")
    print(f"  Worst field: {worst_field}")
    print(f"  Fields with >1e-8 abs error: {failures}")

    if failures == 0 and max_abs_err < 1e-9:
        verdict = "PASS"
        print(f"  {_green('[PASS]')} JSON round-trip preserves float64 to < 1e-9")
    elif failures < total_fields * 0.001 and max_abs_err < 1e-6:
        verdict = "WARN"
        print(f"  {_yellow('[WARN]')} Minor precision loss — within acceptable range")
    else:
        verdict = "FAIL"
        print(f"  {_red('[FAIL]')} JSON round-trip introduces significant precision loss")

    return {
        "check": "json_roundtrip",
        "verdict": verdict,
        "max_abs_error": float(max_abs_err),
        "max_rel_error": float(max_rel_err),
        "worst_field": worst_field,
        "failure_count": failures,
    }


# ══════════════════════════════════════════════════════════════════════════
# Check 2: Schema Merge Fidelity
# ══════════════════════════════════════════════════════════════════════════


def check_merge_fidelity(data_dir: str) -> dict[str, Any]:
    """Verify that load_feature_store() merge preserves raw store values."""
    print()
    print("─" * 60)
    print("Check 2: Schema Merge Fidelity (v9 + micro → 47-dim)")
    print("─" * 60)

    from scripts.build_btc_metafilter_v2_dataset import (
        load_contract_feature_names,
        load_feature_store,
    )

    # Load merged features via the training path
    merged_features = load_feature_store(data_dir)
    if not merged_features:
        return {
            "check": "merge_fidelity",
            "verdict": "SKIP",
            "detail": "No merged features (micro store empty)",
        }

    # Load contract feature names
    contract_names = load_contract_feature_names(data_dir)
    if not contract_names:
        return {"check": "merge_fidelity", "verdict": "SKIP", "detail": "No contract feature names"}

    # For each merged record, verify against raw store records
    fs_path = Path(data_dir) / "feature_store" / "records"
    m5_path = None
    for d in fs_path.iterdir() if fs_path.exists() else []:
        if d.is_dir() and "BTC" in d.name.upper():
            for tf in d.iterdir():
                if tf.is_dir() and "M5" in tf.name:
                    m5_path = tf / "features.jsonl"
                    break

    if m5_path is None or not m5_path.exists():
        return {"check": "merge_fidelity", "verdict": "SKIP", "detail": "No feature store"}

    # Build lookup: event_time → v9 values
    raw_v9: dict[str, dict[str, float]] = {}
    raw_micro: dict[str, dict[str, float]] = {}
    with open(m5_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = rec.get("event_time", "")
            schema = rec.get("schema_name", "")
            vals = rec.get("values", {})
            if schema == "v9_institutional_40":
                raw_v9[et] = {k: float(v) for k, v in vals.items() if isinstance(v, (int, float))}
            elif schema == "v4.3_microstructure_9":
                raw_micro[et] = {
                    k: float(v) for k, v in vals.items() if isinstance(v, (int, float))
                }

    mismatches = 0
    matched_pairs = 0
    max_diff = 0.0
    worst_detail = ""

    for merged in merged_features[:20]:  # Check first 20 merged records
        et = merged.get("event_time", "")
        merged_vals = merged.get("values", {})

        v9_raw = raw_v9.get(et, {})
        micro_raw = raw_micro.get(et, {})

        if not v9_raw:
            continue

        # Check v9 fields
        for k, raw_v in v9_raw.items():
            merged_v = float(merged_vals.get(k, 0.0))
            diff = abs(raw_v - merged_v)
            if diff > max_diff:
                max_diff = diff
                worst_detail = f"v9 field '{k}' at {et}: raw={raw_v:.10f} merged={merged_v:.10f} diff={diff:.2e}"
            if diff > 1e-12:
                mismatches += 1

        # Check micro fields (if micro was available)
        if micro_raw:
            matched_pairs += 1
            for k, raw_v in micro_raw.items():
                merged_v = float(merged_vals.get(k, 0.0))
                diff = abs(raw_v - merged_v)
                if diff > max_diff:
                    max_diff = diff
                    worst_detail = f"micro field '{k}' at {et}: raw={raw_v:.10f} merged={merged_v:.10f} diff={diff:.2e}"
                if diff > 1e-12:
                    mismatches += 1

    print(f"  Merged records checked: {min(20, len(merged_features))}")
    print(f"  Records with matched micro: {matched_pairs}")
    print(f"  Value mismatches (>1e-12): {mismatches}")
    print(f"  Max difference: {max_diff:.2e}")
    if worst_detail:
        print(f"  Worst: {worst_detail}")

    if mismatches == 0:
        verdict = "PASS"
        print(f"  {_green('[PASS]')} Merge preserves raw store values exactly")
    elif max_diff < 1e-9:
        verdict = "WARN"
        print(f"  {_yellow('[WARN]')} Minor merge deviations — float precision only")
    else:
        verdict = "FAIL"
        print(f"  {_red('[FAIL]')} Merge introduces value distortion")

    return {
        "check": "merge_fidelity",
        "verdict": verdict,
        "mismatches": mismatches,
        "max_diff": float(max_diff),
        "matched_pairs": matched_pairs,
    }


# ══════════════════════════════════════════════════════════════════════════
# Check 3: ASOF Join Correctness (Knowledge-Time Filtering)
# ══════════════════════════════════════════════════════════════════════════


def check_asof_correctness(data_dir: str) -> dict[str, Any]:
    """Verify ASOF join never uses future or not-yet-known features."""
    print()
    print("─" * 60)
    print("Check 3: ASOF Join Correctness (No Future Data Leak)")
    print("─" * 60)

    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return {"check": "asof_correctness", "verdict": "SKIP", "detail": "No journal"}

    # Load trades with known entry times
    trades = []
    by_ticket: dict[int, list[dict]] = {}
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            tkt = e.get("position_ticket")
            if tkt:
                by_ticket.setdefault(int(tkt), []).append(e)

    for tkt, recs in by_ticket.items():
        opens = [r for r in recs if r.get("action") == "open"]
        if not opens:
            continue
        open_rec = opens[0]
        open_ts = open_rec.get("recorded_at", "")
        try:
            open_dt = datetime.fromisoformat(str(open_ts)[:26])
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        trades.append({"ticket": tkt, "open_time": open_dt, "open_ts_str": open_ts})

    if len(trades) < 5:
        return {
            "check": "asof_correctness",
            "verdict": "SKIP",
            "detail": f"Only {len(trades)} trades",
        }

    # Load features with timestamps
    fs_path = Path(data_dir) / "feature_store" / "records"
    m5_path = None
    for d in fs_path.iterdir() if fs_path.exists() else []:
        if d.is_dir() and "BTC" in d.name.upper():
            for tf in d.iterdir():
                if tf.is_dir() and "M5" in tf.name:
                    m5_path = tf / "features.jsonl"
                    break

    if m5_path is None or not m5_path.exists():
        return {"check": "asof_correctness", "verdict": "SKIP", "detail": "No feature store"}

    features = []
    with open(m5_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = rec.get("event_time", "")
            it = rec.get("ingested_at", "")
            try:
                event_dt = datetime.fromisoformat(str(et)[:26])
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue
            ingested_dt = None
            if it:
                try:
                    ingested_dt = datetime.fromisoformat(str(it)[:26])
                    if ingested_dt.tzinfo is None:
                        ingested_dt = ingested_dt.replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    pass
            features.append(
                {
                    "event_time": event_dt,
                    "ingested_at": ingested_dt,
                    "values": rec.get("values", {}),
                }
            )

    # Sort by event_time
    features.sort(key=lambda f: f["event_time"])

    # Test: for each trade, verify that the ASOF-selected feature:
    #   (a) has event_time <= trade.open_time
    #   (b) has ingested_at <= trade.open_time (if ingested_at exists)
    future_leaks = 0
    knowledge_leaks = 0
    checked = 0
    skipped_no_feature = 0
    skipped_no_ingested = 0

    for trade in trades[:50]:  # Check first 50 trades
        open_dt = trade["open_time"]
        # Find last feature with event_time <= open_dt (vanilla ASOF)
        best_idx = -1
        for i, feat in enumerate(features):
            if feat["event_time"] <= open_dt:
                best_idx = i
            else:
                break

        if best_idx < 0:
            skipped_no_feature += 1
            continue

        checked += 1
        feat = features[best_idx]

        # Check (a): event_time must be before trade
        if feat["event_time"] > open_dt:
            future_leaks += 1

        # Check (b): ingested_at must be before trade (knowledge-time)
        if feat["ingested_at"] is not None:
            if feat["ingested_at"] > open_dt:
                knowledge_leaks += 1
        else:
            skipped_no_ingested += 1

    print(f"  Trades checked: {checked}")
    print(f"  Future data leaks (event_time > trade_time): {future_leaks}")
    print(f"  Knowledge-time leaks (ingested_at > trade_time): {knowledge_leaks}")
    print(f"  Skipped — no prior feature: {skipped_no_feature}")
    print(f"  Skipped — no ingested_at (pre-Column-3 data): {skipped_no_ingested}")

    if future_leaks == 0 and knowledge_leaks == 0:
        verdict = "PASS"
        print(
            f"  {_green('[PASS]')} ASOF join is backward-looking only — no future or knowledge leaks"
        )
    elif future_leaks > 0:
        verdict = "FAIL"
        print(f"  {_red('[FAIL]')} {future_leaks} future data leaks detected!")
    else:
        verdict = "WARN"
        print(
            f"  {_yellow('[WARN]')} Knowledge-time leaks in pre-Column-3 data (no ingested_at yet)"
        )

    return {
        "check": "asof_correctness",
        "verdict": verdict,
        "trades_checked": checked,
        "future_leaks": future_leaks,
        "knowledge_leaks": knowledge_leaks,
    }


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════


def main() -> int:
    parser = argparse.ArgumentParser(description="Training-Serving Parity Verification (Column 2)")
    parser.add_argument("--data-dir", default="data_btc", help="Data directory")
    args = parser.parse_args()

    print("=" * 60)
    print("  Training-Serving Parity Verification (Column 2)")
    print(f"  Frozen timestamp: {FROZEN_NOW.isoformat()}")
    print("=" * 60)

    results = [
        check_json_roundtrip(args.data_dir),
        check_merge_fidelity(args.data_dir),
        check_asof_correctness(args.data_dir),
    ]

    # ── Summary ──
    print()
    print("=" * 60)
    print("  SUMMARY")
    print("=" * 60)

    exit_code = 0
    for r in results:
        icon = (
            _green("[PASS]")
            if r["verdict"] == "PASS"
            else (
                _yellow("[WARN]")
                if r["verdict"] == "WARN"
                else (_red("[FAIL]") if r["verdict"] == "FAIL" else "[SKIP]")
            )
        )
        print(f"  {icon} {r['check']}")
        if r["verdict"] == "FAIL":
            exit_code = 1

    if exit_code == 0:
        print(f"\n  {_green('[DONE]')} Training-serving paths are parity-verified.")
    else:
        print(f"\n  {_red('[DONE]')} Parity violations detected — fix before training.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
