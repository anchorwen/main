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

    # ── Exit management ──
    p.add_argument(
        "--disable-exit-management",
        action="store_true",
        help="Disable dynamic exit management; fall back to static SL/TP",
    )
    p.add_argument(
        "--exit-trail-atr-mult",
        type=float,
        default=2.0,
        help="Chandelier trail multiplier in ATR units (default: 2.0)",
    )
    p.add_argument(
        "--exit-trail-atr-mult-low",
        type=float,
        default=1.5,
        help="Trail multiplier in low-volatility regime (default: 1.5)",
    )
    p.add_argument(
        "--exit-trail-atr-mult-high",
        type=float,
        default=3.0,
        help="Trail multiplier in high-volatility regime (default: 3.0)",
    )
    p.add_argument(
        "--exit-breakeven-atr",
        type=float,
        default=1.0,
        help="ATR multiple to trigger breakeven SL move (default: 1.0)",
    )
    p.add_argument(
        "--exit-brain-reeval-interval",
        type=int,
        default=5,
        help="Cycles between brain re-evaluation during management (default: 5)",
    )
    p.add_argument(
        "--exit-flip-threshold",
        type=float,
        default=0.5,
        help="Fraction of supporting brains that must flip to trigger exit (default: 0.5)",
    )
    p.add_argument(
        "--exit-confidence-drop",
        type=float,
        default=0.10,
        help="Drop in consensus score that triggers confidence exit (default: 0.10)",
    )
    p.add_argument(
        "--exit-max-hold-cycles",
        type=int,
        default=60,
        help="Max cycles to hold without min R before time exit (default: 60)",
    )
    p.add_argument(
        "--exit-require-min-r",
        type=float,
        default=0.3,
        help="Minimum R-multiple to avoid time-based exit (default: 0.3)",
    )
    p.add_argument(
        "--config",
        default=None,
        help="Path to live.yaml for strategy_lines config overrides",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── Build LiveCycleConfig from args ──
    # ── Load strategy_lines overrides from live.yaml ──
    strategy_configs: dict[str, Any] = {}
    if args.config:
        try:
            import yaml

            with open(args.config, encoding="utf-8") as fh:
                full_cfg = yaml.safe_load(fh)
            strategy_configs = full_cfg.get("strategy_lines", {})
            if strategy_configs:
                print(
                    json.dumps(
                        {
                            "event": "strategy_configs_loaded",
                            "time": _utc_iso(),
                            "config_path": args.config,
                            "strategies": list(strategy_configs.keys()),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception as exc:
            print(
                json.dumps(
                    {"event": "strategy_configs_load_warning", "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

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
        exit_management_enabled=not args.disable_exit_management,
        exit_trail_atr_mult=args.exit_trail_atr_mult,
        exit_trail_atr_mult_low=args.exit_trail_atr_mult_low,
        exit_trail_atr_mult_high=args.exit_trail_atr_mult_high,
        exit_breakeven_threshold_atr=args.exit_breakeven_atr,
        exit_brain_reeval_interval=args.exit_brain_reeval_interval,
        exit_flip_threshold=args.exit_flip_threshold,
        exit_confidence_drop=args.exit_confidence_drop,
        exit_max_hold_cycles=args.exit_max_hold_cycles,
        exit_require_min_r=args.exit_require_min_r,
        strategy_configs=strategy_configs,
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
    micro_feature_adapter: Any = None
    micro_feature_computer: Any = None

    if not args.no_mt5:
        from core.features.computers.v9_live_computer import V9LiveFeatureComputer

        feature_computer = V9LiveFeatureComputer(mt5, args.symbol)

        from core.features.adapters.v9_feature_adapter import V9FeatureAdapter

        feature_adapter = V9FeatureAdapter(
            rolling_normalizer=rolling_norm,
            normalization_config=norm_config,
        )

        # Microstructure 9-feature computer + adapter (for Transformer V4.3 & XGBoost V4.5)
        from core.features.adapters.microstructure_feature_adapter import (
            MicrostructureFeatureAdapter,
        )
        from core.features.computers.microstructure_computer import (
            MicrostructureFeatureComputer,
        )

        micro_feature_computer = MicrostructureFeatureComputer(mt5, args.symbol)
        micro_feature_adapter = MicrostructureFeatureAdapter(
            scaler_path="data/models/mtx_transformer_scaler.joblib",
        )

        _store_dir = Path(args.feature_store_dir)
        if not _store_dir.is_absolute():
            _store_dir = PROJECT_ROOT / _store_dir
        from core.features.local_feature_store import LocalFeatureStore

        feature_store = LocalFeatureStore(str(_store_dir))
        from core.deployment.feature_update_producer import build_v9_schema
        from core.features.schemas.microstructure_schema import build_microstructure_schema

        feature_schema = build_v9_schema(symbol=args.symbol)
        feature_store.register_schema(feature_schema)
        feature_store.register_schema(build_microstructure_schema(symbol=args.symbol))

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
                        "brain_type": entry.get("brain_type", ""),
                        "magic": entry.get("magic", 90001),
                        "feature_schema_id": entry.get("feature_schema_id", ""),
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

        # ── Warm-start brain buffers from MT5 historical data ──
        if not args.no_mt5 and mt5 is not None:
            for b_info in brains:
                btype = b_info.get("brain_type", "")
                adapter = b_info["adapter"]

                if btype == "ou_params_v6":
                    try:
                        rates = mt5.copy_rates_from_pos(args.symbol, mt5.TIMEFRAME_M5, 0, 300)
                        if rates is not None and len(rates) >= 30:
                            prices = [float(r["close"]) for r in rates]
                            adapter.bootstrap_buffer(prices)
                            print(
                                json.dumps(
                                    {
                                        "event": "ou_buffer_bootstrapped",
                                        "time": _utc_iso(),
                                        "brain_id": b_info["brain_id"],
                                        "prices_loaded": len(prices),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    except Exception:
                        pass

                elif btype == "transformer_v4.3":
                    try:
                        if micro_feature_computer is not None and micro_feature_adapter is not None:
                            micro_feats = micro_feature_computer.compute_all()
                            fv = micro_feature_adapter.build_model_input(micro_feats).ravel()
                            # Fill buffer with current features so model can
                            # produce signals immediately; entries rotate out
                            # as live data arrives over the next 32 cycles.
                            adapter.bootstrap_buffer([fv] * 32)
                            print(
                                json.dumps(
                                    {
                                        "event": "transformer_buffer_bootstrapped",
                                        "time": _utc_iso(),
                                        "brain_id": b_info["brain_id"],
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                    except Exception:
                        pass
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

    # ── Initialize MetaExitEngine (multi-factor exit scoring) ──
    meta_exit_engine: Any = None
    if not args.no_mt5:
        try:
            from core.execution.meta_exit_engine import create_exit_engine

            meta_model = args.meta_exit_model if hasattr(args, "meta_exit_model") else None
            meta_exit_engine = create_exit_engine(
                model_path=meta_model or "data/models/meta_exit_model.txt",
                urgency_threshold=getattr(args, "meta_exit_threshold", 0.65),
            )
        except Exception:
            pass

    # ── Initialize ActivePositionManager with restart recovery ──
    position_manager: Any = None
    _pos_state_path = Path(args.base_dir) / "state" / "active_position.json"
    if not args.disable_exit_management and not args.no_mt5 and _broker is not None:
        from core.execution.position_manager import ActivePositionManager

        position_manager = ActivePositionManager(
            trail_atr_mult=args.exit_trail_atr_mult,
            trail_atr_mult_low=args.exit_trail_atr_mult_low,
            trail_atr_mult_high=args.exit_trail_atr_mult_high,
            breakeven_threshold_atr=args.exit_breakeven_atr,
            brain_reeval_interval=args.exit_brain_reeval_interval,
            flip_exit_threshold=args.exit_flip_threshold,
            confidence_drop_threshold=args.exit_confidence_drop,
            max_hold_cycles=args.exit_max_hold_cycles,
            require_min_r=args.exit_require_min_r,
            pnl_store=pnl_ledger,
            meta_exit_engine=meta_exit_engine,
        )

        # ── Restart recovery: try persisted state first, fall back to MT5 ──
        recovered = False
        try:
            restored = position_manager.load_state(_pos_state_path)
            if restored is not None:
                # Verify the position still exists in MT5
                mt5_positions = mt5.positions_get(ticket=restored.ticket)
                if mt5_positions and len(mt5_positions) > 0:
                    mp = mt5_positions[0]
                    # Sync current SL/TP from MT5 (ground truth)
                    restored.current_sl = float(mp.sl) if mp.sl > 0 else restored.current_sl
                    restored.current_tp = float(mp.tp) if mp.tp > 0 else restored.current_tp
                    # Update price extremes from current (MT5 doesn't track historical highs)
                    restored.highest_high = max(restored.highest_high, float(mp.price_current))
                    restored.lowest_low = min(restored.lowest_low, float(mp.price_current))
                    recovered = True
                    print(
                        json.dumps(
                            {
                                "event": "position_restored_from_state",
                                "time": _utc_iso(),
                                "ticket": restored.ticket,
                                "side": restored.side,
                                "cycles_held": restored.cycles_held,
                                "breakeven_triggered": restored.breakeven_triggered,
                                "trail_multiplier": restored.trail_multiplier,
                                "highest_r": round(restored.highest_r, 4),
                                "current_sl": restored.current_sl,
                                "current_tp": restored.current_tp,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    # Position no longer exists on MT5 — stale state, ignore
                    position_manager.clear_position()
        except Exception:
            pass

        if not recovered:
            # ── Fallback: reconstruct from MT5 (basic recovery, no trail state) ──
            try:
                open_positions = _broker.get_open_positions_detail(args.symbol)
                if open_positions:
                    pos_detail = open_positions[0]
                    ticket = pos_detail.get("ticket", 0)
                    if ticket > 0:
                        mt5_positions = mt5.positions_get(ticket=ticket)
                        if mt5_positions and len(mt5_positions) > 0:
                            mp = mt5_positions[0]
                            side = "long" if mp.type == 0 else "short"
                            entry_price = float(mp.price_open)
                            current_sl_val = float(mp.sl) if mp.sl > 0 else entry_price
                            current_tp_val = float(mp.tp) if mp.tp > 0 else 0.0
                            volume = float(mp.volume)

                            recovered_consensus: dict[str, Any] = {
                                "aggregated_bias": side,
                                "consensus_score": 0.5,
                            }
                            recovered_supporting: list[str] = []

                            if _journal_path.exists():
                                for line in _journal_path.read_text(encoding="utf-8").splitlines():
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        rec = json.loads(line)
                                        if (
                                            rec.get("position_ticket") == ticket
                                            and rec.get("action") == "open"
                                        ):
                                            recovered_supporting = rec.get("brain_ids", [])
                                            break
                                    except Exception:
                                        pass

                            recovery_atr = (
                                _broker.fetch_current_atr(args.symbol)
                                if _broker is not None
                                else 2.31
                            )
                            if recovery_atr <= 0:
                                recovery_atr = 2.31

                            current_high = max(entry_price, float(mp.price_current))
                            min(entry_price, float(mp.price_current))

                            position_manager.register_position(
                                ticket=ticket,
                                side=side,
                                entry_price=entry_price,
                                volume=volume,
                                initial_sl=current_sl_val,
                                initial_tp=current_tp_val,
                                entry_atr=recovery_atr,
                                entry_cycle=0,
                                entry_consensus=recovered_consensus,
                                supporting_brain_ids=recovered_supporting,
                                current_high=current_high,
                            )
                            print(
                                json.dumps(
                                    {
                                        "event": "position_recovered_from_mt5",
                                        "time": _utc_iso(),
                                        "ticket": ticket,
                                        "side": side,
                                        "entry_price": entry_price,
                                        "current_sl": current_sl_val,
                                        "current_tp": current_tp_val,
                                        "volume": volume,
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
            except Exception as _recovery_exc:
                print(
                    json.dumps(
                        {
                            "event": "position_recovery_error",
                            "time": _utc_iso(),
                            "error": str(_recovery_exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    # ── Initialize GroupCorrelationTracker ──
    correlation_tracker: Any = None
    try:
        from core.execution.capital_allocator import GroupCorrelationTracker

        correlation_tracker = GroupCorrelationTracker(ema_alpha=0.05)
    except Exception:
        pass

    # ── Initial state ──
    state = LiveCycleState(
        known_open_tickets=known_open_tickets,
        position_manager=position_manager,
        correlation_tracker=correlation_tracker,
    )

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

    # ── Config hot-reload ──
    hot_reload: Any = None
    _hot_path = (
        Path(args.config) if hasattr(args, "config") and args.config else Path("configs/live.yaml")
    )
    if _hot_path.exists():
        try:
            from core.deployment.config_hot_reload import ConfigHotReload

            hot_reload = ConfigHotReload(str(_hot_path))
            hot_reload.load()
        except Exception:
            pass

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
                    micro_feature_computer=micro_feature_computer,
                    micro_feature_adapter=micro_feature_adapter,
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

            # ── Persist state every ~1 hour + check config hot-reload ──
            state.cycle_count += 1
            if hot_reload is not None and state.loop_iteration % 30 == 0:
                try:
                    changes = hot_reload.check_and_reload()
                    if changes:
                        print(
                            json.dumps(
                                {
                                    "event": "config_hot_reloaded",
                                    "time": _utc_iso(),
                                    "changes": list(changes.keys()),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                except Exception:
                    pass
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
        # ── Graceful shutdown: persist all state ──
        print(
            json.dumps({"event": "shutdown_start", "time": _utc_iso()}, ensure_ascii=False),
            flush=True,
        )

        if position_manager is not None and position_manager.has_position():
            try:
                _pos_path = Path(args.base_dir) / "state" / "active_position.json"
                position_manager.save_state(str(_pos_path))
                print(
                    json.dumps(
                        {
                            "event": "position_state_saved",
                            "time": _utc_iso(),
                            "ticket": position_manager.get_position().ticket
                            if position_manager.get_position()
                            else 0,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception:
                pass

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
        print(
            json.dumps({"event": "shutdown_complete", "time": _utc_iso()}, ensure_ascii=False),
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
