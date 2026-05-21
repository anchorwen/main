"""Optimize ensemble weights from validation OOF probabilities.

After training both LGB and MLP Stage 2 classifiers, this script loads
each model's OOF (out-of-fold) validation predictions and grid-searches
for the optimal weight blend that minimizes log-loss.

Usage::

    python scripts/training/optimize_ensemble_weights.py \\
        --lgb-oof data/training/meta_lgb_oof.npy \\
        --mlp-oof data/training/meta_mlp_oof.npy \\
        --y-true data/training/meta_features_runtime.npz \\
        --output configs/brains/meta_stage2_filter_v3.json

    # Or inline with raw probabilities:
    python scripts/training/optimize_ensemble_weights.py \\
        --lgb-probs data/training/lgb_val_probs.npy \\
        --mlp-probs data/training/mlp_val_probs.npy \\
        --y-true data/training/y_val.npy \\
        --output configs/brains/meta_stage2_filter_v3.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Optimize ensemble weights for LGB+MLP Stage 2")
    p.add_argument("--lgb-probs", help="NPY file with LGB validation probabilities")
    p.add_argument("--mlp-probs", help="NPY file with MLP validation probabilities")
    p.add_argument("--lgb-oof", help="NPZ file (from train.py) containing LGB OOF probs")
    p.add_argument("--mlp-oof", help="NPZ file (from train.py) containing MLP OOF probs")
    p.add_argument("--y-true", required=True, help="NPY/NPZ file with binary true labels")
    p.add_argument("--output", help="Filter config JSON to update with optimal weights")
    p.add_argument("--step", type=float, default=0.05, help="Grid search step size (default: 0.05)")
    return p.parse_args()


def _load_probs(path: str | None, oof_path: str | None) -> np.ndarray:
    if path and Path(path).exists():
        return np.load(path).ravel()
    if oof_path and Path(oof_path).exists():
        return np.load(oof_path, allow_pickle=True).ravel()
    return np.array([])


def _load_y(path: str) -> np.ndarray:
    p = Path(path)
    if p.suffix == ".npz":
        data = np.load(p, allow_pickle=True)
        if "y" in data:
            return np.asarray(data["y"], dtype=np.int32).ravel()
        sys.exit(f"ERROR: No 'y' field in {path}")
    return np.load(path).ravel()


def main() -> None:
    args = _parse_args()

    lgb_probs = _load_probs(args.lgb_probs, args.lgb_oof)
    mlp_probs = _load_probs(args.mlp_probs, args.mlp_oof)
    y_true = _load_y(args.y_true)

    if len(lgb_probs) == 0 and len(mlp_probs) == 0:
        print("ERROR: At least one of --lgb-probs or --mlp-probs is required", file=sys.stderr)
        sys.exit(1)

    print(
        f"LGB  probs: {len(lgb_probs)} samples, mean={float(np.mean(lgb_probs)):.4f}"
        if len(lgb_probs)
        else "LGB: not provided"
    )
    print(
        f"MLP  probs: {len(mlp_probs)} samples, mean={float(np.mean(mlp_probs)):.4f}"
        if len(mlp_probs)
        else "MLP: not provided"
    )
    print(f"y_true:     {len(y_true)} samples, pos_rate={float(np.mean(y_true)):.4f}")

    # ── Individual model log-loss ──
    results: dict = {}
    if len(lgb_probs) == len(y_true):
        results["lgb_logloss"] = float(log_loss(y_true, lgb_probs))
        print(f"LGB  logloss = {results['lgb_logloss']:.6f}")
    if len(mlp_probs) == len(y_true):
        results["mlp_logloss"] = float(log_loss(y_true, mlp_probs))
        print(f"MLP  logloss = {results['mlp_logloss']:.6f}")

    # ── Grid search for optimal w_lgb ──
    if len(lgb_probs) == len(y_true) and len(mlp_probs) == len(y_true):
        best_w = 0.5
        best_loss = float("inf")
        grid_results: list[dict] = []

        for w in np.arange(0.0, 1.0 + args.step / 2, args.step):
            w = round(float(w), 4)
            ensemble_prob = w * lgb_probs + (1.0 - w) * mlp_probs
            loss = float(log_loss(y_true, ensemble_prob))
            grid_results.append({"w_lgb": w, "w_mlp": round(1 - w, 4), "logloss": round(loss, 6)})
            if loss < best_loss:
                best_loss = loss
                best_w = w

        results["optimal_w_lgb"] = round(best_w, 4)
        results["optimal_w_mlp"] = round(1 - best_w, 4)
        results["ensemble_logloss"] = round(best_loss, 6)
        results["grid_search"] = grid_results

        print(f"\nOptimal weights: LGB={best_w:.2f}, MLP={1-best_w:.2f}, logloss={best_loss:.6f}")

        # ── Logistic regression blender (optional) ──
        try:
            X_blend = np.column_stack([lgb_probs, mlp_probs])
            blender = LogisticRegression(penalty=None, solver="lbfgs")
            blender.fit(X_blend, y_true)
            blender_prob = blender.predict_proba(X_blend)[:, 1]
            blender_loss = float(log_loss(y_true, blender_prob))
            results["lr_blender_coef"] = [float(c) for c in blender.coef_[0]]
            results["lr_blender_intercept"] = float(blender.intercept_[0])
            results["lr_blender_logloss"] = round(blender_loss, 6)
            print(
                f"LR Blender: coef={results['lr_blender_coef']}, intercept={results['lr_blender_intercept']}, logloss={blender_loss:.6f}"
            )
        except Exception:
            pass
    elif len(lgb_probs) == len(y_true):
        results["optimal_w_lgb"] = 1.0
        results["optimal_w_mlp"] = 0.0
        results["ensemble_logloss"] = results.get("lgb_logloss", 0.0)
        print("\nOnly LGB available — weight set to 1.0")
    elif len(mlp_probs) == len(y_true):
        results["optimal_w_lgb"] = 0.0
        results["optimal_w_mlp"] = 1.0
        results["ensemble_logloss"] = results.get("mlp_logloss", 0.0)
        print("\nOnly MLP available — weight set to 1.0")

    # ── Write back to filter config ──
    if args.output:
        output_path = Path(args.output)
        if output_path.exists():
            with open(output_path, encoding="utf-8") as f:
                config = json.load(f)
            config["ensemble_weights"] = [
                results.get("optimal_w_lgb", 0.6),
                results.get("optimal_w_mlp", 0.4),
            ]
            if "ensemble_logloss" in results:
                config["ensemble_logloss"] = results["ensemble_logloss"]
            output_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            print(f"\nUpdated {output_path}: ensemble_weights = {config['ensemble_weights']}")

    print("\n" + json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
