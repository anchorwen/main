"""CRT lane trainer wrapper: Meta_ppo_v6 OU Statistical Arbitrage (lane=arb).

Bridges Meta_ppo_v6 OU process mean-reversion strategy into the CRT pipeline.

Unlike sur/mtx which train neural networks, the arb lane is a parameter optimization
problem: find the optimal OU theta / Z-Score thresholds / half-life filter that
maximizes risk-adjusted return on historical data.

Protocol:
1. Accept --manifest-path (CRT manifest JSON, read-only input)
2. Accept --result-json-path (where to write result.json for your_trainer.py to ingest)
3. Accept --artifact-path (target path for arb_params.json)
4. Accept --trainer-root (default: data/training/arb_v6)
5. Accept --dataset-csv (override data CSV, default: Exness_XAUUSDm_2026_04.csv)
6. Run OU parameter grid search backtest, output optimal params JSON
7. Write result.json with metrics / artifact_primary / risk_notes

Usage (as lane command template):
  python scripts/training/trainers/arb_trainer.py \\
    --manifest-path {manifest_path} \\
    --result-json-path {manifest_path}.result.json \\
    --artifact-path {artifact_path}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent  # scripts/training/trainers/
PROJECT_ROOT = SCRIPTS_DIR.parent.parent.parent  # future/


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
        description="CRT lane trainer: Meta_ppo_v6 OU Statistical Arbitrage (lane=arb)",
    )
    p.add_argument(
        "--manifest-path", type=Path, required=True, help="Path to CRT manifest JSON (input)"
    )
    p.add_argument(
        "--result-json-path",
        type=Path,
        required=True,
        help="Path to write result.json for your_trainer.py ingestion",
    )
    p.add_argument(
        "--artifact-path", type=Path, required=True, help="Target path for arb_params.json"
    )
    p.add_argument(
        "--dataset-csv",
        type=Path,
        default=None,
        help="CSV training data (default: <trainer-root>/Exness_XAUUSDm_2026_04.csv)",
    )
    p.add_argument(
        "--trainer-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "training" / "arb_v6",
        help="Directory containing Meta_ppo_v6 scripts (default: data/training/arb_v6)",
    )
    p.add_argument(
        "--recipe",
        type=Path,
        default=None,
        help="Path to Training Recipe JSON for hyperparameters and provenance",
    )
    return p


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def run_arb_backtest(
    trainer_root: Path,
    dataset_csv: Path,
    artifact_path: Path,
    seed: int = 42,
) -> subprocess.CompletedProcess:
    """Run OU parameter grid search backtest via inline Python.

    The backtest:
    1. Loads historical price data from CSV
    2. For each parameter combination (theta_min, z_entry, z_exit, max_half_life, window):
       - Runs rolling OU process detection
       - Generates long/short signals on Z-Score deviations
       - Computes P&L, Sharpe, winrate, max drawdown
    3. Selects the parameter set with highest Sharpe ratio
    4. Outputs arb_params.json with optimal parameters
    """
    inline = f"""
import os, sys, json, warnings
import traceback
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding='utf-8')

np.random.seed({seed})

# ==========================================
# 1. Load data
# ==========================================
csv_path = r"{dataset_csv}"
try:
    df = pd.read_csv(csv_path)
    print(f"[arb] Loaded {{len(df)}} rows")
except Exception as e:
    print("RESULT_ERROR=csv_load_failed", flush=True)
    print(f"[arb] CSV load error: {{e}}", flush=True)
    traceback.print_exc()
    sys.exit(7)

# Detect price column (common MT5 export formats)
price_col = None
for col in ['close', 'Close', 'CLOSE']:
    if col in df.columns:
        price_col = col
        break
if price_col is None:
    for col in ['Bid', 'bid', 'Ask', 'ask']:
        if col in df.columns:
            price_col = col
            break
if price_col is None:
    for col in df.columns:
        lower = col.lower()
        if 'close' in lower or 'bid' in lower or 'mid' in lower:
            price_col = col
            break
if price_col is None:
    print("RESULT_ERROR=no_price_column_found")
    print(f"[arb] Available columns: {{list(df.columns)}}")
    sys.exit(6)

prices_all = df[price_col].dropna().values.astype(np.float64)
if len(prices_all) < 200:
    print(f"RESULT_ERROR=insufficient_data; rows={{len(prices_all)}}")
    sys.exit(7)
# Downsample to max 50000 points for speed
MAX_DATAPOINTS = 50000
if len(prices_all) > MAX_DATAPOINTS:
    step = len(prices_all) // MAX_DATAPOINTS
    prices = prices_all[::step][:MAX_DATAPOINTS]
    print(f"[arb] Downsampled {{len(prices_all)}} -> {{len(prices)}} points (step={{step}})")
else:
    prices = prices_all
print(f"[arb] Price series: {{len(prices)}} observations, range [{{prices.min():.2f}}, {{prices.max():.2f}}]")

# ==========================================
# 2. OU Process Detection Function (from v6)
# ==========================================
def calc_ou_params(window_prices):
    y = np.diff(window_prices)
    x = window_prices[:-1]
    if len(x) < 2:
        return 0.0, np.mean(window_prices), np.inf, 0.0
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    beta = np.sum((x - x_mean) * (y - y_mean)) / (np.sum((x - x_mean) ** 2) + 1e-12)
    alpha = y_mean - beta * x_mean
    theta = -beta
    if theta <= 1e-6:
        return 0.0, np.mean(window_prices), np.inf, 0.0
    mu = alpha / theta
    half_life = np.log(2) / theta
    current_price = window_prices[-1]
    std_dev = np.std(window_prices)
    min_std = 0.50
    effective_std = max(std_dev, min_std)
    z_score = (current_price - mu) / effective_std if effective_std > 0 else 0.0
    if abs(mu - current_price) > effective_std * 10:
        mu = np.mean(window_prices)
        z_score = (current_price - mu) / effective_std if effective_std > 0 else 0.0
    return theta, mu, half_life, z_score

# ==========================================
# 3. Backtest Engine
# ==========================================
def run_backtest(prices, window, z_entry, z_exit, max_half_life, theta_min):
    position = 0
    entry_price = 0.0
    trades = []
    equity_curve = [0.0]
    n = len(prices)
    for i in range(window, n - 1):
        window_prices = prices[i - window : i]
        current_price = prices[i]
        theta, mu, half_life, z_score = calc_ou_params(window_prices)
        if position == 0:
            if half_life < max_half_life and theta > theta_min:
                if z_score < -z_entry:
                    position = 1
                    entry_price = current_price
                elif z_score > z_entry:
                    position = -1
                    entry_price = current_price
        elif position == 1:
            if z_score > -z_exit or z_score > z_entry * 0.3:
                pnl = current_price - entry_price
                trades.append(pnl)
                equity_curve.append(equity_curve[-1] + pnl)
                position = 0
        elif position == -1:
            if z_score < z_exit or z_score < -z_entry * 0.3:
                pnl = entry_price - current_price
                trades.append(pnl)
                equity_curve.append(equity_curve[-1] + pnl)
                position = 0
    if position != 0:
        final_pnl = (prices[-1] - entry_price) * position
        trades.append(final_pnl)
        equity_curve.append(equity_curve[-1] + final_pnl)
    if len(trades) < 5:
        return {{
            "total_trades": len(trades),
            "winrate": 0.0,
            "total_pnl": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 100.0,
            "profit_factor": 0.0,
        }}
    trades_arr = np.array(trades)
    wins = trades_arr > 0
    losses = trades_arr < 0
    total_pnl = float(trades_arr.sum())
    winrate = float(wins.sum() / len(trades_arr)) if len(trades_arr) > 0 else 0.0
    gross_profit = float(trades_arr[wins].sum()) if wins.any() else 0.0
    gross_loss = float(abs(trades_arr[losses].sum())) if losses.any() else 1e-8
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    returns = np.diff(np.array(equity_curve))
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(288 * 252))
    else:
        sharpe = 0.0
    equity = np.array(equity_curve)
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / (peak + 1e-8) * 100
    max_dd = float(np.max(dd))
    return {{
        "total_trades": len(trades),
        "winrate": winrate,
        "total_pnl": total_pnl,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd,
        "profit_factor": profit_factor,
    }}

# ==========================================
# 4. Parameter Grid Search
# ==========================================
print("[arb] Running OU parameter grid search...")
param_grid = [
    {{"window": w, "z_entry": ze, "z_exit": zx, "max_half_life": mhl, "theta_min": tm}}
    for w in [50, 100, 200]
    for ze in [1.5, 2.0, 2.5, 3.0]
    for zx in [0.3, 0.5, 0.8]
    for mhl in [8, 20, 40]
    for tm in [0.001, 0.005, 0.01]
]
print(f"[arb] Total parameter combinations: {{len(param_grid)}}")

best_sharpe = -999.0
best_params = None
best_metrics = None
results_log = []

for idx, params in enumerate(param_grid):
    metrics = run_backtest(
        prices,
        window=params["window"],
        z_entry=params["z_entry"],
        z_exit=params["z_exit"],
        max_half_life=params["max_half_life"],
        theta_min=params["theta_min"],
    )
    results_log.append({{**params, **metrics}})
    if metrics["sharpe"] > best_sharpe and metrics["winrate"] >= 0.48:
        best_sharpe = metrics["sharpe"]
        best_params = params
        best_metrics = metrics
    if (idx + 1) % 50 == 0:
        print(f"[arb] Progress: {{idx+1}}/{{len(param_grid)}} ... best Sharpe={{best_sharpe:.2f}}", flush=True)

if best_params is None:
    print("RESULT_ERROR=no_valid_params_found")
    sys.exit(8)

print(f"\\n[arb] === OPTIMAL PARAMETERS FOUND ===")
print(f"[arb] Window: {{best_params['window']}}")
print(f"[arb] Z-Entry: {{best_params['z_entry']}} sigma")
print(f"[arb] Z-Exit: {{best_params['z_exit']}} sigma")
print(f"[arb] Max Half-Life: {{best_params['max_half_life']}} bars")
print(f"[arb] Theta Min: {{best_params['theta_min']}}")
print(f"[arb] Sharpe: {{best_metrics['sharpe']:.2f}}")
print(f"[arb] Winrate: {{best_metrics['winrate']*100:.1f}}%")
print(f"[arb] Total PnL: {{best_metrics['total_pnl']:.2f}}")
print(f"[arb] Max DD: {{best_metrics['max_drawdown_pct']:.1f}}%")
print(f"[arb] Profit Factor: {{best_metrics['profit_factor']:.2f}}")
print(f"[arb] Trades: {{best_metrics['total_trades']}}")

# ==========================================
# 5. Output artifact
# ==========================================
artifact = {{
    "trainer": "arb_trainer",
    "version": "arb-v6-ou-sniper-1.0.0",
    "model_type": "ou_statistical_arbitrage",
    "seed": {seed},
    "dataset": str(csv_path),
    "data_points": len(prices),
    "optimal_params": best_params,
    "metrics": best_metrics,
    "search_grid_size": len(param_grid),
    "top_10_results": sorted(results_log, key=lambda x: x["sharpe"], reverse=True)[:10],
}}

artifact_path = r"{artifact_path}"
os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
with open(artifact_path, "w", encoding="utf-8") as f:
    json.dump(artifact, f, ensure_ascii=False, indent=2)
print(f"RESULT_ARTIFACT={{artifact_path}}")
print(f"[arb] Artifact saved to {{artifact_path}}")
"""
    # Write inline script to temp file (avoids cmd-line length limits and escaping issues)
    tmp_py = artifact_path.parent / f"_arb_backtest_{seed}.py"
    tmp_py.write_text(inline, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, str(tmp_py)],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
            env=env,
        )
    finally:
        try:
            tmp_py.unlink(missing_ok=True)
        except Exception:
            pass
    return proc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    manifest = load_manifest(args.manifest_path)
    model_id = manifest.get("model_id", "unknown")
    lane = manifest.get("lane", "arb")
    generation = manifest.get("generation", "g2026.1")
    seed = manifest.get("seed", 42)

    # ── Load recipe if provided ──
    recipe_id: str | None = None
    if args.recipe:
        from core.contracts.training.training_recipe import TrainingRecipe

        recipe_obj = TrainingRecipe.from_file(args.recipe)
        recipe_id = recipe_obj.recipe_id
        print(f"[arb_trainer] Recipe: {recipe_id}")

    trainer_root = args.trainer_root.resolve()
    if not trainer_root.exists():
        legacy = Path(r"D:\ai\Meta_ppo_v6")
        if legacy.exists():
            print(f"[arb_trainer] Internal trainer root not found: {trainer_root}")
            print(f"[arb_trainer] Falling back to legacy path: {legacy}")
            trainer_root = legacy
    dataset_csv = (args.dataset_csv or (trainer_root / "Exness_XAUUSDm_2026_04.csv")).resolve()
    artifact_path = args.artifact_path.resolve()
    result_path = args.result_json_path.resolve()

    if not dataset_csv.exists():
        # Fallback: try the mega tick file
        alt_csv = trainer_root / "00_Data_Lake" / "XAUUSDm_Mega_Tick_Full_11Months.csv"
        if alt_csv.exists():
            dataset_csv = alt_csv
            print(f"[arb_trainer] Using mega tick dataset: {dataset_csv}")
        else:
            print(f"[arb_trainer] ERROR: Dataset CSV not found: {dataset_csv}", file=sys.stderr)
            print(f"[arb_trainer] Also tried: {alt_csv}", file=sys.stderr)
            return 2

    print(f"[arb_trainer] Lane={lane}  Model={model_id}  Generation={generation}  Seed={seed}")
    print(f"[arb_trainer] Trainer root: {trainer_root}")
    print(f"[arb_trainer] Dataset CSV: {dataset_csv}")
    print(f"[arb_trainer] Artifact target: {artifact_path}")
    print("[arb_trainer] Starting OU parameter grid search backtest...")

    proc = run_arb_backtest(trainer_root, dataset_csv, artifact_path, seed)

    print(proc.stdout)
    if proc.stderr:
        print(f"[arb_trainer] STDERR:\n{proc.stderr[-3000:]}", file=sys.stderr)

    # --- Build result.json ---
    result: dict[str, Any] = {
        "trainer": "arb_trainer",
        "trainer_version": "arb-v6-ou-sniper-1.0.0",
        "completed_at_utc": utc_now_iso_z(),
        "model_id": model_id,
        "lane": lane,
        "generation": generation,
        "seed": seed,
        "exit_code": proc.returncode,
        "metrics": {
            "train_finished": proc.returncode == 0,
            "trainer_exit_code": proc.returncode,
            "dataset_csv": str(dataset_csv),
        },
        "risk_notes": [],
        "artifact_primary": None,
        "norm_artifact": None,
    }

    # Parse RESULT_ lines from stdout
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("RESULT_ARTIFACT="):
            result["artifact_primary"] = line.split("=", 1)[1]
        elif line.startswith("RESULT_ERROR="):
            result["risk_notes"].append(f"backtest_error: {line.split('=', 1)[1]}")

    # Extract metrics from stdout
    sharpe_match = re.search(r"Sharpe:\s*([\d.-]+)", proc.stdout)
    if sharpe_match:
        result["metrics"]["sharpe"] = float(sharpe_match.group(1))
    wr_match = re.search(r"Winrate:\s*([\d.]+)%", proc.stdout)
    if wr_match:
        result["metrics"]["winrate_pct"] = float(wr_match.group(1))
    pnl_match = re.search(r"Total PnL:\s*([\d.-]+)", proc.stdout)
    if pnl_match:
        result["metrics"]["total_pnl"] = float(pnl_match.group(1))
    dd_match = re.search(r"Max DD:\s*([\d.]+)%", proc.stdout)
    if dd_match:
        result["metrics"]["max_drawdown_pct"] = float(dd_match.group(1))
    pf_match = re.search(r"Profit Factor:\s*([\d.]+)", proc.stdout)
    if pf_match:
        result["metrics"]["profit_factor"] = float(pf_match.group(1))

    if proc.returncode != 0:
        result["risk_notes"].append(f"backtest exited with code {proc.returncode}")
        result["metrics"]["error_tail"] = proc.stderr[-2000:] if proc.stderr else ""
    else:
        if artifact_path.exists():
            result["artifact_primary"] = str(artifact_path)
            result["metrics"]["artifact_size_bytes"] = artifact_path.stat().st_size
            print(f"[arb_trainer] Artifact confirmed: {artifact_path}")

            # Try loading artifact to extract full metrics
            try:
                artifact_data = json.loads(artifact_path.read_text(encoding="utf-8"))
                opt_params = artifact_data.get("optimal_params", {})
                opt_metrics = artifact_data.get("metrics", {})
                result["metrics"]["optimal_params"] = opt_params
                result["metrics"]["backtest_metrics"] = opt_metrics
            except Exception:
                pass
        else:
            result["risk_notes"].append("Artifact not found after backtest")
            result["metrics"]["train_finished"] = False

    # Inject recipe provenance
    if recipe_id:
        result["recipe_id"] = recipe_id

    # Write result.json
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[arb_trainer] Result written: {result_path}")

    if proc.returncode != 0:
        print(f"[arb_trainer] FAILED exit={proc.returncode}", file=sys.stderr)
        return proc.returncode

    print("[arb_trainer] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
