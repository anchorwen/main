"""OOS blind test — the hard gate that kills hopes before a model ships.

Phase 3 / M2 (FIX-20260803-004, 战役三 — 自动 OOS / 盈亏平衡门槛 / IC 最高批准):

    A model whose blind-test Spearman rho underperforms, or whose directional
    win rate cannot cover the physical wear of spread & slippage, is
    HARD-VETOED here.  ``train.py`` runs this gate after quality gates pass
    and BEFORE any registry write / brain config — failure raises
    ``ModelQualityException`` and the run dies in CI/CD.

    Verdict rule (fail-closed):
      - ``INSUFFICIENT_OOS``   → blind sample < min_samples (warning, non-fatal
        by default — the TF-calibrated gate decides).
      - ``FAIL_RHO``           → spearman_rho < min_rho (when min_rho > 0).
      - ``FAIL_WIN_RATE``      → directional win_rate < min_win_rate.
      - ``FAIL_EXPECTANCY``    → expectancy < min_expectancy (R units).
      - ``FAIL_BREAKEVEN``     → win_rate <= breakeven_win_rate (when --breakeven).

Usage:
  python scripts/training/oos_blind_test.py \
    --model data_btc/models/btc_ssot_v2/best.txt \
    --blind-npz data_btc/training/btc_ssot_v2/test.npz \
    --label-contract configs/training/label_contracts/label-expected-r-btc-m15.json \
    --strategy btc_expected_r_m15 --live configs/live_btc.yaml \
    --min-rho 0.05 --min-win-rate 0.40 --breakeven
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Windows GBK console: force UTF-8 so Chinese/symbol output never crashes.
_stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
if callable(_stdout_reconfigure):
    _stdout_reconfigure(encoding="utf-8")


class OOSBlindError(RuntimeError):
    """Raised when the blind test fails — hard veto, no human waiver."""


def _load_model(path: Path):
    """Load a trained model by file extension.

    Supports the arch formats produced by ``train.py``:
      - ``.txt``  → LightGBM Booster
      - ``.json`` → XGBoost Booster (tree model_file) or legacy MLP weights
    Raises ``OOSBlindError`` for unsupported formats.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".txt":
            import lightgbm as lgb

            return lgb.Booster(model_file=str(path))
        if suffix == ".json":
            import xgboost as xgb

            bst = xgb.Booster()
            bst.load_model(str(path))
            return bst
    except (ImportError, OSError, ValueError) as e:
        raise OOSBlindError(f"Failed to load model {path}: {e}") from e
    raise OOSBlindError(
        f"Unsupported model format '{suffix}' for {path}. "
        f"Support .txt (LightGBM) / .json (XGBoost)."
    )


def _predict(model: Any, X: np.ndarray) -> np.ndarray:
    """Predict regression values (or class probabilities flattened to score)."""
    try:
        preds = model.predict(X)
    except Exception as e:  # noqa: BLE001 — model-specific predict signatures
        raise OOSBlindError(f"Prediction failed: {e}") from e
    arr = np.asarray(preds, dtype=np.float64)
    if arr.ndim == 1:
        return arr
    if arr.ndim == 2 and arr.shape[1] > 1:
        # multi-class: use class-1 probability as the directional score.
        return arr[:, 1]
    return arr.ravel()


def run_blind_test(
    model_path: str | Path,
    blind_npz: str | Path,
    *,
    y_key: str = "y_long",
    min_rho: float = 0.0,
    min_win_rate: float = 0.0,
    min_expectancy: float = 0.0,
    breakeven_win_rate: float | None = None,
    min_samples: int = 100,
    model: Any | None = None,
) -> dict[str, Any]:
    """Run the OOS blind test on a held-out NPZ.

    ``model`` is an optional preloaded object with a ``predict(X)`` method;
    when provided it bypasses ``_load_model`` (used by tests and by callers
    that already hold the trained booster).

    Returns a verdict dict.  Raises ``OOSBlindError`` on hard failure.
    """
    data = np.load(blind_npz, allow_pickle=True)
    X = np.asarray(data["X"], dtype=np.float64)
    if y_key not in data:
        raise OOSBlindError(
            f"blind NPZ {blind_npz} missing key '{y_key}'. " f"Available: {sorted(data.files)}"
        )
    y = np.asarray(data[y_key], dtype=np.float64)

    if X.shape[0] != y.shape[0]:
        raise OOSBlindError(f"blind NPZ shape mismatch: X={X.shape} vs {y_key}={y.shape}")

    # Drop invalid labels / features.
    bad_label = ~np.isfinite(y)
    bad_feat = ~np.all(np.isfinite(X), axis=1)
    mask = ~(bad_label | bad_feat)
    Xc, yc = X[mask], y[mask]
    n_total = int(len(y))
    n_valid = int(mask.sum())
    n_dropped = n_total - n_valid
    if n_valid < 3:
        raise OOSBlindError(f"Blind NPZ has only {n_valid} valid rows (dropped {n_dropped}).")

    if model is not None:
        pred = _predict(model, Xc)
    else:
        model = _load_model(Path(model_path))
        pred = _predict(model, Xc)

    # Directional simulation: take a position when |E[R]| meaningful.
    active = np.abs(pred) > 0.0
    n_active = int(active.sum())

    from core.training.utils import spearman_rho

    out: dict[str, Any] = {
        "model_path": str(model_path),
        "blind_npz": str(blind_npz),
        "y_key": y_key,
        "n_total": n_total,
        "n_valid": n_valid,
        "n_dropped": n_dropped,
        "n_active": n_active,
    }

    rho = spearman_rho(pred, yc)
    out["spearman_rho"] = round(rho, 6)

    if n_active < min_samples:
        # Plan (FIX-20260803-004): < min_samples → INSUFFICIENT_OOS is a
        # WARNING, not a hard veto.  The stats (when computable) are reported;
        # the training gate treats this as advisory, not blocking.
        out["verdict"] = "INSUFFICIENT_OOS"
        out["failures"] = [f"INSUFFICIENT_OOS: {n_active} active trades < {min_samples} minimum"]
        if n_active >= 3:
            dirs = np.where(pred > 0, 1.0, -1.0)[active]
            returns = dirs * yc[active]
            out["win_rate"] = round(float(np.mean(returns > 0)), 6)
            out["expectancy"] = round(float(np.mean(returns)), 6)
        else:
            out["win_rate"] = 0.0
            out["expectancy"] = 0.0
        out["breakeven_win_rate"] = (
            round(breakeven_win_rate, 6) if breakeven_win_rate is not None else None
        )
        return out

    dirs = np.where(pred > 0, 1.0, -1.0)[active]
    returns = dirs * yc[active]
    win_rate = float(np.mean(returns > 0))
    expectancy = float(np.mean(returns))
    out["win_rate"] = round(win_rate, 6)
    out["expectancy"] = round(expectancy, 6)
    out["breakeven_win_rate"] = (
        round(breakeven_win_rate, 6) if breakeven_win_rate is not None else None
    )

    failures: list[str] = []
    if min_rho > 0 and rho < min_rho:
        failures.append(f"FAIL_RHO: spearman {rho:.4f} < min {min_rho}")
    if min_win_rate > 0 and win_rate < min_win_rate:
        failures.append(f"FAIL_WIN_RATE: {win_rate:.4f} < min {min_win_rate}")
    if min_expectancy != 0 and expectancy < min_expectancy:
        failures.append(f"FAIL_EXPECTANCY: {expectancy:.4f} < min {min_expectancy}")
    if breakeven_win_rate is not None and win_rate <= breakeven_win_rate:
        failures.append(
            f"FAIL_BREAKEVEN: win_rate {win_rate:.4f} <= breakeven {breakeven_win_rate:.4f} "
            f"(spread+slippage physical wear not covered)"
        )

    out["verdict"] = "PASS" if not failures else "FAIL"
    out["failures"] = failures
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="OOS blind test — hard training gate")
    parser.add_argument("--model", required=True, help="Model file (.txt LightGBM / .json XGBoost)")
    parser.add_argument("--blind-npz", required=True, help="Held-out NPZ (time non-overlapping)")
    parser.add_argument("--y-key", default="y_long", help="Label key in NPZ (default y_long)")
    parser.add_argument("--min-rho", type=float, default=0.0, help="Spearman floor (0 = disabled)")
    parser.add_argument("--min-win-rate", type=float, default=0.0, help="Directional WR floor")
    parser.add_argument(
        "--min-expectancy", type=float, default=0.0, help="Expectancy floor (R units)"
    )
    parser.add_argument("--min-samples", type=int, default=100, help="Min active trades")
    parser.add_argument(
        "--breakeven",
        action="store_true",
        help="Compare win_rate vs breakeven_win_rate from label contract",
    )
    parser.add_argument(
        "--label-contract",
        default="configs/training/label_contracts/" "label-expected-r-btc-m15.json",
    )
    parser.add_argument("--strategy", default="btc_expected_r_m15")
    parser.add_argument("--live", default="configs/live_btc.yaml")
    parser.add_argument("--json-out", default="", help="Write verdict JSON to this path")
    args = parser.parse_args()

    breakeven_wr: float | None = None
    if args.breakeven:
        from core.contracts.training.label_contract import LabelContract
        from core.contracts.training.label_from_live_yaml import label_params_from_live_yaml
        from core.training.breakeven import breakeven_from_contract
        from scripts.training.validate_label_vs_live import validate_label_contract_vs_live

        contract = LabelContract.from_file(PROJECT_ROOT / args.label_contract)
        issues = validate_label_contract_vs_live(contract, args.strategy, PROJECT_ROOT / args.live)
        if issues:
            print("[oos] LABEL-LIVE FUSE: " + "; ".join(issues), file=sys.stderr)
            return 2
        breakeven_wr = breakeven_from_contract(contract).breakeven_win_rate
        print(f"[oos] breakeven_win_rate (friction-aware) = {breakeven_wr:.4f}")

    result = run_blind_test(
        args.model,
        args.blind_npz,
        y_key=args.y_key,
        min_rho=args.min_rho,
        min_win_rate=args.min_win_rate,
        min_expectancy=args.min_expectancy,
        breakeven_win_rate=breakeven_wr,
        min_samples=args.min_samples,
    )

    print("=" * 70)
    print(f"[oos] model       : {result['model_path']}")
    print(f"[oos] blind NPZ   : {result['blind_npz']} (y={result['y_key']})")
    print(f"[oos] valid rows  : {result['n_valid']} (dropped {result['n_dropped']})")
    print(f"[oos] active trades: {result['n_active']}")
    print(f"[oos] spearman_rho: {result['spearman_rho']:.6f}")
    print(f"[oos] win_rate    : {result['win_rate']:.6f}")
    print(f"[oos] expectancy  : {result['expectancy']:.6f}")
    if result.get("breakeven_win_rate") is not None:
        print(f"[oos] breakeven WR: {result['breakeven_win_rate']:.6f}")
    print(f"[oos] verdict     : {result['verdict']}")
    if result["failures"]:
        for f in result["failures"]:
            print(f"[oos]   ✗ {f}")
    print("=" * 70)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"[oos] verdict written to {args.json_out}")

    if result["verdict"] == "FAIL":
        print("[oos] HARD VETO — model must NOT enter candidate pool.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
