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
        # Skip normalization configs
        if "normalization" in p.name.lower():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
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
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        print(
            f"[shadow_ensemble] build_failed brain_id={bid} error={err_msg}",
            flush=True,
        )
        return None, err_msg


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
        pred = signal.prediction if hasattr(signal, "prediction") else {}
        return {
            "brain_id": brain_id,
            "brain_type": brain_type,
            "status": "ok",
            "runtime_ms": elapsed_ms,
            "direction_bias": pred.get("direction_bias", "neutral"),
            "up_probability": round(float(pred.get("up_probability", 0.5)), 6),
            "down_probability": round(float(pred.get("down_probability", 0.5)), 6),
            "confidence": round(float(pred.get("confidence", 0.0)), 6),
            "backend": adapter.describe().get("backend", "unknown")
            if hasattr(adapter, "describe")
            else "unknown",
        }
    except Exception as exc:
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
            from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

            store = LocalFeatureStore(str(store_dir))
            record = store.latest(symbol, "M5", schema_name="v9_institutional_40")
            if record is not None and record.values:
                vec = np.array(
                    [float(record.values.get(name, 0.0)) for name in V9_INSTITUTIONAL_40_FEATURES],
                    dtype=np.float64,
                )
                return vec, "store"
        except Exception:
            pass
    return np.zeros(feature_dim, dtype=np.float64), "stub"


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
                    scaler_path="data/models/mtx_transformer_scaler.joblib",
                )
                vec = adapter.build_model_input(record.values).ravel()
                return vec, "store"
        except Exception:
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
                fv = micro_feature_vector if "microstructure" in schema_id else feature_vector
                futures[executor.submit(_run_single_brain, adapter, bid, fv, btype)] = bid
            for future in as_completed(futures):
                results.append(future.result())
    else:
        for bid, (adapter, btype, schema_id) in adapters.items():
            fv = micro_feature_vector if "microstructure" in schema_id else feature_vector
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
        except Exception as exc:
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
