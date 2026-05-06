"""First real training: CSV OHLC → barrier labels → feature dataset → train → ONNX.

Uses the Recipe + Label Contract infrastructure to run a complete, reproducible
training run from exported MT5 CSV data.

Usage:
  python scripts/training/train_from_csv.py                          # default recipe
  python scripts/training/train_from_csv.py --csv data/raw/xauusd_m5_1y.csv
  python scripts/training/train_from_csv.py --recipe configs/training/recipes/sur-g2026.1-recipe.json
  python scripts/training/train_from_csv.py --epochs 100 --dry-run   # validate only
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
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_ohlc_csv(
    csv_path: Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load OHLC CSV into numpy arrays. Handles 5-col (no volume) and 7-col formats."""
    # Detect column count from header
    with open(csv_path) as fh:
        header = fh.readline().strip()
    cols = header.split(",")
    has_volume = len(cols) >= 7

    if has_volume:
        dtype = [
            ("time", "U30"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("tick_volume", "f8"),
            ("spread", "f8"),
        ]
    else:
        dtype = [("time", "U30"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")]

    data = np.genfromtxt(csv_path, delimiter=",", skip_header=1, dtype=dtype)
    volumes = data["tick_volume"].copy() if has_volume else np.zeros(len(data), dtype=np.float64)
    return (
        data["open"].copy(),
        data["high"].copy(),
        data["low"].copy(),
        data["close"].copy(),
        volumes,
    )


def compute_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Compute ATR(period) for the full series."""
    n = len(close)
    atr = np.zeros(n, dtype=np.float64)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]

    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))

    # Wilder smoothing
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def _resample_ohlc(
    o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray, factor: int
) -> tuple[np.ndarray, ...]:
    """Resample M5 OHLCV to a higher timeframe by aggregating `factor` bars."""
    n = len(o)
    new_n = n // factor
    if new_n == 0:
        return np.array([]), np.array([]), np.array([]), np.array([]), np.array([])
    trim = new_n * factor
    r_o = o[:trim].reshape(-1, factor)
    r_h = h[:trim].reshape(-1, factor)
    r_l = l[:trim].reshape(-1, factor)
    r_c = c[:trim].reshape(-1, factor)
    r_v = v[:trim].reshape(-1, factor)
    return (
        r_o[:, 0].copy(),  # open of first bar in window
        r_h.max(axis=1),  # highest high
        r_l.min(axis=1),  # lowest low
        r_c[:, -1].copy(),  # close of last bar in window
        r_v.sum(axis=1),  # total tick volume
    )


def _feature_series_for_tf(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    v: np.ndarray,
    min_lookback: int = 40,
) -> dict[str, np.ndarray]:
    """Compute all 10 V9 institutional features as full-length series for one timeframe.

    Features: Ret_1, Body_Ratio, ATR_14, RSI_14, MACD, Vol_ZScore,
              Macro1_Corr, Macro_Gold_Silver_Spread, OU_Theta, Hurst
    """
    n = len(c)
    if n < min_lookback:
        empty = np.zeros(n, dtype=np.float32)
        return {
            k: empty
            for k in [
                "Ret_1",
                "Body_Ratio",
                "ATR_14",
                "RSI_14",
                "MACD",
                "Vol_ZScore",
                "Macro1_Corr",
                "Macro_Gold_Silver_Spread",
                "OU_Theta",
                "Hurst",
            ]
        }

    eps = 1e-8

    # ── Ret_1 ──
    ret_1 = np.zeros(n, dtype=np.float32)
    ret_1[1:] = (c[1:] - c[:-1]) / (c[:-1] + eps) * 100.0

    # ── Body_Ratio ──
    body_ratio = np.zeros(n, dtype=np.float32)
    denom = h - l
    valid = denom > eps
    body_ratio[valid] = np.clip((c[valid] - o[valid]) / denom[valid], -1.0, 1.0)

    # ── ATR_14 (Wilder) ──
    atr_14 = np.zeros(n, dtype=np.float32)
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr_14[13] = np.mean(tr[:14])
    for i in range(14, n):
        atr_14[i] = (atr_14[i - 1] * 13 + tr[i]) / 14.0

    # ── RSI_14 ──
    rsi_14 = np.full(n, 50.0, dtype=np.float32)
    delta = np.diff(c, prepend=c[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.zeros(n, dtype=np.float32)
    avg_loss = np.zeros(n, dtype=np.float32)
    avg_gain[14] = float(np.mean(gain[1:15]))
    avg_loss[14] = float(np.mean(loss[1:15]))
    for i in range(15, n):
        avg_gain[i] = (avg_gain[i - 1] * 13.0 + gain[i]) / 14.0
        avg_loss[i] = (avg_loss[i - 1] * 13.0 + loss[i]) / 14.0
        rs = avg_gain[i] / max(avg_loss[i], eps)
        rsi_14[i] = 100.0 - 100.0 / (1.0 + rs)

    # ── MACD (EMA12 - EMA26) ──
    macd = np.zeros(n, dtype=np.float32)
    if n >= 12:
        ema12 = np.zeros(n, dtype=np.float32)
        ema26 = np.zeros(n, dtype=np.float32)
        ema12[11] = float(np.mean(c[:12]))
        a12 = 2.0 / 13.0
        a26 = 2.0 / 27.0
        for i in range(12, n):
            ema12[i] = a12 * c[i] + (1.0 - a12) * ema12[i - 1]
        if n >= 26:
            ema26[25] = float(np.mean(c[:26]))
            for i in range(26, n):
                ema26[i] = a26 * c[i] + (1.0 - a26) * ema26[i - 1]
        macd = (ema12 - ema26).astype(np.float32)

    # ── Vol_ZScore (20-bar on tick_volume) ──
    vol_zscore = np.zeros(n, dtype=np.float32)
    for i in range(20, n):
        win = v[i - 19 : i + 1]
        ws = np.std(win)
        if ws > eps:
            vol_zscore[i] = (v[i] - np.mean(win)) / ws

    # ── Macro1_Corr (20-bar lag-1 return autocorrelation) ──
    macro1 = np.zeros(n, dtype=np.float32)
    for i in range(22, n):
        win_r = ret_1[i - 20 : i + 1]
        if len(win_r) >= 5:
            corr = np.corrcoef(win_r[:-1], win_r[1:])[0, 1]
            macro1[i] = corr if not np.isnan(corr) else 0.0

    # ── Macro_Gold_Silver_Spread (20-bar price z-score) ──
    macro_gs = np.zeros(n, dtype=np.float32)
    for i in range(20, n):
        win = c[i - 19 : i + 1]
        ws = np.std(win)
        if ws > eps:
            macro_gs[i] = (c[i] - np.mean(win)) / ws

    # ── OU_Theta (20-bar OLS theta) ──
    ou_theta = np.zeros(n, dtype=np.float32)
    for i in range(21, n):
        win = c[i - 20 : i + 1]
        y = win[1:]
        x = win[:-1]
        xm, ym = np.mean(x), np.mean(y)
        num = np.sum((x - xm) * (y - ym))
        den = np.sum((x - xm) ** 2)
        if den > eps:
            beta = np.clip(num / den, 1e-8, 0.99999999)
            ou_theta[i] = -np.log(beta)

    # ── Hurst (20-bar R/S) ──
    hurst = np.full(n, 0.5, dtype=np.float32)
    for i in range(20, n):
        win = c[i - 19 : i + 1]
        dev = win - np.mean(win)
        cum = np.cumsum(dev)
        r = np.max(cum) - np.min(cum)
        s = np.std(win)
        if s > eps:
            hurst[i] = np.log(r / s) / np.log(20)

    return {
        "Ret_1": ret_1,
        "Body_Ratio": body_ratio,
        "ATR_14": atr_14,
        "RSI_14": rsi_14,
        "MACD": macd,
        "Vol_ZScore": vol_zscore,
        "Macro1_Corr": macro1,
        "Macro_Gold_Silver_Spread": macro_gs,
        "OU_Theta": ou_theta,
        "Hurst": hurst,
    }


def build_barrier_labels_from_csv(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    atr: np.ndarray,
    contract: Any,
    entry_stride: int = 5,
) -> list[dict[str, Any]]:
    """Generate barrier labels by simulating entries at regular intervals.

    At each entry bar, uses the LabelContract to determine if price hits
    SL or TP first within horizon_bars. Both long and short directions tested.
    """
    from core.contracts.training.label_contract import BarrierResult

    horizon = contract.horizon_bars
    labels: list[dict[str, Any]] = []

    for i in range(14, len(closes) - horizon - 1, entry_stride):
        if atr[i] < 0.01:
            continue

        for side in ("long", "short"):
            result: BarrierResult = contract.build_barrier_labels(
                highs,
                lows,
                closes,
                entry_idx=i,
                side=side,
            )
            labels.append(
                {
                    "bar_index": i,
                    "side": side,
                    "entry_price": result.entry_price,
                    "atr_at_entry": result.atr_at_entry,
                    "sl_price": result.sl_price,
                    "tp_price": result.tp_price,
                    "label": result.label,
                    "hit_bar": result.hit_bar_index,
                    "hit_price": result.hit_price,
                    "horizon_bars": result.horizon_bars,
                }
            )

    return labels


# V9 Institutional 40 feature names in ONNX input order.
V9_FEATURE_NAMES = [
    # M5
    "M5_Ret_1",
    "M5_Body_Ratio",
    "M5_ATR_14",
    "M5_RSI_14",
    "M5_MACD",
    "M5_Vol_ZScore",
    "M5_Macro1_Corr",
    "M5_Macro_Gold_Silver_Spread",
    # M15
    "M15_Ret_1",
    "M15_Body_Ratio",
    "M15_ATR_14",
    "M15_RSI_14",
    "M15_MACD",
    "M15_Vol_ZScore",
    "M15_Macro1_Corr",
    "M15_Macro_Gold_Silver_Spread",
    # M30
    "M30_Ret_1",
    "M30_Body_Ratio",
    "M30_ATR_14",
    "M30_RSI_14",
    "M30_MACD",
    "M30_Vol_ZScore",
    "M30_Macro1_Corr",
    "M30_Macro_Gold_Silver_Spread",
    # H1
    "H1_Ret_1",
    "H1_Body_Ratio",
    "H1_ATR_14",
    "H1_RSI_14",
    "H1_MACD",
    "H1_Vol_ZScore",
    "H1_Macro1_Corr",
    "H1_Macro_Gold_Silver_Spread",
    # Cross-timeframe
    "M5_OU_Theta",
    "M15_OU_Theta",
    "M30_OU_Theta",
    "H1_OU_Theta",
    "M5_Hurst",
    "M15_Hurst",
    "M30_Hurst",
    "H1_Hurst",
]


def build_features_from_csv(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    labels: list[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    """Build 40-dim V9 Institutional feature matrix X and label vector y.

    Computes 10 features per timeframe (M5/M15/M30/H1) via resampling,
    then assembles 40-dim vectors at each labeled M5 entry bar.
    """
    n_m5 = len(closes)
    tf_factors = {"M5": 1, "M15": 3, "M30": 6, "H1": 12}
    base_features = (
        "Ret_1",
        "Body_Ratio",
        "ATR_14",
        "RSI_14",
        "MACD",
        "Vol_ZScore",
        "Macro1_Corr",
        "Macro_Gold_Silver_Spread",
        "OU_Theta",
        "Hurst",
    )

    # ── Compute features for each timeframe ──
    tf_series: dict[str, dict[str, np.ndarray]] = {}
    for tf_name, factor in tf_factors.items():
        if factor == 1:
            o, hi, lo, cl, vo = opens, highs, lows, closes, volumes
        else:
            o, hi, lo, cl, vo = _resample_ohlc(opens, highs, lows, closes, volumes, factor)
        tf_series[tf_name] = _feature_series_for_tf(o, hi, lo, cl, vo)
        print(
            f"  {tf_name}: {len(cl) if factor == 1 else len(o)} bars, "
            f"{len(tf_series[tf_name])} features computed"
        )

    # ── Map M5 label indices to multi-timeframe bar indices ──
    label_idx_map = {lbl["bar_index"]: lbl for lbl in labels}
    indices = sorted(label_idx_map.keys())

    # Filter out entries too close to the edges
    valid_indices = []
    for m5_idx in indices:
        if m5_idx < 40:
            continue
        m15_idx = m5_idx // 3
        m30_idx = m5_idx // 6
        h1_idx = m5_idx // 12
        if m15_idx < 40 or m30_idx < 40 or h1_idx < 20:
            continue
        if (
            m5_idx >= n_m5
            or m15_idx >= len(tf_series["M15"]["Ret_1"])
            or m30_idx >= len(tf_series["M30"]["Ret_1"])
            or h1_idx >= len(tf_series["H1"]["Ret_1"])
        ):
            continue
        valid_indices.append(m5_idx)

    tf_index = {
        "M5": lambda i: i,
        "M15": lambda i: i // 3,
        "M30": lambda i: i // 6,
        "H1": lambda i: i // 12,
    }

    # ── Assemble X, y ──
    n_feat = len(V9_FEATURE_NAMES)
    X = np.zeros((len(valid_indices), n_feat), dtype=np.float32)
    y = np.zeros(len(valid_indices), dtype=np.int32)

    for j, m5_idx in enumerate(valid_indices):
        feat_vec = []
        for tf_name, feat_list in [
            ("M5", base_features),
            ("M15", base_features),
            ("M30", base_features),
            ("H1", base_features),
        ]:
            map_idx = tf_index[tf_name](m5_idx)
            series = tf_series[tf_name]
            for fn in feat_list:
                val = series[fn][map_idx]
                feat_vec.append(float(np.nan_to_num(val, nan=0.0, posinf=0.0, neginf=0.0)))

        X[j] = np.array(feat_vec, dtype=np.float32)

        lbl = label_idx_map[m5_idx]["label"]
        if lbl == "tp_hit_first":
            y[j] = 1
        elif lbl == "sl_hit_first":
            y[j] = -1
        else:
            y[j] = 0

    return X, y


def train_mlp(X_train, y_train, X_val, y_val, recipe, seed=42):
    """Train a simple MLP on the feature dataset. Returns model and metrics."""
    import torch
    import torch.nn as nn
    import torch.optim as optim

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    input_dim = X_train.shape[1]
    hidden = recipe["training"].get("hidden_dims", [128, 64, 32])
    num_classes = 3  # -1, 0, 1 → indices 0, 1, 2

    # Map labels from {-1, 0, 1} to {0, 1, 2}
    y_train_idx = y_train + 1
    y_val_idx = y_val + 1

    class MLP(nn.Module):
        def __init__(self):
            super().__init__()
            layers = []
            prev = input_dim
            for h in hidden:
                layers.extend([nn.Linear(prev, h), nn.ReLU(), nn.Dropout(0.3)])
                prev = h
            layers.append(nn.Linear(prev, num_classes))
            self.net = nn.Sequential(*layers)

        def forward(self, x):
            return self.net(x)

    torch.manual_seed(seed)
    model = MLP().to(device)
    optimizer = optim.Adam(model.parameters(), lr=recipe["training"].get("learning_rate", 0.001))
    criterion = nn.CrossEntropyLoss()

    X_tr = torch.tensor(X_train, dtype=torch.float32).to(device)
    y_tr = torch.tensor(y_train_idx, dtype=torch.long).to(device)
    X_va = torch.tensor(X_val, dtype=torch.float32).to(device)
    y_va = torch.tensor(y_val_idx, dtype=torch.long).to(device)

    epochs = recipe["training"].get("epochs", 200)
    batch_size = recipe["training"].get("batch_size", 256)
    best_val_acc = 0.0
    best_state = None

    for epoch in range(epochs):
        model.train()
        perm = torch.randperm(len(X_tr))
        for b in range(0, len(X_tr), batch_size):
            idx = perm[b : b + batch_size]
            optimizer.zero_grad()
            loss = criterion(model(X_tr[idx]), y_tr[idx])
            loss.backward()
            optimizer.step()

        model.eval()
        with torch.no_grad():
            val_logits = model(X_va)
            val_loss = criterion(val_logits, y_va).item()
            val_acc = (val_logits.argmax(1) == y_va).float().mean().item()

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 50 == 0:
            print(f"    Epoch {epoch + 1}/{epochs}: val_loss={val_loss:.4f} val_acc={val_acc:.4f}")

    model.load_state_dict(best_state)
    return model, {"best_val_acc": round(best_val_acc, 4)}


def main() -> int:
    p = argparse.ArgumentParser(prog="train_from_csv")
    p.add_argument("--csv", type=Path, default=PROJECT_ROOT / "data/raw/xauusd_m5_1y.csv")
    p.add_argument(
        "--recipe",
        type=Path,
        default=PROJECT_ROOT / "configs/training/recipes/sur-g2026.1-recipe.json",
    )
    p.add_argument(
        "--label-contract",
        type=Path,
        default=PROJECT_ROOT / "configs/training/label_contracts/label-survival-barrier-1.0.0.json",
    )
    p.add_argument("--epochs", type=int, default=None, help="Override recipe epochs")
    p.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "data/models/crt_sur_chlg_g2026"
    )
    p.add_argument("--dry-run", action="store_true", help="Validate data only, skip training")
    p.add_argument("--entry-stride", type=int, default=5, help="Bars between label entries")
    p.add_argument(
        "--val-split", type=float, default=0.20, help="Validation fraction (chronological)"
    )
    args = p.parse_args()

    print("=" * 60)
    print("  V9 INSTITUTIONAL 40-DIM TRAINING RUN")
    print(f"  Data: {args.csv}")
    print(f"  Recipe: {args.recipe}")
    print(f"  Label Contract: {args.label_contract}")
    print("=" * 60)

    # ── Load contract ──
    from core.contracts.training.label_contract import LabelContract
    from core.contracts.training.training_recipe import TrainingRecipe

    contract = LabelContract.from_file(args.label_contract)
    issues = contract.validate()
    if issues:
        print(f"Contract issues: {issues}")
        return 1
    print(
        f"\nLabel Contract: {contract.contract_id} (type={contract.type}, "
        f"horizon={contract.horizon_bars} bars, SL={contract.sl_atr_mult}x, TP={contract.tp_atr_mult}x)"
    )

    recipe = TrainingRecipe.from_file(args.recipe)
    recipe_issues = recipe.validate()
    if recipe_issues:
        print(f"Recipe issues: {recipe_issues}")
        return 1
    print(f"Training Recipe: {recipe.recipe_id}")

    recipe_dict = recipe.to_dict()
    if args.epochs:
        recipe_dict["training"]["epochs"] = args.epochs

    # ── Load data ──
    t0 = time.perf_counter()
    print("\nLoading CSV...")
    opens, highs, lows, closes, volumes = load_ohlc_csv(args.csv)
    n_bars = len(closes)
    print(f"  {n_bars} bars")
    if volumes.sum() == 0:
        # CSV may lack volume columns; fall back to dummy volumes
        volumes = np.ones(n_bars, dtype=np.float64)
        print("  WARNING: zero tick_volume — using dummy volume for Vol_ZScore")

    # ── Compute ATR ──
    print("Computing ATR(14)...")
    atr = compute_atr(highs, lows, closes)

    # ── Build barrier labels ──
    print(f"Building barrier labels (stride={args.entry_stride})...")
    labels = build_barrier_labels_from_csv(
        opens, highs, lows, closes, atr, contract, args.entry_stride
    )
    n_labels = len(labels)

    tp_count = sum(1 for l in labels if l["label"] == "tp_hit_first")
    sl_count = sum(1 for l in labels if l["label"] == "sl_hit_first")
    to_count = sum(1 for l in labels if l["label"] == "timeout")

    print(
        f"  {n_labels} labels: {tp_count} TP ({tp_count / n_labels * 100:.1f}%), "
        f"{sl_count} SL ({sl_count / n_labels * 100:.1f}%), "
        f"{to_count} timeout ({to_count / n_labels * 100:.1f}%)"
    )

    # ── Quality gate check ──
    if n_labels < 200:
        print(f"  FAIL: too few labels ({n_labels} < 200)")
        return 1
    min_class_pct = min(tp_count, sl_count, to_count) / n_labels
    if min_class_pct < 0.05:
        print(f"  FAIL: label imbalance (min class {min_class_pct:.1%} < 5%)")
        return 1
    print(f"  Quality gate: PASS (labels={n_labels}, min_class={min_class_pct:.1%})")

    # ── Build features ──
    print("Building 40-dim V9 Institutional features (M5/M15/M30/H1)...")
    X, y = build_features_from_csv(opens, highs, lows, closes, volumes, labels)

    # NaN check
    nan_count = np.sum(np.isnan(X))
    if nan_count > 0:
        print(f"  WARNING: {nan_count} NaN values in features — filling with 0")
        X = np.nan_to_num(X, nan=0.0)
    print(f"  Feature matrix: {X.shape}, labels: {y.shape}")
    print(f"  Feature stats: mean={X.mean(axis=0)[:5].round(3)}, std={X.std(axis=0)[:5].round(3)}")

    # ── Train/val split (chronological, no shuffle) ──
    n_val = int(len(X) * args.val_split)
    X_train, X_val = X[:-n_val], X[-n_val:]
    y_train, y_val = y[:-n_val], y[-n_val:]
    print(f"  Train: {len(X_train)}, Val: {len(X_val)}")

    if args.dry_run:
        print("\n  [DRY RUN] Data validation complete — skipping training.")
        elapsed = round(time.perf_counter() - t0, 1)
        print(f"  Completed in {elapsed}s")
        return 0

    # ── Train ──
    print(f"\nTraining MLP ({recipe_dict['training']['architecture']})...")
    t_train = time.perf_counter()
    model, metrics = train_mlp(X_train, y_train, X_val, y_val, recipe_dict)
    elapsed_train = round(time.perf_counter() - t_train, 1)

    print(f"\n  Training complete in {elapsed_train}s")
    print(f"  Best val accuracy: {metrics['best_val_acc']:.4f}")

    # ── Export ONNX ──
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        import torch

        dummy = torch.randn(1, X.shape[1])
        onnx_path = output_dir / "CRT.sur.chlg.g2026.1.40dim.onnx"

        # Use legacy ONNX exporter — dynamo exporter prints emoji that
        # crashes GBK-encoded terminals on Windows.
        torch.onnx.export(
            model.cpu(),
            dummy,
            str(onnx_path),
            input_names=["features"],
            output_names=["logits"],
            dynamic_axes={"features": {0: "batch"}},
            dynamo=False,
        )
        print(f"  ONNX exported: {onnx_path} ({onnx_path.stat().st_size / 1024:.1f} KB)")
    except Exception as exc:
        print(f"  ONNX export failed: {exc}")
        import torch

        torch.save(model.state_dict(), str(output_dir / "model.pt"))
        print(f"  Saved PyTorch model: {output_dir / 'model.pt'}")

    # ── Save manifest ──
    manifest = {
        "schema_version": "crt_model_manifest.v1",
        "model_id": "CRT.sur.chlg.g2026.1.40dim",
        "feature_schema": "v9_institutional_40",
        "trained_at": datetime.now(UTC).isoformat(),
        "data": {
            "source": str(args.csv),
            "bars": n_bars,
            "labels": n_labels,
            "label_distribution": {"tp": tp_count, "sl": sl_count, "timeout": to_count},
        },
        "training": {
            "val_acc": metrics["best_val_acc"],
            "epochs": recipe_dict["training"]["epochs"],
            "train_samples": len(X_train),
            "val_samples": len(X_val),
        },
        "contract": {
            "contract_id": contract.contract_id,
            "sl_atr_mult": contract.sl_atr_mult,
            "tp_atr_mult": contract.tp_atr_mult,
            "horizon_bars": contract.horizon_bars,
        },
        "recipe_id": recipe.recipe_id,
    }
    manifest_path = output_dir / "CRT.sur.chlg.g2026.1.40dim.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"  Manifest saved: {manifest_path}")

    elapsed = round(time.perf_counter() - t0, 1)
    print(f"\n{'=' * 60}")
    print(f"  TRAINING COMPLETE in {elapsed}s")
    print(f"  Model: {output_dir}")
    print(f"  Val accuracy: {metrics['best_val_acc']:.4f}")
    print(f"{'=' * 60}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
