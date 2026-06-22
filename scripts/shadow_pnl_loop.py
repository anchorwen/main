"""Shadow P&L accumulation loop — run all brains in shadow mode, track counterfactual P&L.

Every cycle (~60s):
  1. Settle previous pending signals with current mid_price
  2. Fetch latest V9 + microstructure features
  3. Run all 9 brain adapters
  4. Record each brain's directional signal to BrainPnLStore
  5. Write decision records for cross-brain comparison
  6. Persist state every save_interval cycles

Never places trades. Pure data collection for per-brain leaderboard.
Accumulates ~1440 P&L records/brain/day (at 60s interval).

Usage:
  python scripts/shadow_pnl_loop.py --mt5-terminal-path "D:\\MetaTrader 5\\terminal64.exe"
  python scripts/shadow_pnl_loop.py --mt5-terminal-path "..." --interval 30 --no-decisions
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.brains.services.brain_factory import BrainFactory
from core.deployment.feature_update_producer import build_v9_schema
from core.features.adapters.microstructure_feature_adapter import (
    MicrostructureFeatureAdapter,
)
from core.features.adapters.v9_feature_adapter import V9FeatureAdapter
from core.features.computers.microstructure_computer import (
    MicrostructureFeatureComputer,
)
from core.features.computers.v9_live_computer import V9LiveFeatureComputer
from core.features.local_feature_store import LocalFeatureStore
from core.features.rolling_normalizer import RollingNormalizer
from core.features.schemas.microstructure_schema import build_microstructure_schema
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from core.features.store_contracts import FeatureRecord
from core.feedback.brain_pnl_ledger import BrainPnLStore
from core.runtime.fault_handler import fail_open_guard

SCHEMA_VERSION = "shadow_pnl_loop.v1"


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).replace(microsecond=0).isoformat()


def _load_brain_entries(brains_dir: str) -> list[dict[str, Any]]:
    """Load all brain registry entry JSON files from a directory."""
    p = Path(brains_dir)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.is_dir():
        raise FileNotFoundError(f"brains directory not found: {p}")
    entries: list[dict[str, Any]] = []
    for f in sorted(p.glob("*.json")):
        if "normalization" in f.name.lower():
            continue
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
            if entry.get("schema_version") == "brain_registry_entry.v1":
                entries.append(entry)
        except (json.JSONDecodeError, OSError):
            pass
    return entries


def _build_brains(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build and load all brain adapters from registry entries."""
    factory = BrainFactory()
    brains: list[dict[str, Any]] = []
    for entry in entries:
        bid = entry.get("brain_id", "unknown")
        try:
            adapter = factory.build(entry)
            brains.append(
                {
                    "brain_id": bid,
                    "adapter": adapter,
                    "brain_type": entry.get("brain_type", "?"),
                    "feature_schema_id": entry.get("feature_schema_id", ""),
                    "hmre_layer": entry.get("hmre_layer", "M5"),
                }
            )
            print(f"  [shadow_pnl] loaded {bid} [{entry.get('brain_type', '?')}]", flush=True)
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("shadow_pnl_loop:_build_brains"):
                print(f"  [shadow_pnl] SKIP {bid}: {exc}", flush=True)
    return brains


def _get_prices(mt5: Any, symbol: str) -> tuple[float, float, float, float] | None:
    """Get current (bid, ask, mid, spread) from MT5."""
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        bid = float(tick.bid)
        ask = float(tick.ask)
        if bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2.0
        spread = ask - bid
        return (bid, ask, mid, spread)
    except Exception:  # BLE001:FOG
        with fail_open_guard("shadow_pnl_loop:_get_prices"):
            return None


def _run_brain_inference(
    adapter: Any,
    brain_id: str,
    brain_type: str,
    feature_vector: np.ndarray,
) -> dict[str, Any]:
    """Run inference on one brain, return standard result dict."""
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
        }
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("shadow_pnl_loop:_run_brain_inference"):
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            return {
                "brain_id": brain_id,
                "brain_type": brain_type,
                "status": "error",
                "runtime_ms": elapsed_ms,
                "error": str(exc)[:500],
            }


def _compare_directions(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute direction consensus across brains."""
    ok_results = [r for r in results if r["status"] == "ok"]
    if not ok_results:
        return {"consensus": "no_results", "total_brains": 0}

    long_count = sum(1 for r in ok_results if r["direction_bias"] == "long")
    short_count = sum(1 for r in ok_results if r["direction_bias"] == "short")
    neutral_count = sum(1 for r in ok_results if r["direction_bias"] == "neutral")
    n = len(ok_results)

    if long_count > n // 2:
        consensus = "long"
    elif short_count > n // 2:
        consensus = "short"
    elif neutral_count == n:
        consensus = "neutral"
    else:
        consensus = "split"

    return {
        "consensus": consensus,
        "total_brains": n,
        "long_count": long_count,
        "short_count": short_count,
        "neutral_count": neutral_count,
        "agreement_score": round(max(long_count, short_count, neutral_count) / n, 4) if n else 0,
    }


def _write_decision_records(
    results: list[dict[str, Any]],
    consensus: dict[str, Any],
    symbol: str,
    base_dir: str,
    *,
    feature_vector: np.ndarray | None = None,
    regime_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write shadow decision records to ledger."""
    try:
        from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
        from scripts.shadow_decision_recorder import record_shadow_from_ensemble

        store = JsonlLedgerStore(base_dir)
        return record_shadow_from_ensemble(
            results=results,
            consensus=consensus,
            symbol=symbol,
            store=store,
        )
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("shadow_pnl_loop:_write_decision_records"):
            return {"written": False, "error": str(exc)[:500]}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shadow_pnl_loop",
        description="Run all brains in shadow mode, accumulate counterfactual P&L",
    )
    p.add_argument("--mt5-terminal-path", required=True)
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--interval-seconds", type=float, default=60.0)
    p.add_argument("--base-dir", default="data")
    p.add_argument("--brains-dir", default="configs/brains")
    p.add_argument("--feature-store-dir", default="data/feature_store")
    p.add_argument(
        "--normalization-config",
        default="configs/brains/v9_institutional_01.normalization.json",
    )
    p.add_argument("--pnl-ledger-path", default=None, help="Override PnL ledger path")
    p.add_argument("--save-interval", type=int, default=60, help="Save state every N cycles")
    p.add_argument("--no-decisions", action="store_true", help="Skip writing decision records")
    p.add_argument("--once", action="store_true", help="Run one cycle and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── MT5 ──
    try:
        import MetaTrader5 as mt5_mod
    except Exception:  # BLE001:FOG
        with fail_open_guard("shadow_pnl_loop:main"):
            print(json.dumps({"error": "MetaTrader5 package required"}), flush=True)
            return 2
    mt5 = mt5_mod
    if not mt5.initialize(path=args.mt5_terminal_path):
        print(json.dumps({"error": "mt5_initialize_failed", "detail": str(mt5.last_error())}))
        return 2

    symbol = args.symbol
    base_dir = Path(args.base_dir)

    # ── Load normalization ──
    norm_path = Path(args.normalization_config)
    if not norm_path.is_absolute():
        norm_path = PROJECT_ROOT / norm_path
    norm_config = json.loads(norm_path.read_text(encoding="utf-8"))
    mean = np.array(norm_config["mean"])
    std = np.maximum(np.array(norm_config["std"]), 1e-8)

    # ── Feature computers ──
    v9_computer = V9LiveFeatureComputer(mt5, symbol)
    V9FeatureAdapter(normalization_config=norm_config)

    micro_computer = MicrostructureFeatureComputer(mt5, symbol)
    # DQAF-055: auto-discover per-symbol micro scaler
    _micro_scaler_path = MicrostructureFeatureAdapter.resolve_scaler_path(base_dir, symbol)
    micro_adapter = MicrostructureFeatureAdapter(
        scaler_path=_micro_scaler_path,
        require_scaler=True,
    )

    # ── Feature store ──
    fs_dir = Path(args.feature_store_dir)
    if not fs_dir.is_absolute():
        fs_dir = PROJECT_ROOT / fs_dir
    feature_store = LocalFeatureStore(str(fs_dir))
    feature_store.register_schema(build_v9_schema(symbol=symbol))
    feature_store.register_schema(build_microstructure_schema(symbol=symbol))

    # ── Normalizer ──
    norm_enabled = norm_config.get("normalize", True)
    rolling_norm = None
    if norm_enabled:
        rolling_norm = RollingNormalizer.from_static(
            mean=mean,
            std=std,
            warmup_bars=100,
        )
        rn_path = base_dir / "rolling_norm_state.json"
        if rn_path.exists():
            try:  # noqa: SIM105
                rolling_norm.load_state(rn_path)
            except Exception:  # BLE001:FOG
                with fail_open_guard("shadow_pnl_loop:main"):
                    pass
    # ── Load brains ──
    print(f"[shadow_pnl] Loading brains from {args.brains_dir}...", flush=True)
    entries = _load_brain_entries(args.brains_dir)
    if not entries:
        print(json.dumps({"error": "no_brain_entries_found"}), flush=True)
        mt5.shutdown()
        return 2

    brains = _build_brains(entries)
    if not brains:
        print(json.dumps({"error": "no_brains_loaded"}), flush=True)
        mt5.shutdown()
        return 2

    print(
        f"[shadow_pnl] {len(brains)} brains loaded: {[b['brain_id'] for b in brains]}", flush=True
    )

    # ── PnL Ledger ──
    # ── FIX-20260611-021: Event Sourcing — shadow events with source="shadow" ──
    from core.data.event_writer import get_event_writer

    _shadow_event_writer = get_event_writer(str(base_dir))

    # ── FIX-20260611-022: Event stream as primary recovery source ──
    _stream_path = base_dir / "ledger_events.jsonl"
    _loaded_from = "none"
    pnl_ledger = None

    if _stream_path.exists():
        try:
            pnl_ledger = BrainPnLStore.load_from_stream(
                _stream_path, event_writer=_shadow_event_writer, event_source="shadow"
            )
            _loaded_from = "event_stream"
        except Exception:  # BLE001:FOG
            with fail_open_guard("shadow_pnl_loop:main"):
                pass
    if pnl_ledger is None:
        pnl_path = (
            Path(args.pnl_ledger_path)
            if args.pnl_ledger_path
            else (base_dir / "brain_pnl_ledger.json")
        )
        if pnl_path.exists():
            try:
                pnl_ledger = BrainPnLStore.load(
                    pnl_path, event_writer=_shadow_event_writer, event_source="shadow"
                )
                _loaded_from = "old_json"
            except Exception:  # BLE001:FOG
                with fail_open_guard("shadow_pnl_loop:main"):
                    pass
    if pnl_ledger is None:
        pnl_ledger = BrainPnLStore(
            window_size=5000, event_writer=_shadow_event_writer, event_source="shadow"
        )
        _loaded_from = "fresh"

    print(
        f"[shadow_pnl] PnL ledger loaded: {pnl_ledger.total_settled} settled, {pnl_ledger.pending_count} pending (from={_loaded_from})",
        flush=True,
    )

    # ── Governance filter: skip retired brains when recording PnL ──
    retired_ids: set[str] = set()
    try:
        import json as _json

        _gov_path = base_dir / "governance_state.json"
        if _gov_path.exists():
            _gov_data = _json.loads(_gov_path.read_text(encoding="utf-8"))

            # brain_states entries with status == retired
            for bid, s in _gov_data.get("brain_states", {}).items():
                if s.get("status") == "retired":
                    retired_ids.add(bid)

            # transition_log: brain_ids whose last transition was to retired
            _log = _gov_data.get("transition_log", [])
            _last_to: dict[str, str] = {}
            for entry in _log:
                bid = entry.get("brain_id", "")
                to_status = entry.get("to_status", "")
                if bid and to_status:
                    _last_to[bid] = to_status
            for bid, to_status in _last_to.items():
                if to_status == "retired":
                    retired_ids.add(bid)

            if retired_ids:
                print(
                    f"[shadow_pnl] governance filter: skipping {len(retired_ids)} retired brains: "
                    f"{sorted(retired_ids)}",
                    flush=True,
                )
    except Exception:  # BLE001:FOG
        with fail_open_guard("shadow_pnl_loop:main"):
            pass
    # ── Regime detector ──
    from core.risk.regime_detector import RegimeDetector

    regime_detector = RegimeDetector()
    # Bootstrap from MT5
    try:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 200)
        if rates is not None and len(rates) >= 30:
            h = np.array([r["high"] for r in rates], dtype=np.float64)
            low = np.array([r["low"] for r in rates], dtype=np.float64)
            c = np.array([r["close"] for r in rates], dtype=np.float64)
            for i in range(14, len(c)):
                tr = np.maximum(
                    h[i - 13 : i + 1] - low[i - 13 : i + 1],
                    np.maximum(
                        np.abs(h[i - 13 : i + 1] - c[i - 14 : i]),
                        np.abs(low[i - 13 : i + 1] - c[i - 14 : i]),
                    ),
                )
                atr_val = float(np.mean(tr))
                if atr_val > 0.01:
                    regime_detector.update(atr_val)
    except Exception:  # BLE001:FOG
        with fail_open_guard("shadow_pnl_loop:main"):
            pass
    # ── Cycle state ──
    cycle_count = 0
    last_save_cycle = 0

    start_event = {
        "event": "shadow_pnl_loop_start",
        "time": _utc_iso(),
        "symbol": symbol,
        "interval_seconds": args.interval_seconds,
        "brain_count": len(brains),
        "brain_ids": [b["brain_id"] for b in brains],
        "pnl_ledger_path": str(pnl_path),
        "settled_count": pnl_ledger.total_settled,
        "pending_count": pnl_ledger.pending_count,
    }
    print(json.dumps(start_event, ensure_ascii=False), flush=True)

    try:
        while True:
            cycle_start = time.perf_counter()
            cycle_count += 1

            try:
                # ── 1. Get current prices ──
                prices = _get_prices(mt5, symbol)
                if prices is None:
                    time.sleep(1)
                    continue
                bid, ask, mid_price, live_spread = prices
                if mid_price <= 0:
                    time.sleep(1)
                    continue

                # ── 2. Settle previous pending signals ──
                if pnl_ledger.pending_count > 0:
                    try:
                        settled = pnl_ledger.settle_all(
                            mid_price,
                            spread=live_spread,
                            slippage=0.10,
                        )
                        if settled and cycle_count % 20 == 0:
                            print(
                                json.dumps(
                                    {
                                        "event": "pnl_settled",
                                        "time": _utc_iso(),
                                        "cycle": cycle_count,
                                        "settled_count": len(settled),
                                        "mid_price": round(mid_price, 2),
                                        "spread": round(live_spread, 5),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    except Exception:  # BLE001:FOG
                        with fail_open_guard("shadow_pnl_loop:main"):
                            pass
                # ── 3. Compute features ──
                feature_source = v9_computer.compute_all()
                raw_features = np.array(
                    [feature_source.get(fn, 0.0) for fn in V9_INSTITUTIONAL_40_FEATURES],
                    dtype=np.float64,
                )
                if rolling_norm is not None:
                    normed_features = rolling_norm.normalize(raw_features)
                else:
                    normed_features = (raw_features - mean) / std
                normed_features = normed_features.astype(np.float32)

                # Microstructure features — single bar (backward-compat) + multi-TF sequences
                micro_source = micro_computer.compute_all()
                _micro_features = micro_adapter.build_model_input(micro_source).ravel()
                micro_sequences: dict[str, np.ndarray] = {}
                try:  # noqa: SIM105
                    micro_sequences = micro_computer.compute_all_sequences(32)
                except Exception:  # BLE001:FOG
                    with fail_open_guard("shadow_pnl_loop:main"):
                        pass
                # ── 4. Persist features to store ──
                try:
                    from datetime import UTC
                    from datetime import datetime as dt

                    _now = dt.now(UTC).replace(tzinfo=None)
                    _records = []
                    if feature_source:
                        _records.append(
                            FeatureRecord(
                                schema_name="v9_institutional_40",
                                schema_version="1.0.0",
                                symbol=symbol,
                                timeframe="M5",
                                event_time=_now,
                                values=feature_source,
                                source="shadow_pnl",
                                ingested_at=_now,
                            )
                        )
                    if micro_source:
                        _records.append(
                            FeatureRecord(
                                schema_name="v4.3_microstructure_9",
                                schema_version="1.0.0",
                                symbol=symbol,
                                timeframe="M5",
                                event_time=_now,
                                values=micro_source,
                                source="shadow_pnl",
                                ingested_at=_now,
                            )
                        )
                    if _records:
                        feature_store.write_records(_records)
                except Exception:  # BLE001:FOG
                    with fail_open_guard("shadow_pnl_loop:main"):
                        pass
                # ── 5. Run all brains ──
                results: list[dict[str, Any]] = []
                for b in brains:
                    schema_id = b.get("feature_schema_id", "")
                    btype = b.get("brain_type", "")
                    if btype == "ou_params_v6":
                        result = _run_brain_inference(
                            b["adapter"],
                            b["brain_id"],
                            btype,
                            np.array([mid_price], dtype=np.float32)
                            if mid_price
                            else np.zeros(1, dtype=np.float32),
                        )
                    elif "microstructure" in schema_id:
                        hmre_layer = b.get("hmre_layer", "M5")
                        seq = micro_sequences.get(hmre_layer)
                        if seq is not None and seq.ndim == 2 and seq.shape[0] >= 32:
                            try:
                                prop = b["adapter"].run(None, seq)
                                pred = prop.prediction if hasattr(prop, "prediction") else {}
                                result = {
                                    "brain_id": b["brain_id"],
                                    "brain_type": btype,
                                    "status": "ok",
                                    "runtime_ms": round(
                                        float(getattr(prop.health, "runtime_ms", 0)), 2
                                    )
                                    if hasattr(prop, "health")
                                    else 0,
                                    "direction_bias": pred.get("direction_bias", "neutral"),
                                    "up_probability": round(
                                        float(pred.get("up_probability", 0.5)), 6
                                    ),
                                    "down_probability": round(
                                        float(pred.get("down_probability", 0.5)), 6
                                    ),
                                    "confidence": round(float(pred.get("confidence", 0.0)), 6),
                                }
                            except Exception:  # BLE001:FOG
                                with fail_open_guard("shadow_pnl_loop:main"):
                                    # Fallback: zero-padded (32,9) sequence to match 288-dim model
                                    fallback_seq = np.zeros((32, 9), dtype=np.float32)
                                    try:
                                        prop = b["adapter"].run(None, fallback_seq)
                                        pred = (
                                            prop.prediction if hasattr(prop, "prediction") else {}
                                        )
                                        result = {
                                            "brain_id": b["brain_id"],
                                            "brain_type": btype,
                                            "status": "fallback",
                                            "runtime_ms": round(
                                                float(getattr(prop.health, "runtime_ms", 0)), 2
                                            )
                                            if hasattr(prop, "health")
                                            else 0,
                                            "direction_bias": pred.get("direction_bias", "neutral"),
                                            "up_probability": round(
                                                float(pred.get("up_probability", 0.5)), 6
                                            ),
                                            "down_probability": round(
                                                float(pred.get("down_probability", 0.5)), 6
                                            ),
                                            "confidence": round(
                                                float(pred.get("confidence", 0.0)), 6
                                            ),
                                        }
                                    except Exception:  # BLE001:FOG
                                        with fail_open_guard("shadow_pnl_loop:main"):
                                            result = {
                                                "brain_id": b["brain_id"],
                                                "brain_type": btype,
                                                "status": "error",
                                                "runtime_ms": 0,
                                                "error": "adapter.run failed even with fallback sequence",
                                            }
                        else:
                            # Cold start: zero-padded sequence for correct dimensionality
                            fallback_seq = np.zeros((32, 9), dtype=np.float32)
                            try:
                                prop = b["adapter"].run(None, fallback_seq)
                                pred = prop.prediction if hasattr(prop, "prediction") else {}
                                result = {
                                    "brain_id": b["brain_id"],
                                    "brain_type": btype,
                                    "status": "cold_start",
                                    "runtime_ms": round(
                                        float(getattr(prop.health, "runtime_ms", 0)), 2
                                    )
                                    if hasattr(prop, "health")
                                    else 0,
                                    "direction_bias": pred.get("direction_bias", "neutral"),
                                    "up_probability": round(
                                        float(pred.get("up_probability", 0.5)), 6
                                    ),
                                    "down_probability": round(
                                        float(pred.get("down_probability", 0.5)), 6
                                    ),
                                    "confidence": round(float(pred.get("confidence", 0.0)), 6),
                                }
                            except Exception:  # BLE001:FOG
                                with fail_open_guard("shadow_pnl_loop:main"):
                                    result = {
                                        "brain_id": b["brain_id"],
                                        "brain_type": btype,
                                        "status": "error",
                                        "runtime_ms": 0,
                                        "error": "seq unavailable and adapter.run failed",
                                    }
                    else:
                        result = _run_brain_inference(
                            b["adapter"],
                            b["brain_id"],
                            btype,
                            normed_features,
                        )
                    results.append(result)

                    # ── Record P&L signal ──
                    # Skip cold-start / fallback / invalid-feature predictions
                    # so garbage predictions don't corrupt the PnL ledger.
                    # Also skip retired brains — their PnL is frozen.
                    _status = result.get("status", "ok")
                    _cold = _status in ("cold_start", "fallback", "error")
                    _valid = result.get("features_valid", True)
                    _retired = result["brain_id"] in retired_ids
                    if (
                        not _cold
                        and _valid
                        and not _retired
                        and result["status"] == "ok"
                        and result["direction_bias"] != "neutral"
                    ):
                        try:  # noqa: SIM105
                            pnl_ledger.record_signal(
                                brain_id=result["brain_id"],
                                symbol=symbol,
                                direction=result["direction_bias"],
                                entry_price=mid_price,
                                confidence=result["confidence"],
                                entry_spread=live_spread,
                                entry_slippage=0.10,
                            )
                        except Exception:  # BLE001:FOG
                            with fail_open_guard("shadow_pnl_loop:main"):
                                pass
                # ── 6. Compute consensus ──
                consensus = _compare_directions(results)

                # ── 7. Write decision records ──
                if not args.no_decisions:
                    _write_decision_records(
                        results,
                        consensus,
                        symbol,
                        str(base_dir),
                        feature_vector=normed_features,
                    )

                # ── 8. Log summary ──
                if cycle_count % 10 == 0:
                    dirs: dict[str, int] = {}
                    for r in results:
                        if r["status"] == "ok":
                            d = r["direction_bias"]
                            dirs[d] = dirs.get(d, 0) + 1
                    print(
                        json.dumps(
                            {
                                "event": "shadow_pnl_cycle",
                                "time": _utc_iso(),
                                "cycle": cycle_count,
                                "mid_price": round(mid_price, 2),
                                "directions": dirs,
                                "settled_total": pnl_ledger.total_settled,
                                "pending": pnl_ledger.pending_count,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

                # ── 9. Persist state ──
                # FIX-20260611-022: EventWriter handles persistence.
                # Old JSON save() is redundant in event stream mode.
                if cycle_count - last_save_cycle >= args.save_interval:
                    try:
                        # pnl_ledger.save(pnl_path)  # Replaced by EventWriter
                        if rolling_norm is not None:
                            rolling_norm.save_state(base_dir / "rolling_norm_state.json")
                        if regime_detector is not None:
                            regime_detector.save_state(base_dir / "regime_detector_state.json")
                        last_save_cycle = cycle_count
                        print(
                            json.dumps(
                                {
                                    "event": "shadow_pnl_state_saved",
                                    "time": _utc_iso(),
                                    "cycle": cycle_count,
                                    "settled_total": pnl_ledger.total_settled,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    except Exception as exc:  # BLE001:FOG
                        with fail_open_guard("shadow_pnl_loop:main"):
                            print(
                                json.dumps(
                                    {"event": "save_error", "error": str(exc)}, ensure_ascii=False
                                ),
                                flush=True,
                            )
            except Exception as exc:  # BLE001:FOG
                with fail_open_guard("shadow_pnl_loop:main"):
                    print(
                        json.dumps(
                            {
                                "event": "cycle_error",
                                "time": _utc_iso(),
                                "cycle": cycle_count,
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            if args.once:
                break

            # Sleep to maintain interval
            elapsed = time.perf_counter() - cycle_start
            sleep_time = max(0.1, args.interval_seconds - elapsed)
            time.sleep(sleep_time)

    finally:
        # ── Final persistence (EventWriter handles this) ──
        # FIX-20260611-022: pnl_ledger.save() replaced by EventWriter
        print(
            json.dumps(
                {
                    "event": "shadow_pnl_loop_shutdown",
                    "time": _utc_iso(),
                    "cycles": cycle_count,
                    "settled_total": pnl_ledger.total_settled,
                    "pnl_ledger_path": str(pnl_path),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if rolling_norm is not None:
            try:  # noqa: SIM105
                rolling_norm.save_state(base_dir / "rolling_norm_state.json")
            except Exception:  # BLE001:FOG
                with fail_open_guard("shadow_pnl_loop:main"):
                    logging.getLogger(__name__).warning(
                        "shadow_pnl_loop: failed to persist rolling normalizer state — "
                        "feature normalization may reset on restart"
                    )
        mt5.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
