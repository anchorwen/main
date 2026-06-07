"""CRT lane trainer wrapper: OU Statistical Arbitrage (lane=arb).

Optimizes OU process mean-reversion parameters via Optuna Bayesian search
(or grid search fallback), with Kalman filter dynamic half-life and ADX
trend-mute for adaptive signal quality.

Upgraded from v6 (324-combination grid search) to v7:
  - Optuna TPE Bayesian optimization (200 trials, 600s timeout)
  - Kalman filter for dynamic half-life tracking
  - ADX-based trend detection to auto-mute in strong trends
  - Direct in-process execution (no subprocess)

Protocol:
1. Accept --manifest-path (CRT manifest JSON, read-only input)
2. Accept --result-json-path (where to write result.json)
3. Accept --artifact-path (target path for arb_params.json)
4. Accept --dataset-csv (override data CSV)
5. Accept --n-trials (Optuna trials, default 200)
6. Run Optuna TPE optimization, output optimal params JSON
7. Write result.json with metrics / artifact_primary / risk_notes
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def utc_now_iso_z() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arb_trainer",
        description="CRT lane trainer: OU Statistical Arbitrage with Optuna + Kalman + Trend Mute (lane=arb)",
    )
    p.add_argument(
        "--manifest-path", type=Path, required=True, help="Path to CRT manifest JSON (input)"
    )
    p.add_argument(
        "--result-json-path", type=Path, required=True, help="Path for result.json output"
    )
    p.add_argument(
        "--artifact-path", type=Path, required=True, help="Target path for arb_params.json"
    )
    p.add_argument("--dataset-csv", type=Path, default=None, help="CSV training data")
    p.add_argument("--timeframe", type=str, default=None, help="Timeframe label (M5/M15/H1/H4)")
    p.add_argument(
        "--trainer-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "training" / "arb_v6",
        help="Directory containing training data",
    )
    p.add_argument("--recipe", type=Path, default=None, help="Training Recipe JSON for provenance")
    p.add_argument("--n-trials", type=int, default=200, help="Optuna trials (default: 200)")
    p.add_argument("--no-kalman", action="store_true", help="Disable Kalman filter")
    p.add_argument("--no-trend-mute", action="store_true", help="Disable ADX trend mute")
    p.add_argument(
        "--timeout", type=int, default=600, help="Optuna timeout in seconds (default: 600)"
    )
    return p


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def run_arb_optimization(
    dataset_csv: Path,
    artifact_path: Path,
    seed: int = 42,
    n_trials: int = 200,
    use_kalman: bool = True,
    use_trend_mute: bool = True,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run OU parameter optimization via Optuna TPE (or grid search fallback)."""
    from core.alpha.ou_optimizer import load_price_data, optimize

    print(f"[arb] Loading price data from {dataset_csv}...")
    prices = load_price_data(str(dataset_csv))
    print(
        f"[arb] Price series: {len(prices)} observations, range [{prices.min():.2f}, {prices.max():.2f}]"
    )
    print(f"[arb] Running Optuna optimization (n_trials={n_trials}, timeout={timeout_seconds}s)")
    print(f"[arb] Kalman filter: {use_kalman}, Trend mute: {use_trend_mute}")

    result = optimize(
        prices,
        n_trials=n_trials,
        seed=seed,
        use_kalman=use_kalman,
        use_trend_mute=use_trend_mute,
        timeout_seconds=timeout_seconds,
    )

    best_params = result["optimal_params"]
    best_metrics = result["metrics"]
    search_meta = result["search_meta"]

    print(f"\n[arb] === OPTIMAL PARAMETERS ({search_meta['method']}) ===")
    print(f"[arb] Window: {best_params['window']}")
    print(f"[arb] Z-Entry: {best_params['z_entry']} sigma")
    print(f"[arb] Z-Exit: {best_params['z_exit']} sigma")
    print(f"[arb] Max Half-Life: {best_params['max_half_life']} bars")
    print(f"[arb] Theta Min: {best_params['theta_min']}")
    print(f"[arb] Sharpe: {best_metrics['sharpe']:.2f}")
    print(f"[arb] Winrate: {best_metrics['winrate']*100:.1f}%")
    print(f"[arb] Total PnL: {best_metrics['total_pnl']:.2f}")
    print(f"[arb] Max DD: {best_metrics['max_drawdown_pct']:.1f}%")
    print(f"[arb] Profit Factor: {best_metrics['profit_factor']:.2f}")
    print(f"[arb] Trades: {best_metrics['total_trades']}")

    artifact = {
        "trainer": "arb_trainer",
        "version": "arb-v7-optuna-kalman-1.0.0",
        "model_type": "ou_statistical_arbitrage",
        "seed": seed,
        "dataset": str(dataset_csv),
        "data_points": len(prices),
        "optimal_params": best_params,
        "metrics": best_metrics,
        "search_meta": search_meta,
        "top_10_results": result["top_10_results"],
    }

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[arb] Artifact saved to {artifact_path}")
    return artifact


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    manifest = load_manifest(args.manifest_path)
    model_id = manifest.get("model_id", "unknown")
    lane = manifest.get("lane", "arb")
    generation = manifest.get("generation", "g2026.1")
    seed = manifest.get("train_seed", manifest.get("seed", 42))
    timeframe = args.timeframe or manifest.get("timeframe", "M5")
    dataset_override = manifest.get("dataset_override", None)

    recipe_id: str | None = None
    if args.recipe:
        from core.contracts.training.training_recipe import TrainingRecipe

        recipe_obj = TrainingRecipe.from_file(args.recipe)
        recipe_id = recipe_obj.recipe_id
        print(f"[arb_trainer] Recipe: {recipe_id}")

    trainer_root = args.trainer_root.resolve()
    dataset_csv = (
        args.dataset_csv
        or (Path(dataset_override) if dataset_override else None)
        or (trainer_root / "Exness_XAUUSDm_2026_04.csv")
    ).resolve()
    artifact_path = args.artifact_path.resolve()
    result_path = args.result_json_path.resolve()

    if not dataset_csv.exists():
        alt_csv = trainer_root / "00_Data_Lake" / "XAUUSDm_Mega_Tick_Full_11Months.csv"
        if alt_csv.exists():
            dataset_csv = alt_csv
            print(f"[arb_trainer] Using mega tick dataset: {dataset_csv}")
        else:
            print(f"[arb_trainer] ERROR: Dataset CSV not found: {dataset_csv}", file=sys.stderr)
            return 2

    print(
        f"[arb_trainer] Lane={lane}  Model={model_id}  Generation={generation}  TF={timeframe}  Seed={seed}"
    )
    print(f"[arb_trainer] Dataset CSV: {dataset_csv}")
    print(f"[arb_trainer] Artifact target: {artifact_path}")
    print("[arb_trainer] Starting OU parameter optimization...")

    try:
        artifact = run_arb_optimization(
            dataset_csv=dataset_csv,
            artifact_path=artifact_path,
            seed=seed,
            n_trials=args.n_trials,
            use_kalman=not args.no_kalman,
            use_trend_mute=not args.no_trend_mute,
            timeout_seconds=args.timeout,
        )
        exit_code = 0
    except Exception as exc:  # noqa: BLE001
        print(f"[arb_trainer] Optimization failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        artifact = {}
        exit_code = 1

    best_metrics = artifact.get("metrics", {})
    search_meta = artifact.get("search_meta", {})
    result: dict[str, Any] = {
        "trainer": "arb_trainer",
        "trainer_version": "arb-v7-optuna-kalman-1.0.0",
        "completed_at_utc": utc_now_iso_z(),
        "model_id": model_id,
        "lane": lane,
        "generation": generation,
        "timeframe": timeframe,
        "seed": seed,
        "exit_code": exit_code,
        "metrics": {
            "train_finished": exit_code == 0,
            "trainer_exit_code": exit_code,
            "dataset_csv": str(dataset_csv),
            "sharpe": best_metrics.get("sharpe"),
            "winrate_pct": best_metrics.get("winrate", 0) * 100
            if best_metrics.get("winrate")
            else None,
            "total_pnl": best_metrics.get("total_pnl"),
            "max_drawdown_pct": best_metrics.get("max_drawdown_pct"),
            "profit_factor": best_metrics.get("profit_factor"),
            "total_trades": best_metrics.get("total_trades"),
            "optimal_params": artifact.get("optimal_params", {}),
            "search_method": search_meta.get("method", "unknown"),
            "n_trials": search_meta.get("n_trials", 0),
            "kalman_filter": search_meta.get("kalman_filter", True),
            "trend_mute": search_meta.get("trend_mute", True),
        },
        "risk_notes": [],
        "artifact_primary": str(artifact_path) if artifact_path.exists() else None,
        "norm_artifact": None,
    }

    if exit_code != 0:
        result["risk_notes"].append("optimization failed")

    if artifact_path.exists():
        result["metrics"]["artifact_size_bytes"] = artifact_path.stat().st_size

    if recipe_id:
        result["recipe_id"] = recipe_id

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[arb_trainer] Result written: {result_path}")

    if exit_code != 0:
        print(f"[arb_trainer] FAILED exit={exit_code}", file=sys.stderr)
        return exit_code

    print("[arb_trainer] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
