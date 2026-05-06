"""Optuna hyperparameter search for Training Recipes.

Searches the hyperparameter space defined by a base recipe to find optimal
parameters. Outputs a new recipe JSON with the best found configuration.

Supports:
  - xgb_trainer (in-process, fast, recommended for search)
  - sur_trainer (subprocess to D:\\ai, slow, use --trainer sur)

Usage:
  # Fast search on XGBoost with 50 trials
  python scripts/training/recipe_search.py \\
    --recipe blueprints/recipes/sur-g2026.1-recipe-001.json \\
    --data data/training/train.npz \\
    --trials 50

  # Search + export best recipe
  python scripts/training/recipe_search.py \\
    --recipe blueprints/recipes/sur-g2026.1-recipe-001.json \\
    --data data/training/train.npz \\
    --trials 30 \\
    --output-recipe blueprints/recipes/sur-g2026.1-recipe-002.json

  # Resume previous study
  python scripts/training/recipe_search.py \\
    --recipe blueprints/recipes/sur-g2026.1-recipe-001.json \\
    --data data/training/train.npz \\
    --study-name sur-g2026.1-search \\
    --trials 20
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ═══════════════════════════════════════════════════════════════════════
# Search space definition
# ═══════════════════════════════════════════════════════════════════════


def suggest_params(trial, recipe: dict[str, Any]) -> dict[str, Any]:
    """Suggest hyperparameters for one Optuna trial.

    Uses the recipe's ranges as bounds. Each parameter gets a sensible
    search distribution based on its type and role.
    """
    t = recipe.get("training", {})
    d = recipe.get("data", {})

    params: dict[str, Any] = {}

    # ── Core training params ──
    params["n_estimators"] = trial.suggest_int(
        "n_estimators",
        max(50, t.get("epochs", 200) // 4),
        t.get("epochs", 200) * 2,
        step=25,
    )
    params["learning_rate"] = trial.suggest_float(
        "learning_rate",
        t.get("learning_rate", 0.001) * 0.1,
        t.get("learning_rate", 0.001) * 3.0,
        log=True,
    )
    params["max_depth"] = trial.suggest_int(
        "max_depth",
        3,
        max(t.get("hidden_dims", [128, 64, 32])[0] // 4, 8),
    )

    # ── Regularization ──
    params["subsample"] = trial.suggest_float("subsample", 0.5, 1.0, step=0.1)
    params["colsample_bytree"] = trial.suggest_float("colsample_bytree", 0.5, 1.0, step=0.1)
    params["dropout"] = trial.suggest_float(
        "dropout",
        t.get("dropout", 0.3) * 0.3,
        min(t.get("dropout", 0.3) * 2.0, 0.8),
    )

    # ── Data augmentation (on/off) ──
    da = d.get("data_augmentation", {})
    if da.get("enabled", False):
        params["augment"] = trial.suggest_categorical("augment", [True, False])

    return params


# ═══════════════════════════════════════════════════════════════════════
# Objective function
# ═══════════════════════════════════════════════════════════════════════


def _train_and_evaluate(
    data_path: Path,
    params: dict[str, Any],
    *,
    val_data_path: Path | None = None,
    augment: bool = False,
    augment_vol_scales: list[float] | None = None,
    augment_noise_std: float = 0.0,
    seed: int = 42,
) -> dict[str, Any]:
    """Train XGBoost and return metrics."""
    from core.features.data_augmentation import augment_dataset
    from scripts.training.trainers.xgb_trainer import (
        load_training_data,
        train_xgboost,
    )

    X, y, _pnl, feature_names = load_training_data(data_path)

    val_data = None
    if val_data_path is not None:
        Xv, yv, _, _ = load_training_data(val_data_path)
        val_data = (Xv, yv)

    if augment:
        X, y = augment_dataset(
            X,
            y,
            volatility_scaling=augment_vol_scales or [1.0],
            noise_std=augment_noise_std,
            seed=seed,
            concat_original=True,
        )

    xgb_params = {
        "n_estimators": params.get("n_estimators", 200),
        "max_depth": params.get("max_depth", 5),
        "learning_rate": params.get("learning_rate", 0.05),
        "subsample": params.get("subsample", 0.8),
        "colsample_bytree": params.get("colsample_bytree", 0.8),
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "random_state": seed,
        "n_jobs": -1,
        "early_stopping_rounds": 20,
    }

    _booster, metrics = train_xgboost(
        X, y, params=xgb_params, val_data=val_data, feature_names=feature_names
    )
    return metrics


def _load_recipe(recipe_path: Path) -> dict[str, Any]:
    return json.loads(recipe_path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════
# Main search loop
# ═══════════════════════════════════════════════════════════════════════


def run_search(
    recipe: dict[str, Any],
    data_path: Path,
    *,
    n_trials: int = 30,
    val_data_path: Path | None = None,
    study_name: str = "recipe-search",
    storage: str | None = None,
    seed: int = 42,
    direction: str = "maximize",
    metric: str = "val_accuracy",
) -> tuple[dict[str, Any], Any]:
    """Run Optuna hyperparameter search.

    Args:
        recipe: Loaded recipe dict (training_recipe.v1).
        data_path: Path to training data (NPZ or Parquet).
        n_trials: Number of Optuna trials.
        val_data_path: Optional separate validation data.
        study_name: Optuna study name for resuming.
        storage: Optuna storage URL (None = in-memory).
        seed: Random seed.
        direction: "maximize" or "minimize".
        metric: Metric to optimize ("val_accuracy", "train_accuracy", "train_time_seconds").

    Returns:
        (best_params, study) tuple.
    """
    import optuna

    da = recipe.get("data", {}).get("data_augmentation", {})

    def objective(trial):
        params = suggest_params(trial, recipe)

        augment = params.pop("augment", False)
        metrics = _train_and_evaluate(
            data_path,
            params,
            val_data_path=val_data_path,
            augment=augment,
            augment_vol_scales=da.get("volatility_scaling"),
            augment_noise_std=da.get("noise_std", 0.0),
            seed=seed + trial.number,
        )

        # Use val_accuracy if available, else train_accuracy
        score = metrics.get(metric, metrics.get("train_accuracy", 0.5))
        if score is None:
            score = metrics.get("train_accuracy", 0.5)

        # Report intermediate values for pruning
        trial.set_user_attr("train_accuracy", metrics.get("train_accuracy"))
        trial.set_user_attr("val_accuracy", metrics.get("val_accuracy"))
        trial.set_user_attr("n_estimators", metrics.get("n_estimators"))
        trial.set_user_attr("early_stopped", metrics.get("early_stopped", False))

        return float(score)

    # Create or load study
    study = optuna.create_study(
        study_name=study_name,
        storage=storage,
        direction=direction,
        sampler=optuna.samplers.TPESampler(seed=seed),
        pruner=optuna.pruners.MedianPruner(n_warmup_steps=5),
        load_if_exists=True,
    )

    print(f"[recipe_search] Study: {study_name}  Trials: {n_trials}  Direction: {direction}")
    print(f"[recipe_search] Metric: {metric}  Data: {data_path}")
    print("=" * 60)

    t0 = time.perf_counter()
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    elapsed = round(time.perf_counter() - t0, 1)

    best = study.best_params
    best_value = study.best_value

    print("=" * 60)
    print(f"[recipe_search] COMPLETE: {elapsed}s  Best {metric}={best_value:.4f}")
    print(f"[recipe_search] Best params: {json.dumps(best, indent=2)}")
    print(
        f"[recipe_search] Trials: {len(study.trials)}  Pruned: {sum(1 for t in study.trials if t.state.name == 'PRUNED')}"
    )

    return best, study


def export_best_recipe(
    recipe: dict[str, Any],
    best_params: dict[str, Any],
    output_path: Path,
    *,
    study_name: str = "",
    best_value: float = 0.0,
) -> Path:
    """Create a new recipe JSON with the best found hyperparameters."""
    new_recipe = json.loads(json.dumps(recipe))  # deep copy

    t = new_recipe.get("training", {})
    t["epochs"] = best_params.get("n_estimators", t.get("epochs", 200))
    t["learning_rate"] = best_params.get("learning_rate", t.get("learning_rate", 0.001))
    t["dropout"] = best_params.get("dropout", t.get("dropout", 0.3))

    # Update hidden_dims to reflect max_depth
    if "max_depth" in best_params and "hidden_dims" in t:
        t["hidden_dims"] = [best_params["max_depth"]] + t["hidden_dims"][1:]

    # Generate new recipe_id
    old_id = recipe.get("recipe_id", "unknown-recipe-000")
    parts = old_id.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        new_seq = int(parts[1]) + 1
        new_recipe["recipe_id"] = f"{parts[0]}-{new_seq:03d}"
    else:
        new_recipe["recipe_id"] = f"{old_id}-tuned"

    # Metadata
    new_recipe.setdefault("metadata", {})
    new_recipe["metadata"]["tuned_from"] = old_id
    new_recipe["metadata"]["optuna_study"] = study_name
    new_recipe["metadata"]["best_value"] = best_value
    new_recipe["metadata"]["tuned_at"] = _utc_now_iso()
    new_recipe["metadata"]["tuned_params"] = best_params

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(new_recipe, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[recipe_search] Best recipe exported: {output_path}")
    print(f"[recipe_search] New recipe_id: {new_recipe['recipe_id']}")
    return output_path


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recipe_search",
        description="Optuna hyperparameter search for Training Recipes",
    )
    p.add_argument("--recipe", type=Path, required=True, help="Base Training Recipe JSON")
    p.add_argument("--data", type=Path, required=True, help="Training data (NPZ or Parquet)")
    p.add_argument("--val-data", type=Path, default=None, help="Validation data (NPZ or Parquet)")
    p.add_argument("--trials", type=int, default=30, help="Number of Optuna trials (default: 30)")
    p.add_argument(
        "--study-name",
        default=None,
        help="Optuna study name for resuming (default: auto-generated)",
    )
    p.add_argument(
        "--storage",
        default=None,
        help="Optuna storage URL (default: in-memory, use sqlite:///study.db for persistence)",
    )
    p.add_argument("--seed", type=int, default=42, help="Random seed")
    p.add_argument(
        "--metric",
        default="val_accuracy",
        choices=["val_accuracy", "train_accuracy", "train_time_seconds"],
        help="Metric to optimize (default: val_accuracy)",
    )
    p.add_argument(
        "--direction",
        default="maximize",
        choices=["maximize", "minimize"],
        help="Optimization direction",
    )
    p.add_argument(
        "--output-recipe",
        type=Path,
        default=None,
        help="Export best recipe to this path",
    )
    p.add_argument(
        "--output-study",
        type=Path,
        default=None,
        help="Export study results as JSON",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.recipe.exists():
        print(f"[ERROR] Recipe not found: {args.recipe}", file=sys.stderr)
        return 2
    if not args.data.exists():
        print(f"[ERROR] Data not found: {args.data}", file=sys.stderr)
        return 2

    recipe = _load_recipe(args.recipe)
    study_name = args.study_name or f"{recipe.get('recipe_id', 'search')}-optuna"

    best_params, study = run_search(
        recipe,
        args.data,
        n_trials=args.trials,
        val_data_path=args.val_data,
        study_name=study_name,
        storage=args.storage,
        seed=args.seed,
        direction=args.direction,
        metric=args.metric,
    )

    if args.output_recipe:
        export_best_recipe(
            recipe,
            best_params,
            args.output_recipe,
            study_name=study_name,
            best_value=study.best_value,
        )

    if args.output_study:
        out = args.output_study
        out.parent.mkdir(parents=True, exist_ok=True)
        study_data = {
            "study_name": study_name,
            "best_params": best_params,
            "best_value": study.best_value,
            "n_trials": len(study.trials),
            "direction": args.direction,
            "metric": args.metric,
            "exported_at": _utc_now_iso(),
        }
        out.write_text(json.dumps(study_data, indent=2, ensure_ascii=False))
        print(f"[recipe_search] Study results exported: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
