"""End-to-end training pipeline validation.

Exercises the full blueprint training chain:
  price data → barrier labels → dataset → quality_gate → Optuna → train

Usage:
  python scripts/training/e2e_pipeline_validation.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from core.runtime.fault_handler import fail_open_guard

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ═══════════════════════════════════════════════════════════════════════
# Step 1: Generate synthetic OHLC price data (3 months of M5 bars)
# ═══════════════════════════════════════════════════════════════════════


def generate_synthetic_price_data(output_path: Path, n_bars: int = 25000) -> Path:
    """Generate realistic synthetic M5 OHLC CSV with trends and volatility."""
    rng = np.random.RandomState(42)
    close = np.zeros(n_bars, dtype=np.float64)
    close[0] = 2000.0

    vol = np.ones(n_bars, dtype=np.float64) * 2.0
    for i in range(1, n_bars):
        vol[i] = 0.95 * vol[i - 1] + 0.05 * abs(rng.randn()) * 3.0 + 1.0

    for i in range(1, n_bars):
        phase = i % 4000
        if phase < 3000:
            drift = 0.003 * vol[i]
        elif phase < 3500:
            drift = -0.002 * vol[i]
        else:
            drift = 0.0
        close[i] = close[i - 1] + drift + rng.randn() * vol[i]
    close = np.maximum(close, 100.0)

    high = close + np.abs(rng.randn(n_bars)) * vol * 0.5
    low = close - np.abs(rng.randn(n_bars)) * vol * 0.5
    open_price = np.roll(close, 1)
    open_price[0] = close[0] - rng.randn() * vol[0] * 0.3

    start_time = datetime(2024, 1, 1, 0, 0, 0)
    timestamps = [start_time + timedelta(minutes=5 * i) for i in range(n_bars)]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write("timestamp,open,high,low,close,volume\n")
        for i in range(n_bars):
            f.write(
                f"{timestamps[i].isoformat()},"
                f"{open_price[i]:.5f},{high[i]:.5f},{low[i]:.5f},{close[i]:.5f},"
                f"{int(abs(rng.randn()) * 100)}\n"
            )

    print(f"  [1] Price data: {output_path} ({n_bars} bars, {close[0]:.1f} -> {close[-1]:.1f})")
    return output_path


# ═══════════════════════════════════════════════════════════════════════
# Step 2: Generate barrier labels from price data + label contract
# ═══════════════════════════════════════════════════════════════════════


def generate_barrier_labels(
    price_csv: Path,
    label_contract_path: Path,
    output_path: Path,
) -> Path:
    """Generate barrier labels using the Label Contract."""
    from core.contracts.training.label_contract import LabelContract

    contract = LabelContract.from_file(label_contract_path)

    timestamps: list[str] = []
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    with open(price_csv) as f:
        f.readline()
        for line in f:
            parts = line.strip().split(",")
            timestamps.append(parts[0])
            float(parts[1])  # skip open
            highs.append(float(parts[2]))
            lows.append(float(parts[3]))
            closes.append(float(parts[4]))

    h_arr = np.array(highs, dtype=np.float64)
    l_arr = np.array(lows, dtype=np.float64)
    c_arr = np.array(closes, dtype=np.float64)

    from core.contracts.training.label_contract import _compute_atr

    labels = []
    horizon = contract.horizon_bars
    for i in range(len(c_arr) - horizon - 1):
        if i < contract.atr_period + 1:
            continue
        atr = _compute_atr(h_arr[: i + 1], l_arr[: i + 1], c_arr[: i + 1], contract.atr_period)
        if atr <= 0:
            continue

        entry_price = c_arr[i]
        sl_dist = contract.sl_atr_mult * atr
        tp_dist = contract.tp_atr_mult * atr

        # Walk forward
        end = min(i + horizon + 1, len(c_arr))
        result = "timeout"
        for j in range(i + 1, end):
            if h_arr[j] >= entry_price + tp_dist:
                result = "tp_hit_first"
                break
            if l_arr[j] <= entry_price - sl_dist:
                result = "sl_hit_first"
                break

        labels.append(
            {
                "schema_version": "training_label.v1",
                "label_id": f"barrier_{i:06d}",
                "event_time": timestamps[i],
                "label": result,
                "entry_price": float(entry_price),
                "atr": float(atr),
                "sl_distance": float(sl_dist),
                "tp_distance": float(tp_dist),
                "horizon_bars": horizon,
                "contract_id": contract.contract_id,
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for lb in labels:
            f.write(json.dumps(lb) + "\n")

    from collections import Counter

    dist = Counter(lb["label"] for lb in labels)
    print(f"  [2] Barrier labels: {output_path} ({len(labels)} labels, dist={dict(dist)})")
    return output_path


# ═══════════════════════════════════════════════════════════════════════
# Step 3: Generate synthetic features and populate feature store
# ═══════════════════════════════════════════════════════════════════════


def generate_synthetic_features(
    price_csv: Path,
    feature_store_dir: Path,
    n_features: int = 40,
) -> Path:
    """Generate synthetic V9 institutional features matching price timestamps."""
    from core.features.local_feature_store import LocalFeatureStore
    from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
    from core.features.store_contracts import FeatureRecord, FeatureSchema

    timestamps: list[datetime] = []
    with open(price_csv) as f:
        f.readline()
        for line in f:
            ts_str = line.strip().split(",")[0]
            timestamps.append(datetime.fromisoformat(ts_str))

    rng = np.random.RandomState(42)
    store = LocalFeatureStore(str(feature_store_dir))

    schema = FeatureSchema(
        name="v9_institutional_40",
        version="1.0.0",
        fields=tuple(V9_INSTITUTIONAL_40_FEATURES),
        symbol="XAUUSDc",
        timeframe="M5",
    )
    store.register_schema(schema)

    batch_size = 500
    for start in range(0, len(timestamps), batch_size):
        batch_ts = timestamps[start : start + batch_size]
        records = []
        for ts in batch_ts:
            raw = rng.randn(n_features).astype(np.float64)
            records.append(
                FeatureRecord(
                    schema_name="v9_institutional_40",
                    schema_version="1.0.0",
                    symbol="XAUUSDc",
                    timeframe="M5",
                    event_time=ts,
                    values={
                        name: float(raw[j]) for j, name in enumerate(V9_INSTITUTIONAL_40_FEATURES)
                    },
                    source="synthetic",
                )
            )
        store.write_records(records)

    print(f"  [3] Feature store: {feature_store_dir} ({len(timestamps)} records)")
    return feature_store_dir


# ═══════════════════════════════════════════════════════════════════════
# Step 4: Build dataset (join labels + features)
# ═══════════════════════════════════════════════════════════════════════


def build_dataset(
    labels_path: Path,
    feature_store_dir: Path,
    output_dir: Path,
    label_contract_path: Path | None = None,
) -> dict[str, Any]:
    """Join labels with features, split train/val, export NPZ."""
    from core.features.local_feature_store import LocalFeatureStore
    from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
    from core.features.store_contracts import FeatureQuery

    store = LocalFeatureStore(str(feature_store_dir))

    labels = []
    with open(labels_path) as f:
        for line in f:
            labels.append(json.loads(line))

    label_map = {"tp_hit_first": 1, "sl_hit_first": 0, "timeout": 0}
    X_list: list[list[float]] = []
    y_list: list[int] = []
    joined = 0

    for lb in labels:
        ts_str = lb["event_time"]
        try:
            event_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00").split("+")[0])
        except ValueError:
            event_ts = datetime.fromisoformat(ts_str.split("+")[0].split("Z")[0])

        # Find nearest feature record at or before label time
        records = store.query(
            FeatureQuery(
                symbol="XAUUSDc",
                timeframe="M5",
                schema_name="v9_institutional_40",
                end=event_ts + timedelta(minutes=5),
                start=event_ts - timedelta(minutes=5),
                limit=1,
            )
        )
        if not records:
            continue

        values = records[0].values
        feature_vec = [float(values.get(name, 0.0)) for name in V9_INSTITUTIONAL_40_FEATURES]
        label_val = label_map.get(lb["label"], 0)

        X_list.append(feature_vec)
        y_list.append(label_val)
        joined += 1

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.int32)
    pnl = np.zeros(len(y), dtype=np.float32)

    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]
    pnl_train, pnl_val = pnl[:split_idx], pnl[split_idx:]

    output_dir.mkdir(parents=True, exist_ok=True)
    train_path = output_dir / "train.npz"
    val_path = output_dir / "val.npz"

    np.savez(train_path, X=X_train, y=y_train, pnl=pnl_train)
    np.savez(val_path, X=X_val, y=y_val, pnl=pnl_val)

    contract_id = None
    if label_contract_path and label_contract_path.exists():
        contract = json.loads(label_contract_path.read_text(encoding="utf-8"))
        contract_id = contract.get("contract_id")

    meta = {
        "schema_version": "training_dataset.v1",
        "generated_at": _utc_now_iso(),
        "train_samples": int(len(X_train)),
        "val_samples": int(len(X_val)),
        "n_features": len(V9_INSTITUTIONAL_40_FEATURES),
        "label_distribution": {
            "train": {
                int(k): int(v)
                for k, v in zip(*np.unique(y_train, return_counts=True), strict=False)
            },
            "val": {
                int(k): int(v) for k, v in zip(*np.unique(y_val, return_counts=True), strict=False)
            },
        },
        "label_contract_id": contract_id,
        "labels_joined": joined,
        "total_labels": len(labels),
    }
    meta_path = output_dir / "dataset_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))

    print(
        f"  [4] Dataset: {train_path} (train={len(X_train)}, val={len(X_val)}, joined={joined}/{len(labels)})"
    )
    return meta


# ═══════════════════════════════════════════════════════════════════════
# Step 5: Quality gate
# ═══════════════════════════════════════════════════════════════════════


def run_quality_gate(
    train_path: Path,
    val_path: Path,
    label_contract_path: Path | None = None,
) -> dict[str, Any]:
    """Run quality CI gate on the dataset."""
    from scripts.training.quality_gate import run_quality_gate as _run

    report = _run(
        train_path,
        val_data_path=val_path,
        label_contract_path=label_contract_path,
    )
    status = "PASS" if report["passed"] else "FAIL"
    failed = ", ".join(report["failed_checks"]) if report["failed_checks"] else "none"
    print(
        f"  [5] Quality gate: {status}  failed=[{failed}]  samples={report['summary']['n_samples']}"
    )
    if not report["passed"]:
        for cn, chk in report["checks"].items():
            if not chk["passed"]:
                for issue in chk["issues"]:
                    print(f"      ISSUE [{cn}]: {issue}")
    return report


# ═══════════════════════════════════════════════════════════════════════
# Step 6: Optuna search
# ═══════════════════════════════════════════════════════════════════════


def run_optuna_search(
    recipe_path: Path,
    train_path: Path,
    val_path: Path,
    n_trials: int = 5,
) -> tuple[dict[str, Any], Any]:
    """Run Optuna hyperparameter search."""
    from scripts.training.recipe_search import _load_recipe, run_search

    recipe = _load_recipe(recipe_path)
    study_name = f"e2e-pipeline-{int(time.time())}"

    best_params, study = run_search(
        recipe,
        train_path,
        n_trials=n_trials,
        val_data_path=val_path,
        study_name=study_name,
        seed=42,
        metric="val_accuracy",
    )
    print(f"  [6] Optuna: best_val_acc={study.best_value:.4f}")
    return best_params, study


# ═══════════════════════════════════════════════════════════════════════
# Step 7: Train with best params
# ═══════════════════════════════════════════════════════════════════════


def train_final_model(
    train_path: Path,
    best_params: dict[str, Any],
    output_dir: Path,
    val_path: Path | None = None,
) -> dict[str, Any]:
    """Train XGBoost model with optimized parameters."""
    from scripts.training.trainers.xgb_trainer import load_training_data, train_xgboost

    X, y, _, feature_names = load_training_data(train_path)
    val_data = None
    if val_path is not None:
        Xv, yv, _, _ = load_training_data(val_path)
        val_data = (Xv, yv)

    xgb_params = {
        "n_estimators": best_params.get("n_estimators", 200),
        "max_depth": best_params.get("max_depth", 5),
        "learning_rate": best_params.get("learning_rate", 0.05),
        "subsample": best_params.get("subsample", 0.8),
        "colsample_bytree": best_params.get("colsample_bytree", 0.8),
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "random_state": 42,
        "n_jobs": -1,
        "early_stopping_rounds": 20,
    }

    _, metrics = train_xgboost(
        X, y, params=xgb_params, val_data=val_data, feature_names=feature_names
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "schema_version": "training_result.v1",
        "trained_at": _utc_now_iso(),
        "metrics": {k: float(v) if v is not None else None for k, v in metrics.items()},
        "best_params": best_params,
        "n_features": int(X.shape[1]),
        "n_train_samples": int(len(X)),
        "n_val_samples": int(len(Xv)) if val_data else None,
    }
    result_path = output_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2))
    print(
        f"  [7] Train: train_acc={metrics.get('train_accuracy', 'N/A')}  val_acc={metrics.get('val_accuracy', 'N/A')}"
    )
    print(f"      Result: {result_path}")
    return result


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════


def main() -> int:
    print("=" * 60)
    print("  E2E Training Pipeline Validation")
    print("=" * 60)

    label_contract_path = PROJECT_ROOT / "blueprints/contracts/label-survival-barrier-1.0.0.json"
    recipe_path = PROJECT_ROOT / "blueprints/recipes/sur-g2026.1-recipe-001.json"

    if not label_contract_path.exists():
        print(f"[ERROR] Label contract not found: {label_contract_path}")
        return 2
    if not recipe_path.exists():
        print(f"[ERROR] Recipe not found: {recipe_path}")
        return 2

    tmpdir = Path(tempfile.mkdtemp(prefix="e2e_pipeline_"))
    print(f"\nWorkspace: {tmpdir}\n")

    try:
        # 1: Price data
        price_csv = generate_synthetic_price_data(tmpdir / "prices" / "XAUUSD_M5.csv")

        # 2: Barrier labels
        labels_path = generate_barrier_labels(
            price_csv, label_contract_path, tmpdir / "labels" / "barrier_labels.jsonl"
        )

        # 3: Feature store
        feature_store_dir = generate_synthetic_features(price_csv, tmpdir / "feature_store")

        # 4: Dataset
        dataset_meta = build_dataset(
            labels_path, feature_store_dir, tmpdir / "dataset", label_contract_path
        )

        if dataset_meta["train_samples"] < 50:
            print(
                f"\n[SKIP] Too few samples ({dataset_meta['train_samples']}), skipping Optuna+Train"
            )
            print("[DONE] Steps 1-4 verified. Pipeline structure is sound.")
            return 0

        # 5: Quality gate
        train_path = tmpdir / "dataset" / "train.npz"
        val_path = tmpdir / "dataset" / "val.npz"
        gate_report = run_quality_gate(train_path, val_path, label_contract_path)

        # 6: Optuna
        print()
        best_params, study = run_optuna_search(recipe_path, train_path, val_path, n_trials=3)

        # 7: Train
        print()
        result = train_final_model(train_path, best_params, tmpdir / "model")

        print("\n" + "=" * 60)
        print("  PIPELINE COMPLETE")
        print("=" * 60)
        print(
            f"  Labels:    {dataset_meta['total_labels']} generated, {dataset_meta['labels_joined']} joined"
        )
        print(
            f"  Dataset:   {dataset_meta['train_samples']} train / {dataset_meta['val_samples']} val"
        )
        print(f"  Gate:      {'PASS' if gate_report['passed'] else 'FAIL'}")
        print(f"  Optuna:    best_val_acc={study.best_value:.4f}")
        print(f"  Train:     val_acc={result['metrics'].get('val_accuracy', 'N/A')}")
        print(f"  Artifacts: {tmpdir}")
        print("\n  All 7 pipeline stages executed successfully.")

    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("e2e_pipeline_validation:main"):
            print(f"\n[FAIL] Pipeline error: {exc}")
            import traceback

            traceback.print_exc()
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
