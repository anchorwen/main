"""Per-regime evaluation — segment model predictions by volatility regime.

Splits evaluation data into low/normal/high volatility buckets based on ATR
percentiles, then computes metrics per bucket. This catches models that look
good on average but fail in specific market conditions.

Usage:
  # From NPZ dataset + model predictions
  python scripts/training/eval_regime.py \\
    --data data/training/val.npz \\
    --predictions data/models/preds.npy \\
    --output data/reports/regime_eval.json

  # From Parquet with label and ATR columns
  python scripts/training/eval_regime.py \\
    --data data/training/val.parquet \\
    --output data/reports/regime_eval.json

  # Custom regime splits via JSON config
  python scripts/training/eval_regime.py \\
    --data data/training/val.npz \\
    --predictions data/models/preds.npy \\
    --regime-config blueprints/recipes/sur-g2026.1-recipe-001.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np

# M5_ATR_14 is at index 2 in V9_INSTITUTIONAL_40_FEATURES
DEFAULT_ATR_FEATURE_INDEX = 2


def _compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    """Compute classification metrics for a single regime bucket."""
    n = len(y_true)
    if n == 0:
        return {"n_samples": 0}

    correct = (y_pred == y_true).sum()
    tp = ((y_pred == 1) & (y_true == 1)).sum()
    fp = ((y_pred == 1) & (y_true == 0)).sum()
    fn = ((y_pred == 0) & (y_true == 1)).sum()

    accuracy = correct / n
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    pos_ratio = y_true.mean()
    pred_pos_ratio = y_pred.mean()

    return {
        "n_samples": n,
        "accuracy": round(accuracy, 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "true_positive_rate": round(pos_ratio, 4),
        "predicted_positive_rate": round(pred_pos_ratio, 4),
    }


def _compute_pnl_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    pnl: np.ndarray,
) -> dict[str, float]:
    """Compute P&L-based metrics for a regime bucket."""
    n = len(y_true)
    if n == 0:
        return {"n_samples": 0}

    # Simulate P&L: when prediction is correct, gain = abs(pnl); wrong, loss = -abs(pnl)
    correct = y_pred == y_true
    trade_pnl = np.where(correct, np.abs(pnl), -np.abs(pnl))

    total = float(trade_pnl.sum())
    avg = float(trade_pnl.mean())
    std = float(trade_pnl.std()) if n > 1 else 0.0
    sharpe = avg / std * np.sqrt(252) if std > 0 else 0.0

    wins = float((trade_pnl > 0).sum())
    win_rate = wins / n

    gross_profit = float(trade_pnl[trade_pnl > 0].sum())
    gross_loss = float(abs(trade_pnl[trade_pnl < 0].sum()))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    max_dd = 0.0
    peak = 0.0
    cumsum = 0.0
    for v in trade_pnl:
        cumsum += v
        if cumsum > peak:
            peak = cumsum
        dd = peak - cumsum
        if dd > max_dd:
            max_dd = dd

    return {
        "n_samples": n,
        "total_pnl": round(total, 6),
        "avg_pnl": round(avg, 6),
        "sharpe_ratio": round(sharpe, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else 999.0,
        "win_rate": round(win_rate, 4),
        "max_drawdown": round(max_dd, 4),
        "sortino_ratio": _sortino(trade_pnl),
    }


def _sortino(pnl: np.ndarray, target: float = 0.0) -> float:
    downside = pnl[pnl < target]
    if len(downside) == 0:
        return 999.0
    downside_std = float(np.std(downside))
    if downside_std == 0:
        return 0.0
    return round(float(pnl.mean() - target) / downside_std * np.sqrt(252), 4)


def evaluate_per_regime(
    X: np.ndarray,
    y_true: np.ndarray,
    y_pred: np.ndarray,
    pnl: np.ndarray | None = None,
    *,
    atr_feature_index: int = DEFAULT_ATR_FEATURE_INDEX,
    bounds_low: tuple[float, float] = (0.0, 0.33),
    bounds_normal: tuple[float, float] = (0.33, 0.67),
    bounds_high: tuple[float, float] = (0.67, 1.0),
) -> dict[str, Any]:
    """Evaluate predictions across three volatility regimes.

    Regimes are defined by ATR percentile boundaries:
      - Low:   ATR in [p_low_low, p_low_high)
      - Normal: ATR in [p_normal_low, p_normal_high)
      - High:   ATR in [p_high_low, p_high_high)

    Args:
        X: Feature matrix. Column `atr_feature_index` used for regime split.
        y_true: Ground-truth labels (binary 0/1).
        y_pred: Model predictions (binary 0/1).
        pnl: Optional per-sample P&L for financial metrics.
        atr_feature_index: Which column in X is the ATR feature.
        bounds_low, bounds_normal, bounds_high: Percentile boundaries.

    Returns:
        Dict with per-regime metrics and global summary.
    """
    if atr_feature_index >= X.shape[1]:
        return {"error": f"ATR feature index {atr_feature_index} >= n_features {X.shape[1]}"}

    atr_values = X[:, atr_feature_index]
    p_low = np.percentile(atr_values, [bounds_low[0] * 100, bounds_low[1] * 100])
    p_normal = np.percentile(atr_values, [bounds_normal[0] * 100, bounds_normal[1] * 100])
    p_high = np.percentile(atr_values, [bounds_high[0] * 100, bounds_high[1] * 100])

    mask_low = (atr_values >= p_low[0]) & (atr_values < p_low[1])
    mask_normal = (atr_values >= p_normal[0]) & (atr_values < p_normal[1])
    mask_high = (atr_values >= p_high[0]) & (atr_values <= p_high[1])

    pnl_arr = pnl if pnl is not None else np.zeros(len(y_true))

    regime_results: dict[str, Any] = {
        "low_volatility": {
            "percentile_range": list(bounds_low),
            "atr_range": [round(float(p_low[0]), 4), round(float(p_low[1]), 4)],
            "classification": _compute_metrics(y_true[mask_low], y_pred[mask_low]),
            "pnl": _compute_pnl_metrics(y_true[mask_low], y_pred[mask_low], pnl_arr[mask_low]),
        },
        "normal_volatility": {
            "percentile_range": list(bounds_normal),
            "atr_range": [round(float(p_normal[0]), 4), round(float(p_normal[1]), 4)],
            "classification": _compute_metrics(y_true[mask_normal], y_pred[mask_normal]),
            "pnl": _compute_pnl_metrics(
                y_true[mask_normal], y_pred[mask_normal], pnl_arr[mask_normal]
            ),
        },
        "high_volatility": {
            "percentile_range": list(bounds_high),
            "atr_range": [round(float(p_high[0]), 4), round(float(p_high[1]), 4)],
            "classification": _compute_metrics(y_true[mask_high], y_pred[mask_high]),
            "pnl": _compute_pnl_metrics(y_true[mask_high], y_pred[mask_high], pnl_arr[mask_high]),
        },
    }

    # ── Consistency checks ──
    f1_low = regime_results["low_volatility"]["classification"]["f1"]
    f1_norm = regime_results["normal_volatility"]["classification"]["f1"]
    f1_high = regime_results["high_volatility"]["classification"]["f1"]
    pf_low = regime_results["low_volatility"]["pnl"]["profit_factor"]
    pf_norm = regime_results["normal_volatility"]["pnl"]["profit_factor"]
    pf_high = regime_results["high_volatility"]["pnl"]["profit_factor"]

    regimes_with_profit = sum(1 for pf in [pf_low, pf_norm, pf_high] if pf > 1.0)
    regime_consistency_passed = regimes_with_profit >= 2

    f1_values = [f1_low, f1_norm, f1_high]
    max_f1_drop = max(f1_values) - min(f1_values) if f1_values else 0.0

    return {
        "schema_version": "regime_eval.v1",
        "regime_feature": f"f_{atr_feature_index}",
        "atr_percentiles": {
            "low": [round(float(p_low[0]), 4), round(float(p_low[1]), 4)],
            "normal": [round(float(p_normal[0]), 4), round(float(p_normal[1]), 4)],
            "high": [round(float(p_high[0]), 4), round(float(p_high[1]), 4)],
        },
        "regimes": regime_results,
        "global": {
            "classification": _compute_metrics(y_true, y_pred),
            "pnl": _compute_pnl_metrics(y_true, y_pred, pnl_arr),
        },
        "consistency": {
            "regime_consistency_passed": regime_consistency_passed,
            "regimes_with_profit_factor_gt_1": regimes_with_profit,
            "max_f1_drop": round(max_f1_drop, 4),
            "f1_stability": "stable" if max_f1_drop < 0.15 else "unstable",
        },
    }


# ── Data loading for eval ──


def _load_eval_data(
    data_path: Path,
    predictions_path: Path | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load evaluation data from NPZ or Parquet.

    Returns (X, y_true, y_pred, pnl).
    """
    ext = data_path.suffix.lower()

    if ext == ".npz":
        data = np.load(data_path)
        X = data["X"]
        y_true = data["y"]
        pnl = data.get("pnl", np.zeros(len(y_true)))
        if predictions_path:
            y_pred = np.load(predictions_path)
            if y_pred.ndim > 1:
                y_pred = (y_pred > 0.5).astype(np.int32).ravel()
        else:
            y_pred = y_true.copy()
        return X, y_true, y_pred, pnl

    if ext == ".parquet":
        import pandas as pd

        df = pd.read_parquet(data_path)
        feature_cols = [f"f_{i}" for i in range(40)]
        X = df[feature_cols].to_numpy(dtype=np.float64)
        y_true = df["label"].map({"win": 1, "tp_hit_first": 1}).fillna(0).to_numpy(dtype=np.int32)
        pnl = df["pnl"].fillna(0.0).to_numpy(dtype=np.float64)
        if predictions_path:
            y_pred = np.load(predictions_path)
            if y_pred.ndim > 1:
                y_pred = (y_pred > 0.5).astype(np.int32).ravel()
        else:
            y_pred = y_true.copy()
        return X, y_true, y_pred, pnl

    raise ValueError(f"Unsupported format: {ext}")


def _load_regime_config(config_path: Path | None) -> dict[str, tuple[float, float]] | None:
    """Load regime split definitions from a recipe JSON or regime config."""
    if config_path is None:
        return None
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    # Try recipe's evaluation.regime_splits
    rs = cfg.get("evaluation", {}).get("regime_splits", {})
    if rs:
        # Take the first regime split definition
        first = next(iter(rs.values()))
        return {
            "low": tuple(first.get("low", [0.0, 0.33])),
            "normal": tuple(first.get("normal", [0.33, 0.67])),
            "high": tuple(first.get("high", [0.67, 1.0])),
        }
    return None


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval_regime",
        description="Per-regime evaluation by ATR percentile",
    )
    p.add_argument("--data", type=Path, required=True, help="Path to val.npz or val.parquet")
    p.add_argument(
        "--predictions",
        type=Path,
        default=None,
        help="Path to predictions.npy (default: use ground-truth labels)",
    )
    p.add_argument(
        "--regime-config",
        type=Path,
        default=None,
        help="Path to recipe JSON with regime_splits definition",
    )
    p.add_argument(
        "--atr-feature-index",
        type=int,
        default=DEFAULT_ATR_FEATURE_INDEX,
        help=f"Column index of ATR feature in X (default: {DEFAULT_ATR_FEATURE_INDEX} = M5_ATR_14)",
    )
    p.add_argument("--output", type=Path, default=None, help="Write report JSON to file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.data.exists():
        print(f"[ERROR] Data file not found: {args.data}", file=__import__("sys").stderr)
        return 2

    X, y_true, y_pred, pnl = _load_eval_data(args.data, args.predictions)

    regime_cfg = _load_regime_config(args.regime_config)
    kwargs: dict[str, Any] = {"atr_feature_index": args.atr_feature_index}
    if regime_cfg:
        kwargs["bounds_low"] = regime_cfg["low"]
        kwargs["bounds_normal"] = regime_cfg["normal"]
        kwargs["bounds_high"] = regime_cfg["high"]

    report = evaluate_per_regime(X, y_true, y_pred, pnl, **kwargs)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        print(f"[eval_regime] Report written to {out}")
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    # Return non-zero if regime consistency failed
    if not report.get("consistency", {}).get("regime_consistency_passed", True):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
