"""Initial trainer for Online Adaptive MLP.

Trains a small 3-layer MLP (40→32→16→3) with LayerNorm + GELU on barrier-labeled
data, then exports weights as JSON for OnlineLearnerAdapter to load and continue
with streaming partial_fit updates.

Replaces the sklearn SGDClassifier initial training with a non-linear architecture
that captures feature interactions while remaining small enough (≈2,115 params)
for stable single-sample online learning.

Usage:
  python scripts/training/trainers/online_mlp_trainer.py \
    --data data/training/train.npz \
    --output data/models/online_mlp_v1.json \
    --epochs 50
"""

from __future__ import annotations

from core.training.utils import utc_now_iso as _utc_now_iso  # noqa: F401

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_data(data_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Load training data, return (X, y) with y in {0, 1, 2}.

    If pnl is present, derives 3-class labels from P&L:
      - P&L < -threshold → class 0 (short/loss)
      - |P&L| <= threshold → class 1 (neutral/timeout)
      - P&L > threshold → class 2 (long/win)
    Otherwise maps from y_raw.
    """
    ext = data_path.suffix.lower()
    if ext == ".npz":
        d = np.load(data_path)
        X = d["X"]
        if "pnl" in d:
            pnl = d["pnl"]
            threshold = float(np.std(pnl[pnl != 0]) / 3.0) if (pnl != 0).any() else 10.0
            y = np.full(len(pnl), 1, dtype=np.int64)  # default: neutral
            y[pnl > threshold] = 2  # long/win
            y[pnl < -threshold] = 0  # short/loss
            n_short = int((y == 0).sum())
            n_neutral = int((y == 1).sum())
            n_long = int((y == 2).sum())
            print(
                f"[online_mlp] 3-class from P&L (threshold={threshold:.2f}):"
                f" short={n_short}, neutral={n_neutral}, long={n_long}"
            )
        else:
            y_raw = d["y"]
            y = np.where(y_raw == -1, 0, np.where(y_raw == 1, 2, 1)).astype(np.int64)
    elif ext == ".parquet":
        import pandas as pd

        df = pd.read_parquet(data_path)
        feature_cols = [c for c in df.columns if c.startswith("f_")]
        if not feature_cols:
            feature_cols = [f"f_{i}" for i in range(40)]
        X = df[feature_cols].to_numpy(dtype=np.float64)
        if "pnl" in df.columns:
            pnl = df["pnl"].fillna(0.0).to_numpy(dtype=np.float64)
            threshold = float(np.std(pnl[pnl != 0]) / 3.0) if (pnl != 0).any() else 10.0
            y = np.full(len(pnl), 1, dtype=np.int64)
            y[pnl > threshold] = 2
            y[pnl < -threshold] = 0
        else:
            y_raw = df["label"].map({"win": 1, "loss": -1}).fillna(0).to_numpy(dtype=np.int32)
            y = np.where(y_raw == -1, 0, np.where(y_raw == 1, 2, 1)).astype(np.int64)
    else:
        raise ValueError(f"unsupported format: {ext}")

    return X, y


def train_mlp(
    X: np.ndarray,
    y: np.ndarray,
    n_features: int = 40,
    n_classes: int = 3,
    epochs: int = 50,
    lr: float = 0.001,
    batch_size: int = 64,
    val_split: float = 0.15,
    seed: int = 42,
) -> tuple[Any, dict[str, Any]]:
    """Train the online MLP with mini-batch SGD.

    Returns (mlp_model, metrics_dict).
    """
    import torch

    torch.manual_seed(seed)
    np.random.seed(seed)

    n = len(X)
    n_val = int(n * val_split)
    idx = np.random.permutation(n)
    X_train, y_train = X[idx[n_val:]], y[idx[n_val:]]
    X_val, y_val = X[idx[:n_val]], y[idx[:n_val]]

    from core.brains.online_mlp_model import _TorchOnlineMLP

    model: Any = _TorchOnlineMLP(n_features, n_classes)
    model.train()

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    t0 = time.perf_counter()
    best_val_acc = 0.0
    train_losses = []
    val_accs = []

    for _epoch in range(epochs):
        # Shuffle
        perm = np.random.permutation(len(X_train))
        X_shuf = X_train[perm]
        y_shuf = y_train[perm]

        epoch_losses = []
        for i in range(0, len(X_shuf), batch_size):
            Xb = torch.from_numpy(X_shuf[i : i + batch_size].astype(np.float32))
            yb = torch.from_numpy(y_shuf[i : i + batch_size])

            optimizer.zero_grad()
            logits = model(Xb)
            loss = torch.nn.functional.cross_entropy(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            epoch_losses.append(loss.item())

        scheduler.step()
        train_losses.append(float(np.mean(epoch_losses)))

        # Validation
        model.eval()
        with torch.no_grad():
            Xv = torch.from_numpy(X_val.astype(np.float32))
            yv = torch.from_numpy(y_val)
            val_logits = model(Xv)
            val_preds = val_logits.argmax(dim=1)
            val_acc = float((val_preds == yv).float().mean())
            val_accs.append(val_acc)
        model.train()

        if val_acc > best_val_acc:
            best_val_acc = val_acc

    elapsed = round(time.perf_counter() - t0, 3)

    # Final evaluation
    model.eval()
    with torch.no_grad():
        Xt = torch.from_numpy(X.astype(np.float32))
        yt = torch.from_numpy(y)
        train_preds = model(Xt).argmax(dim=1)
        train_acc = float((train_preds == yt).float().mean())

    # Convert to numpy OnlineMLP for saving
    from core.brains.online_mlp_model import OnlineMLP

    mlp = OnlineMLP(n_features=n_features, n_classes=n_classes, seed=seed)
    mlp._from_torch(model)

    metrics = {
        "train_accuracy": round(train_acc, 6),
        "best_val_accuracy": round(best_val_acc, 6),
        "final_train_loss": round(train_losses[-1], 6) if train_losses else None,
        "epochs": epochs,
        "train_time_seconds": elapsed,
        "batch_size": batch_size,
        "learning_rate": lr,
        "n_parameters": sum(p.numel() for p in model.parameters()),
    }

    return mlp, metrics


def save_mlp(mlp: Any, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mlp.save(str(output_path))
    return output_path


def save_result(
    metrics: dict,
    model_path: Path,
    result_path: Path,
    *,
    data_path: str = "",
    samples: int = 0,
    features: int = 0,
) -> Path:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "trainer": "online_mlp_trainer",
        "trainer_version": "online-mlp-1.0.0",
        "completed_at_utc": _utc_now_iso(),
        "exit_code": 0,
        "artifact_primary": str(model_path),
        "metrics": {"train_finished": True, **metrics},
        "data": {"source": data_path, "samples": samples, "features": features},
        "risk_notes": [],
    }
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="online_mlp_trainer", description="Train Online Adaptive MLP initial weights"
    )
    p.add_argument("--data", type=Path, required=True, help="Path to train.npz or train.parquet")
    p.add_argument("--output", type=Path, required=True, help="Path for MLP weights JSON")
    p.add_argument("--output-result", type=Path, default=None, help="Path for result.json")
    p.add_argument("--epochs", type=int, default=50, help="Training epochs (default: 50)")
    p.add_argument("--lr", type=float, default=0.001, help="Learning rate (default: 0.001)")
    p.add_argument("--batch-size", type=int, default=64, help="Batch size (default: 64)")
    p.add_argument("--seed", type=int, default=42)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.data.exists():
        print(f"[online_mlp_trainer] ERROR: data file not found: {args.data}", file=sys.stderr)
        return 2

    print(f"[online_mlp_trainer] Loading data from {args.data}...")
    X, y = load_data(args.data.resolve())
    print(f"[online_mlp_trainer] Loaded {len(X)} samples, {X.shape[1]} features")

    print(
        f"[online_mlp_trainer] Training MLP (epochs={args.epochs}, lr={args.lr}, batch={args.batch_size})..."
    )
    mlp, metrics = train_mlp(
        X,
        y,
        epochs=args.epochs,
        lr=args.lr,
        batch_size=args.batch_size,
        seed=args.seed,
    )

    model_path = save_mlp(mlp, args.output.resolve())
    print(f"[online_mlp_trainer] Model saved: {model_path}")
    print(f"[online_mlp_trainer] Metrics: {json.dumps(metrics, indent=2)}")

    result_path = (
        args.output_result.resolve()
        if args.output_result
        else model_path.with_suffix(".result.json")
    )
    save_result(
        metrics,
        model_path,
        result_path,
        data_path=str(args.data),
        samples=len(X),
        features=X.shape[1],
    )
    print(f"[online_mlp_trainer] Result written: {result_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
