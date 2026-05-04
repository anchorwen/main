"""Periodic live intent producer → mt5_outbox (V9 Institutional ONNX pipeline).

Replaces the simple anchor-price-delta strategy with the full V9 feature
computation engine + ONNX inference, producing BrainDecisionProposal signals.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.deployment.feature_update_producer import build_v9_schema, produce_from_live_computer
from core.features.feature_service import FeatureService
from core.features.local_feature_store import LocalFeatureStore
from scripts.send_live_order import (
    _validate_sl_tp,
    dispatch_live_open_order,
    resolve_protection_flag_path,
)

# ── Feature engine defaults ──
DEFAULT_NORM_CONFIG = "configs/brains/v9_institutional_01.normalization.json"
DEFAULT_BRAIN_ENTRY = "configs/brains/v9_institutional_01.json"
DEFAULT_FEATURE_STORE_DIR = "data/feature_store"


def _utc_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def decide_side_from_anchor(price: float, anchor: float, threshold: float) -> str | None:
    """Determine trade direction from price vs anchor ± threshold."""
    if price > anchor + threshold:
        return "long"
    if price < anchor - threshold:
        return "short"
    return None


def compute_sl_tp_for_side(
    side: str,
    *,
    ref_long: float,
    ref_short: float,
    sl_distance: float,
    tp_distance: float,
) -> tuple[float, float, float]:
    """Returns stop_loss, take_profit, ref_for_guard."""
    if side == "long":
        stop_loss = ref_long - sl_distance
        take_profit = ref_long + tp_distance
        ref_for_guard = ref_long
    else:
        stop_loss = ref_short + sl_distance
        take_profit = ref_short - tp_distance
        ref_for_guard = ref_short
    return stop_loss, take_profit, ref_for_guard


def cooldown_blocks_fire(now: float, last_fire: float, cooldown_seconds: float) -> bool:
    return (now - last_fire) < cooldown_seconds


def _mid_and_prices(mt5: Any, symbol: str) -> tuple[float, float, float]:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError("tick unavailable")
    bid = float(tick.bid)
    ask = float(tick.ask)
    return (bid + ask) / 2.0, bid, ask


def _position_count(mt5: Any, symbol: str) -> int:
    pos = mt5.positions_get(symbol=symbol)
    return len(pos) if pos else 0


def load_normalization_config(path: str) -> dict[str, Any]:
    """Load normalization config from JSON, resolving relative paths."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(os.getcwd()) / p
    if not p.exists():
        raise FileNotFoundError(f"normalization config not found: {p}")
    with open(p) as fh:
        return json.load(fh)


def load_brain_entry(path: str) -> dict[str, Any]:
    """Load brain registry entry from JSON, resolving relative paths."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(os.getcwd()) / p
    if not p.exists():
        raise FileNotFoundError(f"brain entry not found: {p}")
    with open(p) as fh:
        return json.load(fh)


def proposal_to_side(proposal: Any) -> str | None:
    """Convert BrainDecisionProposal to trade side, applying confidence threshold.

    Returns 'long', 'short', or None (neutral/insufficient confidence).
    """
    direction = proposal.prediction.get("direction_bias", "neutral")
    confidence = proposal.prediction.get("confidence", 0.0)

    # Only act on sufficiently confident signals
    MIN_CONFIDENCE = 0.55
    if confidence < MIN_CONFIDENCE:
        return None

    if direction == "long":
        return "long"
    elif direction == "short":
        return "short"
    return None


def _resolve_consensus_side(consensus: dict[str, Any], min_confidence: float) -> str | None:
    """Convert ParliamentService consensus dict to trade side.

    Returns 'long', 'short', or None (neutral/insufficient consensus).
    """
    bias = consensus.get("aggregated_bias", "neutral")
    score = consensus.get("consensus_score", 0.0)

    if score < min_confidence or bias == "neutral":
        return None
    if bias in ("long", "short"):
        return bias
    return None


def _load_brain_entries_from_dir(brains_dir: str) -> list[dict[str, Any]]:
    """Load all brain registry entry JSON files from a directory."""
    p = Path(brains_dir)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_dir():
        raise FileNotFoundError(f"brains directory not found: {p}")
    entries: list[dict[str, Any]] = []
    for f in sorted(p.glob("*.json")):
        if f.name.endswith(".normalization.json"):
            continue
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
            if entry.get("schema_version") == "brain_registry_entry.v1":
                entries.append(entry)
        except (json.JSONDecodeError, OSError):
            pass
    if not entries:
        raise FileNotFoundError(f"no brain_registry_entry.v1 files found in {p}")
    return entries


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_intent_loop")
    p.add_argument("--base-dir", default="data")
    p.add_argument("--mt5-terminal-path", required=True)
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--interval-seconds", type=float, default=30.0)
    p.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.55,
        help="Minimum prediction confidence to fire a trade (0.0-1.0)",
    )
    p.add_argument(
        "--sl-distance",
        type=float,
        default=15.0,
        help="Distance from reference fill price to SL (absolute)",
    )
    p.add_argument(
        "--tp-distance",
        type=float,
        default=25.0,
        help="Distance from reference fill price to TP (absolute)",
    )
    p.add_argument("--cooldown-seconds", type=float, default=300.0)
    p.add_argument("--max-positions", type=int, default=1)
    p.add_argument(
        "--protection-flag-path",
        default="data/live_dispatch_block.flag",
        help="Resolved vs base-dir when not found under cwd",
    )
    p.add_argument(
        "--ignore-protection-flag",
        action="store_true",
        help="Dangerous: dispatch even if policy flag exists",
    )
    p.add_argument(
        "--volume",
        type=float,
        default=None,
        help="Optional lots on envelope payload; omit to use bridge --default-volume only",
    )
    p.add_argument(
        "--normalization-config",
        default=DEFAULT_NORM_CONFIG,
        help="Path to V9 normalization JSON (mean/std for 40 features)",
    )
    p.add_argument(
        "--brain-entry",
        default=DEFAULT_BRAIN_ENTRY,
        help="Path to brain registry entry JSON (artifact_path, etc.)",
    )
    p.add_argument(
        "--onnx-artifact",
        default=None,
        help="Override ONNX artifact path (default: from brain entry)",
    )
    p.add_argument(
        "--disable-onnx", action="store_true", help="Use deterministic fallback instead of ONNX"
    )
    p.add_argument(
        "--brain-type",
        default="onnx_v9",
        choices=["onnx_v9", "xgboost_v4.5", "ou_params_v6"],
        help="Brain type for inference adapter dispatch (default: onnx_v9)",
    )
    p.add_argument(
        "--multi-brain",
        action="store_true",
        help="Enable multi-brain joint decision via ParliamentService",
    )
    p.add_argument(
        "--brains-dir",
        default="configs/brains",
        help="Directory of brain registry entry JSON files (multi-brain mode only)",
    )
    p.add_argument(
        "--feature-store-dir",
        default=DEFAULT_FEATURE_STORE_DIR,
        help="Directory for LocalFeatureStore persistence (relative to cwd or absolute)",
    )
    p.add_argument(
        "--disable-feature-store",
        action="store_true",
        help="Skip feature persistence to LocalFeatureStore (use only live computation)",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single iteration and exit (useful for one-shot testing)",
    )
    p.add_argument(
        "--no-mt5",
        action="store_true",
        help="Skip MT5 initialization; use zero feature vector for brain inference verification",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── Import MT5 (skip if --no-mt5) ──
    mt5: Any = None
    if not args.no_mt5:
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            print(
                json.dumps({"error": "MetaTrader5 package required", "detail": str(exc)}, indent=2)
            )
            return 2

        if not mt5.initialize(path=args.mt5_terminal_path):  # type: ignore[attr-defined]
            print(
                json.dumps(
                    {"error": "mt5_initialize_failed", "detail": str(mt5.last_error())},
                    indent=2,  # type: ignore[attr-defined]
                )
            )
            return 2

    # ── Load configs ──
    try:
        norm_config = load_normalization_config(args.normalization_config)
    except Exception as exc:
        print(
            json.dumps({"error": "normalization_config_load_failed", "detail": str(exc)}, indent=2)
        )
        if mt5 is not None:
            mt5.shutdown()  # type: ignore[attr-defined]
        return 2

    try:
        brain_entry = load_brain_entry(args.brain_entry)
    except Exception as exc:
        print(json.dumps({"error": "brain_entry_load_failed", "detail": str(exc)}, indent=2))
        if mt5 is not None:
            mt5.shutdown()  # type: ignore[attr-defined]
        return 2

    # Apply overrides
    if args.onnx_artifact:
        brain_entry["artifact_path"] = args.onnx_artifact
    if args.disable_onnx:
        brain_entry["enable_onnxruntime"] = False

    # Ensure project root is on sys.path so core.* imports resolve
    _project_root = Path(__file__).resolve().parent.parent
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

    # ── Initialize feature computer (skip if --no-mt5) ──
    feature_adapter: Any = None
    feature_service: Any = None

    if not args.no_mt5:
        from core.features.computers.v9_live_computer import V9LiveFeatureComputer

        feature_computer = V9LiveFeatureComputer(mt5, args.symbol)

        # ── Initialize feature adapter with normalization ──
        from core.features.adapters.v9_feature_adapter import V9FeatureAdapter

        feature_adapter = V9FeatureAdapter(normalization_config=norm_config)

        # ── Initialize LocalFeatureStore (Fix 1: feature persistence) ──
        _store_dir = Path(args.feature_store_dir)
        if not _store_dir.is_absolute():
            _store_dir = Path.cwd() / _store_dir
        feature_store = LocalFeatureStore(str(_store_dir))
        feature_schema = build_v9_schema(symbol=args.symbol)
        feature_store.register_schema(feature_schema)
        feature_store_disabled = args.disable_feature_store

        # ── Initialize FeatureService (Fix 2: hierarchical store→live→stub resolution) ──
        feature_service = FeatureService(
            feature_adapter=feature_adapter,
            feature_computer=feature_computer,
            default_venue="MT5",
            feature_store=feature_store,
            default_symbol=args.symbol,
            store_schema_name="v9_institutional",
            store_timeframe="M1",
        )
    else:
        feature_store_disabled = True

    # ── Initialize brain adapter(s) ──
    multi_brain = args.multi_brain
    brains: list[dict[str, Any]] = []  # list of {"brain_id": str, "adapter": brain}
    parliament: Any = None

    if multi_brain:
        entries = _load_brain_entries_from_dir(args.brains_dir)
        from core.brains.services.brain_factory import BrainFactory
        from core.parliament.parliament_service import ParliamentService

        factory = BrainFactory()
        for entry in entries:
            try:
                b = factory.build(entry)
                brains.append({"brain_id": entry.get("brain_id", "unknown"), "adapter": b})
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "event": "brain_build_skip",
                            "brain_id": entry.get("brain_id", "unknown"),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        if not brains:
            print(json.dumps({"error": "no_brains_loaded", "dir": args.brains_dir}, indent=2))
            if mt5 is not None:
                mt5.shutdown()
            return 2
        parliament = ParliamentService()
    else:
        if args.brain_type == "onnx_v9" and not args.no_mt5:
            from core.brains.adapters.v9_onnx_brain_adapter import V9OnnxBrainAdapter

            brain = V9OnnxBrainAdapter(brain_entry, feature_adapter=feature_adapter)
            brain.load()
        else:
            from core.brains.services.brain_factory import BrainFactory

            brain = BrainFactory().build(brain_entry)

    last_fire = 0.0
    flag_notice = False

    start_event: dict[str, Any] = {
        "event": "live_intent_loop_start",
        "time": _utc_iso(),
        "symbol": args.symbol,
        "confidence_threshold": args.confidence_threshold,
        "interval_seconds": args.interval_seconds,
        "volume": args.volume,
    }
    if multi_brain:
        start_event["mode"] = "multi_brain"
        start_event["brain_count"] = len(brains)
        start_event["brain_ids"] = [b["brain_id"] for b in brains]
    else:
        start_event["backend"] = brain.describe()["backend"]
        start_event["brain_id"] = brain_entry.get("brain_id", "unknown")
    print(json.dumps(start_event, ensure_ascii=False), flush=True)

    try:
        while True:
            try:
                # ── Protection flag check ──
                flag_path = resolve_protection_flag_path(args.base_dir, args.protection_flag_path)
                if flag_path.exists() and not args.ignore_protection_flag:
                    if not flag_notice:
                        print(
                            json.dumps(
                                {
                                    "event": "protection_skip",
                                    "time": _utc_iso(),
                                    "flag": str(flag_path),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        flag_notice = True
                    time.sleep(args.interval_seconds)
                    continue
                flag_notice = False

                # ── Cooldown check ──
                now = time.monotonic()
                if cooldown_blocks_fire(now, last_fire, args.cooldown_seconds):
                    time.sleep(args.interval_seconds)
                    continue

                # ── Position limit check (skip if --no-mt5) ──
                if not args.no_mt5 and _position_count(mt5, args.symbol) >= args.max_positions:
                    time.sleep(args.interval_seconds)
                    continue

                # ── Compute features (zero vector if --no-mt5) ──
                if args.no_mt5:
                    import numpy as np

                    feature_vector: Any = np.zeros(40, dtype=np.float64)
                else:
                    trigger = {"symbol": args.symbol, "venue": "MT5"}
                    feature_vector = feature_service.build_feature_vector(trigger)

                # ── Persist features to LocalFeatureStore (skip if --no-mt5) ──
                if not feature_store_disabled and not args.no_mt5:
                    try:
                        for record in produce_from_live_computer(
                            feature_computer, feature_schema, args.symbol
                        ):
                            feature_store.write_records([record])
                    except Exception:
                        pass

                # ── Run inference ──
                raw_output: dict[str, Any] = {}
                proposal: Any = None

                if multi_brain:
                    proposals = []
                    consensus_extra: dict[str, Any] = {}
                    for b_info in brains:
                        try:
                            raw = b_info["adapter"].infer(feature_vector)
                            prop = b_info["adapter"].get_signal(raw)
                            proposals.append(prop)
                        except Exception:
                            pass
                    consensus = parliament._compute_consensus(proposals)
                    direction = consensus.get("aggregated_bias", "neutral")
                    confidence = consensus.get("consensus_score", 0.0)
                    consensus_extra = {
                        "voter_count": consensus.get("voter_count", 0),
                        "majority_ratio": consensus.get("majority_ratio", 0.0),
                        "disagreement_score": consensus.get("disagreement_score", 0.0),
                    }
                else:
                    raw_output = brain.infer(feature_vector)
                    proposal = brain.get_signal(raw_output)
                    direction = proposal.prediction.get("direction_bias", "neutral")
                    confidence = proposal.prediction.get("confidence", 0.0)

                if confidence < args.confidence_threshold or direction == "neutral":
                    # Log low-confidence skip
                    skip_event: dict[str, Any] = {
                        "event": "low_confidence_skip",
                        "time": _utc_iso(),
                        "direction": direction,
                        "confidence": round(confidence, 6),
                        "threshold": args.confidence_threshold,
                    }
                    if multi_brain:
                        skip_event["mode"] = "multi_brain"
                        skip_event.update(consensus_extra)
                    else:
                        skip_event["out_risk"] = round(raw_output.get("out_risk", 0.0), 6)
                        skip_event["out_vol"] = round(raw_output.get("out_vol", 0.0), 6)
                        skip_event["runtime_ms"] = round(raw_output.get("runtime_ms", 0.0), 2)
                        skip_event["backend"] = brain.describe()["backend"]
                    print(json.dumps(skip_event, ensure_ascii=False), flush=True)
                    if args.once:
                        break
                    time.sleep(args.interval_seconds)
                    continue

                side = direction  # "long" or "short"

                if args.no_mt5:
                    # ── Verification-only mode: report consensus, skip dispatch ──
                    verify_event: dict[str, Any] = {
                        "event": "inference_verified",
                        "time": _utc_iso(),
                        "side": side,
                        "confidence": round(confidence, 6),
                        "mode": "no_mt5_dry_run",
                    }
                    if multi_brain:
                        verify_event.update(consensus_extra)
                    print(json.dumps(verify_event, ensure_ascii=False, default=str), flush=True)
                else:
                    # ── Get current prices for SL/TP computation ──
                    mid, bid, ask = _mid_and_prices(mt5, args.symbol)
                    ref_long = ask
                    ref_short = bid

                    # ── Compute SL/TP ──
                    stop_loss, take_profit, ref_for_guard = compute_sl_tp_for_side(
                        side,
                        ref_long=ref_long,
                        ref_short=ref_short,
                        sl_distance=args.sl_distance,
                        tp_distance=args.tp_distance,
                    )

                    _validate_sl_tp(
                        side=side,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        reference_price=ref_for_guard,
                    )

                    # ── Dispatch order ──
                    out = dispatch_live_open_order(
                        base_dir=args.base_dir,
                        mt5_terminal_path=args.mt5_terminal_path,
                        symbol=args.symbol,
                        side=side,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        skip_price_guard=True,
                        ignore_protection_flag=args.ignore_protection_flag,
                        protection_flag_path=args.protection_flag_path,
                        volume=args.volume,
                    )
                    last_fire = now

                    dispatch_event = {
                        "event": "intent_dispatched",
                        "time": _utc_iso(),
                        "mid": mid,
                        "side": side,
                        "confidence": round(confidence, 6),
                        "reference_used": ref_for_guard,
                        "sl": stop_loss,
                        "tp": take_profit,
                        "dispatch": out,
                    }
                    if multi_brain:
                        dispatch_event["mode"] = "multi_brain"
                        dispatch_event.update(consensus_extra)
                    else:
                        dispatch_event["out_risk"] = round(raw_output.get("out_risk", 0.0), 6)
                        dispatch_event["out_vol"] = round(raw_output.get("out_vol", 0.0), 6)
                        dispatch_event["runtime_ms"] = round(raw_output.get("runtime_ms", 0.0), 2)
                        dispatch_event["backend"] = brain.describe()["backend"]

                    print(json.dumps(dispatch_event, ensure_ascii=False, default=str), flush=True)

            except Exception as exc:
                print(
                    json.dumps(
                        {"event": "cycle_error", "time": _utc_iso(), "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

            if args.once:
                break

            time.sleep(args.interval_seconds)
    finally:
        if mt5 is not None:
            mt5.shutdown()  # type: ignore[attr-defined]

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
