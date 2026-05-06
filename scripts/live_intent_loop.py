"""Periodic live intent producer → mt5_outbox (multi-brain / single-brain pipeline).

Thin CLI + init + main loop shell. The cycle execution logic lives in
core.runtime.live_cycle so it can be tested and reused independently.

Usage:
  python scripts/live_intent_loop.py --mt5-terminal-path "C:\\..." [--multi-brain] [--no-mt5]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.features.rolling_normalizer import RollingNormalizer
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.risk.regime_detector import RegimeDetector
from core.runtime.live_cycle import (
    LiveCycleConfig,
    LiveCycleState,
    _utc_iso,
    compute_sl_tp_for_side,
    cooldown_blocks_fire,
    execute_live_cycle,
)

# Re-export symbols that moved to core.runtime.live_cycle so existing
# test imports (scripts.live_intent_loop.xxx) keep working.
__all__ = [
    "LiveCycleConfig",
    "LiveCycleState",
    "build_parser",
    "compute_sl_tp_for_side",
    "cooldown_blocks_fire",
    "decide_side_from_anchor",
    "execute_live_cycle",
    "load_brain_entry",
    "load_normalization_config",
    "main",
]

# ── Feature engine defaults ──
DEFAULT_NORM_CONFIG = "configs/brains/v9_institutional_01.normalization.json"
DEFAULT_BRAIN_ENTRY = "configs/brains/v9_institutional_01.json"
DEFAULT_FEATURE_STORE_DIR = "data/feature_store"


def decide_side_from_anchor(price: float, anchor: float, threshold: float) -> str | None:
    """Determine trade direction from price vs anchor ± threshold."""
    if price > anchor + threshold:
        return "long"
    if price < anchor - threshold:
        return "short"
    return None


def _resolve_consensus_side(consensus: dict[str, Any], min_confidence: float) -> str | None:
    """Convert ParliamentService consensus dict to trade side."""
    bias = consensus.get("aggregated_bias", "neutral")
    score = consensus.get("consensus_score", 0.0)
    if score < min_confidence or bias == "neutral":
        return None
    if bias in ("long", "short"):
        return bias
    return None


def _bootstrap_regime_detector(
    mt5: Any, symbol: str, detector: Any, *, bootstrap_bars: int = 200
) -> bool:
    """Warm-start regime detector from MT5 historical ATR data."""
    if detector.is_warmed_up and detector.atr_mean > 0.1:
        return True

    try:
        import numpy as np

        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, bootstrap_bars)
        if rates is None or len(rates) < 30:
            return False

        h = np.array([r["high"] for r in rates], dtype=np.float64)
        low = np.array([r["low"] for r in rates], dtype=np.float64)
        c = np.array([r["close"] for r in rates], dtype=np.float64)
        n = len(c)

        atr_period = 14
        atr_values = []
        for i in range(atr_period, n):
            cur_h = h[i - atr_period + 1 : i + 1]
            cur_l = low[i - atr_period + 1 : i + 1]
            prev_c = c[i - atr_period : i]
            tr = np.maximum(
                cur_h - cur_l,
                np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)),
            )
            atr_val = float(np.mean(tr))
            if atr_val > 0.01:
                atr_values.append(atr_val)
                detector.update(atr_val)

        if atr_values and detector.count > 0:
            sample_mean = float(np.mean(atr_values))
            sample_var = float(np.var(atr_values))
            if sample_mean > 0.1 and sample_var > 0.01:
                detector._mean = sample_mean
                detector._var = sample_var - detector._eps

        return detector.is_warmed_up
    except Exception:
        return False


def load_normalization_config(path: str) -> dict[str, Any]:
    """Load normalization config from JSON, resolving relative paths."""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
        if not p.exists():
            p = Path.cwd() / Path(path)
    if not p.exists():
        raise FileNotFoundError(f"normalization config not found: {p}")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def load_brain_entry(path: str) -> dict[str, Any]:
    """Load brain registry entry from JSON, resolving relative paths."""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
        if not p.exists():
            p = Path.cwd() / Path(path)
    if not p.exists():
        raise FileNotFoundError(f"brain entry not found: {p}")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _load_brain_entries_from_dir(brains_dir: str) -> list[dict[str, Any]]:
    """Load all brain registry entry JSON files from a directory."""
    p = Path(brains_dir)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
        if not p.is_dir():
            p = Path.cwd() / Path(brains_dir)
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


def _apply_governance_filter(
    entries: list[dict[str, Any]], base_dir: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter brain entries by governance status and apply weight penalties."""
    report: dict[str, Any] = {
        "governance_loaded": False,
        "total_entries": len(entries),
        "removed": [],
        "penalized": [],
        "kept": [],
    }
    gov_path = Path(base_dir) / "governance_state.json"
    if not gov_path.exists():
        report["reason"] = "no_governance_state"
        return entries, report

    try:
        from core.governance.governance_service import GovernanceService

        gov = GovernanceService.load(gov_path)
        report["governance_loaded"] = True
    except Exception as exc:
        report["reason"] = f"governance_load_failed: {exc}"
        return entries, report

    filtered: list[dict[str, Any]] = []
    for entry in entries:
        brain_id = entry.get("brain_id", "unknown")
        state = gov.get_brain_state(brain_id)

        if state is None:
            filtered.append(entry)
            report["kept"].append(brain_id)
            continue

        status = state.get("status", "candidate")
        if status in ("retired", "frozen"):
            report["removed"].append({"brain_id": brain_id, "status": status})
            print(
                json.dumps(
                    {
                        "event": "brain_governance_skip",
                        "brain_id": brain_id,
                        "status": status,
                        "reason": f"brain is {status}",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        if status == "probation":
            entry = dict(entry)
            original_weight = entry.get("vote_weight", 1.0)
            entry["vote_weight"] = round(original_weight * 0.5, 4)
            entry["_governance_status"] = "probation"
            report["penalized"].append(
                {
                    "brain_id": brain_id,
                    "original_weight": original_weight,
                    "new_weight": entry["vote_weight"],
                }
            )
            print(
                json.dumps(
                    {
                        "event": "brain_governance_penalty",
                        "brain_id": brain_id,
                        "status": status,
                        "vote_weight": entry["vote_weight"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        report["kept"].append(brain_id)
        filtered.append(entry)

    return filtered, report


def _check_single_brain_governance(brain_id: str, base_dir: str) -> dict[str, Any]:
    """Check whether a single brain should be blocked or warned by governance."""
    gov_path = Path(base_dir) / "governance_state.json"
    if not gov_path.exists():
        return {"blocked": False, "warning": False, "reason": "no_governance_state"}

    try:
        from core.governance.governance_service import GovernanceService

        gov = GovernanceService.load(gov_path)
    except Exception as exc:
        return {"blocked": False, "warning": False, "reason": f"governance_load_failed: {exc}"}

    state = gov.get_brain_state(brain_id)
    if state is None:
        return {"blocked": False, "warning": False, "reason": "not_registered"}

    status = state.get("status", "candidate")
    if status in ("retired", "frozen"):
        return {
            "blocked": True,
            "warning": False,
            "status": status,
            "reason": f"brain is {status}",
        }
    if status == "probation":
        return {
            "blocked": False,
            "warning": True,
            "status": status,
            "reason": "brain is on probation — run with reduced weight",
        }

    return {"blocked": False, "warning": False, "status": status}


def _init_risk_service() -> Any:
    """Create RiskEvaluationService with standard live trading policies."""
    from core.risk.risk_evaluation_service import RiskEvaluationService
    from core.risk.risk_policies import (
        ConcentrationPolicy,
        DrawdownPolicy,
        ExposurePolicy,
        ModePolicy,
        PositionLimitPolicy,
    )

    svc = RiskEvaluationService()
    svc.add_policy(DrawdownPolicy(max_drawdown_pct=5.0))
    svc.add_policy(PositionLimitPolicy(max_open_positions=10))
    svc.add_policy(ConcentrationPolicy(max_per_symbol=3))
    svc.add_policy(ExposurePolicy(max_notional=1_000_000.0))
    svc.add_policy(ModePolicy())
    return svc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_intent_loop")
    p.add_argument("--base-dir", default="data")
    p.add_argument("--mt5-terminal-path", required=True)
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--interval-seconds", type=float, default=30.0)
    p.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.50,
        help="Minimum prediction confidence to fire a trade (0.0-1.0)",
    )
    p.add_argument(
        "--sl-atr-mult",
        type=float,
        default=2.0,
        help="SL distance as multiple of M5 ATR(14) (e.g. 2.0 = 2x ATR)",
    )
    p.add_argument(
        "--tp-atr-mult",
        type=float,
        default=3.5,
        help="TP distance as multiple of M5 ATR(14) (e.g. 3.5 = 3.5x ATR)",
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
        help="Skip feature persistence to LocalFeatureStore",
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

    # ── Build LiveCycleConfig from args ──
    config = LiveCycleConfig(
        symbol=args.symbol,
        base_dir=args.base_dir,
        interval_seconds=args.interval_seconds,
        confidence_threshold=args.confidence_threshold,
        cooldown_seconds=args.cooldown_seconds,
        max_positions=args.max_positions,
        sl_atr_mult=args.sl_atr_mult,
        tp_atr_mult=args.tp_atr_mult,
        volume=args.volume,
        no_mt5=args.no_mt5,
        once=args.once,
        ignore_protection_flag=args.ignore_protection_flag,
        protection_flag_path=args.protection_flag_path,
        mt5_terminal_path=args.mt5_terminal_path,
        brain_type=args.brain_type,
        multi_brain=args.multi_brain,
        feature_store_dir=args.feature_store_dir,
        disable_feature_store=args.disable_feature_store,
    )

    # ── Import MT5 ──
    mt5: Any = None
    if not args.no_mt5:
        try:
            import MetaTrader5 as mt5_mod

            mt5 = mt5_mod
        except Exception as exc:
            print(
                json.dumps({"error": "MetaTrader5 package required", "detail": str(exc)}, indent=2)
            )
            return 2

        if not mt5.initialize(path=args.mt5_terminal_path):
            print(
                json.dumps(
                    {"error": "mt5_initialize_failed", "detail": str(mt5.last_error())},
                    indent=2,
                )
            )
            return 2

    # ── Build broker adapter (swap point for future FIX / cloud brokers) ──
    _broker: Any = None
    if not args.no_mt5 and mt5 is not None:
        from core.execution.mt5_broker_adapter import MT5BrokerAdapter

        _broker = MT5BrokerAdapter(mt5)

    # ── Load configs ──
    try:
        norm_config = load_normalization_config(args.normalization_config)
    except Exception as exc:
        print(
            json.dumps({"error": "normalization_config_load_failed", "detail": str(exc)}, indent=2)
        )
        if mt5 is not None:
            mt5.shutdown()
        return 2

    # ── Initialize rolling normalizer ──
    rolling_norm: Any = None
    normalize_enabled = norm_config.get("normalize", True)
    if not args.no_mt5 and normalize_enabled:
        rolling_norm = RollingNormalizer.from_static(
            mean=norm_config["mean"],
            std=norm_config["std"],
            warmup_bars=100,
        )
        _state_path = Path(args.base_dir) / "rolling_norm_state.json"
        if _state_path.exists():
            try:
                rolling_norm.load_state(_state_path)
                print(
                    json.dumps(
                        {
                            "event": "rolling_norm_state_loaded",
                            "time": _utc_iso(),
                            "path": str(_state_path),
                            "count": rolling_norm.count,
                            "warmed_up": rolling_norm.is_warmed_up,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "event": "rolling_norm_state_load_error",
                            "time": _utc_iso(),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    # ── Initialize regime detector ──
    regime_detector: Any = None
    if not args.no_mt5:
        regime_detector = RegimeDetector()
        _regime_path = Path(args.base_dir) / "regime_detector_state.json"
        if _regime_path.exists():
            try:
                regime_detector.load_state(_regime_path)
                print(
                    json.dumps(
                        {
                            "event": "regime_detector_state_loaded",
                            "time": _utc_iso(),
                            "path": str(_regime_path),
                            "count": regime_detector.count,
                            "atr_mean": regime_detector.atr_mean,
                            "atr_std": regime_detector.atr_std,
                            "warmed_up": regime_detector.is_warmed_up,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "event": "regime_detector_state_load_error",
                            "time": _utc_iso(),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        needs_bootstrap = not regime_detector.is_warmed_up or regime_detector.atr_mean < 0.1
        if needs_bootstrap:
            bootstrapped = _bootstrap_regime_detector(mt5, args.symbol, regime_detector)
            print(
                json.dumps(
                    {
                        "event": "regime_detector_bootstrap",
                        "time": _utc_iso(),
                        "bootstrapped": bootstrapped,
                        "warmed_up": regime_detector.is_warmed_up,
                        "count": regime_detector.count,
                        "atr_mean": round(regime_detector.atr_mean, 4),
                        "atr_std": round(regime_detector.atr_std, 4),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    try:
        brain_entry = load_brain_entry(args.brain_entry)
    except Exception as exc:
        print(json.dumps({"error": "brain_entry_load_failed", "detail": str(exc)}, indent=2))
        if mt5 is not None:
            mt5.shutdown()
        return 2

    config.brain_entry = brain_entry

    # Apply overrides
    if args.onnx_artifact:
        brain_entry["artifact_path"] = args.onnx_artifact
    if args.disable_onnx:
        brain_entry["enable_onnxruntime"] = False

    # ── Initialize feature services ──
    feature_adapter: Any = None
    feature_service: Any = None
    feature_computer: Any = None
    feature_schema: Any = None
    feature_store: Any = None

    if not args.no_mt5:
        from core.features.computers.v9_live_computer import V9LiveFeatureComputer

        feature_computer = V9LiveFeatureComputer(mt5, args.symbol)

        from core.features.adapters.v9_feature_adapter import V9FeatureAdapter

        feature_adapter = V9FeatureAdapter(
            rolling_normalizer=rolling_norm,
            normalization_config=norm_config,
        )

        _store_dir = Path(args.feature_store_dir)
        if not _store_dir.is_absolute():
            _store_dir = PROJECT_ROOT / _store_dir
        from core.features.local_feature_store import LocalFeatureStore

        feature_store = LocalFeatureStore(str(_store_dir))
        from core.deployment.feature_update_producer import build_v9_schema

        feature_schema = build_v9_schema(symbol=args.symbol)
        feature_store.register_schema(feature_schema)

        from core.features.feature_service import FeatureService

        feature_service = FeatureService(
            feature_adapter=feature_adapter,
            feature_computer=feature_computer,
            default_venue="MT5",
            feature_store=feature_store,
            default_symbol=args.symbol,
            store_schema_name="v9_institutional_40",
            store_timeframe="M5",
        )

    # ── Initialize risk service ──
    risk_service = _init_risk_service()

    # ── Initialize performance tracker ──
    tracker_path = Path(args.base_dir) / "brain_performance.json"
    if tracker_path.exists():
        try:
            tracker = BrainPerformanceTracker.load(tracker_path)
            print(
                json.dumps(
                    {
                        "event": "brain_performance_loaded",
                        "time": _utc_iso(),
                        "path": str(tracker_path),
                        "brain_count": len(tracker.get_brain_ids()),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception:
            tracker = BrainPerformanceTracker(window_size=100)
    else:
        tracker = BrainPerformanceTracker(window_size=100)

    # ── Initialize P&L ledger ──
    from core.feedback.brain_pnl_ledger import BrainPnLStore

    pnl_ledger_path = Path(args.base_dir) / "brain_pnl_ledger.json"
    pnl_ledger: Any = None
    if args.multi_brain:
        try:
            pnl_ledger = BrainPnLStore.load(pnl_ledger_path)
            print(
                json.dumps(
                    {
                        "event": "pnl_ledger_loaded",
                        "time": _utc_iso(),
                        "settled_count": pnl_ledger.total_settled,
                        "pending_count": pnl_ledger.pending_count,
                        "brain_ids": pnl_ledger.brain_ids,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception:
            pnl_ledger = BrainPnLStore(window_size=100)
    else:
        pnl_ledger = BrainPnLStore(window_size=100)

    # ── Load open positions from journal ──
    _journal_path = Path(args.base_dir) / "live_trade_journal.jsonl"
    known_open_tickets: dict[int, dict[str, Any]] = {}
    if _journal_path.exists():
        try:
            for line in _journal_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("action") == "open" and rec.get("ack_status") == "accepted":
                    ticket = rec.get("position_ticket")
                    if ticket is not None and isinstance(ticket, int) and ticket > 0:
                        known_open_tickets[ticket] = rec
        except Exception:
            pass

    # ── Initialize brain adapter(s) ──
    brains: list[dict[str, Any]] = []
    parliament: Any = None

    if args.multi_brain:
        entries = _load_brain_entries_from_dir(args.brains_dir)
        entries, gov_report = _apply_governance_filter(entries, args.base_dir)

        from core.brains.services.brain_factory import BrainFactory
        from core.parliament.parliament_service import ParliamentService

        factory = BrainFactory()
        for entry in entries:
            try:
                b = factory.build(entry)
                brains.append(
                    {
                        "brain_id": entry.get("brain_id", "unknown"),
                        "adapter": b,
                        "magic": entry.get("magic", 90001),
                    }
                )
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
        # Single-brain mode: check governance
        brain_id = brain_entry.get("brain_id", "unknown")
        gov_check = _check_single_brain_governance(brain_id, args.base_dir)
        if gov_check.get("blocked"):
            print(json.dumps(gov_check, ensure_ascii=False), flush=True)
            if mt5 is not None:
                mt5.shutdown()
            return 2
        if gov_check.get("warning"):
            print(json.dumps(gov_check, ensure_ascii=False), flush=True)

        if args.brain_type == "onnx_v9" and not args.no_mt5:
            from core.brains.adapters.v9_onnx_brain_adapter import V9OnnxBrainAdapter

            brain = V9OnnxBrainAdapter(brain_entry, feature_adapter=feature_adapter)
            brain.load()
        else:
            from core.brains.services.brain_factory import BrainFactory

            brain = BrainFactory().build(brain_entry)

        brains.append(
            {
                "brain_id": brain_entry.get("brain_id", "unknown"),
                "adapter": brain,
                "magic": brain_entry.get("magic", 90001),
            }
        )

    # ── Initial state ──
    state = LiveCycleState(known_open_tickets=known_open_tickets)

    # ── Start event ──
    start_event: dict[str, Any] = {
        "event": "live_intent_loop_start",
        "time": _utc_iso(),
        "symbol": args.symbol,
        "confidence_threshold": args.confidence_threshold,
        "interval_seconds": args.interval_seconds,
        "volume": args.volume,
    }
    if args.multi_brain:
        start_event["mode"] = "multi_brain"
        start_event["brain_count"] = len(brains)
        start_event["brain_ids"] = [b["brain_id"] for b in brains]
        start_event["governance_filter"] = gov_report
    else:
        start_event["backend"] = brain.describe()["backend"]
        start_event["brain_id"] = brain_entry.get("brain_id", "unknown")
    print(json.dumps(start_event, ensure_ascii=False), flush=True)

    # ── Main loop ──
    try:
        while True:
            try:
                state, should_continue = execute_live_cycle(
                    config,
                    state,
                    mt5=mt5,
                    broker=_broker,
                    feature_service=feature_service,
                    feature_computer=feature_computer,
                    feature_schema=feature_schema,
                    feature_store=feature_store,
                    brains=brains,
                    parliament=parliament,
                    risk_service=risk_service,
                    regime_detector=regime_detector,
                    tracker=tracker,
                    rolling_norm=rolling_norm,
                    feature_adapter=feature_adapter,
                    journal_path=_journal_path,
                    pnl_ledger=pnl_ledger,
                )
                if not should_continue:
                    break
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

            # ── Persist normalizer + regime detector state every ~1 hour ──
            state.cycle_count += 1
            if state.loop_iteration % config.state_save_interval == 0:
                if rolling_norm is not None:
                    try:
                        _state_path = Path(args.base_dir) / "rolling_norm_state.json"
                        rolling_norm.save_state(_state_path)
                    except Exception:
                        pass
                if regime_detector is not None:
                    try:
                        _regime_path = Path(args.base_dir) / "regime_detector_state.json"
                        regime_detector.save_state(_regime_path)
                    except Exception:
                        pass
                if pnl_ledger is not None:
                    try:
                        pnl_ledger.save(pnl_ledger_path)
                    except Exception:
                        pass

            time.sleep(args.interval_seconds)
    finally:
        # ── Persist state on shutdown ──
        if rolling_norm is not None:
            try:
                _state_path = Path(args.base_dir) / "rolling_norm_state.json"
                rolling_norm.save_state(_state_path)
            except Exception:
                pass
        if regime_detector is not None:
            try:
                _regime_path = Path(args.base_dir) / "regime_detector_state.json"
                regime_detector.save_state(_regime_path)
            except Exception:
                pass
        try:
            save_path = Path(args.base_dir) / "brain_performance.json"
            tracker.save(save_path)
        except Exception as exc:
            print(
                json.dumps(
                    {"event": "tracker_save_error", "time": _utc_iso(), "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if pnl_ledger is not None:
            try:
                pnl_ledger.save(pnl_ledger_path)
                print(
                    json.dumps(
                        {
                            "event": "pnl_ledger_saved",
                            "time": _utc_iso(),
                            "settled_count": pnl_ledger.total_settled,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:
                print(
                    json.dumps(
                        {"event": "pnl_ledger_save_error", "time": _utc_iso(), "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        if mt5 is not None:
            mt5.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
