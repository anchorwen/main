"""Train Stage 2 MLP binary classifier on PiT meta-features.

Uses OnlineMLP's native architecture (Input→32→LayerNorm→GELU→16→LayerNorm
→GELU→n_classes→softmax) with v2-proven hyperparameters.

Usage:
    python scripts/training/train_stage2_mlp_pit.py \
        --data data/training/meta_features_pit_v3.npz \
        --output data/models/institutional/meta_stage2_mlp_pit_v3.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def _compute_sharpe(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """Compute Sharpe ratio from directional bets scaled by confidence."""
    direction = 2 * y_true - 1  # 0→-1, 1→+1
    confidence = 2 * np.abs(y_prob - 0.5)
    returns = direction * confidence
    if returns.std() < 1e-10:
        return 0.0
    return float(np.sqrt(252 * 24) * returns.mean() / returns.std())


def _compute_win_rate(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    """Directional win rate."""
    pred_dir = (y_prob >= 0.5).astype(int)
    correct = (pred_dir == y_true).sum()
    return float(correct / len(y_true))


def train_mlp_stage2(
    data_path: str | Path,
    output_path: str | Path,
    *,
    n_epochs: int = 200,
    learning_rate: float = 0.001,
    batch_size: int = 64,
    early_stopping_rounds: int = 20,
    validation_split: float = 0.15,
    seed: int = 42,
) -> None:
    """Train a Stage 2 MLP binary classifier on PiT meta-features.

    Uses OnlineMLP's native architecture (32→16→n_classes) with the
    _to_torch / _from_torch bridge for PyTorch training and numpy inference.
    """
    import torch
    import torch.nn as nn

    data_path = Path(data_path)
    output_path = Path(output_path)

    if not data_path.exists():
        print(f"[mlp_train] ERROR: Data not found: {data_path}")
        sys.exit(1)

    # ── Load data ──
    print(f"[mlp_train] Loading: {data_path}")
    raw = np.load(data_path, allow_pickle=True)
    X = np.asarray(raw["X"], dtype=np.float64)
    y = np.asarray(raw["y"], dtype=np.int32).ravel()
    feature_names = list(raw.get("feature_names", [f"f_{i}" for i in range(X.shape[1])]))

    print(f"[mlp_train] X: {X.shape}, y: {y.shape}")
    print(f"[mlp_train] Label distribution: {np.sum(y == 1)} TP, {np.sum(y == 0)} non-TP")

    # ── Chronological split ──
    n_val = int(len(X) * validation_split)
    if n_val < 100:
        print(f"[mlp_train] ERROR: Validation set too small ({n_val})")
        sys.exit(1)

    X_train, X_val = X[:-n_val], X[-n_val:]
    y_train, y_val = y[:-n_val], y[-n_val:]
    print(f"[mlp_train] Train: {len(X_train)}, Val: {len(X_val)}")

    n_features = X_train.shape[1]

    # ── Create OnlineMLP → convert to torch ──
    from core.brains.online_mlp_model import OnlineMLP

    online_mlp = OnlineMLP(n_features=n_features, n_classes=2, seed=seed)
    torch_model = online_mlp._to_torch()

    # ── Optimizer ──
    optimizer = torch.optim.Adam(
        torch_model.parameters(),
        lr=learning_rate,
        weight_decay=0.0001,
    )
    criterion = nn.CrossEntropyLoss()

    X_train_t = torch.from_numpy(X_train.astype(np.float32))
    y_train_t = torch.from_numpy(y_train.astype(np.int64))
    X_val_t = torch.from_numpy(X_val.astype(np.float32))
    y_val_t = torch.from_numpy(y_val.astype(np.int64))

    # ── Training loop ──
    best_val_loss = float("inf")
    patience_counter = 0

    param_count = sum(p.numel() for p in torch_model.parameters())
    print(f"[mlp_train] Training {n_features}→32→16→2, {param_count} params")

    for epoch in range(n_epochs):
        torch_model.train()
        perm = torch.randperm(len(X_train_t))
        total_loss = 0.0
        n_batches = 0

        for start in range(0, len(X_train_t), batch_size):
            idx = perm[start : start + batch_size]
            xb, yb = X_train_t[idx], y_train_t[idx]

            optimizer.zero_grad()
            logits = torch_model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            optimizer.step()

            total_loss += float(loss.item())
            n_batches += 1

        train_loss = total_loss / max(n_batches, 1)

        # Validation
        torch_model.eval()
        with torch.no_grad():
            val_logits = torch_model(X_val_t)
            val_loss = float(criterion(val_logits, y_val_t).item())
            val_probs_t = torch.softmax(val_logits, dim=1)[:, 1]
            val_probs = val_probs_t.cpu().numpy()

        val_win_rate = _compute_win_rate(y_val, val_probs)

        if val_loss < best_val_loss - 1e-6:
            best_val_loss = val_loss
            patience_counter = 0
            # Sync best weights back to OnlineMLP
            online_mlp._from_torch(torch_model)
        else:
            patience_counter += 1

        if epoch % 20 == 0 or epoch == n_epochs - 1 or patience_counter >= early_stopping_rounds:
            print(
                f"[mlp_train]   Epoch {epoch:3d}/{n_epochs}: "
                f"train_loss={train_loss:.4f}, val_loss={val_loss:.4f}, "
                f"val_win_rate={val_win_rate:.3f}"
            )

        if patience_counter >= early_stopping_rounds:
            print(f"[mlp_train] Early stopping at epoch {epoch}")
            break

    # ── Evaluate final model (best weights already in online_mlp) ──
    val_probs = np.zeros(len(X_val), dtype=np.float64)
    for i in range(len(X_val)):
        raw = online_mlp.forward_numpy(X_val[i : i + 1].astype(np.float32))
        if raw.ndim == 1:
            val_probs[i] = float(raw[1]) if len(raw) >= 2 else float(raw[0])
        else:
            val_probs[i] = float(raw[0, 1]) if raw.shape[1] >= 2 else float(raw[0, 0])

    val_sharpe = _compute_sharpe(y_val, val_probs)
    val_win_rate = _compute_win_rate(y_val, val_probs)
    print(f"[mlp_train] Val Sharpe (12-bar): {val_sharpe:.4f}")
    print(f"[mlp_train] Val Win Rate: {val_win_rate:.4f}")

    raw_mean = float(np.mean(val_probs))
    raw_std = float(np.std(val_probs))
    print(
        f"[mlp_train] Val probs: mean={raw_mean:.4f}, std={raw_std:.4f}, "
        f"range=[{float(np.min(val_probs)):.4f}, {float(np.max(val_probs)):.4f}]"
    )

    # ── Save ──
    output_path.parent.mkdir(parents=True, exist_ok=True)
    online_mlp.save(str(output_path))

    meta = {
        "n_features": n_features,
        "n_classes": 2,
        "feature_names": feature_names,
        "val_sharpe_12bar": val_sharpe,
        "val_win_rate": val_win_rate,
        "training_data": str(data_path),
        "n_train": len(X_train),
        "n_val": len(X_val),
        "architecture": "OnlineMLP(32→16→2)",
    }
    meta_path = str(output_path).rsplit(".", 1)[0] + ".meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"[mlp_train] Saved model to: {output_path}")
    print(f"[mlp_train] Saved metadata to: {meta_path}")
    print("[mlp_train] Done.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Train Stage 2 MLP on PiT meta-features")
    ap.add_argument("--data", required=True, help="Path to PiT meta-features NPZ")
    ap.add_argument("--output", required=True, help="Output model path (.json)")
    ap.add_argument("--n-epochs", type=int, default=200)
    ap.add_argument("--lr", type=float, default=0.001, dest="learning_rate")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--early-stopping", type=int, default=20, dest="early_stopping_rounds")
    ap.add_argument("--validation-split", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    train_mlp_stage2(
        data_path=args.data,
        output_path=args.output,
        n_epochs=args.n_epochs,
        learning_rate=args.learning_rate,
        batch_size=args.batch_size,
        early_stopping_rounds=args.early_stopping_rounds,
        validation_split=args.validation_split,
        seed=args.seed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
