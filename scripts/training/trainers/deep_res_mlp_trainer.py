"""In-repo DeepResMLP trainer — residual MLP with LayerNorm/GELU for V9 institutional features.

Architecture:
  Input(40) → Linear(128) → LayerNorm → GELU
  → ResBlock(128→64→128) × 2
  → MultiHead: Linear(3) direction, Linear(1) risk, Linear(1) vol

Upgrades the legacy 3-layer MLP (128→64→32, no normalization, ReLU) to a
modern residual architecture with:
  - LayerNorm for training stability and consistent inference
  - GELU activation (smooth gradients vs ReLU's zero-gradient region)
  - Residual connections to allow gradient flow through deeper layers
  - Multi-head outputs (direction + risk + vol) for V9OnnxBrainAdapter compatibility

Exports ONNX with 3 outputs for direct drop-in replacement of the current V9 ONNX model.

Usage:
  python scripts/training/trainers/deep_res_mlp_trainer.py \
    --data data/training/train.npz \
    --output-model data/models/deep_res_mlp_v1.onnx \
    --output-result data/models/deep_res_mlp_result.json \
    --epochs 200
"""

from __future__ import annotations

import argparse
import json
import sys

# Ensure UTF-8 stdout on Windows (torch.onnx prints emoji)
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
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
# PyTorch Model
# ═══════════════════════════════════════════════════════════════════════


class ResBlock:
    """Residual block: Linear(128→64) → LayerNorm → GELU → Linear(64→128) → + input.

    Defined as a plain class with explicit parameters (like _TorchOnlineMLP)
    so weights can be inspected without a torch import at rest.
    """

    def __new__(cls, in_dim: int = 128, bottleneck_dim: int = 64):
        import torch.nn as nn

        class _Block(nn.Module):
            def __init__(self):
                super().__init__()
                self.fc1 = nn.Linear(in_dim, bottleneck_dim)
                self.ln1 = nn.LayerNorm(bottleneck_dim)
                self.fc2 = nn.Linear(bottleneck_dim, in_dim)
                self.ln2 = nn.LayerNorm(in_dim)

            def forward(self, x):
                residual = x
                out = self.ln1(nn.functional.gelu(self.fc1(x)))
                out = self.ln2(self.fc2(out))
                return nn.functional.gelu(out + residual)

        return _Block()


class DeepResMLP:
    """Container for DeepResMLP weights and training logic.

    Supports two modes:
      - classification (default): head_direction(128→3) outputs 3-class logits
      - regression: head_regression(128→1) outputs scalar P&L prediction
    """

    def __new__(cls, n_features: int = 40, regression: bool = False):
        import torch.nn as nn

        class _Model(nn.Module):
            def __init__(self):
                super().__init__()
                self.input_proj = nn.Linear(n_features, 128)
                self.input_ln = nn.LayerNorm(128)

                self.res_block_1: nn.Module = ResBlock(128, 64)
                self.res_block_2: nn.Module = ResBlock(128, 64)

                if regression:
                    self.head_regression = nn.Linear(128, 1)  # scalar P&L
                else:
                    self.head_direction = nn.Linear(128, 3)  # 3-class logits
                self.head_risk = nn.Linear(128, 1)  # scalar risk
                self.head_vol = nn.Linear(128, 1)  # scalar volatility

                self._regression = regression
                self._init_weights()

            def _init_weights(self):
                for m in self.modules():
                    if isinstance(m, nn.Linear):
                        nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                        if m.bias is not None:
                            nn.init.constant_(m.bias, 0)

            def forward(self, x):
                h = nn.functional.gelu(self.input_ln(self.input_proj(x)))
                h = self.res_block_1(h)
                h = self.res_block_2(h)
                if self._regression:
                    return self.head_regression(h), self.head_risk(h), self.head_vol(h)
                return self.head_direction(h), self.head_risk(h), self.head_vol(h)

        return _Model()


# ═══════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════


def load_data(
    data_path: Path, *, regression: bool = False
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Load training data, return (X, y, feature_names).

    When regression=True, y is the P&L regression target (y_reg or pnl).
    """
    ext = data_path.suffix.lower()
    if ext == ".npz":
        d = np.load(data_path)
        X = d["X"]
        if regression:
            y_reg = d.get("y_reg")
            if y_reg is not None:
                y = y_reg.astype(np.float64)
            elif "pnl" in d:
                y = d["pnl"].astype(np.float64)
            else:
                y = d["y"].astype(np.float64)
        else:
            y = d["y"]
        feat_raw = d.get("feature_names")
        if feat_raw is None:
            feature_names = [f"f_{i}" for i in range(X.shape[1])]
        elif isinstance(feat_raw, np.ndarray):
            feature_names = feat_raw.tolist()
        else:
            feature_names = list(feat_raw)
    elif ext == ".parquet":
        import pandas as pd

        df = pd.read_parquet(data_path)
        feature_cols = [c for c in df.columns if c.startswith("f_")]
        if not feature_cols:
            feature_cols = [f"f_{i}" for i in range(40)]
        X = df[feature_cols].to_numpy(dtype=np.float64)
        if regression:
            y = (
                df["pnl"].fillna(0.0).to_numpy(dtype=np.float64)
                if "pnl" in df.columns
                else df["label"].map({"win": 1}).fillna(0).to_numpy(dtype=np.float64)
            )
        else:
            y = df["label"].map({"win": 1}).fillna(0).to_numpy(dtype=np.int32)
        feature_names = feature_cols
    else:
        raise ValueError(f"unsupported format: {ext}")

    return X, y, feature_names


# ═══════════════════════════════════════════════════════════════════════
# Training
# ═══════════════════════════════════════════════════════════════════════


def train_deep_res_mlp(
    X: np.ndarray,
    y: np.ndarray,
    epochs: int = 200,
    lr: float = 3e-4,
    batch_size: int = 128,
    val_split: float = 0.15,
    dropout: float = 0.2,
    weight_decay: float = 1e-4,
    seed: int = 42,
    regression: bool = False,
    class_weights: list[float] | None = None,
) -> tuple[Any, dict[str, Any]]:
    import torch

    torch.manual_seed(seed)
    np.random.seed(seed)

    n = len(X)
    n_val = int(n * val_split)
    idx = np.random.permutation(n)
    X_train, y_train = X[idx[n_val:]], y[idx[n_val:]]
    X_val, y_val = X[idx[:n_val]], y[idx[:n_val]]

    model: torch.nn.Module = DeepResMLP(regression=regression)
    model.train()

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer,
        max_lr=lr,
        epochs=epochs,
        steps_per_epoch=max(1, len(X_train) // batch_size),
        pct_start=0.1,
    )

    t0 = time.perf_counter()
    best_val_metric = float("-inf") if regression else 0.0
    best_state = None
    patience_counter = 0
    patience = 30

    for epoch in range(epochs):
        perm = np.random.permutation(len(X_train))
        X_shuf = X_train[perm]
        y_shuf = y_train[perm]

        epoch_loss = 0.0
        n_batches = 0
        for i in range(0, len(X_shuf), batch_size):
            Xb = torch.from_numpy(X_shuf[i : i + batch_size].astype(np.float32))
            yb = torch.from_numpy(y_shuf[i : i + batch_size].astype(np.float32))

            optimizer.zero_grad()
            primary, risk, vol = model(Xb)

            if regression:
                loss_main = torch.nn.functional.mse_loss(primary.squeeze(-1), yb)
                with torch.no_grad():
                    pred_err = (primary.squeeze(-1) - yb).abs()
                    err_norm = pred_err / (pred_err.std() + 1e-8)
                loss_risk = torch.nn.functional.mse_loss(risk.squeeze(-1), err_norm)
                loss_vol = torch.nn.functional.mse_loss(vol.squeeze(-1), err_norm * 0.8 + 0.2)
            else:
                yb_cls = yb.long()
                if class_weights is not None:
                    cls_weight = torch.tensor(class_weights, dtype=torch.float32)
                    loss_main = torch.nn.functional.cross_entropy(
                        primary, yb_cls, weight=cls_weight
                    )
                else:
                    loss_main = torch.nn.functional.cross_entropy(primary, yb_cls)
                with torch.no_grad():
                    preds = primary.argmax(dim=1)
                    wrong_mask = (preds != yb_cls).float()
                loss_risk = torch.nn.functional.mse_loss(risk.squeeze(-1), wrong_mask)
                loss_vol = torch.nn.functional.mse_loss(vol.squeeze(-1), wrong_mask * 0.8 + 0.2)

            loss = loss_main + 0.1 * loss_risk + 0.05 * loss_vol
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            scheduler.step()

            epoch_loss += loss.item()
            n_batches += 1

        # Validation
        model.eval()
        with torch.no_grad():
            Xv = torch.from_numpy(X_val.astype(np.float32))
            if regression:
                yv = torch.from_numpy(y_val.astype(np.float32))
                val_pred, _, _ = model(Xv)
                val_pred = val_pred.squeeze(-1)
                ss_res = float(((yv - val_pred) ** 2).sum())
                ss_tot = float(((yv - yv.mean()) ** 2).sum())
                val_r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
                val_metric = val_r2
            else:
                yv = torch.from_numpy(y_val.astype(np.int64))
                val_logits, _, _ = model(Xv)
                val_preds = val_logits.argmax(dim=1)
                val_metric = float((val_preds == yv).float().mean())
        model.train()

        if val_metric > best_val_metric:
            best_val_metric = val_metric
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            patience_counter = 0
        else:
            patience_counter += 1

        if patience_counter >= patience:
            label = "val_r2" if regression else "val_acc"
            print(
                f"[deep_res_mlp] Early stopping at epoch {epoch+1} (best {label}={best_val_metric:.4f})"
            )
            break

        if (epoch + 1) % 20 == 0:
            label = "val_r2" if regression else "val_acc"
            print(
                f"[deep_res_mlp] Epoch {epoch+1}/{epochs}: loss={epoch_loss/n_batches:.4f}, {label}={val_metric:.4f}"
            )

    if best_state is not None:
        model.load_state_dict(best_state)

    elapsed = round(time.perf_counter() - t0, 3)

    # Final metrics
    model.eval()
    with torch.no_grad():
        Xt = torch.from_numpy(X.astype(np.float32))
        if regression:
            yt = torch.from_numpy(y.astype(np.float32))
            train_pred, _, _ = model(Xt)
            train_pred = train_pred.squeeze(-1)
            ss_res = float(((yt - train_pred) ** 2).sum())
            ss_tot = float(((yt - yt.mean()) ** 2).sum())
            train_r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
            train_rmse = float(torch.sqrt(torch.nn.functional.mse_loss(train_pred, yt)))
        else:
            yt = torch.from_numpy(y.astype(np.int64))
            train_logits, _, _ = model(Xt)
            train_preds = train_logits.argmax(dim=1)
            train_acc = float((train_preds == yt).float().mean())

    n_params = sum(p.numel() for p in model.parameters())

    metrics: dict[str, Any] = {
        "epochs_completed": epoch + 1,
        "train_time_seconds": elapsed,
        "learning_rate": lr,
        "batch_size": batch_size,
        "dropout": dropout,
        "weight_decay": weight_decay,
        "n_parameters": n_params,
        "architecture": "DeepResMLP_v1",
        "hidden_dims": [128, 64, 128],
        "activation": "GELU",
        "normalization": "LayerNorm",
    }

    if regression:
        metrics["train_r2"] = round(train_r2, 6)
        metrics["train_rmse"] = round(train_rmse, 6)
        metrics["best_val_r2"] = round(best_val_metric, 6)
    else:
        metrics["train_accuracy"] = round(train_acc, 6)
        metrics["best_val_accuracy"] = round(best_val_metric, 6)

    return model, metrics


# ═══════════════════════════════════════════════════════════════════════
# ONNX Export
# ═══════════════════════════════════════════════════════════════════════


def export_onnx(
    model: Any, output_path: Path, n_features: int = 40, *, regression: bool = False
) -> Path:
    import torch

    output_path.parent.mkdir(parents=True, exist_ok=True)
    model.eval()
    dummy = torch.randn(1, n_features)

    if regression:
        output_names = ["regression", "risk", "vol"]
    else:
        output_names = ["direction", "risk", "vol"]

    torch.onnx.export(
        model,
        dummy,
        str(output_path),
        export_params=True,
        do_constant_folding=True,
        input_names=["input"],
        output_names=output_names,
        opset_version=14,
    )
    return output_path


def compute_scaler_params(X: np.ndarray) -> dict[str, Any]:
    """Compute StandardScaler mean/std for normalization artifact."""
    mean = X.mean(axis=0).tolist()
    std = X.std(axis=0).tolist()
    std = [max(s, 1e-8) for s in std]  # avoid div by zero
    return {"mean": mean, "std": std, "n_features": len(mean)}


# ═══════════════════════════════════════════════════════════════════════
# Result saving
# ═══════════════════════════════════════════════════════════════════════


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
        "trainer": "deep_res_mlp_trainer",
        "trainer_version": "deep-res-mlp-1.0.0",
        "completed_at_utc": _utc_now_iso(),
        "exit_code": 0,
        "artifact_primary": str(model_path),
        "metrics": {"train_finished": True, **metrics},
        "data": {"source": data_path, "samples": samples, "features": features},
        "risk_notes": [],
    }
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result_path


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deep_res_mlp_trainer", description="Train DeepResMLP and export to ONNX"
    )
    p.add_argument("--data", type=Path, required=True, help="Path to train.npz or train.parquet")
    p.add_argument("--val-data", type=Path, default=None, help="Optional validation file")
    p.add_argument("--output-model", type=Path, required=True, help="Path for ONNX artifact")
    p.add_argument("--output-result", type=Path, default=None, help="Path for result.json")
    p.add_argument("--output-scaler", type=Path, default=None, help="Path for scaler JSON")
    p.add_argument("--epochs", type=int, default=200, help="Training epochs (default: 200)")
    p.add_argument("--lr", type=float, default=3e-4, help="Learning rate (default: 3e-4)")
    p.add_argument("--batch-size", type=int, default=128, help="Batch size (default: 128)")
    p.add_argument("--dropout", type=float, default=0.2, help="Dropout rate (default: 0.2)")
    p.add_argument("--weight-decay", type=float, default=1e-4, help="Weight decay (default: 1e-4)")
    p.add_argument(
        "--class-weights",
        type=str,
        default=None,
        help="Comma-separated class weights for CrossEntropyLoss (e.g. '1.0,6.58,0.0')",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--mode",
        choices=["cls", "reg"],
        default="cls",
        help="Training mode: cls (3-class) or reg (P&L regression)",
    )
    p.add_argument("--recipe", type=Path, default=None, help="Training Recipe JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.data.exists():
        print(f"[deep_res_mlp] ERROR: data file not found: {args.data}", file=sys.stderr)
        return 2

    regression = args.mode == "reg"
    recipe_id: str | None = None
    epochs = args.epochs
    lr = args.lr
    batch_size = args.batch_size
    dropout = args.dropout
    weight_decay = args.weight_decay

    if args.recipe:
        from core.contracts.training.training_recipe import TrainingRecipe

        recipe_obj = TrainingRecipe.from_file(args.recipe)
        recipe_id = recipe_obj.recipe_id
        if args.epochs == 200:
            epochs = recipe_obj.training.epochs
        if args.lr == 3e-4:
            lr = recipe_obj.training.learning_rate
        if args.batch_size == 128:
            batch_size = recipe_obj.training.batch_size
        print(f"[deep_res_mlp] Recipe: {recipe_id}")

    print(
        f"[deep_res_mlp] Loading data from {args.data} (mode={'reg' if regression else 'cls'})..."
    )
    X, y, feature_names = load_data(args.data.resolve(), regression=regression)
    print(f"[deep_res_mlp] Loaded {len(X)} samples, {X.shape[1]} features")

    print(
        f"[deep_res_mlp] Training DeepResMLP (epochs={epochs}, lr={lr}, batch={batch_size}, dropout={dropout}, mode={'reg' if regression else 'cls'})..."
    )
    class_weights = None
    if args.class_weights:
        class_weights = [float(w) for w in args.class_weights.split(",")]
        print(f"[deep_res_mlp] Class weights: {class_weights}")

    model, metrics = train_deep_res_mlp(
        X,
        y,
        epochs=epochs,
        lr=lr,
        batch_size=batch_size,
        dropout=dropout,
        weight_decay=weight_decay,
        seed=args.seed,
        regression=regression,
        class_weights=class_weights,
    )

    model_path = export_onnx(model, args.output_model.resolve(), regression=regression)
    print(f"[deep_res_mlp] ONNX exported: {model_path} ({model_path.stat().st_size} bytes)")

    # Scaler params
    if args.output_scaler:
        scaler = compute_scaler_params(X)
        scaler_path = args.output_scaler.resolve()
        scaler_path.parent.mkdir(parents=True, exist_ok=True)
        scaler_path.write_text(json.dumps(scaler, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[deep_res_mlp] Scaler saved: {scaler_path}")

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

    if recipe_id:
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        result_data["recipe_id"] = recipe_id
        result_path.write_text(json.dumps(result_data, indent=2, ensure_ascii=False))

    print(f"[deep_res_mlp] Result written: {result_path}")
    print(f"[deep_res_mlp] Metrics: {json.dumps(metrics, indent=2)}")
    print("[deep_res_mlp] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
