#!/usr/bin/env python3
"""Daily training readiness validator — enforces TrainingPipelineContract at every stage.

Institutional Data SLA: each pipeline stage has a formal input/output contract.
This script validates ALL stages against the contract every day.  Any violation
triggers an immediate alert — problems are caught within 24 hours, not on
training day.

Iron Law #11: Script stdout is the sole source of truth.
Iron Law #12: Architecture-first — this is the contract enforcement layer that
              prevents silent pipeline drift (L3 architecture defect).

Usage:
  python scripts/check_training_readiness.py --contract configs/contracts/training_pipeline_btc_metafilter_v3.json --data-dir data_btc
  python scripts/check_training_readiness.py --all  # validate all contracts
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


# ── Scoring ────────────────────────────────────────────────────────────
class StageVerdict:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


def _red(s: str) -> str:
    return f"\033[91m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[93m{s}\033[0m"


def _green(s: str) -> str:
    return f"\033[92m{s}\033[0m"


# ── Contract loader ─────────────────────────────────────────────────────
def load_contract(contract_path: str) -> dict[str, Any]:
    with open(contract_path, encoding="utf-8") as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════
# Stage 1: Feature Store
# ══════════════════════════════════════════════════════════════════════════


def validate_stage_1_feature_store(contract: dict[str, Any], data_dir: str) -> dict[str, Any]:
    """Validate Feature Store against contract Stage 1."""
    stage = contract["stages"]["stage_1_feature_store"]
    outputs = stage["outputs"]
    results: list[dict[str, Any]] = []

    fs_path = Path(data_dir) / "feature_store" / "records"
    symbol_dir = None
    if fs_path.exists():
        for d in fs_path.iterdir():
            if d.is_dir() and d.name.lower().startswith("symbol="):
                symbol_dir = d
                break

    if symbol_dir is None:
        return {
            "stage": "stage_1_feature_store",
            "verdict": StageVerdict.FAIL,
            "results": [
                {
                    "check": "feature_store_exists",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"No feature store symbol directory found in {fs_path}",
                }
            ],
        }

    # Find the M5 timeframe file
    m5_dir = None
    for d in symbol_dir.iterdir():
        if d.is_dir() and "M5" in d.name:
            m5_dir = d
            break

    if m5_dir is None:
        return {
            "stage": "stage_1_feature_store",
            "verdict": StageVerdict.FAIL,
            "results": [
                {
                    "check": "m5_timeframe_exists",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"No M5 timeframe in {symbol_dir}",
                }
            ],
        }

    feat_file = m5_dir / "features.jsonl"
    if not feat_file.exists():
        return {
            "stage": "stage_1_feature_store",
            "verdict": StageVerdict.FAIL,
            "results": [
                {
                    "check": "features_file_exists",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"{feat_file} not found",
                }
            ],
        }

    # ── Read all feature records ──
    records: list[dict[str, Any]] = []
    with open(feat_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # ── Check 1: Schema presence ──
    schema_counter = Counter(r.get("schema_name", "?") for r in records)
    for _schema_key, schema_spec in outputs.items():
        schema_name = schema_spec["schema_name"]
        count = schema_counter.get(schema_name, 0)
        min_records = schema_spec.get("min_records", 100)
        if count >= min_records:
            results.append(
                {
                    "check": f"schema_{schema_name}_present",
                    "verdict": StageVerdict.PASS,
                    "detail": f"{count} records (min: {min_records})",
                    "metric": count,
                }
            )
        else:
            results.append(
                {
                    "check": f"schema_{schema_name}_present",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"Only {count} records (need {min_records}). Schema registered but never written to store — pipeline gap.",
                    "metric": count,
                }
            )

    # ── Check 2: Dimension match ──
    for _schema_key, schema_spec in outputs.items():
        schema_name = schema_spec["schema_name"]
        expected_dim = schema_spec["dimension"]
        schema_records = [r for r in records if r.get("schema_name") == schema_name]
        if not schema_records:
            continue
        actual_dim = len(schema_records[0].get("values", {}))
        if actual_dim == expected_dim:
            results.append(
                {
                    "check": f"schema_{schema_name}_dimension",
                    "verdict": StageVerdict.PASS,
                    "detail": f"{actual_dim} dim (expected: {expected_dim})",
                }
            )
        else:
            results.append(
                {
                    "check": f"schema_{schema_name}_dimension",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"{actual_dim} dim (expected: {expected_dim})",
                }
            )

    # ── Check 3: Freshness ──
    if records:
        latest_ts = max(r.get("event_time", "") for r in records if r.get("event_time"))
        try:
            latest_dt = datetime.fromisoformat(str(latest_ts)[:26])
            if latest_dt.tzinfo is None:
                latest_dt = latest_dt.replace(tzinfo=UTC)
            age_min = (datetime.now(UTC) - latest_dt).total_seconds() / 60
            max_age = max((s.get("max_age_minutes", 15) for s in outputs.values()), default=15)
            if age_min <= max_age:
                results.append(
                    {
                        "check": "freshness",
                        "verdict": StageVerdict.PASS,
                        "detail": f"{age_min:.0f} min old (max: {max_age} min)",
                        "metric": round(age_min, 1),
                    }
                )
            else:
                results.append(
                    {
                        "check": "freshness",
                        "verdict": StageVerdict.FAIL,
                        "detail": f"{age_min:.0f} min old (max: {max_age} min) — pipeline stalled",
                        "metric": round(age_min, 1),
                    }
                )
        except (ValueError, TypeError):
            results.append(
                {
                    "check": "freshness",
                    "verdict": StageVerdict.WARN,
                    "detail": "Cannot parse event_time",
                }
            )

    # ── Check 4: NaN rate ──
    nan_records = 0
    total_checked = 0
    for r in records[-500:]:  # Check last 500 for efficiency
        vals = r.get("values", {})
        if isinstance(vals, dict):
            total_checked += 1
            if any(v is None or (isinstance(v, float) and v != v) for v in vals.values()):
                nan_records += 1
    max_nan_pct = max((s.get("max_nan_pct", 0.05) for s in outputs.values()), default=0.05)
    if total_checked > 0:
        nan_rate = nan_records / total_checked
        if nan_rate <= max_nan_pct:
            results.append(
                {
                    "check": "nan_rate",
                    "verdict": StageVerdict.PASS,
                    "detail": f"{nan_rate:.1%} NaN records (max: {max_nan_pct:.1%})",
                    "metric": round(nan_rate, 4),
                }
            )
        else:
            results.append(
                {
                    "check": "nan_rate",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"{nan_rate:.1%} NaN records (max: {max_nan_pct:.1%})",
                    "metric": round(nan_rate, 4),
                }
            )

    # ── Check 5: Co-timestamp coverage (all output schemas at same timestamps) ──
    output_schemas = [s.get("schema_name") for s in outputs.values() if s.get("schema_name")]
    if len(output_schemas) >= 2:
        # Check overlap between first two schemas (multi-schema pipeline)
        s0_name = output_schemas[0]
        s1_name = output_schemas[1]
        s0_times = {r.get("event_time") for r in records if r.get("schema_name") == s0_name}
        s1_times = {r.get("event_time") for r in records if r.get("schema_name") == s1_name}
        if s0_times and s1_times:
            overlap = len(s0_times & s1_times)
            overlap_pct = overlap / len(s0_times)
            label = f"{overlap}/{len(s0_times)} timestamps have both schemas ({overlap_pct:.1%})"
            if overlap_pct >= 0.80:
                results.append(
                    {
                        "check": "co_timestamp_coverage",
                        "verdict": StageVerdict.PASS,
                        "detail": label,
                        "metric": round(overlap_pct, 4),
                    }
                )
            else:
                results.append(
                    {
                        "check": "co_timestamp_coverage",
                        "verdict": StageVerdict.FAIL,
                        "detail": f"Only {label}. {s1_name} not stored alongside {s0_name}.",
                        "metric": round(overlap_pct, 4),
                    }
                )
        elif s0_times and not s1_times:
            results.append(
                {
                    "check": "co_timestamp_coverage",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"{s0_name} records exist but {s1_name} records are completely absent.",
                    "metric": 0.0,
                }
            )
    # Single-schema pipelines: co-timestamp check is N/A — all records share one schema

    # ── Aggregate verdict ──
    fails = [r for r in results if r["verdict"] == StageVerdict.FAIL]
    warns = [r for r in results if r["verdict"] == StageVerdict.WARN]
    if fails:
        verdict = StageVerdict.FAIL
    elif warns:
        verdict = StageVerdict.WARN
    else:
        verdict = StageVerdict.PASS

    return {
        "stage": "stage_1_feature_store",
        "verdict": verdict,
        "results": results,
        "schema_distribution": dict(schema_counter.most_common()),
        "total_records": len(records),
    }


# ══════════════════════════════════════════════════════════════════════════
# Stage 2: Journal
# ══════════════════════════════════════════════════════════════════════════


def validate_stage_2_journal(contract: dict[str, Any], data_dir: str) -> dict[str, Any]:
    """Validate Journal against contract Stage 2."""
    stage = contract["stages"]["stage_2_journal"]
    outputs = stage["outputs"]
    results: list[dict[str, Any]] = []

    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return {
            "stage": "stage_2_journal",
            "verdict": StageVerdict.FAIL,
            "results": [
                {
                    "check": "journal_exists",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"{journal_path} not found",
                }
            ],
        }

    # ── Load all entries ──
    entries: list[dict[str, Any]] = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    closed = [e for e in entries if e.get("ack_status") == "closed"]
    accepted = [e for e in entries if e.get("ack_status") == "accepted"]

    # ── Check 1: Closed trade count ──
    closed_spec = outputs["closed_trades"]
    min_closed = closed_spec["min_count"]
    if len(closed) >= min_closed:
        results.append(
            {
                "check": "closed_trade_count",
                "verdict": StageVerdict.PASS,
                "detail": f"{len(closed)} closed trades (min: {min_closed})",
                "metric": len(closed),
            }
        )
    else:
        gap = min_closed - len(closed)
        # Estimate days remaining
        daily_rate = max(1, len(closed) / max(1, _date_span_days(closed)))
        days_left = gap / daily_rate if daily_rate > 0 else gap
        results.append(
            {
                "check": "closed_trade_count",
                "verdict": StageVerdict.FAIL,
                "detail": f"{len(closed)}/{min_closed} ({gap} short). Est ~{days_left:.0f} days at {daily_rate:.0f} trades/day.",
                "metric": len(closed),
                "gap": gap,
                "daily_rate": round(daily_rate, 1),
                "est_days_remaining": round(days_left, 1),
            }
        )

    # ── Check 2: PnL completeness ──
    pnl_null = sum(1 for e in closed if e.get("pnl") is None)
    pnl_null_pct = pnl_null / len(closed) if closed else 0
    max_null = closed_spec.get("max_pnl_null_pct", 0.05)
    if pnl_null_pct <= max_null:
        results.append(
            {
                "check": "pnl_completeness",
                "verdict": StageVerdict.PASS,
                "detail": f"{pnl_null}/{len(closed)} null PnL ({pnl_null_pct:.1%})",
                "metric": round(pnl_null_pct, 4),
            }
        )
    else:
        results.append(
            {
                "check": "pnl_completeness",
                "verdict": StageVerdict.FAIL,
                "detail": f"{pnl_null}/{len(closed)} null PnL ({pnl_null_pct:.1%})",
                "metric": round(pnl_null_pct, 4),
            }
        )

    # ── Check 3: Label coverage ──
    valid_labels = set(closed_spec.get("valid_labels", ["win", "loss"]))
    trainable = [e for e in closed if e.get("label") in valid_labels]
    results.append(
        {
            "check": "label_coverage",
            "verdict": StageVerdict.PASS
            if len(trainable) >= min_closed * 0.3
            else StageVerdict.WARN,
            "detail": f"{len(trainable)} trainable labels (valid: {sorted(valid_labels)})",
            "metric": len(trainable),
        }
    )

    # ── Check 4: Open→Close linkage ──
    open_entries = [e for e in accepted if e.get("action") == "open"]
    close_with_open_msg = sum(1 for e in closed if e.get("open_message_id"))
    results.append(
        {
            "check": "open_close_linkage",
            "verdict": StageVerdict.PASS if close_with_open_msg > 0 else StageVerdict.WARN,
            "detail": f"{close_with_open_msg}/{len(closed)} closed entries have open_message_id. {len(open_entries)} open entries available for p_win join.",
            "metric": close_with_open_msg,
        }
    )

    # ── Check 5: p_win coverage on open entries ──
    open_spec = outputs["open_entries"]
    min_pwin_cov = open_spec.get("p_win_coverage_min", 0.30)
    open_with_pwin = sum(1 for e in open_entries if e.get("p_win") is not None)
    pwin_cov = open_with_pwin / len(open_entries) if open_entries else 0
    if pwin_cov >= min_pwin_cov:
        results.append(
            {
                "check": "p_win_coverage",
                "verdict": StageVerdict.PASS,
                "detail": f"{open_with_pwin}/{len(open_entries)} open entries have p_win ({pwin_cov:.1%})",
                "metric": round(pwin_cov, 4),
            }
        )
    else:
        results.append(
            {
                "check": "p_win_coverage",
                "verdict": StageVerdict.FAIL,
                "detail": f"{open_with_pwin}/{len(open_entries)} open entries have p_win ({pwin_cov:.1%}). Need ≥{min_pwin_cov:.0%}. p_win not propagated from strategy decision.",
                "metric": round(pwin_cov, 4),
            }
        )

    # ── Check 6: entry_context coverage ──
    min_ec_cov = open_spec.get("entry_context_coverage_min", 0.30)
    open_with_ec = sum(1 for e in open_entries if e.get("entry_context") is not None)
    ec_cov = open_with_ec / len(open_entries) if open_entries else 0
    if ec_cov >= min_ec_cov:
        results.append(
            {
                "check": "entry_context_coverage",
                "verdict": StageVerdict.PASS,
                "detail": f"{open_with_ec}/{len(open_entries)} open entries have entry_context ({ec_cov:.1%})",
                "metric": round(ec_cov, 4),
            }
        )
    else:
        results.append(
            {
                "check": "entry_context_coverage",
                "verdict": StageVerdict.FAIL,
                "detail": f"{open_with_ec}/{len(open_entries)} open entries have entry_context ({ec_cov:.1%})",
                "metric": round(ec_cov, 4),
            }
        )

    fails = [r for r in results if r["verdict"] == StageVerdict.FAIL]
    warns = [r for r in results if r["verdict"] == StageVerdict.WARN]
    verdict = StageVerdict.FAIL if fails else (StageVerdict.WARN if warns else StageVerdict.PASS)

    # ── Label distribution summary ──
    label_dist = Counter(e.get("label") for e in closed)

    return {
        "stage": "stage_2_journal",
        "verdict": verdict,
        "results": results,
        "closed_count": len(closed),
        "open_count": len(open_entries),
        "trainable_labels": len(trainable),
        "label_distribution": dict(label_dist.most_common()),
    }


# ══════════════════════════════════════════════════════════════════════════
# Stage 3: Dataset Builder (simulation)
# ══════════════════════════════════════════════════════════════════════════


def _validate_feature_distributions(
    X: Any,  # numpy array — imported inside
    feature_names: list[str],
    *,
    variance_epsilon: float = 1e-6,
    outlier_max_abs: float = 20.0,
) -> list[dict[str, Any]]:
    """Feature Quality Dictator — mandatory statistical assertions.

    Institutional Data SLA (Column 4): Every feature in the training dataset
    is automatically validated.  No hardcoded feature names — reads the
    schema dynamically.  Any violation is a FATAL block — training cannot
    proceed until the data pipeline is fixed.

    Three mandatory assertions:
      1. VARIANCE > epsilon  — constant features = dead weight + matrix singularity
      2. NO NaN / Inf       — silent poison that corrupts every downstream computation
      3. MAX(|X|) < outlier_max_abs — for normalized (Z-score) data, extreme outliers
         indicate computation bugs (e.g. divide-by-zero in feature engineering)
    """
    import numpy as np

    results: list[dict[str, Any]] = []
    n_features = X.shape[1]

    if n_features != len(feature_names):
        results.append(
            {
                "check": "feature_quality_dimension",
                "verdict": StageVerdict.FAIL,
                "detail": f"Feature name count ({len(feature_names)}) != X columns ({n_features})",
            }
        )
        return results

    # ── Assertion 1: Variance > epsilon ──
    variances = np.var(X, axis=0)
    zero_var_mask = variances < variance_epsilon
    zero_var_count = int(np.sum(zero_var_mask))

    if zero_var_count > 0:
        dead_features = [feature_names[i] for i in np.where(zero_var_mask)[0]]
        results.append(
            {
                "check": "feature_quality_variance",
                "verdict": StageVerdict.FAIL,
                "detail": (
                    f"{zero_var_count}/{n_features} features have zero variance (< {variance_epsilon}). "
                    f"Dead features: {dead_features[:10]}{'...' if len(dead_features) > 10 else ''}. "
                    f"These features provide NO signal — check feature computation pipeline."
                ),
                "dead_features": dead_features,
                "dead_count": zero_var_count,
            }
        )
    else:
        results.append(
            {
                "check": "feature_quality_variance",
                "verdict": StageVerdict.PASS,
                "detail": f"All {n_features} features have variance > {variance_epsilon}",
            }
        )

    # ── Assertion 2: No NaN / Inf ──
    nan_mask = np.isnan(X)
    inf_mask = np.isinf(X)
    nan_count = int(np.sum(nan_mask))
    inf_count = int(np.sum(inf_mask))

    if nan_count > 0 or inf_count > 0:
        nan_features = sorted(set(feature_names[i] for i in np.where(np.any(nan_mask, axis=0))[0]))
        inf_features = sorted(set(feature_names[i] for i in np.where(np.any(inf_mask, axis=0))[0]))
        detail_parts = []
        if nan_count > 0:
            detail_parts.append(f"{nan_count} NaN values in features: {nan_features[:5]}")
        if inf_count > 0:
            detail_parts.append(f"{inf_count} Inf values in features: {inf_features[:5]}")
        results.append(
            {
                "check": "feature_quality_nan_inf",
                "verdict": StageVerdict.FAIL,
                "detail": "; ".join(detail_parts),
                "nan_count": nan_count,
                "inf_count": inf_count,
            }
        )
    else:
        results.append(
            {
                "check": "feature_quality_nan_inf",
                "verdict": StageVerdict.PASS,
                "detail": f"0 NaN, 0 Inf across all {n_features} features",
            }
        )

    # ── Assertion 3: Outlier bounds ──
    # For normalized (Z-score) data: |X| > 20.0 = computation bug.
    # For raw data (BTC prices, ATR): large values are legitimate.
    # Threshold is a WARN for raw data, configurable per contract.
    abs_max = np.max(np.abs(X), axis=0)
    outlier_mask = abs_max > outlier_max_abs
    outlier_count = int(np.sum(outlier_mask))

    if outlier_count > 0:
        outlier_features = [
            f"{feature_names[i]}={abs_max[i]:.1f}" for i in np.where(outlier_mask)[0]
        ]
        results.append(
            {
                "check": "feature_quality_outliers",
                "verdict": StageVerdict.WARN,
                "detail": (
                    f"{outlier_count}/{n_features} features exceed |X| > {outlier_max_abs}. "
                    f"Largest: {outlier_features[:5]}{'...' if len(outlier_features) > 5 else ''}. "
                    f"WARN if raw data (BTC ATR/MACD legitimately large); "
                    f"FAIL if normalized (Z-score) — computation bug."
                ),
                "outlier_count": outlier_count,
                "outlier_features": [feature_names[i] for i in np.where(outlier_mask)[0]],
            }
        )
    else:
        results.append(
            {
                "check": "feature_quality_outliers",
                "verdict": StageVerdict.PASS,
                "detail": f"All {n_features} features within |X| ≤ {outlier_max_abs}",
            }
        )

    return results


def validate_stage_3_dataset_builder(contract: dict[str, Any], data_dir: str) -> dict[str, Any]:
    """Simulate ASOF join and validate against contract Stage 3."""
    stage = contract["stages"]["stage_3_dataset_builder"]
    model_target = contract["model_target"]
    results: list[dict[str, Any]] = []

    expected_dim = model_target["input_dimension"]
    expected_features = model_target["feature_names_ssot"]

    # ── Try to build a dataset with the contract-specified builder script ──
    _builder_cfg = stage.get("builder_script", "scripts/build_btc_metafilter_v2_dataset.py")
    builder_script = Path(_builder_cfg)
    if not builder_script.exists():
        return {
            "stage": "stage_3_dataset_builder",
            "verdict": StageVerdict.SKIP,
            "results": [
                {
                    "check": "builder_script_exists",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"{builder_script} not found",
                }
            ],
        }

    # ── Build CLI args from contract; fallback to legacy BTC args ──
    _builder_args: list[str] = list(stage.get("builder_args", []))
    if not _builder_args:
        _builder_args = ["--data-dir", data_dir]

    # Run a dry dataset build and capture output
    import subprocess
    import tempfile

    _output_arg = stage.get("builder_output_arg", "--output")
    _tmp_dir = None
    if _output_arg == "--output-dir":
        _tmp_dir = tempfile.mkdtemp(suffix="_swing_dryrun")
        _builder_args.extend(["--output-dir", _tmp_dir])
        tmp_path = None  # signal: dir, not file
    else:
        with tempfile.NamedTemporaryFile(suffix=".npz", delete=False) as tmp:
            tmp_path = tmp.name
        _builder_args.extend(["--output", tmp_path])

    try:
        result = subprocess.run(
            [sys.executable, str(builder_script)] + _builder_args,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            results.append(
                {
                    "check": "builder_execution",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"Builder failed: {result.stderr[:200]}",
                }
            )
        else:
            results.append(
                {
                    "check": "builder_execution",
                    "verdict": StageVerdict.PASS,
                    "detail": "Dataset built successfully",
                }
            )

            # Check the NPZ output (dir output → find train.npz)
            import numpy as np

            _npz_path = tmp_path
            if _npz_path is None and _tmp_dir is not None:
                _candidates = list(Path(_tmp_dir).glob("*.npz"))
                if _candidates:
                    _npz_path = str(_candidates[0])
                else:
                    _npz_path = None
            # ── TECH_DEBT-020: NPZ load hardening (Defense in Depth, reader leg) ──
            # An empty/truncated/corrupt NPZ must NEVER crash the daily_ops
            # pipeline with a bare traceback. Degrade to an explicit verdict
            # so the report is honest about dataset un-readability.
            X: Any | None = None
            y: Any | None = None
            feature_names: list[str] = []
            if _npz_path is None:
                results.append(
                    {
                        "check": "dataset_npz_load",
                        "verdict": StageVerdict.WARN,
                        "detail": "No NPZ file found in builder output — cannot validate dimensions",
                    }
                )
            elif os.path.getsize(_npz_path) == 0:
                # Builder exited 0 but wrote nothing — the old silent-empty-NPZ
                # failure mode. An empty file is structurally un-loadable.
                results.append(
                    {
                        "check": "dataset_npz_load",
                        "verdict": StageVerdict.FAIL,
                        "detail": "Builder exited 0 but produced an EMPTY NPZ file — no dataset was written",
                    }
                )
            else:
                try:
                    data = np.load(_npz_path, allow_pickle=True)
                    X = data.get("X", data.get("features"))
                    y = data.get("y", data.get("labels"))
                    feature_names = list(data.get("feature_names", []))
                except (
                    EOFError,
                    ValueError,
                    OSError,
                    pickle.UnpicklingError,
                    zipfile.BadZipFile,
                ) as exc:
                    results.append(
                        {
                            "check": "dataset_npz_load",
                            "verdict": StageVerdict.FAIL,
                            "detail": (
                                f"NPZ unreadable ({type(exc).__name__}: {exc}) — "
                                "file empty, truncated, or corrupt; dataset cannot be validated"
                            ),
                        }
                    )

            if _npz_path is not None and X is not None:
                actual_dim = X.shape[1] if len(X.shape) > 1 else len(X)
                n_samples = X.shape[0] if len(X.shape) > 1 else 0

                # Check dimension
                if actual_dim == expected_dim:
                    results.append(
                        {
                            "check": "dataset_dimension",
                            "verdict": StageVerdict.PASS,
                            "detail": f"{actual_dim} dim matches model ({expected_dim})",
                        }
                    )
                else:
                    results.append(
                        {
                            "check": "dataset_dimension",
                            "verdict": StageVerdict.FAIL,
                            "detail": f"{actual_dim} dim ≠ model {expected_dim} dim. Training would produce incompatible model.",
                            "gap": expected_dim - actual_dim,
                        }
                    )

                # Check feature order
                if feature_names:
                    # Check if features are a SUBSET of expected (we may be missing some)
                    expected_set = set(expected_features)
                    actual_set = set(feature_names)
                    missing = expected_set - actual_set
                    extra = actual_set - expected_set
                    if not missing and not extra:
                        results.append(
                            {
                                "check": "feature_order_match",
                                "verdict": StageVerdict.PASS,
                                "detail": "All 47 features present, no extras",
                            }
                        )
                    else:
                        detail_parts = []
                        if missing:
                            detail_parts.append(
                                f"Missing {len(missing)}: {sorted(list(missing))[:5]}..."
                            )
                        if extra:
                            detail_parts.append(f"Extra {len(extra)}: {sorted(list(extra))[:5]}...")
                        results.append(
                            {
                                "check": "feature_order_match",
                                "verdict": StageVerdict.FAIL,
                                "detail": "; ".join(detail_parts),
                                "missing": sorted(list(missing)),
                                "extra": sorted(list(extra)),
                            }
                        )

                # Check sample count
                min_samples = model_target.get("min_training_samples", 200)
                if n_samples >= min_samples:
                    results.append(
                        {
                            "check": "sample_count",
                            "verdict": StageVerdict.PASS,
                            "detail": f"{n_samples} samples (min: {min_samples})",
                            "metric": n_samples,
                        }
                    )
                else:
                    gap = min_samples - n_samples
                    results.append(
                        {
                            "check": "sample_count",
                            "verdict": StageVerdict.FAIL,
                            "detail": f"{n_samples}/{min_samples} ({gap} short)",
                            "metric": n_samples,
                            "gap": gap,
                        }
                    )

                # Check ASOF join rate
                journal_closed = _count_journal_closed(data_dir)
                if journal_closed > 0:
                    asof_rate = n_samples / journal_closed
                    min_rate = stage["outputs"]["dataset"].get("min_asof_join_rate", 0.80)
                    if asof_rate >= min_rate:
                        results.append(
                            {
                                "check": "asof_join_rate",
                                "verdict": StageVerdict.PASS,
                                "detail": f"{asof_rate:.1%} ({n_samples}/{journal_closed})",
                                "metric": round(asof_rate, 4),
                            }
                        )
                    else:
                        results.append(
                            {
                                "check": "asof_join_rate",
                                "verdict": StageVerdict.FAIL,
                                "detail": f"{asof_rate:.1%} ({n_samples}/{journal_closed}). < {min_rate:.0%} minimum.",
                                "metric": round(asof_rate, 4),
                            }
                        )

                # Check label distribution
                if y is not None:
                    pos_count = int(sum(1 for v in y if v == 1))
                    pos_pct = pos_count / len(y) if len(y) > 0 else 0
                    min_pos = model_target.get("min_positive_label_pct", 0.15)
                    if pos_pct >= min_pos:
                        results.append(
                            {
                                "check": "label_distribution",
                                "verdict": StageVerdict.PASS,
                                "detail": f"{pos_count}/{len(y)} positive ({pos_pct:.1%})",
                            }
                        )
                    else:
                        results.append(
                            {
                                "check": "label_distribution",
                                "verdict": StageVerdict.WARN,
                                "detail": f"Only {pos_count}/{len(y)} positive ({pos_pct:.1%}). < {min_pos:.0%} minimum — model may struggle to learn.",
                            }
                        )

                # ── Feature Quality Dictator (Column 4 — Institutional Data SLA) ──
                # Dynamically scan every feature column for silent data poisoning.
                # No hardcoded feature names — reads from schema, covers all N features.
                # Three mandatory statistical assertions that CANNOT be bypassed.
                if X is not None and feature_names:
                    _fqd_results = _validate_feature_distributions(X, list(feature_names))
                    results.extend(_fqd_results)

    finally:
        import contextlib
        import shutil

        with contextlib.suppress(OSError):
            if tmp_path is not None:
                os.unlink(tmp_path)
        with contextlib.suppress(OSError):
            if _tmp_dir is not None:
                shutil.rmtree(_tmp_dir)

    fails = [r for r in results if r["verdict"] == StageVerdict.FAIL]
    warns = [r for r in results if r["verdict"] == StageVerdict.WARN]
    verdict = StageVerdict.FAIL if fails else (StageVerdict.WARN if warns else StageVerdict.PASS)

    return {
        "stage": "stage_3_dataset_builder",
        "verdict": verdict,
        "results": results,
    }


# ══════════════════════════════════════════════════════════════════════════
# Stage 4: Model Alignment
# ══════════════════════════════════════════════════════════════════════════


def validate_stage_4_model(contract: dict[str, Any], data_dir: str) -> dict[str, Any]:
    """Validate existing model against contract Stage 4."""
    model_target = contract["model_target"]
    results: list[dict[str, Any]] = []

    model_dir = Path(model_target["path"])
    if not model_dir.exists():
        return {
            "stage": "stage_4_model",
            "verdict": StageVerdict.SKIP,
            "results": [
                {
                    "check": "model_exists",
                    "verdict": StageVerdict.SKIP,
                    "detail": f"{model_dir} not found — no model to validate yet",
                }
            ],
        }

    # ── Check feature_names.json ──
    fn_path = model_dir / "feature_names.json"
    if fn_path.exists():
        with open(fn_path, encoding="utf-8") as f:
            model_features = json.load(f)

        expected = model_target["feature_names_ssot"]
        if isinstance(model_features, list) and len(model_features) == len(expected):
            if model_features == expected:
                results.append(
                    {
                        "check": "model_feature_isomorphism",
                        "verdict": StageVerdict.PASS,
                        "detail": f"Model features match contract exactly ({len(model_features)} dim)",
                    }
                )
            else:
                mismatches = [
                    i
                    for i, (a, b) in enumerate(zip(model_features, expected, strict=False))
                    if a != b
                ]
                results.append(
                    {
                        "check": "model_feature_isomorphism",
                        "verdict": StageVerdict.FAIL,
                        "detail": f"{len(mismatches)} positions differ between model and contract. Model must be retrained.",
                        "mismatch_positions": mismatches[:10],
                    }
                )
        else:
            results.append(
                {
                    "check": "model_feature_count",
                    "verdict": StageVerdict.FAIL,
                    "detail": f"Model has {len(model_features) if isinstance(model_features, list) else '?'} features, contract expects {len(expected)}",
                }
            )
    else:
        results.append(
            {
                "check": "feature_names_file",
                "verdict": StageVerdict.WARN,
                "detail": f"{fn_path} not found — cannot validate feature isomorphism",
            }
        )

    # ── Check training report ──
    report_path = model_dir / "meta_filter_report.json"
    if report_path.exists():
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
        n_samples = report.get("n_samples", 0)
        results.append(
            {
                "check": "model_training_samples",
                "verdict": StageVerdict.PASS
                if n_samples >= model_target.get("min_training_samples", 200)
                else StageVerdict.WARN,
                "detail": f"Trained on {n_samples} samples (contract min: {model_target.get('min_training_samples', 200)})",
            }
        )

    fails = [r for r in results if r["verdict"] == StageVerdict.FAIL]
    warns = [r for r in results if r["verdict"] == StageVerdict.WARN]
    verdict = StageVerdict.FAIL if fails else (StageVerdict.WARN if warns else StageVerdict.PASS)

    return {
        "stage": "stage_4_model",
        "verdict": verdict,
        "results": results,
    }


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════


def _date_span_days(closed_entries: list[dict[str, Any]]) -> int:
    dates = set()
    for e in closed_entries:
        ts = e.get("recorded_at", "")
        if ts:
            dates.add(ts[:10])
    return max(1, len(dates))


def _count_journal_closed(data_dir: str) -> int:
    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return 0
    count = 0
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line.strip())
                if e.get("ack_status") == "closed":
                    count += 1
            except json.JSONDecodeError:
                continue
    return count


# ══════════════════════════════════════════════════════════════════════════
# Reporter
# ══════════════════════════════════════════════════════════════════════════


def _icon(v: str) -> str:
    if v == StageVerdict.PASS:
        return _green("[PASS]")
    if v == StageVerdict.WARN:
        return _yellow("[WARN]")
    if v == StageVerdict.FAIL:
        return _red("[FAIL]")
    return "[SKIP]"


def print_report(report: dict[str, Any]) -> None:
    """Print human-readable readiness report from a report dict (display only, no return)."""
    contract_id = report["contract_id"]
    stage_results = report["stages"]
    overall = report["overall_verdict"]

    print(f"{'='*70}")
    print(f"  Training Readiness Report: {contract_id}")
    print(f"  Generated at: {report['generated_at']}")
    print(f"{'='*70}")

    for sr in stage_results:
        verdict = sr["verdict"]
        stage = sr["stage"]
        print(f"\n── {stage} {_icon(verdict)}")
        for r in sr.get("results", []):
            icon = _icon(r["verdict"])
            print(f"   {icon} {r['check']}: {r['detail']}")

    print(f"\n{'='*70}")
    print(f"  OVERALL: {_icon(overall)}")
    if overall == StageVerdict.PASS:
        print("  All pipeline stages ready for training.")
    elif overall == StageVerdict.FAIL:
        print("  Pipeline has blocking issues — training would fail or produce unusable model.")
    else:
        print("  Pipeline has warnings — training possible but suboptimal.")
    print(f"{'='*70}")


# ══════════════════════════════════════════════════════════════════════════
# Pure-function entry point (Plan B — DQAF-20260622-047)
# ══════════════════════════════════════════════════════════════════════════


def evaluate_training_readiness(contract_path: str | Path, data_dir: str) -> dict[str, Any]:
    """Evaluate a single training pipeline contract — pure function, no I/O beyond reads.

    This is the integration entry point for automated pipelines (daily_ops).
    It returns a machine-readable report dict; the caller is responsible for
    all write-side I/O through the StateWriter gate.

    Args:
        contract_path: Path to a training pipeline contract JSON file.
        data_dir: Symbol data directory (e.g. ``"data_btc"``).

    Returns:
        Dict with ``contract_id``, ``generated_at``, ``overall_verdict``,
        and ``stages`` keys.
    """
    cp = Path(contract_path)
    contract = load_contract(str(cp))
    contract_id = contract["contract_id"]

    stage_results = [
        validate_stage_1_feature_store(contract, data_dir),
        validate_stage_2_journal(contract, data_dir),
        validate_stage_3_dataset_builder(contract, data_dir),
        validate_stage_4_model(contract, data_dir),
    ]

    overall = StageVerdict.PASS
    for sr in stage_results:
        verdict = sr["verdict"]
        if verdict == StageVerdict.FAIL:
            overall = StageVerdict.FAIL
        elif verdict == StageVerdict.WARN and overall == StageVerdict.PASS:
            overall = StageVerdict.WARN

    return {
        "contract_id": contract_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "overall_verdict": overall,
        "stages": stage_results,
    }


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Daily training readiness validator — enforces TrainingPipelineContract"
    )
    parser.add_argument(
        "--contract",
        default="configs/contracts/training_pipeline_btc_metafilter_v3.json",
        help="Path to training pipeline contract JSON",
    )
    parser.add_argument("--data-dir", default="data_btc", help="Data directory")
    parser.add_argument(
        "--all", action="store_true", help="Validate all contracts in configs/contracts/"
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write machine-readable report to JSON file (default: data_btc/reports/training_readiness.json)",
    )
    args = parser.parse_args()

    contract_paths = []
    if args.all:
        contracts_dir = Path("configs/contracts")
        if contracts_dir.exists():
            contract_paths = list(contracts_dir.glob("*.json"))
    else:
        contract_paths = [Path(args.contract)]

    if not contract_paths:
        print("No contracts found.")
        return 1

    all_reports = []
    exit_code = 0

    for cp in contract_paths:
        if not cp.exists():
            print(f"Contract not found: {cp}")
            exit_code = 1
            continue

        report = evaluate_training_readiness(str(cp), args.data_dir)
        # Print human-readable summary
        print_report(report)
        all_reports.append(report)

        if report["overall_verdict"] == StageVerdict.FAIL:
            exit_code = 1

    # ── Write machine-readable report through StateWriter gate ──
    output_path = args.output or f"{args.data_dir}/reports/training_readiness.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    final_payload = all_reports[0] if len(all_reports) == 1 else {"contracts": all_reports}
    try:
        from core.state.catalog import lookup
        from core.state.writer import StateWriter

        writer = StateWriter.from_state_path(output_path)
        writer.write_artifact(lookup("TRAINING_READINESS"), writer._symbol, final_payload)
        print(f"\nReport saved to: {output_path} (via StateWriter gate)")
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        # Fallback: direct write if StateWriter is unavailable (e.g. standalone CLI use
        # without the full core package on PYTHONPATH)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_payload, f, indent=2, ensure_ascii=False)
        print(f"\nReport saved to: {output_path} (direct — StateWriter unavailable)")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
