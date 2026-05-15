"""Multi-timeframe model comparison evaluator.

Loads per-lane × per-TF training result JSON files from data/models/,
extracts val metrics, and produces a comparison table.

Usage:
  python scripts/training/eval_tf_comparison.py
  python scripts/training/eval_tf_comparison.py --results-dir data/models
  python scripts/training/eval_tf_comparison.py --format json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# ── Lane resolution ──────────────────────────────────────────────────

_LANE_PATTERNS: list[tuple[str, str]] = [
    # (model_file_substring, lane)
    ("micro_barrier", "mtx"),
    ("microstructure", "mtx"),
    ("transformer_v5", "mtx"),
    ("xgb_v4.5", "mtx"),
    ("lightgbm", "sur"),
    ("xgboost_v9", "sur"),
    ("arb_params", "arb"),
    ("ou_params", "arb"),
    ("meta_learner", "meta"),
]

# Sort by longest match first so "micro_barrier_m15" beats "micro_barrier"
_LANE_PATTERNS.sort(key=lambda x: -len(x[0]))


def _resolve_lane(filename: str) -> str:
    for substr, lane in _LANE_PATTERNS:
        if substr in filename.lower():
            return lane
    return "unknown"


def _resolve_tf(filename: str) -> str:
    name = filename.lower()
    # Check for explicit TF suffix in order of specificity
    for tf in ["_m15", "_h1", "_h4", "_d1", "m15", "h1", "h4"]:
        if tf in name:
            return tf.lstrip("_").upper()
    return "M5"


def _resolve_model_type(filename: str) -> str:
    name = filename.lower()
    if "transformer" in name:
        return "Transformer"
    if "xgb" in name:
        return "XGBoost"
    if "lightgbm" in name:
        return "LightGBM"
    if "meta_learner" in name:
        return "MetaLearner"
    if "arb" in name or "ou_params" in name:
        return "OU_Params"
    if "mlp" in name or "deepres" in name or "onnx" in name:
        return "MLP"
    return "Other"


# ── Discovery ────────────────────────────────────────────────────────


def discover_results(results_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted(results_dir.glob("*_result.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        metrics = data.get("metrics", {})
        if not metrics:
            continue

        filename = p.stem  # e.g. xgb_v4.5_micro_barrier_m15_result
        lane = _resolve_lane(filename)
        tf = _resolve_tf(filename)
        model_type = _resolve_model_type(filename)

        # Skip results without val_accuracy (e.g. test runs, regression-only models)
        # Skip results without barrier classification metrics (legacy regression models)
        if metrics.get("val_accuracy") is None:
            continue
        if metrics.get("val_acc_tp_hit") is None and metrics.get("val_acc_sl_hit") is None:
            continue

        row = {
            "file": p.name,
            "lane": lane,
            "timeframe": tf,
            "model_type": model_type,
            "val_accuracy": metrics.get("val_accuracy"),
            "val_tp": metrics.get("val_acc_tp_hit"),
            "val_sl": metrics.get("val_acc_sl_hit"),
            "val_timeout": metrics.get("val_acc_timeout"),
            "train_time_s": metrics.get("train_time_seconds"),
            "samples": data.get("data", {}).get("samples"),
            "epochs": metrics.get("epochs_completed") or metrics.get("n_estimators"),
        }
        rows.append(row)
    return rows


# ── Reporting ────────────────────────────────────────────────────────


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:.1f}%"


def _fmt_time(s: float | None) -> str:
    if s is None:
        return "n/a"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


_LANE_ORDER = {"mtx": 0, "sur": 1, "arb": 2, "meta": 3, "unknown": 99}
_TF_ORDER = {"M5": 0, "M15": 1, "H1": 2, "H4": 3, "D1": 4, "ALL": 99}


def _sort_key(r: dict) -> tuple:
    return (_LANE_ORDER.get(r["lane"], 99), _TF_ORDER.get(r["timeframe"], 99), r["file"])


def print_table(rows: list[dict[str, Any]]) -> str:
    """Return a markdown-format comparison table."""
    rows = sorted(rows, key=_sort_key)
    if not rows:
        return "No training results found."

    header = (
        f"{'Lane':<6} {'TF':>4} {'Model':<14} {'Acc':>7} {'TP':>7} {'SL':>7} "
        f"{'TOut':>7} {'Time':>6} {'Samples':>8}"
    )
    sep = "-" * len(header)
    lines = [sep, header, sep]

    last_lane = None
    for r in rows:
        if r["lane"] != last_lane and last_lane is not None:
            lines.append("")  # blank between lanes
        last_lane = r["lane"]
        lines.append(
            f"{r['lane']:<6} {r['timeframe']:>4} {r['model_type']:<14} "
            f"{_fmt_pct(r['val_accuracy']):>7} {_fmt_pct(r['val_tp']):>7} "
            f"{_fmt_pct(r['val_sl']):>7} {_fmt_pct(r['val_timeout']):>7} "
            f"{_fmt_time(r['train_time_s']):>6} {r['samples'] or 'n/a':>8}"
        )
    lines.append(sep)

    # Summary per lane
    lines.append("")
    lines.append("**Best by lane (TP detection):**")
    for lane in ["mtx", "sur", "arb", "meta"]:
        lane_rows = [r for r in rows if r["lane"] == lane and r["val_tp"] is not None]
        if not lane_rows:
            continue
        best_tp = max(lane_rows, key=lambda r: r["val_tp"])
        best_sl = max(lane_rows, key=lambda r: r["val_sl"] or 0)
        lines.append(
            f"  {lane}: TP-best = {best_tp['model_type']} {best_tp['timeframe']} "
            f"({_fmt_pct(best_tp['val_tp'])})  "
            f"SL-best = {best_sl['model_type']} {best_sl['timeframe']} "
            f"({_fmt_pct(best_sl['val_sl'])})"
        )

    return "\n".join(lines)


def print_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps(
        [
            {
                "lane": r["lane"],
                "timeframe": r["timeframe"],
                "model_type": r["model_type"],
                "val_accuracy": r["val_accuracy"],
                "val_tp": r["val_tp"],
                "val_sl": r["val_sl"],
                "val_timeout": r["val_timeout"],
                "train_time_s": r["train_time_s"],
                "samples": r["samples"],
            }
            for r in sorted(rows, key=_sort_key)
        ],
        indent=2,
    )


# ── CLI ──────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Multi-TF model comparison evaluator")
    p.add_argument(
        "--results-dir",
        type=Path,
        default=Path("data/models"),
        help="Directory containing *_result.json files (default: data/models)",
    )
    p.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="Output format (default: table)",
    )
    p.add_argument(
        "--lane",
        default=None,
        choices=["mtx", "sur", "arb", "meta"],
        help="Filter to a single lane",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results_dir = args.results_dir.resolve()
    if not results_dir.is_dir():
        print(f"ERROR: results directory not found: {results_dir}")
        return 1

    rows = discover_results(results_dir)
    if args.lane:
        rows = [r for r in rows if r["lane"] == args.lane]

    if args.format == "json":
        print(print_json(rows))
    else:
        print(print_table(rows))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
