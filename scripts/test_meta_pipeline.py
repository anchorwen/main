"""Mock signal injection test for the Meta Pipeline (Stage 2 filter chain).

Verifies that the full LGB+MLP+Platt+Conformal chain is electrically
connected and produces non-trivial FilterResult outputs for synthetic
inputs.  Designed to run BEFORE the dual-track router goes live.

Usage:
    python scripts/test_meta_pipeline.py [--config configs/brains/meta_stage2_filter_v3.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_config(config_path: str) -> dict[str, Any]:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def load_meta_filter(config: dict[str, Any]) -> Any:
    """Instantiate and load the MetaSignalFilter from a V3 config dict."""
    from core.execution.meta_signal_filter import MetaSignalFilter

    conformal = config.get("conformal", {})
    filt = MetaSignalFilter(
        model_path=config.get("model_path", ""),
        mlp_model_path=config.get("mlp_model_path", ""),
        threshold=config.get("threshold", 0.65),
        enabled=True,
        mode=config.get("mode", "binary"),
        ensemble_weights=tuple(config.get("ensemble_weights", [0.6, 0.4])),
        micro_scaler_path=config.get("micro_scaler_path", ""),
        calibrator_path=config.get("calibrator_path", ""),
        conformal_mode=conformal.get("enabled", True),
        conformal_window=conformal.get("window", 500),
        conformal_percentile=conformal.get("percentile", 80.0),
        min_threshold=conformal.get("min_threshold", 0.50),
        conformal_max_age_days=conformal.get("max_age_days", 14.0),
    )

    loaded = filt.load()
    if not loaded:
        print("FATAL: MetaSignalFilter.load() returned False")
        return None
    return filt


def make_mock_v9_features() -> np.ndarray:
    """Construct a realistic (40,) V9 institutional feature vector.

    Uses values in the normalised range typically seen in live trading
    (z-scored or min-max scaled features).
    """
    rng = np.random.RandomState(42)
    features = np.zeros(40, dtype=np.float64)
    # M5 technicals (indices 0-7)
    features[0] = rng.uniform(-0.02, 0.02)  # M5_Ret_1
    features[1] = rng.uniform(0.3, 0.7)  # M5_Body_Ratio
    features[2] = rng.uniform(0.5, 2.5)  # M5_ATR_14
    features[3] = rng.uniform(30, 70)  # M5_RSI_14
    features[4] = rng.uniform(-0.5, 0.5)  # M5_MACD
    features[5] = rng.uniform(-2, 2)  # M5_Vol_ZScore
    features[6] = rng.uniform(-0.5, 0.5)  # M5_Macro1_Corr
    features[7] = rng.uniform(-1, 1)  # M5_Price_ZScore
    # M15 technicals (indices 8-15) — similar pattern
    features[8:16] = features[0:8] * rng.uniform(0.8, 1.2, 8)
    # M30 technicals (indices 16-23)
    features[16:24] = features[0:8] * rng.uniform(0.6, 1.4, 8)
    # H1 technicals (indices 24-31)
    features[24:32] = features[0:8] * rng.uniform(0.4, 1.6, 8)
    # M5_OU_Theta (index 32) — mean-reversion speed
    features[32] = rng.uniform(0.001, 0.05)  # M5_OU_Theta
    features[33] = rng.uniform(0.001, 0.05)  # M15_OU_Theta
    features[34] = rng.uniform(0.001, 0.05)  # M30_OU_Theta
    features[35] = rng.uniform(0.001, 0.05)  # H1_OU_Theta
    # Hurst exponents (indices 36-39)
    features[36] = rng.uniform(0.3, 0.7)  # M5_Hurst
    features[37] = rng.uniform(0.3, 0.7)  # M15_Hurst
    features[38] = rng.uniform(0.3, 0.7)  # M30_Hurst
    features[39] = rng.uniform(0.3, 0.7)  # H1_Hurst
    return features


def make_mock_micro_features() -> np.ndarray:
    """Construct a realistic (9,) microstructure feature vector."""
    rng = np.random.RandomState(99)
    features = np.zeros(9, dtype=np.float64)
    features[0] = rng.uniform(-0.001, 0.001)  # tick_return or similar
    features[1] = rng.uniform(-1, 1)  # HL_ratio
    features[2] = rng.uniform(-1, 1)  # CO_ratio
    features[3] = rng.uniform(0, 5)  # spread_bps
    features[4] = rng.uniform(-1, 1)  # OIM (order imbalance)
    features[5] = rng.uniform(0, 3)  # velocity
    features[6] = rng.uniform(-1, 1)  # FX_corr_1
    features[7] = rng.uniform(-1, 1)  # FX_corr_2
    features[8] = rng.uniform(-1, 1)  # FX_corr_3
    return features


def run_injection_test(
    filt: Any,
    s1_score: float,
    direction: str,
    v9: np.ndarray,
    micro: np.ndarray,
) -> dict[str, Any]:
    """Run a single signal injection and return diagnostics."""
    result = filt.filter_arrays(
        direction=direction,
        s1_prediction=s1_score,
        v9_array=v9,
        micro_array=micro,
        timestamp_utc=time.time(),
    )

    diag: dict[str, Any] = {
        "s1_score": round(s1_score, 6),
        "direction": direction,
        "passed": result.passed,
        "p_win": round(float(getattr(result, "p_win", 0)), 4),
        "threshold": round(float(getattr(result, "threshold", 0)), 4),
        "reason": result.reason,
        "exhaustion_factor": round(float(getattr(result, "exhaustion_factor", 1.0)), 4),
    }

    # Add internal model details if available
    _lgb_prob = getattr(filt, "_last_lgb_prob", None)
    _mlp_prob = getattr(filt, "_last_mlp_prob", None)
    _raw_prob = getattr(filt, "_last_raw_prob", None)
    _calibrated = getattr(filt, "_last_calibrated_prob", None)
    _conformal_thresh = getattr(filt, "_last_conformal_threshold", None)

    if _raw_prob is not None:
        diag["raw_prob"] = round(float(_raw_prob), 4)
    if _calibrated is not None:
        diag["calibrated_prob"] = round(float(_calibrated), 4)
    if _lgb_prob is not None:
        diag["lgb_prob"] = round(float(_lgb_prob), 4)
    if _mlp_prob is not None:
        diag["mlp_prob"] = round(float(_mlp_prob), 4)
    if _conformal_thresh is not None:
        diag["conformal_threshold"] = round(float(_conformal_thresh), 4)

    return diag


def main() -> None:
    parser = argparse.ArgumentParser(description="Meta Pipeline injection test")
    parser.add_argument(
        "--config",
        default="configs/brains/meta_stage2_filter_v3.json",
        help="Path to Stage 2 filter config",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Number of signals to inject per direction",
    )
    args = parser.parse_args()

    config_path = os.path.join(ROOT, args.config)
    if not os.path.exists(config_path):
        print(f"ERROR: config not found: {config_path}")
        sys.exit(1)

    print(f"Loading config: {args.config}")
    config = load_config(config_path)
    print(f"  filter_id: {config.get('filter_id', 'unknown')}")
    print(f"  model_path: {config.get('model_path', 'N/A')}")
    print(f"  mlp_model_path: {config.get('mlp_model_path', 'N/A')}")
    print(f"  threshold: {config.get('threshold', 'N/A')}")
    print(f"  calibrator: {config.get('calibrator_path', 'N/A')}")
    conformal = config.get("conformal", {})
    print(
        f"  conformal: enabled={conformal.get('enabled')}, window={conformal.get('window')}, "
        f"percentile={conformal.get('percentile')}%, min_threshold={conformal.get('min_threshold')}"
    )

    print("\nLoading MetaSignalFilter...")
    filt = load_meta_filter(config)
    if filt is None:
        print("FATAL: Could not load filter. Aborting.")
        sys.exit(1)

    print(f"  Loaded: {type(filt._model).__name__ if filt._model else 'NO LGB'}")
    print(f"  MLP:     {type(filt._mlp_model).__name__ if filt._mlp_model else 'none'}")
    print(f"  Features: {len(filt._feature_names)} — {filt._feature_names[:5]}...")
    print(f"  Calibrator: {'loaded' if filt._calibrator else 'none'}")
    print(f"  Conformal:  {'active' if filt._conformal_mode else 'disabled'}")

    # ── Test 1: Single SHORT signal injection ──
    print("\n" + "=" * 60)
    print("TEST 1: Single SHORT signal (s1_score = -0.88)")
    v9 = make_mock_v9_features()
    micro = make_mock_micro_features()
    diag = run_injection_test(filt, s1_score=-0.88, direction="short", v9=v9, micro=micro)
    for k, v in diag.items():
        print(f"  {k}: {v}")

    # ── Test 2: Single LONG signal injection ──
    print("\n" + "=" * 60)
    print("TEST 2: Single LONG signal (s1_score = 0.75)")
    diag = run_injection_test(filt, s1_score=0.75, direction="long", v9=v9, micro=micro)
    for k, v in diag.items():
        print(f"  {k}: {v}")

    # ── Test 3: Weak signal (should be blocked) ──
    print("\n" + "=" * 60)
    print("TEST 3: Weak SHORT signal (s1_score = -0.20)")
    diag = run_injection_test(filt, s1_score=-0.20, direction="short", v9=v9, micro=micro)
    for k, v in diag.items():
        print(f"  {k}: {v}")

    # ── Test 4: Extreme signal ──
    print("\n" + "=" * 60)
    print("TEST 4: Extreme SHORT signal (s1_score = -1.50)")
    diag = run_injection_test(filt, s1_score=-1.50, direction="short", v9=v9, micro=micro)
    for k, v in diag.items():
        print(f"  {k}: {v}")

    # ── Test 5: Bulk injection with varying scores ──
    print("\n" + "=" * 60)
    print(f"TEST 5: Bulk injection — {args.iterations} signals per direction")
    rng = np.random.RandomState(7)
    passed_short = 0
    passed_long = 0
    p_wins_short: list[float] = []
    p_wins_long: list[float] = []

    for _ in range(args.iterations):
        v9_r = make_mock_v9_features() + rng.normal(0, 0.05, 40)
        micro_r = make_mock_micro_features() + rng.normal(0, 0.02, 9)

        # SHORT
        s1_s = rng.uniform(-1.2, -0.35)
        diag_s = run_injection_test(filt, s1_score=s1_s, direction="short", v9=v9_r, micro=micro_r)
        if diag_s["passed"]:
            passed_short += 1
            p_wins_short.append(diag_s["p_win"])

        # LONG
        s1_l = rng.uniform(0.35, 1.2)
        diag_l = run_injection_test(filt, s1_score=s1_l, direction="long", v9=v9_r, micro=micro_r)
        if diag_l["passed"]:
            passed_long += 1
            p_wins_long.append(diag_l["p_win"])

    print(f"  SHORT: {passed_short}/{args.iterations} passed")
    if p_wins_short:
        print(
            f"    p_win — mean={np.mean(p_wins_short):.4f}, "
            f"min={np.min(p_wins_short):.4f}, max={np.max(p_wins_short):.4f}"
        )
    print(f"  LONG:  {passed_long}/{args.iterations} passed")
    if p_wins_long:
        print(
            f"    p_win — mean={np.mean(p_wins_long):.4f}, "
            f"min={np.min(p_wins_long):.4f}, max={np.max(p_wins_long):.4f}"
        )

    # ── Verify state file was written ──
    state_path = os.path.join(ROOT, "data", "meta_filter_state.json")
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        print(
            f"\n  meta_filter_state.json: pred_history={len(state.get('pred_history', []))} entries"
        )

    # ── Summary ──
    print("\n" + "=" * 60)
    total_passed = passed_short + passed_long
    total_signals = args.iterations * 2
    print(f"SUMMARY: {total_passed}/{total_signals} signals passed the filter")
    if total_passed > 0:
        all_p_wins = p_wins_short + p_wins_long
        print(f"  p_win range: [{min(all_p_wins):.4f}, {max(all_p_wins):.4f}]")
    print("\nMeta Pipeline injection test complete.")


if __name__ == "__main__":
    main()
