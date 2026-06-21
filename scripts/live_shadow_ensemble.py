"""Multi-model shadow ensemble: run all registered brains in parallel, compare outputs.

Runs each brain adapter on the same feature vector and produces a side-by-side
comparison of direction, confidence, and probabilities. Does NOT send orders.

Usage:
  python scripts/live_shadow_ensemble.py                                    # all brains from configs/brains/
  python scripts/live_shadow_ensemble.py --brains v9_institutional_01 xgboost_v4.5  # specific brains
  python scripts/live_shadow_ensemble.py --output data/reports/shadow_ensemble.json
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from core.runtime.fault_handler import fail_open_guard

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
DEFAULT_BRAINS_DIR = PROJECT_ROOT / "configs" / "brains"
DEFAULT_NORM_CONFIG = DEFAULT_BRAINS_DIR / "v9_institutional_01.normalization.json"

SCHEMA_VERSION = "live_shadow_ensemble.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _discover_brain_entries(
    brains_dir: Path, *, brain_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """Load brain entry JSON files from configs/brains/, optionally filtered."""
    entries: list[dict[str, Any]] = []
    if not brains_dir.is_dir():
        return entries
    for p in sorted(brains_dir.glob("*.json")):
        # Skip normalization configs and non-brain files (meta filters, etc.)
        if "normalization" in p.name.lower():
            continue
        if "meta_stage2" in p.name.lower():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # FIX-021: only process brain_registry_entry files
        if data.get("schema_version") != "brain_registry_entry.v1":
            continue
        if "brain_type" not in data:
            continue
        b_id = data.get("brain_id", "")
        if brain_ids and b_id not in brain_ids:
            continue
        entries.append(data)
    return entries


def _build_brain(entry: dict[str, Any]) -> tuple[Any | None, str | None]:
    """Build and load a brain adapter from entry.

    Returns (adapter, error_string). On success, adapter is set and error is None.
    On failure, adapter is None and error contains the exception message.
    """
    bid = entry.get("brain_id", "unknown")
    try:
        from core.brains.services.brain_factory import BrainFactory

        factory = BrainFactory()
        adapter = factory.build(entry)
        return adapter, None
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_shadow_ensemble:_build_brain"):
            err_msg = f"{type(exc).__name__}: {exc}"
            print(
                f"[shadow_ensemble] build_failed brain_id={bid} error={err_msg}",
                flush=True,
            )
            return None, err_msg
def _route_feature_vector(
    schema_id: str,
    default_vector: np.ndarray,
    micro_vector: np.ndarray,
    swing35_vector: np.ndarray,
) -> np.ndarray:
    """Route the correct feature vector to a brain based on its schema.

    DQAF-046 Phase 3: Feature routing dispatcher.
    """
    if "microstructure" in schema_id:
        return micro_vector
    if schema_id == "swing_enhanced_35":
        return swing35_vector
    return default_vector


def _run_single_brain(
    adapter: Any,
    brain_id: str,
    feature_vector: np.ndarray,
    brain_type: str,
) -> dict[str, Any]:
    """Run inference on one brain, returning a standard result dict."""
    t0 = time.perf_counter()
    try:
        raw = adapter.infer(feature_vector)
        signal = adapter.get_signal(raw)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        # DQAF-046: BrainSignal direction/confidence are direct attributes,
        # NOT nested in .prediction dict.  Derive up/down probs from signal.
        direction = getattr(signal, "direction", "neutral")
        confidence = getattr(signal, "confidence", 0.0)
        if direction == "long":
            up_prob = round(0.5 + float(confidence) / 2.0, 6)
            down_prob = round(1.0 - up_prob, 6)
        elif direction == "short":
            down_prob = round(0.5 + float(confidence) / 2.0, 6)
            up_prob = round(1.0 - down_prob, 6)
        else:
            up_prob = 0.5
            down_prob = 0.5
        return {
            "brain_id": brain_id,
            "brain_type": brain_type,
            "status": "ok",
            "runtime_ms": elapsed_ms,
            "direction_bias": direction,
            "up_probability": up_prob,
            "down_probability": down_prob,
            "confidence": round(float(confidence), 6),
            "backend": adapter.describe().get("backend", "unknown")
            if hasattr(adapter, "describe")
            else "unknown",
        }
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("live_shadow_ensemble:_run_single_brain"):
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            err_str = str(exc)[:500]
            print(
                f"[shadow_ensemble] infer_error brain_id={brain_id} "
                f"brain_type={brain_type} error={err_str}",
                flush=True,
            )
            return {
                "brain_id": brain_id,
                "brain_type": brain_type,
                "status": "error",
                "runtime_ms": elapsed_ms,
                "error": err_str,
            }
def _compare_directions(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare direction consensus across brains."""
    ok_results = [r for r in results if r["status"] == "ok"]
    if not ok_results:
        return {"consensus": "no_results", "total_brains": 0}

    long_count = sum(1 for r in ok_results if r["direction_bias"] == "long")
    short_count = sum(1 for r in ok_results if r["direction_bias"] == "short")
    neutral_count = sum(1 for r in ok_results if r["direction_bias"] == "neutral")
    n_ok = len(ok_results)

    if long_count > n_ok // 2:
        consensus = "long"
    elif short_count > n_ok // 2:
        consensus = "short"
    elif neutral_count == n_ok:
        consensus = "neutral"
    else:
        consensus = "split"

    # Direction agreement score (0-1)
    max_same = max(long_count, short_count, neutral_count)
    agreement = max_same / n_ok if n_ok > 0 else 0.0

    return {
        "consensus": consensus,
        "total_brains": n_ok,
        "long_count": long_count,
        "short_count": short_count,
        "neutral_count": neutral_count,
        "agreement_score": round(agreement, 4),
        "disagreeing_brains": [
            r["brain_id"]
            for r in ok_results
            if r["direction_bias"] != consensus
            or (consensus == "split" and r["direction_bias"] not in ("long", "short"))
        ],
    }


def _resolve_feature_vector(
    feature_store_dir: Path | str | None = None,
    feature_dim: int = 40,
    symbol: str = "XAUUSDc",
) -> tuple[np.ndarray, str]:
    """Try to load the latest real feature vector from LocalFeatureStore.

    Returns (vector, source) where source is one of "store", "stub".
    """
    store_dir = Path(feature_store_dir) if feature_store_dir else None
    if store_dir is not None and store_dir.is_dir():
        try:
            from core.features.local_feature_store import LocalFeatureStore

            # FIX-20260612-021: resolve schema from symbol
            _is_btc = "btc" in str(symbol).lower()
            _schema = "btc_macro_enhanced_37" if _is_btc else "v9_institutional_40"
            _dim = 37 if _is_btc else 40
            if _is_btc:
                from core.features.schemas.btc_macro_enhanced_schema import (
                    BTC_MACRO_ENHANCED_37_FEATURES,
                )
                _feature_names = BTC_MACRO_ENHANCED_37_FEATURES
            else:
                from core.features.schemas.v9_institutional_schema import (
                    V9_INSTITUTIONAL_40_FEATURES,
                )
                _feature_names = V9_INSTITUTIONAL_40_FEATURES

            store = LocalFeatureStore(str(store_dir))
            record = store.latest(symbol, "M5", schema_name=_schema)
            if record is not None and record.values:
                vec = np.array(
                    [float(record.values.get(name, 0.0)) for name in _feature_names],
                    dtype=np.float64,
                )
                return vec, str(_dim)
        except Exception:  # BLE001:FOG
            with fail_open_guard("live_shadow_ensemble:_resolve_feature_vector"):
                pass
    return np.zeros(feature_dim, dtype=np.float64), "stub"


# ── DQAF-046: Dual-Track Feature Pipeline ─────────────────────────────
# swing_enhanced_35 features for legacy 35-dim brains that were trained on
# the D1-barrier + micro + TF schema before the v9_institutional_40 migration.

# CSV paths for XAU swing_enhanced_35 daily feature computation
_XAU_D1_CSV = PROJECT_ROOT / "data" / "raw" / "xauusdc_d1_merged.csv"
_XAU_H4_CSV = PROJECT_ROOT / "data" / "raw" / "xauusdc_h4_merged.csv"
_XAU_CROSS_CSVS = {
    "XAGUSDc": PROJECT_ROOT / "data" / "raw" / "xagusdc_d1_merged.csv",
    "EURUSDc": PROJECT_ROOT / "data" / "raw" / "eurusdc_d1_merged.csv",
}


def _resolve_swing35_feature_vector(
    micro_feature_vector: np.ndarray,
    v9_40_vector: np.ndarray,
    feature_store_dir: Path | str | None = None,
    symbol: str = "XAUUSDc",
) -> tuple[np.ndarray, str]:
    """Compute swing_enhanced_35 feature vector (24 daily + 9 micro + 2 TF).

    Uses DailyFeatureComputer for the 24 D1-barrier features (from CSV),
    the existing micro feature vector for 9 microstructure features, and
    extracts M5 OU Theta + M5 Hurst from the v9 40-dim vector for TF features.

    Returns (vector, source) where source is "csv+daily" or "stub".
    """
    store_dir = Path(feature_store_dir) if feature_store_dir else None

    # ── D1-barrier 24 features via DailyFeatureComputer ──
    daily_features: list[float] = []
    if _XAU_D1_CSV.exists() and _XAU_H4_CSV.exists():
        try:
            from core.features.computers.daily_computer import DailyFeatureComputer

            cross_assets = {
                name: str(p)
                for name, p in _XAU_CROSS_CSVS.items()
                if p.exists()
            }
            comp = DailyFeatureComputer(
                d1_csv=str(_XAU_D1_CSV),
                h4_csv=str(_XAU_H4_CSV),
                cross_assets=cross_assets if cross_assets else None,
            )
            # Get the latest (most recent) row
            features_arr, _ = comp.compute_all()
            if len(features_arr) > 0:
                daily_features = [float(v) for v in features_arr[-1]]
        except Exception:
            with fail_open_guard("live_shadow_ensemble:_resolve_swing35_daily"):
                pass

    if len(daily_features) != 24:
        # fallback: zero out D1-barrier portion
        daily_features = [0.0] * 24

    # ── Micro 9 features from existing micro vector ──
    if micro_feature_vector is not None and len(micro_feature_vector) == 9:
        micro_features = [float(v) for v in micro_feature_vector]
        micro_source = "store"
    else:
        micro_features = [0.0] * 9
        micro_source = "stub"

    # ── TF-specific 2 features from v9 40-dim vector ──
    # The 40-dim vector has: ... M5_OU_Theta (idx 34), M5_Hurst (idx 38)
    # Slicing by position is fragile; extract by known index.
    tf_ou = 0.0
    tf_hurst = 0.5
    if v9_40_vector is not None and len(v9_40_vector) >= 40:
        try:
            # M5_OU_Theta is at position 34, M5_Hurst at 38
            tf_ou = float(v9_40_vector[34])
            tf_hurst = float(v9_40_vector[38])
        except (IndexError, TypeError, ValueError):
            pass

    # ── Assemble: 24 DAILY + 9 MICRO + 2 TF = 35 ──
    vec = np.array(daily_features + micro_features + [tf_ou, tf_hurst], dtype=np.float64)
    source = f"csv+daily,micro={micro_source},tf=v9_40"

    # Zero-vector guard
    if np.max(np.abs(vec)) < 1e-10:
        return vec, "stub:all_zero"

    return vec, source


def _resolve_micro_feature_vector(
    feature_store_dir: Path | str | None = None,
    symbol: str = "XAUUSDc",
) -> tuple[np.ndarray, str]:
    """Load latest microstructure 9-feature vector from store, or return stub."""
    store_dir = Path(feature_store_dir) if feature_store_dir else None
    if store_dir is not None and store_dir.is_dir():
        try:
            from core.features.adapters.microstructure_feature_adapter import (
                MicrostructureFeatureAdapter,
            )
            from core.features.local_feature_store import LocalFeatureStore

            store = LocalFeatureStore(str(store_dir))
            record = store.latest(symbol, "M5", schema_name="v4.3_microstructure_9")
            if record is not None and record.values:
                adapter = MicrostructureFeatureAdapter(
                    scaler_path=None,
                )
                vec = adapter.build_model_input(record.values).ravel()
                return vec, "store"
        except Exception:  # BLE001:FOG
            with fail_open_guard("live_shadow_ensemble:_resolve_micro_feature_vector"):
                pass
    return np.zeros(9, dtype=np.float64), "stub"


def build_report(
    brains_dir: Path | None = None,
    *,
    brain_ids: list[str] | None = None,
    feature_dim: int = 40,
    feature_store_dir: Path | None = None,
    parallel: bool = True,
    symbol: str = "XAUUSDc",
    write_decisions: bool = True,
) -> dict[str, Any]:
    brains = brains_dir or DEFAULT_BRAINS_DIR
    entries = _discover_brain_entries(brains, brain_ids=brain_ids)
    if not entries:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "brains_dir": str(brains),
            "total_brains": 0,
            "error": "no_brain_entries_found",
            "results": [],
        }

    feature_vector, feature_source = _resolve_feature_vector(
        feature_store_dir=feature_store_dir,
        feature_dim=feature_dim,
        symbol=symbol,
    )
    micro_feature_vector, micro_source = _resolve_micro_feature_vector(
        feature_store_dir=feature_store_dir,
        symbol=symbol,
    )

    # DQAF-046: Dual-track — compute swing_enhanced_35 for legacy 35-dim brains
    swing35_feature_vector, swing35_source = _resolve_swing35_feature_vector(
        micro_feature_vector=micro_feature_vector,
        v9_40_vector=feature_vector,
        feature_store_dir=feature_store_dir,
        symbol=symbol,
    )

    # Build adapters
    adapters: dict[str, Any] = {}
    load_errors: list[dict[str, Any]] = []
    for entry in entries:
        bid = entry.get("brain_id", "unknown")
        adapter, err_msg = _build_brain(entry)
        if adapter is None:
            load_errors.append(
                {"brain_id": bid, "error": "build_failed", "detail": err_msg or "unknown"}
            )
        else:
            schema_id = entry.get("feature_schema_id", "")
            adapters[bid] = (adapter, entry.get("brain_type", "?"), schema_id)

    # Run inference — route correct feature vector per brain
    results: list[dict[str, Any]] = []
    if parallel and len(adapters) > 1:
        with ThreadPoolExecutor(max_workers=min(len(adapters), 4)) as executor:
            futures = {}
            for bid, (adapter, btype, schema_id) in adapters.items():
                fv = _route_feature_vector(
                    schema_id, feature_vector, micro_feature_vector, swing35_feature_vector
                )
                futures[executor.submit(_run_single_brain, adapter, bid, fv, btype)] = bid
            for future in as_completed(futures):
                results.append(future.result())
    else:
        for bid, (adapter, btype, schema_id) in adapters.items():
            fv = _route_feature_vector(
                schema_id, feature_vector, micro_feature_vector, swing35_feature_vector
            )
            results.append(_run_single_brain(adapter, bid, fv, btype))

    # Add load errors
    results.extend(
        {"brain_id": e["brain_id"], "status": "error", "error": e["error"]} for e in load_errors
    )

    comparison = _compare_directions(results)

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "brains_dir": str(brains),
        "feature_dim": feature_dim,
        "feature_source": feature_source,
        "micro_source": micro_source,
        "swing35_source": swing35_source,
        "total_brains": len(entries),
        "parallel": parallel and len(adapters) > 1,
        "comparison": comparison,
        "results": results,
    }

    # ── Persist shadow decisions to ledger for brain leaderboard ──
    shadow_write_result: dict[str, Any] = {"written": False, "reason": "disabled"}
    if write_decisions:
        try:
            from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
            from scripts.shadow_decision_recorder import record_shadow_from_ensemble

            store = JsonlLedgerStore(str(PROJECT_ROOT / "data"))
            shadow_write_result = record_shadow_from_ensemble(
                results=results,
                consensus=comparison,
                symbol=symbol,
                store=store,
            )
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("live_shadow_ensemble:build_report"):
                shadow_write_result = {"written": False, "error": str(exc)[:500]}
    report["shadow_decisions_written"] = shadow_write_result

    return report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_shadow_ensemble")
    p.add_argument(
        "--brains-dir",
        type=Path,
        default=DEFAULT_BRAINS_DIR,
        help=f"Directory containing brain entry JSON files (default: {DEFAULT_BRAINS_DIR})",
    )
    p.add_argument(
        "--brains",
        nargs="*",
        default=None,
        help="Specific brain IDs to run (default: all .json in --brains-dir)",
    )
    p.add_argument(
        "--feature-dim",
        type=int,
        default=40,
        help="Dimensionality of dummy feature vector (default: 40 for V9)",
    )
    p.add_argument(
        "--sequential",
        action="store_true",
        help="Run brains sequentially instead of in parallel",
    )
    p.add_argument("--output", default=None, help="Write JSON report to file")
    p.add_argument(
        "--symbol",
        default="XAUUSDc",
        help="Trading symbol for decision ledger (default: XAUUSDc)",
    )
    p.add_argument(
        "--no-write-decisions",
        action="store_true",
        help="Skip writing shadow decisions to data/decisions ledger",
    )
    p.add_argument(
        "--feature-store-dir",
        type=Path,
        default=None,
        help="Feature store directory for real feature vectors (default: zeros stub)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(
        brains_dir=args.brains_dir,
        brain_ids=args.brains,
        feature_dim=args.feature_dim,
        feature_store_dir=args.feature_store_dir,
        parallel=not args.sequential,
        symbol=args.symbol,
        write_decisions=not args.no_write_decisions,
    )
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    if report.get("error"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
