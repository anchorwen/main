"""Periodic live intent producer → mt5_outbox (multi-brain / single-brain pipeline).

Thin CLI + init + main loop shell. The cycle execution logic lives in
core.runtime.live_cycle so it can be tested and reused independently.

Usage:
  python scripts/live_intent_loop.py --mt5-terminal-path "C:\\..." [--multi-brain] [--no-mt5]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config.asset_registry import get_asset
from core.features.rolling_normalizer import RollingNormalizer
from core.feedback.brain_performance_tracker import BrainPerformanceTracker
from core.observability.meta_wire_events import record_wired_event
from core.risk.regime_detector import RegimeDetector
from core.runtime.fault_handler import FaultLevel, FaultTolerantContext, fail_open_guard
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

# ── Feature engine defaults (single source: core.deployment.path_defaults) ──
from core.deployment.path_defaults import (
    DEFAULT_BRAIN_ENTRY,
    DEFAULT_FEATURE_STORE_DIR,
    DEFAULT_NORM_CONFIG,
)

# ── Delegated to core.runtime (Strangler Fig #9, #19) ──
from core.runtime.live_bootstrap import init_feature_services
from core.runtime.live_startup import (  # noqa: F401 — re-export
    _resolve_consensus_side,
    decide_side_from_anchor,
    load_brain_entry,
    load_normalization_config,
)
from core.runtime.live_startup import (
    apply_governance_filter as _apply_governance_filter,
)
from core.runtime.live_startup import (
    bootstrap_regime_detector as _bootstrap_regime_detector,
)
from core.runtime.live_startup import (
    check_single_brain_governance as _check_single_brain_governance,
)
from core.runtime.live_startup import (
    init_risk_service as _init_risk_service,
)
from core.runtime.live_startup import (
    inject_performance_metrics as _inject_performance_metrics,
)
from core.runtime.live_startup import (
    load_brain_entries_from_dir as _load_brain_entries_from_dir,
)


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
        "--exit-trail-activation-atr",
        type=float,
        default=1.0,
        help="Trail only activates after unrealized profit exceeds this many ATRs (default: 1.0, FIX-20260609-004)",
    )
    p.add_argument(
        "--exit-brain-reeval-interval",
        type=int,
        default=1,
        help="Cycles between brain re-evaluation during management (default: 1 — every cycle)",
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
    p.add_argument(
        "--bar-sync",
        action="store_true",
        help="Enable event-driven bar sync (waits for real M5 bar instead of blind sleep)",
    )
    p.add_argument(
        "--bar-sync-timeout",
        type=float,
        default=360.0,
        help="Max seconds to wait for new bar before fallback (dynamic floor: max(360, bar_period_s×1.5) — M5=450s, H1=5400s))",
    )
    p.add_argument(
        "--use-limit-orders",
        action="store_true",
        help="Use passive limit orders instead of market orders for entries (K1 strategy)",
    )
    p.add_argument(
        "--use-exit-watchdog",
        action="store_true",
        help="Wrap all exit dispatches with heartbeat watchdog (retry + escalation)",
    )
    p.add_argument(
        "--alert",
        action="store_true",
        help="Enable LiveAlertHub (rules→circuit breaker→Slack/DingTalk/Log pipeline)",
    )
    p.add_argument(
        "--slack-webhook",
        default="",
        help="Slack incoming webhook URL (overrides QUANTOS_SLACK_WEBHOOK_URL env var)",
    )
    p.add_argument(
        "--dingtalk-webhook",
        default="",
        help="DingTalk incoming webhook URL (overrides QUANTOS_DINGTALK_WEBHOOK_URL env var)",
    )
    p.add_argument(
        "--dingtalk-secret",
        default="",
        help="DingTalk HMAC-SHA256 signing secret (overrides QUANTOS_DINGTALK_SECRET env var)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── Build LiveCycleConfig from args ──
    # ── Load strategy_lines and live_trading overrides from live.yaml ──
    strategy_configs: dict[str, Any] = {}
    _yaml_volume: float | None = None
    _yaml_risk_budget: float | None = None
    _yaml_equity_risk_pct: float | None = None
    _yaml_market_type: str = "forex_24_5"  # FIX-082: read from live.yaml
    _yaml_regime_map: dict[str, dict[str, str]] | None = None
    _yaml_adapter_name: str = "mt5"  # FIX-20260613-059: ZMQ vs file transport
    _yaml_zmq_order: str = ""  # FIX-20260613-059c: ZMQ endpoint routing
    _yaml_zmq_ack: str = ""
    _yaml_portfolio_max_net: float | None = None  # FIX-20260601-037
    _yaml_portfolio_max_gross: float | None = None
    _yaml_blocked_hours: list[int] = []  # FIX-20260629-188 (P1-3)
    # ── FIX-20260605-120: reentry thresholds ──
    _reentry_cfg: dict[str, Any] = {}
    # ── FIX-20260729-001: Default to live.yaml when --config not explicitly passed ──
    # Without this, risk_budget_usd falls back to hardcoded 10.0 → all positions 0.01.
    if not args.config:
        _default_cfg = "configs/live.yaml"
        if os.path.exists(_default_cfg):
            args.config = _default_cfg
    if args.config:
        try:
            with open(args.config, encoding="utf-8") as fh:
                full_cfg = yaml.safe_load(fh)
            import yaml as _yaml

            if full_cfg is None:
                import sys as _sys

                print(
                    json.dumps(
                        {
                            "event": "fatal_config_parse",
                            "error": "YAML parsed to None — file may be empty or contain only comments",
                        }
                    )
                )
                _sys.exit(1)
            if not isinstance(full_cfg, dict):
                import sys as _sys

                print(
                    json.dumps(
                        {
                            "event": "fatal_config_parse",
                            "error": f"YAML parsed to {type(full_cfg).__name__}, expected dict",
                        }
                    )
                )
                _sys.exit(1)
            # ── FIX-20260715-018: Bootstrap magic↔strategy mappings from YAML ──
            # The module-level auto-init only loads hardcoded fallback entries.
            # Multi-TF BTC strategies (btc_swing_h4/m30/h1_v2/m15) are only
            # registered in live_btc.yaml, not in _HARDCODED_FALLBACK.  Without
            # this explicit init, STRATEGY_TO_MAGIC.get("btc_swing_h4") → 0,
            # causing modify_sltp/close dispatches to use magic 90401 (sentinel)
            # and journal entries to show "__UNATTRIBUTED_BRIDGE_DEFAULT__".
            try:
                from core.contracts.strategy_magic import init_magic_mappings

                init_magic_mappings(args.config)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass  # hardcoded fallback already in place; FIX-018 bootstrap guard
            strategy_configs = full_cfg.get("strategy_lines", {})
            # ── Regime map: per-strategy discrete hardware guard ──
            _rg_cfg = full_cfg.get("regime_gate", {})
            if isinstance(_rg_cfg, dict):
                _yaml_regime_map = _rg_cfg.get("regime_map")
            # Also read live_trading section for volume/risk wiring (FIX-20260519-009)
            _lt = full_cfg.get("live_trading", {})
            if isinstance(_lt, dict):
                _yaml_volume = _lt.get("volume")
                _yaml_risk_budget = _lt.get("risk_budget_usd")
                _yaml_equity_risk_pct = _lt.get("equity_risk_pct")
                _yaml_market_type = str(_lt.get("market_type", "forex_24_5"))
                # ── FIX-20260629-188 (P1-3): blocked UTC hours from live.yaml ──
                _yaml_blocked_hours = _lt.get("blocked_entry_hours") or []
            # ── Transport adapter: ZMQ vs file dispatch (FIX-20260613-059) ──
            _adapter_cfg = full_cfg.get("adapter", {})
            if isinstance(_adapter_cfg, dict):
                _yaml_adapter_name = str(_adapter_cfg.get("name", "mt5"))
            # ZMQ endpoint routing: each symbol has its own port pair
            _zmq_cfg = full_cfg.get("zmq", {})
            if isinstance(_zmq_cfg, dict):
                _yaml_zmq_order = str(_zmq_cfg.get("order_endpoint", ""))
                _yaml_zmq_ack = str(_zmq_cfg.get("ack_endpoint", ""))
            # ── Portfolio risk limits: per-symbol lot-based exposure (FIX-20260601-037) ──
            _yaml_portfolio_max_net = full_cfg.get("portfolio_max_net")
            _yaml_portfolio_max_gross = full_cfg.get("portfolio_max_gross")
            # ── FIX-20260605-120: reentry thresholds from YAML ──
            _reentry_cfg = full_cfg.get("reentry", {}) if isinstance(full_cfg, dict) else {}
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

            # Auto-scale human-readable exit parameters to M5-bar cycles
            # Must run BEFORE validation so the validator sees scaled values
            if strategy_configs:
                try:
                    from core.runtime.live_cycle import apply_timeframe_scaling

                    apply_timeframe_scaling(strategy_configs)
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                ) as _ts_exc:  # BLE001:FOG
                    print(
                        json.dumps(
                            {
                                "event": "timeframe_scaling_error",
                                "time": _utc_iso(),
                                "error": str(_ts_exc),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            # Validate per-strategy exit configs for unknown keys (RC-09 config drift)
            if strategy_configs:
                try:  # BLE001:FOG (was: FOG/LAC)
                    from core.runtime.strategy_config_validator import (
                        validate_strategy_exit_configs,
                    )

                    exit_warnings = validate_strategy_exit_configs(strategy_configs)
                    if exit_warnings:
                        print(
                            json.dumps(
                                {
                                    "event": "exit_config_validation_warning",
                                    "time": _utc_iso(),
                                    "warnings": exit_warnings,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass

        except (
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            OSError,
            yaml.YAMLError,
        ) as exc:  # BLE001:FOG
            import sys as _sys

            print(
                json.dumps(
                    {"event": "strategy_configs_load_fatal", "error": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            _sys.exit(1)
    # ── Startup integrity check ──
    try:
        from core.deployment.brain_lifecycle_manager import BrainLifecycleManager

        lifecycle = BrainLifecycleManager(
            project_root=PROJECT_ROOT,
            base_dir=args.base_dir,
            brains_dir=str(args.brains_dir) if args.brains_dir else "configs/brains",
            live_yaml_path=args.config
            if hasattr(args, "config") and args.config
            else "configs/live.yaml",
        )
        integrity = lifecycle.verify_startup_integrity(auto_repair=True)
        if (
            integrity.missing_config_files
            or integrity.governance_orphans
            or integrity.hardcoded_path_mismatches
            or integrity.alignment_hard_fails
            or integrity.alignment_warnings
            or integrity.alignment_ensemble_warnings
            or integrity.auto_registered
            or integrity.auto_deleted
            or integrity.contract_violations
        ):
            _event = (
                "startup_integrity_error"
                if integrity.alignment_hard_fails or integrity.contract_violations
                else "startup_integrity_warning"
            )
            print(
                json.dumps(
                    {
                        "event": _event,
                        "time": _utc_iso(),
                        "missing_config_files": integrity.missing_config_files,
                        "missing_yaml_entries": integrity.missing_yaml_entries,
                        "missing_artifacts": integrity.missing_artifacts,
                        "governance_orphans": integrity.governance_orphans,
                        "hardcoded_path_mismatches": integrity.hardcoded_path_mismatches,
                        "alignment_hard_fails": integrity.alignment_hard_fails,
                        "alignment_warnings": integrity.alignment_warnings,
                        "alignment_ensemble_warnings": integrity.alignment_ensemble_warnings,
                        "auto_registered": integrity.auto_registered,
                        "auto_deleted": integrity.auto_deleted,
                        "contract_violations": integrity.contract_violations,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            print(
                json.dumps(
                    {
                        "event": "brain_live_alignment_ok",
                        "time": _utc_iso(),
                        "strategies_checked": len(strategy_configs),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
        print(
            json.dumps(
                {"event": "startup_integrity_error", "error": str(exc)},
                ensure_ascii=False,
            ),
            flush=True,
        )
    _position_state_path = str(Path(args.base_dir) / "state" / "active_position.json")
    config = LiveCycleConfig(
        symbol=args.symbol,
        base_dir=args.base_dir,
        position_state_path=_position_state_path,
        interval_seconds=args.interval_seconds,
        confidence_threshold=args.confidence_threshold,
        cooldown_seconds=args.cooldown_seconds,
        max_positions=args.max_positions,
        sl_atr_mult=args.sl_atr_mult,
        tp_atr_mult=args.tp_atr_mult,
        volume=_yaml_volume if _yaml_volume is not None else args.volume,
        risk_budget_usd=_yaml_risk_budget if _yaml_risk_budget is not None else 10.0,
        equity_risk_pct=_yaml_equity_risk_pct if _yaml_equity_risk_pct is not None else 0.0,
        adapter_name=_yaml_adapter_name,
        zmq_order_endpoint=_yaml_zmq_order,
        zmq_ack_endpoint=_yaml_zmq_ack,
        market_type=_yaml_market_type,
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
        regime_map=_yaml_regime_map,
        contract_size=get_asset(args.symbol).contract_size,
        portfolio_max_net=(
            _yaml_portfolio_max_net if _yaml_portfolio_max_net is not None else 0.05
        ),
        portfolio_max_gross=(
            _yaml_portfolio_max_gross if _yaml_portfolio_max_gross is not None else 0.10
        ),
        # ── FIX-20260605-120: per-asset reentry thresholds from YAML ──
        reentry_sl_cooldown=float(_reentry_cfg.get("sl_cooldown_seconds", 180)),
        reentry_sl_penalty=float(_reentry_cfg.get("sl_confidence_penalty", 0.10)),
        reentry_bleed_cooldown=float(_reentry_cfg.get("bleed_cooldown_seconds", 180)),
        reentry_bleed_penalty=float(_reentry_cfg.get("bleed_confidence_penalty", 0.10)),
        # ── FIX-20260629-188 (P1-3): blocked UTC hours ──
        blocked_entry_hours=_yaml_blocked_hours,
    )

    # ── Initialize MT5Worker (single-threaded engine — all MT5 calls on one thread) ──
    mt5: Any = None
    mt5_worker: Any = None
    if not args.no_mt5:
        from core.execution.mt5_worker import MT5Worker, set_mt5_worker

        mt5_worker = MT5Worker(symbol=args.symbol)
        if not mt5_worker.start(terminal_path=args.mt5_terminal_path):
            print(
                json.dumps(
                    {
                        "error": "mt5_worker_start_failed",
                        "detail": "MT5Worker could not initialize",
                    },
                    indent=2,
                )
            )
            return 2
        set_mt5_worker(mt5_worker)

    # ── Build broker adapter (swap point for future FIX / cloud brokers) ──
    _broker: Any = None
    if not args.no_mt5 and mt5_worker is not None:
        from core.execution.mt5_broker_adapter import MT5BrokerAdapter

        _broker = MT5BrokerAdapter(mt5_worker)

    # ── Load configs ──
    try:
        norm_config = load_normalization_config(args.normalization_config)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
        print(
            json.dumps({"error": "normalization_config_load_failed", "detail": str(exc)}, indent=2)
        )
        if mt5_worker is not None:
            mt5_worker.stop()
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
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
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
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
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
            bootstrapped = _bootstrap_regime_detector(mt5_worker, args.symbol, regime_detector)
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

    if not args.multi_brain:
        try:
            brain_entry = load_brain_entry(args.brain_entry)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            raise
            print(json.dumps({"error": "brain_entry_load_failed", "detail": str(exc)}, indent=2))
            if mt5_worker is not None:
                mt5_worker.stop()
            return 2
        config.brain_entry = brain_entry

    # Apply overrides
    if args.onnx_artifact:
        brain_entry["artifact_path"] = args.onnx_artifact
    if args.disable_onnx:
        brain_entry["enable_onnxruntime"] = False

    # ── Initialize feature services (Strangler Fig #19 → live_bootstrap.py) ──
    _feat = init_feature_services(
        mt5=mt5,
        mt5_worker=mt5_worker,
        symbol=args.symbol,
        feature_store_dir=args.feature_store_dir,
        rolling_norm=rolling_norm,
        norm_config=norm_config,
        project_root=PROJECT_ROOT,
        no_mt5=args.no_mt5,
    )
    feature_adapter = _feat["feature_adapter"]
    feature_service = _feat["feature_service"]
    feature_computer = _feat["feature_computer"]
    feature_schema = _feat["feature_schema"]
    feature_store = _feat["feature_store"]
    micro_feature_adapter = _feat["micro_feature_adapter"]
    micro_feature_computer = _feat["micro_feature_computer"]
    daily_feature_provider = _feat["daily_feature_provider"]

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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            tracker = BrainPerformanceTracker(window_size=100)
    else:
        tracker = BrainPerformanceTracker(window_size=100)

    # ── Initialize P&L ledger ──
    from core.data.event_writer import get_event_writer
    from core.feedback.brain_pnl_ledger import BrainPnLStore

    # ── FIX-20260611-021: Event Sourcing — activate dual-write ──
    _event_writer = get_event_writer(args.base_dir)

    pnl_ledger_path = Path(args.base_dir) / "brain_pnl_ledger.json"
    _stream_path = Path(args.base_dir) / "ledger_events.jsonl"
    pnl_ledger: Any = None

    # ── FIX-20260611-022: Event stream is primary recovery source ──
    # Try load_from_stream() first (immutable, crash-safe).
    # Fall back to old JSON load() for backward compat.
    _loaded_from = "none"
    if _stream_path.exists():
        try:  # BLE001:FOG (was: FOG/LAC)
            pnl_ledger = BrainPnLStore.load_from_stream(_stream_path, event_writer=_event_writer)
            _loaded_from = "event_stream"
            # ── FIX-20260628-169: Sync JSON cache from immutable event stream ──
            # On successful stream load, immediately persist the reconstructed
            # state to JSON so the cache never drifts from the SSOT.
            # Prevents the "stale JSON overrides stream" loop on restart.
            try:
                pnl_ledger.save(pnl_ledger_path)
            except (OSError, ValueError, TypeError):
                pass
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

    if pnl_ledger is None and pnl_ledger_path.exists():
        try:  # BLE001:FOG (was: FOG/LAC)
            pnl_ledger = BrainPnLStore.load(pnl_ledger_path, event_writer=_event_writer)
            _loaded_from = "old_json"
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

    if pnl_ledger is None:
        pnl_ledger = BrainPnLStore(window_size=100, event_writer=_event_writer)
        _loaded_from = "fresh"

    print(
        json.dumps(
            {
                "event": "pnl_ledger_loaded",
                "time": _utc_iso(),
                "settled_count": pnl_ledger.total_settled,
                "pending_count": pnl_ledger.pending_count,
                "brain_ids": pnl_ledger.brain_ids,
                "event_writer": "active",
                "loaded_from": _loaded_from,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    # ── Load open positions from journal ──
    # DQAF-20260621-031: STALE_JOURNAL_HYDRA — blind bulk-load of all historical
    # open entries floods reconciliation with MT5 history_deals_get() (~1.6s
    # each).  Fix: two-pass — (1) collect closed tickets, (2) load only open
    # entries that are ≤7 days old and have no matching close.  7-day cutoff
    # aligns with MT5's practical history retention window (broker-dependent).
    _journal_path = Path(args.base_dir) / "live_trade_journal.jsonl"
    # ── FIX-20260628-XXX: JournalGate — orphan prevention for all journal writes ──
    from core.ledger.services.journal_gate import JournalGate

    _journal_gate = JournalGate(_journal_path, policy="quarantine")
    known_open_tickets: dict[int, dict[str, Any]] = {}
    from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

    if _journal_path.exists():
        try:  # BLE001:FOG (was: FOG/LAC)
            _journal_lines = _journal_path.read_text(encoding="utf-8").splitlines()

            # Pass 1: collect closed tickets (close-match filter)
            _closed_tickets: set[int] = set()
            for _line in _journal_lines:
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _rec = json.loads(_line)
                except json.JSONDecodeError:
                    continue
                if _rec.get("action") == "close":
                    _ct = _rec.get("position_ticket")
                    if _ct is not None and isinstance(_ct, int) and _ct > 0:
                        _closed_tickets.add(_ct)

            # Pass 2: load open entries with age + close-match guards
            _now = datetime.now(UTC)
            _max_age_days = 7
            _loaded_count = 0
            _skipped_stale = 0
            _skipped_closed = 0

            for line in _journal_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("action") == "open" and rec.get("ack_status") == "accepted":
                    ticket = rec.get("position_ticket")
                    if ticket is None or not isinstance(ticket, int) or ticket <= 0:
                        continue
                    # Guard 1: skip if already closed
                    if ticket in _closed_tickets:
                        _skipped_closed += 1
                        continue
                    # Guard 2: skip if >7 days old (MT5 history window)
                    _ts_str = rec.get("recorded_at", "")
                    if _ts_str:
                        try:
                            _ts = datetime.fromisoformat(_ts_str.replace("Z", "+00:00"))
                            if (_now - _ts).days > _max_age_days:
                                _skipped_stale += 1
                                continue
                        except (ValueError, TypeError):
                            pass
                    # Enrich with strategy name for management-phase lookup
                    _magic = rec.get("detail", {}).get("request", {}).get("magic", 0)
                    rec["strategy"] = MAGIC_TO_STRATEGY.get(_magic, "")
                    known_open_tickets[ticket] = rec
                    _loaded_count += 1

            print(
                json.dumps(
                    {
                        "event": "journal_open_loaded",
                        "time": _now.isoformat().replace("+00:00", "Z"),
                        "loaded": _loaded_count,
                        "skipped_stale_days_gt": _max_age_days,
                        "skipped_stale": _skipped_stale,
                        "skipped_closed": _skipped_closed,
                        "total_journal_lines": len(_journal_lines),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

    # ── Initialize brain adapter(s) ──
    brains: list[dict[str, Any]] = []
    parliament: Any = None

    if args.multi_brain:
        # ── DQAF-20260615-007: Per-asset BrainRegistry isolation ──
        # BrainRegistry singleton defaults to configs/brains (XAU).  BTC must
        # explicitly initialize the singleton with configs/brains_btc BEFORE
        # any other code calls BrainRegistry.instance().  Without this, BTC
        # process loads XAU brain data → polluted PnL records + wrong horizons.
        from core.brains.brain_registry import BrainRegistry

        BrainRegistry.reset()
        BrainRegistry.instance(str(args.brains_dir))
        entries = _load_brain_entries_from_dir(args.brains_dir)

        # ── Filter disabled brains (live.yaml brain_registry_entries enabled:false) ──
        _disabled_paths: set[str] = set()
        try:  # BLE001:FOG (was: FOG/LAC)
            import yaml as _yaml

            _live_cfg_path = (
                Path(args.config) if args.config else PROJECT_ROOT / "configs" / "live.yaml"
            )
            if _live_cfg_path.exists():
                with open(_live_cfg_path, encoding="utf-8") as _fh:
                    _live_cfg = _yaml.safe_load(_fh)
                for _reg_entry in (_live_cfg.get("brains") or {}).get("registry_entries", []) or []:
                    if not _reg_entry.get("enabled", True):
                        _rp = Path(_reg_entry["path"])
                        if not _rp.is_absolute():
                            _rp = (PROJECT_ROOT / _rp).resolve()
                        _disabled_paths.add(str(_rp.resolve()))
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

        if _disabled_paths:
            _before = len(entries)
            entries = [e for e in entries if e.get("_source_path", "") not in _disabled_paths]
            print(
                json.dumps(
                    {
                        "event": "disabled_brains_filtered",
                        "time": _utc_iso(),
                        "before_count": _before,
                        "after_count": len(entries),
                        "disabled_paths": sorted(_disabled_paths),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ── Per-brain schema validation (Phase 1a) ──
        try:
            from core.deployment.startup_validator import validate_per_brain_schema
            from core.features.local_feature_store import LocalFeatureStore

            _store = LocalFeatureStore(base_dir=args.base_dir)
            _validation = validate_per_brain_schema(entries, _store)
            if _validation["dropped"]:
                _dropped_ids = {d["brain_id"] for d in _validation["dropped"]}
                entries = [e for e in entries if e.get("brain_id") not in _dropped_ids]
                print(
                    json.dumps(
                        {
                            "event": "schema_validation_dropped",
                            "time": _utc_iso(),
                            "dropped": _validation["dropped"],
                            "ok_count": len(_validation["ok"]),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if _validation["ok"]:
                print(
                    json.dumps(
                        {
                            "event": "schema_validation_ok",
                            "time": _utc_iso(),
                            "brains_ok": len(_validation["ok"]),
                            "brains_total": len(entries),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _vex:  # BLE001:FOG
            print(
                json.dumps(
                    {
                        "event": "schema_validation_error",
                        "time": _utc_iso(),
                        "error": str(_vex),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
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
                        "training_contract": entry.get("training_contract", ""),
                        "hmre_layer": entry.get("hmre_layer"),
                        "contract_group": entry.get("contract_group", ""),
                        "training_horizon": entry.get("training_horizon", 12),
                        "feature_schema": entry.get("feature_schema", ""),
                        "status": entry.get("status", ""),
                        "features": entry.get("features"),
                        "normalization_config_path": entry.get("normalization_config_path"),
                    }
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
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
            if mt5_worker is not None:
                mt5_worker.stop()
            return 2
        parliament = ParliamentService()

        # ── Warm-start brain buffers from MT5 historical data ──
        # DEFERRED: scheduled after main loop start to prevent MT5 API hangs
        # from blocking the entire engine during initialization (FIX-20260522-005).
        # Warm-start is an optimization — brains function normally without it,
        # they just need a few bars of live data to fill internal buffers.
        _warm_start_pending: list[dict[str, Any]] = []
        if not args.no_mt5 and mt5_worker is not None:
            for b_info in brains:
                btype = b_info.get("brain_type", "")
                if btype in ("ou_params_v6", "transformer_v4.3"):
                    _warm_start_pending.append(dict(b_info))
        if _warm_start_pending:
            print(
                json.dumps(
                    {
                        "event": "warm_start_deferred",
                        "time": _utc_iso(),
                        "brain_count": len(_warm_start_pending),
                        "brain_ids": [b["brain_id"] for b in _warm_start_pending],
                        "reason": "MT5 warm-start moved to background to prevent startup deadlock",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    else:
        # Single-brain mode: check governance
        brain_id = brain_entry.get("brain_id", "unknown")
        gov_check = _check_single_brain_governance(brain_id, args.base_dir)
        if gov_check.get("blocked"):
            print(json.dumps(gov_check, ensure_ascii=False), flush=True)
            if mt5_worker is not None:
                mt5_worker.stop()
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
                "brain_type": brain_entry.get("brain_type", ""),
                "training_contract": brain_entry.get("training_contract", ""),
            }
        )

    # ── Initialize MetaExitEngine (multi-factor exit scoring) ──
    meta_exit_engine: Any = None
    if not args.no_mt5:
        try:
            from core.execution.meta_exit_engine import create_exit_engine

            meta_model = args.meta_exit_model if hasattr(args, "meta_exit_model") else None
            if not meta_model:
                # FIX-20260821-008 (The Shadow Deployment — CROSS_ASSET_CONTAMINATION_AUDIT H2):
                # The pre-fix hardcode always loaded XAU's exit model, even in the BTC
                # process ("BTC exits evaluated with XAU exit model"). Load the per-asset
                # 19-dim v3 retrain instead — same convention as the --symbol fallback
                # below (base_dir contains "btc" ⇒ BTC asset).
                from core.deployment.path_defaults import (
                    META_EXIT_MODEL_BTC_PATH,
                    META_EXIT_MODEL_XAU_PATH,
                )

                meta_model = (
                    META_EXIT_MODEL_BTC_PATH
                    if "btc" in str(args.base_dir).lower()
                    else META_EXIT_MODEL_XAU_PATH
                )
            meta_exit_engine = create_exit_engine(
                model_path=meta_model,
                urgency_threshold=getattr(args, "meta_exit_threshold", 0.65),
            )
        except (
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            OSError,
        ) as _meta_exit_exc:  # BLE001:FOG
            print(
                json.dumps(
                    {
                        "event": "meta_exit_engine_load_failed",
                        "time": _utc_iso(),
                        "error": str(_meta_exit_exc),
                        "action": "continuing_without_meta_exit",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    # ── Initialize ActivePositionManager with restart recovery ──
    position_manager: Any = None
    _pos_state_path = Path(args.base_dir) / "state" / "active_position.json"
    if not args.disable_exit_management and not args.no_mt5 and _broker is not None:
        from core.execution.position_manager import ActivePositionManager

        # FIX-20260609-004: Read exit_management from YAML config to override
        # arg defaults.  Previously the entire exit_management section was dead
        # config — never read by any code path.  trail_activation_atr was
        # hardcoded to TrailPolicy default (1.0) ignoring YAML (0.3).
        _yaml_trail_activation_atr = args.exit_trail_activation_atr
        if hasattr(args, "config") and args.config:
            try:  # BLE001:FOG (was: FOG/LAC)
                import yaml as _yaml_exit

                with open(args.config, encoding="utf-8") as _fh_exit:
                    _exit_cfg = _yaml_exit.safe_load(_fh_exit).get("exit_management", {})
                if isinstance(_exit_cfg, dict) and "trail_activation_atr" in _exit_cfg:
                    _yaml_trail_activation_atr = float(_exit_cfg["trail_activation_atr"])
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

        position_manager = ActivePositionManager(
            trail_atr_mult=args.exit_trail_atr_mult,
            trail_atr_mult_low=args.exit_trail_atr_mult_low,
            trail_atr_mult_high=args.exit_trail_atr_mult_high,
            breakeven_threshold_atr=args.exit_breakeven_atr,
            trail_activation_atr=_yaml_trail_activation_atr,  # FIX-20260609-004
            brain_reeval_interval=args.exit_brain_reeval_interval,
            flip_exit_threshold=args.exit_flip_threshold,
            confidence_drop_threshold=args.exit_confidence_drop,
            max_hold_cycles=args.exit_max_hold_cycles,
            require_min_r=args.exit_require_min_r,
            pnl_store=pnl_ledger,
            meta_exit_engine=meta_exit_engine,
        )
        # ── DQAF-20260615-007: Per-asset data isolation ──
        position_manager._data_dir = args.base_dir

        # ── Restart recovery: try persisted state first, fall back to MT5 ──
        recovered = False
        managed_tickets: set[int] = set()
        try:
            restored = position_manager.load_state(_pos_state_path)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            restored = None
        if restored is not None:
            # Verify ALL restored positions still exist on MT5.
            # For v3 SSOT positions, backfill physical-state fields from MT5 ground truth.
            for rt in position_manager.get_all_positions():
                _rt_ticket = rt.ticket
                mt5_positions: Any = []
                with FaultTolerantContext(
                    level=FaultLevel.CRASH, component="MT5_IPC:positions_get:recovery"
                ):
                    mt5_positions = mt5_worker.positions_get(ticket=_rt_ticket)
                if mt5_positions and len(mt5_positions) > 0:
                    mp = mt5_positions[0]
                    managed_tickets.add(_rt_ticket)
                    # Backfill physical-state from MT5 (v3 has side="unknown", entry=0.0)
                    if rt.side == "unknown" or rt.entry_price == 0.0:
                        if rt.side == "unknown":
                            rt.side = "long" if mp.type == 0 else "short"
                        # DQAF-20260621-034 Addendum: entry_price is immutable
                        # after construction.  Only backfill when 0.0 (old V3
                        # save without FIX-018).  Non-zero persisted values are
                        # trusted — MT5 price_open is the ground truth used at
                        # registration time and should match exactly.
                        if rt.entry_price == 0.0:
                            rt._recover_entry_price(float(mp.price_open))
                        rt.volume = float(mp.volume)
                        rt.initial_sl = float(mp.sl) if mp.sl > 0 else float(mp.price_open)
                        rt.initial_tp = float(mp.tp) if mp.tp > 0 else 0.0
                    # Sync current SL/TP from MT5 (ground truth)
                    rt.current_sl = float(mp.sl) if mp.sl > 0 else rt.current_sl
                    rt.current_tp = float(mp.tp) if mp.tp > 0 else rt.current_tp
                    # Update price extremes from current (MT5 doesn't track historical highs)
                    rt.highest_high = max(rt.highest_high, float(mp.price_current))
                    rt.lowest_low = min(rt.lowest_low, float(mp.price_current))
                    recovered = True
                    print(
                        json.dumps(
                            {
                                "event": "position_restored_from_state",
                                "time": _utc_iso(),
                                "ticket": rt.ticket,
                                "side": rt.side,
                                "cycles_held": rt.cycles_held,
                                "breakeven_triggered": rt.breakeven_triggered,
                                "trail_multiplier": rt.trail_multiplier,
                                "highest_r": round(rt.highest_r, 4),
                                "current_sl": rt.current_sl,
                                "current_tp": rt.current_tp,
                                "format_version": "v3" if rt._v3_consensus_hash else "v2",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    # Position no longer exists on MT5 — remove from tracking
                    position_manager.clear_position(ticket=_rt_ticket)
                    # FIX-20260601-036: persist cleanup immediately.
                    # If we crash before the next cycle-end save, the stale
                    # position would be re-loaded on next restart and block
                    # new trades via net_exposure.
                    position_manager.save_state(str(_pos_state_path))
                    print(
                        json.dumps(
                            {
                                "event": "position_startup_cleaned",
                                "time": _utc_iso(),
                                "ticket": _rt_ticket,
                                "note": "Position in state file not found on MT5 — removed from tracking",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

        if not recovered:
            # ── Fallback: reconstruct ALL positions from MT5 (basic recovery, no trail state) ──
            try:
                open_positions = _broker.get_open_positions_detail(args.symbol)
            except (
                RuntimeError,
                ValueError,
                KeyError,
                TypeError,
                OSError,
            ) as _recovery_exc:  # BLE001:FOG
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
                open_positions = None
            if open_positions:
                recovery_atr = (
                    _broker.fetch_current_atr(args.symbol) if _broker is not None else 2.31
                )
                if recovery_atr <= 0:
                    recovery_atr = 2.31

                for pos_detail in open_positions:
                    ticket = pos_detail.get("ticket", 0)
                    if ticket <= 0:
                        continue
                    _recon_positions: Any = []
                    with FaultTolerantContext(
                        level=FaultLevel.CRASH, component="MT5_IPC:positions_get:full_recon"
                    ):
                        _recon_positions = mt5_worker.positions_get(ticket=ticket)
                    if not _recon_positions or len(_recon_positions) == 0:
                        continue
                    mp = _recon_positions[0]
                    managed_tickets.add(ticket)
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
                            try:  # BLE001:FOG (was: FOG/LAC)
                                rec = json.loads(line)
                                if (
                                    rec.get("position_ticket") == ticket
                                    and rec.get("action") == "open"
                                ):
                                    recovered_supporting = rec.get("brain_ids", [])
                                    break
                            except (
                                RuntimeError,
                                ValueError,
                                KeyError,
                                TypeError,
                                OSError,
                            ):  # BLE001:FOG
                                pass

                    current_high = max(entry_price, float(mp.price_current))

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

        # ── FIX-20260613-057: Deterministic MT5 Readiness Barrier ────────
        # Before the post-recovery audit (which calls positions_get), the
        # MT5 bridge must have confirmed its connection is live.  Without
        # this barrier, the intent loop races ahead and crashes because
        # MT5 initialization takes 2-5 seconds while the intent loop starts
        # in <100ms.  This race caused 2401 restarts in 7 days (85% died at
        # cycle=1).  ReB: STARTUP_RACE_POSITIONS_GET_BEFORE_MT5_READY.
        import time as _startup_time

        _health_path = Path(args.base_dir) / "reports" / "mt5_bridge_health.json"
        _barrier_timeout = 60.0  # seconds — generous: covers slow MT5 + auth
        _barrier_start = _startup_time.monotonic()
        _bridge_ready = False
        print(
            json.dumps(
                {
                    "event": "mt5_barrier_waiting",
                    "time": _utc_iso(),
                    "health_path": str(_health_path),
                    "timeout_s": _barrier_timeout,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        while _startup_time.monotonic() - _barrier_start < _barrier_timeout:
            try:
                if _health_path.exists():
                    _hb = json.loads(_health_path.read_text(encoding="utf-8"))
                    if _hb.get("mt5_connected"):
                        # ── FIX-20260613-064: Freshness gate ──
                        # Accept heartbeat only if ≤60s old.  A stale file
                        # with mt5_connected=true (e.g. from a dead bridge PID)
                        # must not satisfy the readiness barrier.
                        _hb_ts = _hb.get("last_heartbeat_utc", "")
                        if _hb_ts:
                            _hb_dt = datetime.fromisoformat(_hb_ts.replace("Z", "+00:00"))
                            _hb_age = (
                                datetime.now(UTC).replace(tzinfo=None) - _hb_dt.replace(tzinfo=None)
                            ).total_seconds()
                            if _hb_age <= 60:
                                _bridge_ready = True
                                break
                            print(
                                json.dumps(
                                    {
                                        "event": "mt5_barrier_stale_heartbeat",
                                        "time": _utc_iso(),
                                        "heartbeat_age_s": round(_hb_age, 1),
                                        "health_path": str(_health_path),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                pass
            _startup_time.sleep(0.5)  # 500ms poll — not time-based wait, event-polling
        if not _bridge_ready:
            # After 60s with no MT5 ready signal, the bridge is genuinely dead.
            # This IS a legitimate CRASH — not a race condition.
            raise RuntimeError(
                f"CRITICAL: MT5 bridge failed to signal readiness within "
                f"{_barrier_timeout}s.  Health file: {_health_path}.  "
                f"Check MT5 terminal, ZMQ bridge, and broker connection."
            )
        _barrier_elapsed = _startup_time.monotonic() - _barrier_start
        print(
            json.dumps(
                {
                    "event": "mt5_barrier_passed",
                    "time": _utc_iso(),
                    "elapsed_s": round(_barrier_elapsed, 1),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # ── Post-recovery audit: detect all MT5 positions and report unmanaged ones ──
        all_mt5_positions: Any = []
        with FaultTolerantContext(
            level=FaultLevel.DEGRADE, component="MT5_IPC:positions_get:post_audit"
        ):
            all_mt5_positions = mt5_worker.positions_get(symbol=args.symbol)
        if all_mt5_positions:
            for mp in all_mt5_positions:
                ticket = mp.ticket
                side = "long" if mp.type == 0 else "short"
                entry_price = float(mp.price_open)
                profit = float(mp.profit)
                sl = float(mp.sl) if mp.sl > 0 else 0.0
                tp = float(mp.tp) if mp.tp > 0 else 0.0
                volume = float(mp.volume)

                if ticket in managed_tickets:
                    continue

                # Unmanaged position — report and validate SL/TP
                no_sl = sl <= 0
                no_tp = tp <= 0
                print(
                    json.dumps(
                        {
                            "event": "position_unmanaged_detected",
                            "time": _utc_iso(),
                            "ticket": ticket,
                            "side": side,
                            "entry_price": entry_price,
                            "volume": volume,
                            "current_sl": sl if sl > 0 else None,
                            "current_tp": tp if tp > 0 else None,
                            "profit": round(profit, 2),
                            "missing_sl": no_sl,
                            "missing_tp": no_tp,
                            "note": "Position tracked by MT5 broker-side SL/TP only — no active trail/exit management",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            # Check for vanished managed positions (tickets we track but MT5 no longer has)
            mt5_ticket_set = {mp.ticket for mp in all_mt5_positions}
            vanished = managed_tickets - mt5_ticket_set
            for vt in vanished:
                position_manager.clear_position(ticket=vt)
                # FIX-20260601-036: persist immediately so a crash/restart
                # doesn't resurrect the vanished position from stale state.
                position_manager.save_state(str(_pos_state_path))
                print(
                    json.dumps(
                        {
                            "event": "position_managed_vanished",
                            "time": _utc_iso(),
                            "ticket": vt,
                            "note": "Managed position no longer on MT5 — may have been closed externally",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    # ── Initialize GroupCorrelationTracker ──
    correlation_tracker: Any = None
    try:  # BLE001:FOG (was: FOG/LAC)
        from core.execution.capital_allocator import GroupCorrelationTracker

        correlation_tracker = GroupCorrelationTracker(ema_alpha=0.05)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        pass

    # ── Initialize MetaSignalFilter (V3 Stage 2: LGB+MLP + Platt + Conformal) ──
    meta_signal_filter: Any = None
    if not args.no_mt5:
        try:
            from core.execution.meta_signal_filter import MetaSignalFilter

            # FIX-20260531-014: resolve MetaFilter config from the correct brain directory
            # (configs/brains/ for XAU, configs/brains_btc/ for BTC).
            # If no MetaFilter config exists for this symbol, meta_signal_filter stays None
            # → p_win falls through to PnL store or neutral default (0.5).
            _brains_dir = (
                Path(args.brains_dir)
                if hasattr(args, "brains_dir") and args.brains_dir
                else (PROJECT_ROOT / "configs" / "brains")
            )
            _filter_cfg_path = Path(_brains_dir) / "meta_stage2_filter_v3.json"
            if _filter_cfg_path.exists():
                with open(_filter_cfg_path, encoding="utf-8") as _fcfh:
                    _fc = json.load(_fcfh)

                # Resolve model paths relative to PROJECT_ROOT
                _lgb_path = _fc.get("model_path", "")
                _resolved_lgb = str(
                    PROJECT_ROOT / _lgb_path
                    if not Path(_lgb_path).is_absolute()
                    else Path(_lgb_path)
                )
                _mlp_path = _fc.get("mlp_model_path", "")
                _resolved_mlp: str | None = None
                if _mlp_path:
                    _candidate = (
                        PROJECT_ROOT / _mlp_path
                        if not Path(_mlp_path).is_absolute()
                        else Path(_mlp_path)
                    )
                    _resolved_mlp = str(_candidate) if _candidate.exists() else None
                _cal_path = _fc.get("calibrator_path", "")
                _resolved_cal: str | None = None
                if _cal_path:
                    _candidate = (
                        PROJECT_ROOT / _cal_path
                        if not Path(_cal_path).is_absolute()
                        else Path(_cal_path)
                    )
                    _resolved_cal = str(_candidate) if _candidate.exists() else None
                # DQAF-20260622-058-bis: auto-discover scaler when config
                # doesn't specify micro_scaler_path (mirrors live_bootstrap).
                _scaler_path = _fc.get("micro_scaler_path", "")
                _resolved_scaler: str | None = None
                if _scaler_path:
                    _candidate = (
                        PROJECT_ROOT / _scaler_path
                        if not Path(_scaler_path).is_absolute()
                        else Path(_scaler_path)
                    )
                    _resolved_scaler = str(_candidate) if _candidate.exists() else None
                if _resolved_scaler is None:
                    from core.features.adapters.microstructure_feature_adapter import (
                        MicrostructureFeatureAdapter,
                    )

                    _auto = MicrostructureFeatureAdapter.resolve_scaler_path(
                        args.base_dir, args.symbol
                    )
                    if _auto is not None:
                        _resolved_scaler = str(_auto)

                _raw_weights = _fc.get("ensemble_weights", [0.6, 0.4])
                _ensemble_weights = (
                    (float(_raw_weights[0]), float(_raw_weights[1]))
                    if len(_raw_weights) >= 2
                    else None
                )

                _cf_cfg = _fc.get("conformal", {})
                meta_signal_filter = MetaSignalFilter(
                    model_path=_resolved_lgb,
                    mlp_model_path=_resolved_mlp,
                    threshold=_fc.get("threshold", 0.65),
                    enabled=True,
                    mode=_fc.get("mode", "binary"),
                    ensemble_weights=_ensemble_weights,
                    micro_scaler_path=_resolved_scaler,
                    calibrator_path=_resolved_cal,
                    conformal_mode=bool(_cf_cfg.get("enabled", False)),
                    conformal_window=int(_cf_cfg.get("window", 500)),
                    conformal_percentile=float(_cf_cfg.get("percentile", 80.0)),
                    min_threshold=float(_cf_cfg.get("min_threshold", 0.50)),
                    conformal_max_age_days=float(_cf_cfg.get("max_age_days", 14.0)),
                )
                if meta_signal_filter.load():
                    # ── MLOps Iron Law #3: Dictionary Isomorphism ──
                    # Validate model feature_names against V9 schema BEFORE
                    # accepting the model.  Prevents V1-style train-serve skew
                    # where D1_* macro features were fed M5_* V9 data.
                    _mf_feature_ok = True
                    _mf_feature_diag = ""
                    try:
                        _fs_schema_path = Path(args.base_dir) / "feature_store" / "schemas.json"
                        if _fs_schema_path.exists():
                            _fs_schemas = json.loads(_fs_schema_path.read_text(encoding="utf-8"))
                            _v9_features: set[str] = set()
                            for _sc_name, _sc in _fs_schemas.items():
                                if (
                                    isinstance(_sc, dict)
                                    and "v9_institutional" in _sc.get("name", "")
                                    and args.symbol in _sc.get("symbol", "")
                                ):
                                    _v9_features = set(_sc.get("fields", []))
                                    break
                            if _v9_features:
                                _mf_model_features = set(meta_signal_filter._feature_names)
                                _missing = _v9_features - _mf_model_features
                                if _missing:
                                    _mf_feature_ok = False
                                    _mf_feature_diag = (
                                        f"Model missing {len(_missing)} V9 features: "
                                        f"{sorted(list(_missing))[:5]}..."
                                    )
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass  # schema file may not exist; skip check
                    if not _mf_feature_ok:
                        print(
                            json.dumps(
                                {
                                    "event": "meta_filter_feature_mismatch",
                                    "time": _utc_iso(),
                                    "error": _mf_feature_diag,
                                    "action": "reject_model_fallback_to_rolling_wr",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        meta_signal_filter = None
                    else:
                        # Restore rolling buffers from previous run (crash recovery)
                        _mf_state_path = Path(args.base_dir) / "meta_filter_state.json"
                        meta_signal_filter.load_state(str(_mf_state_path))
                        _wired_event = {
                            "event": "meta_pipeline_wired",
                            "time": _utc_iso(),
                            "stage2_filter": _resolved_lgb,
                            "threshold": _fc.get("threshold", 0.65),
                            "features": len(meta_signal_filter._feature_names),
                            "mlp_loaded": meta_signal_filter._mlp_model is not None,
                            "lgb_loaded": meta_signal_filter._model is not None,
                            "calibrator_loaded": meta_signal_filter._calibrator is not None,
                            "conformal_enabled": meta_signal_filter._conformal_mode,
                            "conformal_max_age_days": meta_signal_filter._conformal_max_age_days,
                            "ensemble_weights": list(meta_signal_filter._ensemble_weights),
                            "micro_scaler_loaded": meta_signal_filter._micro_scaler is not None,
                        }
                        print(json.dumps(_wired_event, ensure_ascii=False), flush=True)
                        # P7 (TECH_DEBT-018): durable SSOT copy decoupled from the
                        # intent log file lifecycle (crash-loop stdout re-routing).
                        # Non-fatal: leaf catches known types; guard catches the
                        # unexpected so a bad append can never kill the live loop.
                        with fail_open_guard("live_intent_loop:meta_wired_append"):  # BLE001:FOG
                            record_wired_event(args.base_dir, _wired_event)
                else:
                    print(
                        json.dumps(
                            {"event": "meta_filter_load_failed", "time": _utc_iso()},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    meta_signal_filter = None
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _mf_exc:  # BLE001:FOG
            print(
                json.dumps(
                    {"event": "meta_filter_init_error", "time": _utc_iso(), "error": str(_mf_exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    # ── FIX-20260610-007: Direction-specific MetaFilter models ──
    # XAU directional asymmetry: SHORT cascades vs LONG grinds.
    # Separate models capture different feature importance patterns.
    # Attached to config so strategy_line picks them up via getattr.
    _base_dir = Path(args.base_dir)
    for _dir_label, _dir_model in [("long", "lgb_xau_long_v1"), ("short", "lgb_xau_short_v1")]:
        _dir_model_path = str(_base_dir / "models" / f"meta_stage2_{_dir_model}.txt")
        if Path(_dir_model_path).exists():
            try:  # BLE001:FOG (was: FOG/LAC)
                _dir_filter = MetaSignalFilter(
                    model_path=_dir_model_path,
                    threshold=0.55,
                    enabled=True,
                    mode="binary",
                )
                if _dir_filter.load():
                    setattr(config, f"meta_filter_{_dir_label}", _dir_filter)
                    print(
                        json.dumps(
                            {
                                "event": f"meta_filter_{_dir_label}_loaded",
                                "time": _utc_iso(),
                                "model": _dir_model_path,
                                "features": len(_dir_filter._feature_names),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

    # ── Initial state ──
    from core.runtime.settlement_queue import SettlementQueue

    state = LiveCycleState(
        known_open_tickets=known_open_tickets,
        position_manager=position_manager,
        correlation_tracker=correlation_tracker,
        pending_settlement_tickets=SettlementQueue(),
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
        except (
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            OSError,
        ) as _hot_reload_exc:  # BLE001:FOG
            print(
                json.dumps(
                    {
                        "event": "config_hot_reload_failed",
                        "time": _utc_iso(),
                        "error": str(_hot_reload_exc),
                        "action": "continuing_without_hot_reload",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    # ── Initialize BarSyncPoller (event-driven M5 bar detection) ──
    bar_sync: Any = None
    if args.bar_sync and not args.no_mt5 and mt5_worker is not None:
        try:
            from core.protocol.event_bar_sync import BarSyncPoller

            bar_sync = BarSyncPoller(
                symbol=args.symbol,
                timeframe="M5",
                terminal_path=args.mt5_terminal_path,
                state_dir=args.base_dir,
                timeout_seconds=args.bar_sync_timeout,
                mt5_worker=mt5_worker,
                market_type=_yaml_market_type,  # FIX-20260601-042: session-aware bar sync
                strict_mode=True,  # Architect directive: no direct MT5 in production
                # FIX-20260820-001 (TECH_DEBT-013): pulse the in-process watchdog
                # during bar_sync waits so daily-close blocking (no M5 bar for up
                # to bar_period+10s) is never misclassified as a 300s deadlock.
                heartbeat_refresh=lambda: setattr(state, "last_heartbeat", time.time()),
            )
            print(
                json.dumps(
                    {
                        "event": "bar_sync_initialized",
                        "time": _utc_iso(),
                        "symbol": args.symbol,
                        "timeframe": "M5",
                        "timeout_seconds": args.bar_sync_timeout,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _bs_exc:  # BLE001:FOG
            print(
                json.dumps(
                    {
                        "event": "bar_sync_init_failed",
                        "time": _utc_iso(),
                        "error": str(_bs_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    # ── Initialize ExitWatchdog (heartbeat-protected exit dispatch) ──
    # FIX-20260613-086: watchdog_config from live YAML for structural triggers
    exit_watchdog: Any = None
    if args.use_exit_watchdog:
        try:
            from core.execution.exit_watchdog import ExitWatchdog

            _wd_cfg = (
                full_cfg.get("live_trading", {}).get("watchdog_config", {})
                if isinstance(full_cfg, dict)
                else {}
            )
            exit_watchdog = ExitWatchdog(
                data_dir=args.base_dir,
                time_decay_cycles=int(_wd_cfg.get("time_decay_cycles", 60)),
                price_decay_bars=int(_wd_cfg.get("price_decay_bars", 5)),
                price_decay_sl_proximity=float(_wd_cfg.get("price_decay_sl_proximity", 0.5)),
            )
            print(
                json.dumps(
                    {
                        "event": "exit_watchdog_initialized",
                        "time": _utc_iso(),
                        "structural_triggers": {
                            "time_decay_cycles": exit_watchdog.time_decay_cycles,
                            "price_decay_bars": exit_watchdog.price_decay_bars,
                        },
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _ew_exc:  # BLE001:FOG
            print(
                json.dumps(
                    {
                        "event": "exit_watchdog_init_failed",
                        "time": _utc_iso(),
                        "error": str(_ew_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    # ── Initialize LimitOrderMonitor (spread-aware limit order tracking) ──
    limit_monitor: Any = None
    if args.use_limit_orders:
        try:
            from core.execution.limit_order_monitor import LimitOrderMonitor

            limit_monitor = LimitOrderMonitor(data_dir=f"{args.base_dir}/limit_orders")
            print(
                json.dumps(
                    {
                        "event": "limit_monitor_initialized",
                        "time": _utc_iso(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _lm_exc:  # BLE001:FOG
            print(
                json.dumps(
                    {
                        "event": "limit_monitor_init_failed",
                        "time": _utc_iso(),
                        "error": str(_lm_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    # ── Initialize LiveAlertHub (unified alerting: rules → circuit breaker → Slack/DingTalk/Log) ──
    alert_hub: Any = None
    if args.alert:
        try:
            from core.observability.live_alert_hub import LiveAlertHub

            # Read alert thresholds + rules_config from full YAML config
            _thresholds: dict[str, float] = {}
            _rules_config: list[dict[str, Any]] | None = None
            if args.config:
                try:  # BLE001:FOG (was: FOG/LAC)
                    import yaml as _yaml_full

                    with open(args.config, encoding="utf-8") as _fh_full:
                        _full_cfg = _yaml_full.safe_load(_fh_full)
                    if isinstance(_full_cfg, dict):
                        _alert_cfg = _full_cfg.get("alert", {})
                        if isinstance(_alert_cfg, dict):
                            _thresholds = _alert_cfg.get("thresholds", {}) or {}
                        _alert_sys = _full_cfg.get("alert_system", {})
                        if isinstance(_alert_sys, dict):
                            _rules_config = _alert_sys.get("rules", None)
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass

            # Derive symbol for alert instance fingerprinting:
            # prefer explicit --symbol, fall back to base_dir heuristic
            _alert_symbol = (
                args.symbol
                if args.symbol
                else ("BTCUSDc" if "btc" in str(args.base_dir).lower() else "XAUUSDc")
            )

            alert_hub = LiveAlertHub(
                base_dir=args.base_dir,
                symbol=_alert_symbol,
                slack_url=args.slack_webhook or "",
                dingtalk_url=args.dingtalk_webhook or "",
                dingtalk_secret=args.dingtalk_secret or "",
                thresholds=_thresholds or None,
                rules_config=_rules_config,
            )
            print(
                json.dumps(
                    {
                        "event": "alert_hub_initialized",
                        "time": _utc_iso(),
                        "circuit_state": alert_hub.circuit_breaker.state.value,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as _ah_exc:  # BLE001:FOG
            print(
                json.dumps(
                    {
                        "event": "alert_hub_init_failed",
                        "time": _utc_iso(),
                        "error": str(_ah_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    # ── Global crash hook: capture ALL exits including C-level crashes ──
    import sys as _sys
    import traceback as _traceback
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    def _global_excepthook(exc_type, exc_value, exc_tb):
        msg = {
            "event": "fatal_error",
            "time": _datetime.now(_UTC).replace(tzinfo=None).isoformat(),
            "type": str(exc_type) if exc_type is not None else "None",
            "message": str(exc_value) if exc_value is not None else "None",
        }
        if exc_tb is not None:
            msg["traceback"] = "".join(_traceback.format_tb(exc_tb))[-2000:]
        try:
            print(json.dumps(msg, ensure_ascii=False), flush=True)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            print(f"FATAL: {exc_type}: {exc_value}", flush=True)
        _sys.__excepthook__(exc_type, exc_value, exc_tb)

    _sys.excepthook = _global_excepthook

    # ── Main loop ──
    _live_lock: Any = None
    try:
        from core.infrastructure.distributed_lock import get_lock

        _live_lock = get_lock(
            "live_intent_loop",
            backend="auto",
            ttl_seconds=300,
            lock_dir=str(Path(args.base_dir) / "locks"),
        )
        _acquired = _live_lock.acquire()
        if not _acquired.acquired:
            print(
                json.dumps(
                    {
                        "event": "lock_denied",
                        "time": _utc_iso(),
                        "holder": _acquired.holder_id,
                        "error": _acquired.error or "Another live_intent_loop process is running",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            sys.exit(0)

        # ── Synchronous warm-start: fill deferred MT5 buffers BEFORE the main
        # trading loop to eliminate the data race between the warm-start daemon
        # thread and the main loop's MT5 calls (FIX-20260522-014).
        # A 15-second join timeout prevents startup deadlock if MT5 is
        # unresponsive — the buffer fill is best-effort, not critical.
        if _warm_start_pending and mt5_worker is not None:
            import threading as _threading

            def _background_warm_start() -> None:
                for b_info in _warm_start_pending:
                    btype = b_info.get("brain_type", "")
                    adapter = b_info.get("adapter")
                    if adapter is None:
                        continue
                    if btype == "ou_params_v6":
                        cg = b_info.get("contract_group", "")
                        rates = None
                        if cg == "statarb_m15":
                            # M15 OU needs 280 M15 bars (window=280).
                            # Fetch directly from M15 timeframe — resampling
                            # 300 M5 bars only yields ~100 M15 bars (缺口 180).
                            with FaultTolerantContext(
                                level=FaultLevel.CRASH,
                                component="MT5_IPC:copy_rates_from_pos:ou_bootstrap",
                            ):
                                rates = mt5_worker.copy_rates_from_pos(
                                    args.symbol, 15, 0, 350
                                )  # MT5_TIMEFRAME_M15
                            min_required = 280
                        else:
                            with FaultTolerantContext(
                                level=FaultLevel.CRASH,
                                component="MT5_IPC:copy_rates_from_pos:ou_bootstrap",
                            ):
                                rates = mt5_worker.copy_rates_from_pos(
                                    args.symbol, 5, 0, 300
                                )  # MT5_TIMEFRAME_M5
                            min_required = 30
                        if rates is not None and len(rates) >= min_required:
                            try:
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
                            except (
                                RuntimeError,
                                ValueError,
                                KeyError,
                                TypeError,
                                OSError,
                            ) as _wsexc:  # BLE001:FOG
                                print(
                                    json.dumps(
                                        {
                                            "event": "warm_start_background_error",
                                            "time": _utc_iso(),
                                            "brain_id": b_info["brain_id"],
                                            "brain_type": btype,
                                            "error": str(_wsexc),
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )
                    elif btype == "transformer_v4.3":
                        try:
                            if (
                                micro_feature_computer is not None
                                and micro_feature_adapter is not None
                            ):
                                micro_feats = micro_feature_computer.compute_all()
                                fv = micro_feature_adapter.build_model_input(micro_feats).ravel()
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
                        except (
                            RuntimeError,
                            ValueError,
                            KeyError,
                            TypeError,
                            OSError,
                        ) as _wsexc:  # BLE001:FOG
                            print(
                                json.dumps(
                                    {
                                        "event": "warm_start_background_error",
                                        "time": _utc_iso(),
                                        "brain_id": b_info["brain_id"],
                                        "brain_type": btype,
                                        "error": str(_wsexc),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )

            _warm_start_thread = _threading.Thread(
                target=_background_warm_start, daemon=True, name="warm-start"
            )
            _warm_start_thread.start()
            _warm_start_thread.join(timeout=15.0)
            if _warm_start_thread.is_alive():
                print(
                    json.dumps(
                        {
                            "event": "warm_start_timed_out",
                            "time": _utc_iso(),
                            "timeout_seconds": 15.0,
                            "action": "proceeding_to_main_loop",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Degraded-wakeup propagation: set by bar_sync at end of cycle N,
        # consumed by execute_live_cycle at start of cycle N+1.
        _degraded_wakeup = False

        # ── Signal handlers for graceful shutdown (SIGINT / SIGTERM) ──
        # Registered in main thread per CPython requirement.
        _shutdown_flag = [False]  # mutable container for nested scope access

        def _on_shutdown_signal(signum: int, frame: Any) -> None:
            sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
            if not _shutdown_flag[0]:
                _shutdown_flag[0] = True
                print(
                    json.dumps(
                        {
                            "event": "shutdown_signal_received",
                            "time": _utc_iso(),
                            "signal": sig_name,
                            "action": "draining_current_cycle_then_exit",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        _old_sigint_handler = signal.signal(signal.SIGINT, _on_shutdown_signal)
        _old_sigterm_handler = signal.signal(signal.SIGTERM, _on_shutdown_signal)

        # ── FIX-20260610-003: Watchdog daemon thread ─────────────────────
        # Monitors state.last_heartbeat.  If execute_live_cycle() blocks
        # for >300s (e.g. stuck in MT5 IPC), the watchdog force-kills the
        # process via os._exit(1).  systemd/supervisor restarts it.
        # os._exit() is used (not sys.exit) because a stuck C extension
        # call cannot be interrupted by Python-level signals or exceptions.
        # State preservation: execution_state is saved every cycle,
        # position_state during management phase, journal via FileLock —
        # the worst-case data loss is the current in-flight cycle.
        import os as _os_module
        import threading as _threading

        # Seed the heartbeat so the watchdog doesn't fire during startup
        state.last_heartbeat = time.time()

        def _watchdog_loop() -> None:
            while True:
                _threading.Event().wait(10.0)
                _elapsed = time.time() - getattr(state, "last_heartbeat", 0.0)
                if _elapsed > 300.0:
                    # Write last words to a dedicated kill log (stdout may
                    # be buffered / lost on hard kill).
                    try:
                        with open("watchdog_kill.log", "a", encoding="utf-8") as _wf:
                            _wf.write(
                                f"[{_utc_iso()}] WATCHDOG TRIGGERED. "
                                f"elapsed={_elapsed:.1f}s. "
                                f"MT5 IPC deadlock suspected. "
                                f"Hard killing PID {_os_module.getpid()}.\n"
                            )
                    except OSError:
                        pass
                    _os_module._exit(1)

        _watchdog_thread = _threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog")
        _watchdog_thread.start()

        # ── DQAF-20260614-010: Bridge readiness gate ──
        # ZMQ slow joiner: PUSH connects before PULL binds → first N
        # messages are lost.  When the Bridge finally binds, ALL buffered
        # messages arrive simultaneously → duplicate orders (Sev 1).
        # Fix: wait until Bridge health confirms ZMQ transport is ready
        # before entering the dispatch loop.
        if not args.no_mt5:
            _bridge_health_path = Path(args.base_dir) / "reports" / "mt5_bridge_health.json"
            _bridge_ready = False
            for _retry in range(30):  # 30 × 1s = 30s max wait
                try:  # BLE001:FOG (was: FOG/LAC)
                    if _bridge_health_path.exists():
                        _hb = json.loads(_bridge_health_path.read_text(encoding="utf-8"))
                        if _hb.get("transport") == "zmq" and _hb.get("mt5_connected"):
                            _bridge_ready = True
                            print(
                                json.dumps(
                                    {
                                        "event": "bridge_ready",
                                        "time": _utc_iso(),
                                        "pid": _hb.get("pid"),
                                        "transport": "zmq",
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            break
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
                time.sleep(1.0)
            if not _bridge_ready:
                print(
                    json.dumps(
                        {
                            "event": "bridge_not_ready",
                            "time": _utc_iso(),
                            "severity": "WARNING",
                            "message": "Bridge not ready after 30s — proceeding but orders may be lost",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── FIX-20260617-101/M2: Entry Context Guard daemon ──
        # Monitors entry_context.vector completeness (DLR-001).
        # Runs as independent daemon thread — never blocks the main loop.
        try:
            from core.observability.entry_context_guard import start_entry_context_guard

            start_entry_context_guard(Path(args.base_dir), symbol=args.symbol)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass  # Guard failure must never prevent the main loop from starting
        while True:
            # ── TECH_DEBT-017 (L3): Scope-Safe Pre-binding ──
            # _EVENT_STREAM_MODE 必须在循环体最顶层绑定 — 异常跳转路径 (DEGRADE/except)
            # 可能跳过循环中部的原赋值点 (L2553) → 后续引用 UnboundLocalError
            # (8/11→8/13 38 次崩溃次因). 中部赋值保留 (幂等, 供调试切换).
            _EVENT_STREAM_MODE = True
            state.last_heartbeat = time.time()
            if _shutdown_flag[0]:
                print(
                    json.dumps(
                        {"event": "shutdown_graceful_break", "time": _utc_iso()},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                break
            try:
                # ── FIX-20260611-005: Stash pnl_ledger for adapter downstream notification ──
                state._pnl_ledger = pnl_ledger
                state, should_continue = execute_live_cycle(
                    config,
                    state,
                    mt5_worker=mt5_worker,
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
                    daily_feature_provider=daily_feature_provider,
                    journal_path=_journal_path,
                    journal_gate=_journal_gate,
                    pnl_ledger=pnl_ledger,
                    exit_watchdog=exit_watchdog,
                    limit_monitor=limit_monitor,
                    meta_signal_filter=meta_signal_filter,
                    alert_hub=alert_hub,
                    degraded_wakeup=_degraded_wakeup,
                )
                state.last_heartbeat = time.time()  # FIX-003: cycle completed
                _degraded_wakeup = False  # consumed, reset for next cycle
                # ── DQAF-20260616-002/P0.1: Reset consecutive error counter ──
                # A successful cycle resets the zombie-state fuse so that only
                # truly persistent silent-degrade loops trigger the kill-switch.
                state._consecutive_cycle_errors = 0
                # Reload tracker if daily_ops enriched it with realized P&L
                if state._tracker_reload_pending:
                    try:  # BLE001:FOG (was: FOG/LAC)
                        tracker = BrainPerformanceTracker.load(tracker_path)
                        state._tracker_reload_pending = False
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass
                if not should_continue:
                    break
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
                # ── FIX-20260607-140: Fail-Closed on dispatch pipeline crash ──
                # ExecutionQueueFatalError means the dispatch pipeline is broken.
                # Trip the circuit breaker IMMEDIATELY — do NOT continue the
                # cycle without a functioning dispatch path (Fail-Open→Fail-Closed).
                if isinstance(
                    exc,
                    __import__(
                        "core.execution.execution_queue", fromlist=["ExecutionQueueFatalError"]
                    ).ExecutionQueueFatalError,
                ):
                    state._circuit_breaker_tripped = True
                    state._circuit_breaker_tripped_at = time.time()
                    state.block_new_entries = True

                # ── DQAF-20260616-002/P0.1: Consecutive cycle error fuse ──────
                # A single swallowed exception can corrupt the state machine and
                # cause all subsequent cycles to silently skip the trading path
                # (zombie state — heartbeat alive, no trades).  This fuse counts
                # consecutive failing cycles and force-kills the process after 5,
                # letting the launcher restart with a clean state.
                # Persisted on state so it survives across loop iterations.
                _consec = getattr(state, "_consecutive_cycle_errors", 0) + 1
                state._consecutive_cycle_errors = _consec
                _tb = traceback.format_exc()
                print(
                    json.dumps(
                        {
                            "event": "cycle_error",
                            "time": _utc_iso(),
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                            "traceback": _tb,
                            "circuit_breaker_tripped": state._circuit_breaker_tripped,
                            "consecutive_cycle_errors": _consec,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if _consec >= 5:
                    # ── Fuse blown: log final traceback to alert_audit ──
                    _fuse_msg = (
                        f"[DQAF-20260616-002] ZOMBIE_CYCLE_FUSE_BLOWN: "
                        f"{_consec} consecutive cycle failures. "
                        f"Last error: {type(exc).__name__}: {exc!s:.500}. "
                        f"Force-exiting to break silent-degrade loop."
                    )
                    try:  # BLE001:FOG (was: FOG/LAC)
                        from core.observability.live_alert_hub import LiveAlertHub

                        # TECH_DEBT-008: 修复 zombie-fuse 告警全静默 bug — LiveAlertHub
                        # 构造参数签名漂移 (base_dir/symbol, 非 log_dir/ding_webhook_url)
                        # + 告警方法漂移 (send_critical, 非 fire()) + state._alert_hub
                        # 恒 None (从未赋值) 三重合 → 旧代码 TypeError+AttributeError 被
                        # BLE001:FOG 吞掉 → 熔断信号从未送达, 只剩本地 watchdog_kill.log.
                        # 优先复用 main() 已构造的 alert_hub (args.alert=True 时携带真实
                        # webhook), 否则 fallback 构造 (无 webhook → send_critical 入队
                        # → 队列满时 _write_fallback_alert 落盘, 不再全静默).
                        _ah = alert_hub or getattr(state, "_alert_hub", None)
                        if _ah is None:
                            _zm_symbol = args.symbol or (
                                "BTCUSDc" if "btc" in str(config.base_dir).lower() else "XAUUSDc"
                            )
                            _ah = LiveAlertHub(
                                base_dir=config.base_dir,
                                symbol=_zm_symbol,
                                dingtalk_url=args.dingtalk_webhook or "",
                                dingtalk_secret=args.dingtalk_secret or "",
                            )
                        _ah.send_critical(
                            "zombie_cycle_fuse_blown",
                            detail={
                                "consecutive_errors": _consec,
                                "last_error_type": type(exc).__name__,
                                "last_error": str(exc)[:500],
                                "last_traceback": _tb[:2000],
                                "cycle_count": getattr(state, "cycle_count", -1),
                            },
                        )
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass
                    # Write kill log (same pattern as watchdog for diagnostics)
                    try:
                        with open("watchdog_kill.log", "a", encoding="utf-8") as _wf:
                            _wf.write(
                                f"[{_utc_iso()}] ZOMBIE_CYCLE_FUSE PID={_os_module.getpid()}. "
                                f"consecutive_errors={_consec}. "
                                f"last_error={type(exc).__name__}: {exc!s:.300}\n"
                                f"traceback_head={_tb[:1000]}\n"
                            )
                    except OSError:
                        pass
                    _os_module._exit(
                        3
                    )  # exit code 3 = zombie cycle fuse (distinct from watchdog=1)
                if args.once:
                    break
            # ── Persist state every ~1 hour + check config hot-reload ──
            state.cycle_count += 1

            # ── DQAF-20260616-004: Refresh distributed lock TTL ──────────
            # Without refresh, a healthy process appears stale after 300s.
            # A hung process stops refreshing → TTL expires → launcher auto-cleans.
            # Refresh every cycle (~60s) to maintain a 300s TTL window.
            if _live_lock is not None and state.cycle_count % 1 == 0:
                if not _live_lock.refresh():
                    print(
                        json.dumps(
                            {"event": "lock_refresh_failed", "time": _utc_iso()},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

            # ── Governance lifecycle: REMOVED — moved to SSOT thread (FIX-20260801-011/012) ──
            # DQAF-20260801-010: The DEPRECATED apply_promotion_decisions block
            # (BrainPnLStore last-20 window) was the second writer in the
            # dual-track race that oscillated BTC_Swing_V4 live↔probation since
            # 07-09.  Governance now runs in the launcher's 60s SSOT thread
            # (_governance_scheduler_runner) on brain_performance.json via
            # GovernanceRuleEngine.execute_transitions (Iron Law #14 sole
            # writer), protected by observation holds (FIX-20260801-012).
            if hot_reload is not None and state.loop_iteration % 30 == 0:
                try:  # BLE001:FOG (was: FOG/LAC)
                    changes = hot_reload.check_and_reload()
                    if changes:
                        # ── Apply regime_map hot-reload ──
                        _new_regime_cfg = changes.get("regime_gate", {})
                        if isinstance(_new_regime_cfg, dict):
                            _new_regime_map = _new_regime_cfg.get("regime_map")
                            if isinstance(_new_regime_map, dict) and state.regime_gate is not None:
                                state.regime_gate.regime_map = _new_regime_map
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
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
            # ── FIX-20260603-075: persist execution guard state EVERY cycle ──
            # DQAF-20260620-002: removed _strategies guard — breaker state,
            # counter values, and saved_at_utc must be persisted every cycle
            # even when strategies haven't been built yet (COLD_START, early
            # gate blocks).  Previously the save was silently skipped for N
            # cycles → stale execution_state.json → EXEC_STATE_STALE alert.
            try:  # BLE001:FOG (was: FOG/LAC)
                from core.runtime.execution_state import save_execution_state

                _exec_path = Path(args.base_dir) / "state" / "execution_state.json"
                _strategies = getattr(state, "_strategies", None) or {}
                save_execution_state(
                    str(_exec_path),
                    _strategies,
                    getattr(state, "_cooldown_registry", None),
                    getattr(state, "_family_entry_tracker", None),
                    sl_streak_blocks=getattr(state, "sl_streak_blocked_until", {}),
                    sl_streak_global_block=getattr(state, "sl_streak_blocked_all_until", 0.0),
                    consecutive_degraded=state._consecutive_degraded_cycles,
                    circuit_breaker_tripped=state._circuit_breaker_tripped,
                    circuit_breaker_tripped_at=getattr(state, "_circuit_breaker_tripped_at", 0.0),
                    intraday_dd_active=state.block_new_entries,
                    # ── DQAF-20260608-003: full counter persistence ──
                    consecutive_stale_cycles=state._consecutive_stale_cycles,
                    consecutive_stale_features=state._consecutive_stale_features,
                    circuit_breaker_trip_reason=getattr(state, "_circuit_breaker_trip_reason", ""),
                    # ── DQAF-20260615-004 ──
                    known_open_tickets=getattr(state, "known_open_tickets", None),
                    # ── FIX-20260730-011 (L3): Settlement Queue persistence ──
                    pending_settlement_tickets=(
                        state.pending_settlement_tickets.to_dict()["entries"]
                        if getattr(state, "pending_settlement_tickets", None) is not None
                        and state.pending_settlement_tickets.pending_count > 0
                        else None
                    ),
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

            # ── FIX-20260604-077: persist PnL ledger EVERY cycle ──
            # Same root cause as FIX-075: 60-cycle save interval means recent
            # ── FIX-20260611-022: Event Stream Mode ──
            # The event stream (EventWriter → ledger_events.jsonl) is now the
            # authoritative write path.  Old JSON save() is redundant.
            # Set _EVENT_STREAM_MODE=False to restore dual-write for debugging.
            _EVENT_STREAM_MODE = True
            if pnl_ledger is not None:
                try:  # BLE001:FOG (was: FOG/LAC)
                    if not _EVENT_STREAM_MODE:
                        pnl_ledger.save(pnl_ledger_path)
                    _inject_performance_metrics(pnl_ledger, args.base_dir)
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass

            # ── FIX-20260604-079: data health monitor ──
            # Runs every 60 cycles.  Checks feature store freshness,
            # journal growth, and training prerequisite conditions.
            # Alerts via LiveAlertHub when data quality degrades.
            # Bootstrap on first cycle to create state file immediately.
            if state.loop_iteration == 1 or state.loop_iteration % config.state_save_interval == 0:
                try:  # BLE001:FOG (was: FOG/LAC)
                    from core.runtime.data_health_monitor import check_data_health

                    _health = check_data_health(
                        base_dir=args.base_dir,
                        symbol=config.symbol,
                        alert_hub=alert_hub,
                        position_manager=getattr(state, "position_manager", None),
                    )
                    if _health.get("alerts") or _health.get("checks", {}).get("training_ready"):
                        print(
                            json.dumps(
                                {"event": "data_health_report", **_health},
                                ensure_ascii=False,
                                default=str,
                            ),
                            flush=True,
                        )
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass

            if state.loop_iteration % config.state_save_interval == 0:
                if rolling_norm is not None:
                    try:  # BLE001:FOG (was: FOG/LAC)
                        _state_path = Path(args.base_dir) / "rolling_norm_state.json"
                        rolling_norm.save_state(_state_path)
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass
                if regime_detector is not None:
                    try:  # BLE001:FOG (was: FOG/LAC)
                        _regime_path = Path(args.base_dir) / "regime_detector_state.json"
                        regime_detector.save_state(_regime_path)
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass
                if meta_signal_filter is not None:
                    try:  # BLE001:FOG (was: FOG/LAC)
                        meta_signal_filter.save_state(str(_mf_state_path))
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass

            # ── Wait for next cycle: event-driven bar sync or interval sleep ──
            try:
                if bar_sync is not None:
                    new_bar = bar_sync.wait_for_new_bar(timeout_seconds=args.bar_sync_timeout)
                    if new_bar is not None and new_bar.get("_degraded"):
                        _degraded_wakeup = True
                        print(
                            json.dumps(
                                {
                                    "event": "bar_sync_degraded_wakeup",
                                    "time": _utc_iso(),
                                    "last_bar_time": new_bar.get("time"),
                                    "bar_sync_elapsed": ">=bar_period",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    if new_bar is None:
                        # Timeout — MT5 bar not yet formed; synthesize from M1 bars
                        synthetic = bar_sync.fetch_synthetic_bar()
                        sync_state = bar_sync.get_state()
                        if synthetic is not None:
                            print(
                                json.dumps(
                                    {
                                        "event": "bar_sync_synthetic",
                                        "time": _utc_iso(),
                                        "synthetic_bar_time": synthetic["time"],
                                        "synthetic_close": round(synthetic["close"], 2),
                                        "state": sync_state,
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                        else:
                            print(
                                json.dumps(
                                    {
                                        "event": "bar_sync_timeout",
                                        "time": _utc_iso(),
                                        "state": sync_state,
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            time.sleep(args.interval_seconds)
                else:
                    time.sleep(args.interval_seconds)
            except (
                RuntimeError,
                ValueError,
                KeyError,
                TypeError,
                OSError,
            ) as _bar_exc:  # BLE001:FOG
                # FIX-20260820-001: FTC DEGRADE guard -- preserve full traceback
                # for bar_sync degradation (Repairability, #10 hot-path BLE001
                # governance).  Never crashes the main loop; logs and continues.
                with fail_open_guard("live_intent_loop:bar_sync_wait"):
                    print(
                        json.dumps(
                            {
                                "event": "bar_sync_crash",
                                "time": _utc_iso(),
                                "error": str(_bar_exc),
                                "traceback": traceback.format_exc(),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    time.sleep(args.interval_seconds)
    finally:
        # ── Release distributed lock ──
        if _live_lock is not None:
            try:  # BLE001:FOG (was: FOG/LAC)
                _live_lock.release()
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
        # ── Graceful shutdown: persist all state ──
        print(
            json.dumps({"event": "shutdown_start", "time": _utc_iso()}, ensure_ascii=False),
            flush=True,
        )

        # FIX-20260525-025 / P0-1: Temporarily ignore SIGINT/SIGTERM during
        # state save to prevent a second signal from interrupting atomic writes.
        # All saves use tmp+replace, so even if the process is killed after the
        # shield lifts, the original file is never corrupted.
        _old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        _old_sigterm = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if position_manager is not None and position_manager.has_position():
                try:  # BLE001:FOG (was: FOG/LAC)
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
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass

            if rolling_norm is not None:
                try:  # BLE001:FOG (was: FOG/LAC)
                    _state_path = Path(args.base_dir) / "rolling_norm_state.json"
                    rolling_norm.save_state(_state_path)
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
            if regime_detector is not None:
                try:  # BLE001:FOG (was: FOG/LAC)
                    _regime_path = Path(args.base_dir) / "regime_detector_state.json"
                    regime_detector.save_state(_regime_path)
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
            try:
                save_path = Path(args.base_dir) / "brain_performance.json"
                tracker.save(save_path)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
                print(
                    json.dumps(
                        {"event": "tracker_save_error", "time": _utc_iso(), "error": str(exc)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if pnl_ledger is not None:
                try:
                    if not _EVENT_STREAM_MODE:
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
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                ) as exc:  # BLE001:FOG
                    print(
                        json.dumps(
                            {
                                "event": "pnl_ledger_save_error",
                                "time": _utc_iso(),
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            if meta_signal_filter is not None:
                try:  # BLE001:FOG (was: FOG/LAC)
                    meta_signal_filter.save_state(str(_mf_state_path))
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
            # ── FIX-20260603-072: persist execution guard state on shutdown ──
            # DQAF-20260620-002: removed _strategies guard — same fix as per-cycle save above
            try:  # BLE001:FOG (was: FOG/LAC)
                from core.runtime.execution_state import save_execution_state

                _exec_path = Path(args.base_dir) / "state" / "execution_state.json"
                _strategies = getattr(state, "_strategies", None) or {}
                save_execution_state(
                    str(_exec_path),
                    _strategies,
                    getattr(state, "_cooldown_registry", None),
                    getattr(state, "_family_entry_tracker", None),
                    sl_streak_blocks=getattr(state, "sl_streak_blocked_until", {}),
                    sl_streak_global_block=getattr(state, "sl_streak_blocked_all_until", 0.0),
                    consecutive_degraded=state._consecutive_degraded_cycles,
                    circuit_breaker_tripped=state._circuit_breaker_tripped,
                    circuit_breaker_tripped_at=getattr(state, "_circuit_breaker_tripped_at", 0.0),
                    intraday_dd_active=state.block_new_entries,
                    # ── DQAF-20260608-003: full counter persistence ──
                    consecutive_stale_cycles=state._consecutive_stale_cycles,
                    consecutive_stale_features=state._consecutive_stale_features,
                    circuit_breaker_trip_reason=getattr(state, "_circuit_breaker_trip_reason", ""),
                    # ── DQAF-20260615-004 ──
                    known_open_tickets=getattr(state, "known_open_tickets", None),
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
        finally:
            signal.signal(signal.SIGINT, _old_sigint)
            signal.signal(signal.SIGTERM, _old_sigterm)

        # ── Shutdown alert hub (护栏6: graceful drain of queued alerts) ──
        if alert_hub is not None:
            try:  # BLE001:FOG (was: FOG/LAC)
                alert_hub.shutdown()
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

        if mt5_worker is not None:
            mt5_worker.stop()
        print(
            json.dumps({"event": "shutdown_complete", "time": _utc_iso()}, ensure_ascii=False),
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
