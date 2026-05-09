"""In-repo Microstructure Transformer trainer — upgraded V4.3→V5.0 architecture.

Architecture (V5.0):
  Input(seq=32, 9-dim microstructure) → Linear(9→96) + PosEncoding(32×96)
  → RegimeContext: seq_stats(mean+std)→Linear(18→96)
  → TransformerEncoder(d_model=96, n_heads=4, num_layers=2, dropout=0.15)
  → GlobalMeanPool + RegimeContext → decoder: Linear(96→64)→GELU→Dropout→Linear(64→1)

Upgrades from V4.3:
  - d_model: 64→96 (50% more capacity for cross-timescale attention)
  - seq_len: 64→32 (faster warmup: 32min vs 64min at 60s tick interval)
  - RegimeContext: sequence-level stats provide volatility/trend awareness
  - AdamW + OneCycleLR + gradient clipping (modern training recipe)

Exports ONNX with single scalar output for TransformerBrainAdapter compatibility.

Usage:
  python scripts/training/trainers/transformer_trainer.py \
    --data D:/ai/Meta_ppo_v4.5/V4_Train_Tensors.pt \
    --output-model data/models/transformer_v5.onnx \
    --output-result data/models/transformer_v5_result.json \
    --epochs 150
"""

from __future__ import annotations

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

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Constants ──────────────────────────────────────────────────────────
SEQ_LEN = 32
NUM_FEATURES = 9
D_MODEL = 96
N_HEADS = 4
NUM_LAYERS = 2
DROPOUT = 0.15


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


class UpgradedQuantTransformer:
    """Container class — instantiate to get the nn.Module.

    Architecture:
      feature_embedding(9→128) + pos_encoding(1×32×128)
      → RegimeContext: concat(seq_mean, seq_std) → Linear(18→128)
      → TransformerEncoder(128, 4 heads, 3 layers, dropout=0.15)
      → GlobalMeanPool + RegimeContext → decoder(128→64→1)
    """

    def __new__(
        cls,
        num_features: int = NUM_FEATURES,
        d_model: int = D_MODEL,
        n_heads: int = N_HEADS,
        num_layers: int = NUM_LAYERS,
        dropout: float = DROPOUT,
        seq_len: int = SEQ_LEN,
    ):
        import torch
        import torch.nn as nn

        class _Model(nn.Module):
            def __init__(self):
                super().__init__()
                self.feature_embedding = nn.Linear(num_features, d_model)
                self.pos_encoder = nn.Parameter(torch.zeros(1, seq_len, d_model))

                # Regime context: sequence-level statistics → context vector
                self.regime_proj = nn.Sequential(
                    nn.Linear(num_features * 2, d_model),
                    nn.GELU(),
                    nn.Dropout(dropout * 0.5),
                    nn.Linear(d_model, d_model),
                )

                encoder_layer = nn.TransformerEncoderLayer(
                    d_model=d_model,
                    nhead=n_heads,
                    dropout=dropout,
                    batch_first=True,
                    dim_feedforward=d_model * 4,
                )
                self.transformer_encoder = nn.TransformerEncoder(
                    encoder_layer, num_layers=num_layers
                )

                self.decoder = nn.Sequential(
                    nn.Linear(d_model, 64),
                    nn.GELU(),
                    nn.Dropout(dropout),
                    nn.Linear(64, 1),
                )

                self._init_weights()

            def _init_weights(self):
                for m in self.modules():
                    if isinstance(m, nn.Linear):
                        # Use small init for output layers (fan_out <= 1 causes huge std)
                        if m.out_features <= 1:
                            nn.init.normal_(m.weight, mean=0.0, std=1e-3)
                        else:
                            nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                        if m.bias is not None:
                            nn.init.constant_(m.bias, 0)
                # Xavier init for position encoder
                nn.init.xavier_normal_(self.pos_encoder)

            def forward(self, x):
                # x: (batch, seq_len, num_features)
                # Regime context from sequence statistics
                seq_mean = x.mean(dim=1)  # (B, 9)
                seq_std = x.std(dim=1).clamp(min=1e-6)  # (B, 9)
                seq_stats = torch.cat([seq_mean, seq_std], dim=-1)  # (B, 18)
                regime_ctx = self.regime_proj(seq_stats)  # (B, 128)

                # Feature embedding + position encoding
                h = self.feature_embedding(x) + self.pos_encoder  # (B, 32, 128)

                # Transformer encoder
                h = self.transformer_encoder(h)  # (B, 32, 128)

                # Global mean pool + regime context
                h = h.mean(dim=1) + regime_ctx  # (B, 128)

                return self.decoder(h)  # (B, 1)

        return _Model()


# ═══════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════


def load_pt_data(
    data_path: Path,
    seq_len: int = SEQ_LEN,
    max_samples: int = 0,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """Load V4_Train_Tensors.pt and slice sequences to seq_len.

    Takes the *last* seq_len bars of each 64-bar sequence (most recent =
    most predictive).  Uses sequential sampling (first max_samples in
    original order) to preserve temporal structure — random sampling
    across time periods destroys learnable patterns.
    """
    import torch

    d = torch.load(str(data_path), map_location="cpu", weights_only=True)
    X_full = d["X"].numpy() if torch.is_tensor(d["X"]) else d["X"]
    Y_full = d["Y"].numpy().ravel() if torch.is_tensor(d["Y"]) else d["Y"].ravel()

    full_seq_len = X_full.shape[1]
    if full_seq_len < seq_len:
        raise ValueError(f"Data seq_len={full_seq_len} < required seq_len={seq_len}")

    # Use last seq_len bars (most recent, most predictive)
    X_sliced = X_full[:, -seq_len:, :].astype(np.float64)
    Y_sliced = Y_full.astype(np.float64)

    # Sequential sampling: take first max_samples in original temporal order
    if max_samples > 0 and len(X_sliced) > max_samples:
        X_sliced = X_sliced[:max_samples]
        Y_sliced = Y_sliced[:max_samples]

    n_total = len(X_sliced)
    n_pos = int(Y_sliced.sum())
    print(f"[transformer] Loaded {len(Y_full)} samples from {data_path.name}")
    print(
        f"[transformer] Sliced seq {full_seq_len}→{seq_len}"
        f", using first {n_total} samples (sequential)"
    )
    print(f"[transformer] Label balance: {n_pos}/{n_total} pos" f" ({100 * n_pos / n_total:.1f}%)")
    return X_sliced, Y_sliced


# ═══════════════════════════════════════════════════════════════════════
# Training
# ═══════════════════════════════════════════════════════════════════════


def compute_r2(y_true: torch.Tensor, y_pred: torch.Tensor) -> float:
    ss_res = float(((y_true - y_pred) ** 2).sum())
    ss_tot = float(((y_true - y_true.mean()) ** 2).sum())
    return float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0


def train_transformer(
    X: np.ndarray,
    y: np.ndarray,
    epochs: int = 150,
    lr: float = 1e-3,
    batch_size: int = 256,
    val_split: float = 0.15,
    dropout: float = DROPOUT,
    weight_decay: float = 1e-4,
    seed: int = 42,
    seq_len: int = SEQ_LEN,
    d_model: int = D_MODEL,
    n_heads: int = N_HEADS,
    num_layers: int = NUM_LAYERS,
) -> tuple[Any, dict[str, Any]]:
    import torch

    torch.manual_seed(seed)
    np.random.seed(seed)

    n = len(X)
    n_val = int(n * val_split)
    idx = np.random.permutation(n)
    X_train = X[idx[n_val:]]
    y_train = y[idx[n_val:]]
    X_val = X[idx[:n_val]]
    y_val = y[idx[:n_val]]

    print(f"[transformer] Train: {len(X_train)}, Val: {len(X_val)}")

    model = UpgradedQuantTransformer(
        num_features=NUM_FEATURES,
        d_model=d_model,
        n_heads=n_heads,
        num_layers=num_layers,
        dropout=dropout,
        seq_len=seq_len,
    )
    model.train()

    # Count pos/neg for pos_weight in BCE
    n_pos = int(y_train.sum())
    n_neg = len(y_train) - n_pos
    pos_weight = torch.tensor([n_neg / max(n_pos, 1)], dtype=torch.float32)
    criterion = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)

    # Pre-convert to tensors for efficiency
    X_train_t = torch.from_numpy(X_train.astype(np.float32))
    y_train_t = torch.from_numpy(y_train.astype(np.float32))
    X_val_t = torch.from_numpy(X_val.astype(np.float32))
    y_val_t = torch.from_numpy(y_val.astype(np.float32))

    t0 = time.perf_counter()
    best_val_loss = float("inf")
    best_state = None
    patience_counter = 0
    patience = 25

    for epoch in range(epochs):
        # Shuffle indices
        perm = torch.randperm(len(X_train_t))
        X_shuf = X_train_t[perm]
        y_shuf = y_train_t[perm]

        epoch_loss = 0.0
        n_batches = 0
        for i in range(0, len(X_shuf), batch_size):
            Xb = X_shuf[i : i + batch_size]
            yb = y_shuf[i : i + batch_size]

            optimizer.zero_grad()
            logits = model(Xb).squeeze(-1)
            loss = criterion(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()

            epoch_loss += loss.item()
            n_batches += 1

        # Validation
        model.eval()
        with torch.no_grad():
            val_logits = model(X_val_t).squeeze(-1)
            val_loss = float(criterion(val_logits, y_val_t))

            # Binary accuracy at threshold 0
            val_preds = (torch.sigmoid(val_logits) > 0.5).float()
            val_acc = float((val_preds == y_val_t).float().mean())

            # R² against binary labels
            val_r2 = compute_r2(y_val_t, torch.sigmoid(val_logits))
        model.train()

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            patience_counter = 0
        else:
            patience_counter += 1

        if patience_counter >= patience:
            print(
                f"[transformer] Early stopping at epoch {epoch+1}"
                f" (best val_loss={best_val_loss:.4f},"
                f" val_acc={val_acc:.4f}, val_r2={val_r2:.4f})"
            )
            break

        if (epoch + 1) % 20 == 0:
            print(
                f"[transformer] Epoch {epoch+1}/{epochs}:"
                f" loss={epoch_loss / n_batches:.4f},"
                f" val_loss={val_loss:.4f},"
                f" val_acc={val_acc:.4f},"
                f" val_r2={val_r2:.4f}"
            )

    if best_state is not None:
        model.load_state_dict(best_state)

    elapsed = round(time.perf_counter() - t0, 3)

    # Final metrics (use pre-converted tensors)
    model.eval()
    with torch.no_grad():
        train_logits = model(X_train_t).squeeze(-1)
        train_loss = float(criterion(train_logits, y_train_t))
        train_preds = (torch.sigmoid(train_logits) > 0.5).float()
        train_acc = float((train_preds == y_train_t).float().mean())
        train_r2 = compute_r2(y_train_t, torch.sigmoid(train_logits))

        val_logits = model(X_val_t).squeeze(-1)
        val_preds = (torch.sigmoid(val_logits) > 0.5).float()
        val_acc = float((val_preds == y_val_t).float().mean())
        val_r2 = compute_r2(y_val_t, torch.sigmoid(val_logits))

        # Directional signal check: what fraction produce non-neutral signals
        # (using adapter threshold of ±0.1 after tanh on raw logit)
        raw_scores = val_logits.numpy()
        neutral_mask = (raw_scores > -0.1) & (raw_scores < 0.1)
        signal_rate = float(1.0 - neutral_mask.mean())
        long_rate = float((raw_scores > 0.1).mean())
        short_rate = float((raw_scores < -0.1).mean())

    n_params = sum(p.numel() for p in model.parameters())

    metrics: dict[str, Any] = {
        "epochs_completed": epoch + 1,
        "train_time_seconds": elapsed,
        "learning_rate": lr,
        "batch_size": batch_size,
        "dropout": dropout,
        "weight_decay": weight_decay,
        "n_parameters": n_params,
        "architecture": "UpgradedQuantTransformer_V5",
        "d_model": d_model,
        "n_heads": n_heads,
        "num_layers": num_layers,
        "seq_len": seq_len,
        "num_features": NUM_FEATURES,
        "activation": "GELU",
        "pos_weight": round(float(pos_weight[0]), 2),
        "train_loss": round(train_loss, 6),
        "train_accuracy": round(train_acc, 6),
        "train_r2": round(train_r2, 6),
        "best_val_loss": round(best_val_loss, 6),
        "val_accuracy": round(val_acc, 6),
        "val_r2": round(val_r2, 6),
        "signal_rate": round(signal_rate, 6),
        "long_rate": round(long_rate, 6),
        "short_rate": round(short_rate, 6),
    }

    return model, metrics


# ═══════════════════════════════════════════════════════════════════════
# ONNX Export
# ═══════════════════════════════════════════════════════════════════════


def export_onnx(
    model: Any,
    output_path: Path,
    seq_len: int = SEQ_LEN,
    num_features: int = NUM_FEATURES,
) -> Path:
    import torch

    output_path.parent.mkdir(parents=True, exist_ok=True)
    model.eval()
    dummy = torch.randn(1, seq_len, num_features)

    torch.onnx.export(
        model,
        dummy,
        str(output_path),
        export_params=True,
        do_constant_folding=True,
        input_names=["input"],
        output_names=["score"],
        opset_version=14,
        dynamic_axes={
            "input": {0: "batch"},
            "score": {0: "batch"},
        },
    )
    return output_path


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
) -> Path:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "trainer": "transformer_trainer",
        "trainer_version": "transformer-v5.0.0",
        "completed_at_utc": _utc_now_iso(),
        "exit_code": 0,
        "artifact_primary": str(model_path),
        "metrics": {"train_finished": True, **metrics},
        "data": {
            "source": data_path,
            "samples": samples,
            "features": NUM_FEATURES,
            "seq_len": SEQ_LEN,
        },
        "risk_notes": [],
    }
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result_path


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="transformer_trainer",
        description="Train upgraded QuantTransformer V5 and export ONNX",
    )
    p.add_argument(
        "--data",
        type=Path,
        required=True,
        help="Path to V4_Train_Tensors.pt (64-seq microstructure data)",
    )
    p.add_argument(
        "--output-model",
        type=Path,
        required=True,
        help="Path for ONNX artifact",
    )
    p.add_argument(
        "--output-result",
        type=Path,
        default=None,
        help="Path for result.json",
    )
    p.add_argument(
        "--epochs",
        type=int,
        default=80,
        help="Training epochs (default: 80)",
    )
    p.add_argument(
        "--lr",
        type=float,
        default=1e-3,
        help="Learning rate (default: 1e-3)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=128,
        help="Batch size (default: 128)",
    )
    p.add_argument(
        "--max-samples",
        type=int,
        default=10000,
        help="Max training samples (default: 10000, 0=all, sequential from start)",
    )
    p.add_argument(
        "--d-model",
        type=int,
        default=D_MODEL,
        help=f"Transformer d_model (default: {D_MODEL})",
    )
    p.add_argument(
        "--num-layers",
        type=int,
        default=NUM_LAYERS,
        help=f"Transformer encoder layers (default: {NUM_LAYERS})",
    )
    p.add_argument(
        "--dropout",
        type=float,
        default=DROPOUT,
        help=f"Dropout rate (default: {DROPOUT})",
    )
    p.add_argument(
        "--weight-decay",
        type=float,
        default=1e-4,
        help="Weight decay (default: 1e-4)",
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--recipe",
        type=Path,
        default=None,
        help="Training Recipe JSON",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    data_path = args.data.resolve()
    if not data_path.exists():
        print(
            f"[transformer] ERROR: data file not found: {data_path}",
            file=sys.stderr,
        )
        return 2

    recipe_id: str | None = None
    epochs = args.epochs
    lr = args.lr
    batch_size = args.batch_size
    dropout = args.dropout
    weight_decay = args.weight_decay
    max_samples = args.max_samples
    d_model = args.d_model
    num_layers = args.num_layers

    if args.recipe:
        from core.contracts.training.training_recipe import TrainingRecipe

        recipe_obj = TrainingRecipe.from_file(args.recipe)
        recipe_id = recipe_obj.recipe_id
        if args.epochs == 80:
            epochs = recipe_obj.training.epochs
        if args.lr == 1e-3:
            lr = recipe_obj.training.learning_rate
        if args.batch_size == 128:
            batch_size = recipe_obj.training.batch_size
        print(f"[transformer] Recipe: {recipe_id}")

    # Load
    print(f"[transformer] Loading {data_path}...")
    X, y = load_pt_data(data_path, seq_len=SEQ_LEN, max_samples=max_samples, seed=args.seed)

    # Train
    print(
        f"[transformer] Training UpgradedQuantTransformer V5"
        f" (d_model={d_model}, heads={N_HEADS}, layers={num_layers},"
        f" seq_len={SEQ_LEN}, epochs={epochs}, lr={lr}, batch={batch_size})..."
    )
    model, metrics = train_transformer(
        X,
        y,
        epochs=epochs,
        lr=lr,
        batch_size=batch_size,
        dropout=dropout,
        weight_decay=weight_decay,
        seed=args.seed,
        d_model=d_model,
        num_layers=num_layers,
    )

    # Export ONNX
    model_path = export_onnx(model, args.output_model.resolve())
    print(f"[transformer] ONNX exported: {model_path}" f" ({model_path.stat().st_size} bytes)")

    # Save result
    result_path = (
        args.output_result.resolve()
        if args.output_result
        else model_path.with_suffix(".result.json")
    )
    save_result(
        metrics,
        model_path,
        result_path,
        data_path=str(data_path),
        samples=len(X),
    )

    if recipe_id:
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        result_data["recipe_id"] = recipe_id
        result_path.write_text(json.dumps(result_data, indent=2, ensure_ascii=False))

    # Summary
    print(f"[transformer] Result: {result_path}")
    print(
        f"[transformer] val_acc={metrics.get('val_accuracy', 'N/A'):.4f}"
        if isinstance(metrics.get("val_accuracy"), float)
        else f"[transformer] val_acc={metrics.get('val_accuracy', 'N/A')}"
    )
    print(
        f"[transformer] val_r2={metrics.get('val_r2', 'N/A'):.4f}"
        if isinstance(metrics.get("val_r2"), float)
        else f"[transformer] val_r2={metrics.get('val_r2', 'N/A')}"
    )
    print(
        f"[transformer] signal_rate={metrics.get('signal_rate', 'N/A'):.4f}"
        if isinstance(metrics.get("signal_rate"), float)
        else f"[transformer] signal_rate={metrics.get('signal_rate', 'N/A')}"
    )
    print(f"[transformer] n_params={metrics.get('n_parameters', 'N/A')}")
    print("[transformer] SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
