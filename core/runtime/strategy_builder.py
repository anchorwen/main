"""Strategy line builder — partition brains by contract_group into strategy instances.

Extracted from live_cycle.py per the Strangler Fig pattern (Directive 4, P3.1).
Builds StrategyLine objects from brain configs and live.yaml strategy configs.
"""

from __future__ import annotations

import json
from typing import Any

from core.config.asset_registry import get_asset
from core.execution.barrier_strategy import BarrierStrategy
from core.execution.micro_strategy import MicroStrategy
from core.execution.rule_engine_strategy import RuleEngineStrategyWrapper
from core.execution.statarb_strategy import StatArbStrategy
from core.execution.strategy_budget import StrategyBudget
from core.execution.strategy_line import StrategyLineConfig
from core.execution.swing_strategy import SwingStrategy
from core.parliament.contract_groups import (
    ALL_GROUPS,
    ARB_GROUP,
    BARRIER_12BAR_META_GROUP,
    BARRIER_GROUP,
    BTC_SWING_GROUP,
    DAILY_SWING_GROUP,
    H1_SWING_GROUP,
    H4_SWING_GROUP,
    M15_SWING_GROUP,
    M30_SWING_GROUP,
    MICRO_GROUP,
    MICRO_H1_GROUP,
    MICRO_M15_GROUP,
    STATARB_M15_GROUP,
)
from core.runtime.time_utils import _utc_iso  # consolidated


def _warn_contract_mismatch(
    brain_info: dict[str, Any],
    strategy_name: str,
    required_contracts: dict[str, str],
) -> None:
    """Hard-mute a brain whose training contract doesn't match its strategy line.

    A regression-contract brain placed in a barrier strategy would silently
    predict the wrong target.  Previously this was a soft warning; now the
    brain's ``vote_weight`` is forced to 0.0 — it cannot influence any
    parliament decision until its contract is reconciled.

    The brain still runs inference (so we can monitor its output quality)
    but its vote is discarded before consensus aggregation.
    """
    training_contract = str(brain_info.get("training_contract", ""))
    required = required_contracts.get(strategy_name, "")
    contract_ok = (required and required in training_contract) or training_contract.startswith(
        strategy_name
    )
    if required and not contract_ok:
        _prev_weight = brain_info.get("vote_weight", 1.0)
        brain_info["vote_weight"] = 0.0
        if brain_info.get("_contract_muted"):
            return
        brain_info["_contract_muted"] = True
        print(
            json.dumps(
                {
                    "event": "brain_hard_muted_contract",
                    "brain_id": brain_info.get("brain_id", "unknown"),
                    "brain_type": brain_info.get("brain_type", "unknown"),
                    "brain_contract": training_contract,
                    "strategy_name": strategy_name,
                    "strategy_requires": required,
                    "previous_vote_weight": _prev_weight,
                    "new_vote_weight": 0.0,
                    "reason": "training_contract_mismatch",
                    "action_required": "retrain_brain_with_correct_contract_or_reassign_group",
                }
            ),
            flush=True,
        )


def build_strategy_lines(
    brains: list[dict[str, Any]],
    config: Any,  # LiveCycleConfig
) -> dict[str, Any]:
    """Partition brains into contract groups and create strategy line objects.

    Returns dict mapping strategy_name → StrategyLine instance.
    """
    _STRATEGY_CONTRACT_TYPES = {
        "barrier_12bar": "survival_barrier",
        "barrier_12bar_meta": "barrier_12bar_meta_binary_cls",
        "micro_3bar": "label-micro-barrier",
        "micro_m15": "label-micro-barrier",
        "micro_h1": "label-micro-barrier",
        "micro_h4": "label-micro-barrier",
        "statarb_dynamic": "ou_mean_reversion",
        "statarb_m15": "ou_mean_reversion",
        "daily_swing": "d1_swing",
        "m15_swing": "m15_swing",
        "m30_swing": "m30_swing",
        "h1_swing": "h1_swing",
        "h4_swing": "h4_swing",
        "structural_swing_v1": "rule_based",
    }

    _STRATEGY_FAMILY_MAP: dict[str, str] = {
        "statarb_dynamic": "mean_reversion",
        "statarb_m15": "mean_reversion",
    }

    _known_groups: dict[str, list[Any]] = {g["name"]: [] for g in ALL_GROUPS}
    _unknown_brains: list[dict[str, Any]] = []

    for b_info in brains:
        brain_status = b_info.get("status", "")
        if brain_status in ("frozen", "retired"):
            print(
                json.dumps(
                    {
                        "event": "brain_excluded_from_voting",
                        "brain_id": b_info.get("brain_id", "unknown"),
                        "status": brain_status,
                        "reason": "frozen_or_retired",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        cg = b_info.get("contract_group", "")
        if cg in _known_groups:
            _known_groups[cg].append(b_info)
            _warn_contract_mismatch(b_info, cg, _STRATEGY_CONTRACT_TYPES)
        else:
            print(
                json.dumps(
                    {
                        "event": "unknown_contract_group_at_build",
                        "contract_group": cg,
                        "brain_id": b_info.get("brain_id", "unknown"),
                        "brain_type": b_info.get("brain_type", ""),
                        "skipped": True,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            _unknown_brains.append(b_info)

    barrier_brains = _known_groups["barrier_12bar"]
    barrier_12bar_meta_brains = _known_groups["barrier_12bar_meta"]
    micro_brains = _known_groups["micro_3bar"]
    micro_m15_brains = _known_groups["micro_m15"]
    micro_h1_brains = _known_groups["micro_h1"]
    _micro_h4_brains = _known_groups["micro_h4"]
    statarb_brains = _known_groups["statarb_dynamic"]
    statarb_m15_brains = _known_groups["statarb_m15"]
    daily_swing_brains = _known_groups["daily_swing"]
    m15_swing_brains = _known_groups["m15_swing"]
    m30_swing_brains = _known_groups["m30_swing"]
    h1_swing_brains = _known_groups["h1_swing"]
    h4_swing_brains = _known_groups["h4_swing"]
    btc_swing_brains = _known_groups["btc_swing"]
    btc_swing_h1_brains = _known_groups.get("btc_swing_h1", [])

    def _cfg(name: str, key: str, default: Any) -> Any:
        return config.strategy_configs.get(name, {}).get(key, default)

    def _vol_cfg(name: str) -> float:
        sc = config.strategy_configs.get(name, {})
        if "base_volume" in sc:
            return float(sc["base_volume"])
        return float(config.volume or 0.01)

    def _exit_cfg(name: str, key: str, default: Any) -> Any:
        return config.strategy_configs.get(name, {}).get("exit", {}).get(key, default)

    # ── Enforce strategy-level enabled flag ──
    for _gname in list(_known_groups.keys()):
        if not _cfg(_gname, "enabled", True):
            _known_groups[_gname].clear()
            print(
                json.dumps(
                    {
                        "event": "strategy_disabled_by_config",
                        "time": _utc_iso(),
                        "strategy": _gname,
                        "reason": "enabled: false in live.yaml",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    strategies: dict[str, Any] = {}

    if barrier_brains:
        barrier_brains = [
            b for b in barrier_brains if b.get("brain_type", "") in BARRIER_GROUP["brain_types"]
        ]
    if barrier_brains:
        from core.execution.meta_pipeline import discover_probe_specs

        _meta_probe_specs = discover_probe_specs(barrier_brains)
        _yaml_probes = _cfg("barrier_12bar", "meta_probes", None)
        if _yaml_probes is not None:
            from core.execution.meta_pipeline import MetaProbeSpec

            _meta_probe_specs = [
                MetaProbeSpec(
                    brain_id=str(p.get("brain_id", "")),
                    threshold=float(p.get("threshold", 0.30)),
                    filter_stage=str(p.get("filter_stage", "stage2")),
                )
                for p in _yaml_probes
            ]

        strategies["barrier_12bar"] = BarrierStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="barrier_12bar",
                strategy_family=_cfg("barrier_12bar", "strategy_family", None)
                or _STRATEGY_FAMILY_MAP.get("barrier_12bar", "trend_following"),
                magic=90001,
                brain_types=BARRIER_GROUP["brain_types"],
                base_volume=_vol_cfg("barrier_12bar"),
                max_volume=_cfg("barrier_12bar", "max_volume", 0.05),
                base_sl_atr_mult=_cfg("barrier_12bar", "sl", {}).get(
                    "base_atr_mult", config.sl_atr_mult
                ),
                base_tp_atr_mult=_cfg("barrier_12bar", "tp", {}).get(
                    "base_atr_mult", config.tp_atr_mult
                ),
                hard_sl_ratio=_cfg("barrier_12bar", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("barrier_12bar", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("barrier_12bar", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "barrier_12bar", "confidence_threshold", config.confidence_threshold
                ),
                spread_points=_cfg("barrier_12bar", "spread_points", 0.0),
                max_spread_points=_cfg("barrier_12bar", "max_spread_points", 0.0),
                long_bias_discount=_cfg("barrier_12bar", "direction_balance", {}).get(
                    "long_bias_discount", 0.05
                ),
                exit_flip_enabled=_exit_cfg("barrier_12bar", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("barrier_12bar", "time_exit_cycles", None),
                exit_zscore_enabled=_exit_cfg("barrier_12bar", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("barrier_12bar", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("barrier_12bar", "min_valid_brains", 3),
                timeframe=_cfg("barrier_12bar", "timeframe", "M5"),
                exit_hesitation_cycles=_exit_cfg("barrier_12bar", "hesitation_cycles", 0),
                meta_probe_specs=_meta_probe_specs,
            ),
            barrier_brains,
            budget=StrategyBudget(
                "barrier_12bar",
                daily_loss_limit_pct=_cfg("barrier_12bar", "budget", {}).get(
                    "daily_loss_limit_pct", -0.03
                ),
                max_consecutive_losses=_cfg("barrier_12bar", "budget", {}).get(
                    "max_consecutive_losses", 5
                ),
            ),
        )

    if micro_brains:
        strategies["micro_3bar"] = MicroStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="micro_3bar",
                strategy_family=_STRATEGY_FAMILY_MAP.get("micro_3bar", "trend_following"),
                magic=90002,
                brain_types=MICRO_GROUP["brain_types"],
                base_volume=_vol_cfg("micro_3bar"),
                max_volume=_cfg("micro_3bar", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("micro_3bar", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("micro_3bar", "tp", {}).get("base_atr_mult", 2.5),
                hard_sl_ratio=_cfg("micro_3bar", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("micro_3bar", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("micro_3bar", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "micro_3bar", "confidence_threshold", config.confidence_threshold
                ),
                spread_points=_cfg("micro_3bar", "spread_points", 0.0),
                max_spread_points=_cfg("micro_3bar", "max_spread_points", 0.0),
                long_bias_discount=_cfg("micro_3bar", "direction_balance", {}).get(
                    "long_bias_discount", 0.03
                ),
                exit_flip_enabled=_exit_cfg("micro_3bar", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("micro_3bar", "time_exit_cycles", None),
                exit_zscore_enabled=_exit_cfg("micro_3bar", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("micro_3bar", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("micro_3bar", "min_valid_brains", 2),
                timeframe=_cfg("micro_3bar", "timeframe", "M5"),
                exit_hesitation_cycles=_exit_cfg("micro_3bar", "hesitation_cycles", 0),
            ),
            micro_brains,
            budget=StrategyBudget(
                "micro_3bar",
                daily_loss_limit_pct=_cfg("micro_3bar", "budget", {}).get(
                    "daily_loss_limit_pct", -0.02
                ),
                max_consecutive_losses=_cfg("micro_3bar", "budget", {}).get(
                    "max_consecutive_losses", 6
                ),
            ),
        )

    if micro_m15_brains:
        strategies["micro_m15"] = MicroStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="micro_m15",
                strategy_family=_STRATEGY_FAMILY_MAP.get("micro_m15", "trend_following"),
                magic=90101,
                brain_types=MICRO_M15_GROUP["brain_types"],
                base_volume=_vol_cfg("micro_m15"),
                max_volume=_cfg("micro_m15", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("micro_m15", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("micro_m15", "tp", {}).get("base_atr_mult", 2.5),
                hard_sl_ratio=_cfg("micro_m15", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("micro_m15", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("micro_m15", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "micro_m15", "confidence_threshold", config.confidence_threshold
                ),
                spread_points=_cfg("micro_m15", "spread_points", 0.0),
                max_spread_points=_cfg("micro_m15", "max_spread_points", 0.0),
                long_bias_discount=_cfg("micro_m15", "direction_balance", {}).get(
                    "long_bias_discount", 0.03
                ),
                exit_flip_enabled=_exit_cfg("micro_m15", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("micro_m15", "time_exit_cycles", None),
                exit_zscore_enabled=_exit_cfg("micro_m15", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("micro_m15", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("micro_m15", "min_valid_brains", 2),
                timeframe=_cfg("micro_m15", "timeframe", "M15"),
                exit_hesitation_cycles=_exit_cfg("micro_m15", "hesitation_cycles", 0),
            ),
            micro_m15_brains,
            budget=StrategyBudget(
                "micro_m15",
                daily_loss_limit_pct=_cfg("micro_m15", "budget", {}).get(
                    "daily_loss_limit_pct", -0.02
                ),
                max_consecutive_losses=_cfg("micro_m15", "budget", {}).get(
                    "max_consecutive_losses", 6
                ),
            ),
        )

    if micro_h1_brains:
        strategies["micro_h1"] = MicroStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="micro_h1",
                strategy_family=_STRATEGY_FAMILY_MAP.get("micro_h1", "trend_following"),
                magic=90201,
                brain_types=MICRO_H1_GROUP["brain_types"],
                base_volume=_vol_cfg("micro_h1"),
                max_volume=_cfg("micro_h1", "max_volume", 0.02),
                base_sl_atr_mult=_cfg("micro_h1", "sl", {}).get("base_atr_mult", 1.8),
                base_tp_atr_mult=_cfg("micro_h1", "tp", {}).get("base_atr_mult", 2.8),
                hard_sl_ratio=_cfg("micro_h1", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("micro_h1", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("micro_h1", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "micro_h1", "confidence_threshold", config.confidence_threshold
                ),
                spread_points=_cfg("micro_h1", "spread_points", 0.0),
                max_spread_points=_cfg("micro_h1", "max_spread_points", 0.0),
                long_bias_discount=_cfg("micro_h1", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("micro_h1", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("micro_h1", "time_exit_cycles", None),
                exit_zscore_enabled=_exit_cfg("micro_h1", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("micro_h1", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("micro_h1", "min_valid_brains", 2),
                timeframe=_cfg("micro_h1", "timeframe", "H1"),
                exit_hesitation_cycles=_exit_cfg("micro_h1", "hesitation_cycles", 0),
            ),
            micro_h1_brains,
            budget=StrategyBudget(
                "micro_h1",
                daily_loss_limit_pct=_cfg("micro_h1", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("micro_h1", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
            ),
        )

    if statarb_brains:
        strategies["statarb_dynamic"] = StatArbStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="statarb_dynamic",
                strategy_family=_cfg("statarb_dynamic", "strategy_family", None)
                or _STRATEGY_FAMILY_MAP.get("statarb_dynamic", "trend_following"),
                magic=90003,
                brain_types=ARB_GROUP["brain_types"],
                base_volume=_vol_cfg("statarb_dynamic"),
                max_volume=_cfg("statarb_dynamic", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("statarb_dynamic", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("statarb_dynamic", "tp", {}).get("base_atr_mult", 3.0),
                hard_sl_ratio=_cfg("statarb_dynamic", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("statarb_dynamic", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("statarb_dynamic", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "statarb_dynamic", "confidence_threshold", config.confidence_threshold
                ),
                spread_points=_cfg("statarb_dynamic", "spread_points", 0.0),
                max_spread_points=_cfg("statarb_dynamic", "max_spread_points", 0.0),
                long_bias_discount=_cfg("statarb_dynamic", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("statarb_dynamic", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("statarb_dynamic", "time_exit_cycles", 40),
                exit_zscore_enabled=_exit_cfg("statarb_dynamic", "zscore_exit_enabled", True),
                exit_min_r=_exit_cfg("statarb_dynamic", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("statarb_dynamic", "min_valid_brains", 1),
                timeframe=_cfg("statarb_dynamic", "timeframe", "M5"),
                exit_hesitation_cycles=_exit_cfg("statarb_dynamic", "hesitation_cycles", 0),
                min_p_win=_cfg("statarb_dynamic", "min_p_win", 0.50),
            ),
            statarb_brains,
            budget=StrategyBudget(
                "statarb_dynamic",
                daily_loss_limit_pct=_cfg("statarb_dynamic", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("statarb_dynamic", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
            ),
        )

    if statarb_m15_brains:
        strategies["statarb_m15"] = StatArbStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="statarb_m15",
                strategy_family=_cfg("statarb_m15", "strategy_family", None)
                or _STRATEGY_FAMILY_MAP.get("statarb_m15", "trend_following"),
                magic=90103,
                brain_types=STATARB_M15_GROUP["brain_types"],
                base_volume=_vol_cfg("statarb_m15"),
                max_volume=_cfg("statarb_m15", "max_volume", 0.02),
                base_sl_atr_mult=_cfg("statarb_m15", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("statarb_m15", "tp", {}).get("base_atr_mult", 4.0),
                hard_sl_ratio=_cfg("statarb_m15", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("statarb_m15", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("statarb_m15", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg(
                    "statarb_m15", "confidence_threshold", config.confidence_threshold
                ),
                spread_points=_cfg("statarb_m15", "spread_points", 0.0),
                max_spread_points=_cfg("statarb_m15", "max_spread_points", 0.0),
                long_bias_discount=_cfg("statarb_m15", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("statarb_m15", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("statarb_m15", "time_exit_cycles", 120),
                exit_zscore_enabled=_exit_cfg("statarb_m15", "zscore_exit_enabled", True),
                exit_min_r=_exit_cfg("statarb_m15", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("statarb_m15", "min_valid_brains", 1),
                timeframe=_cfg("statarb_m15", "timeframe", "M15"),
                exit_hesitation_cycles=_exit_cfg("statarb_m15", "hesitation_cycles", 0),
                min_p_win=_cfg("statarb_m15", "min_p_win", 0.50),
            ),
            statarb_m15_brains,
            budget=StrategyBudget(
                "statarb_m15",
                daily_loss_limit_pct=_cfg("statarb_m15", "budget", {}).get(
                    "daily_loss_limit_pct", -0.01
                ),
                max_consecutive_losses=_cfg("statarb_m15", "budget", {}).get(
                    "max_consecutive_losses", 3
                ),
            ),
        )

    if barrier_12bar_meta_brains:
        strategies["barrier_12bar_meta"] = BarrierStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="barrier_12bar_meta",
                strategy_family=_cfg("barrier_12bar_meta", "strategy_family", None)
                or _STRATEGY_FAMILY_MAP.get("barrier_12bar_meta", "trend_following"),
                magic=90014,
                brain_types=BARRIER_12BAR_META_GROUP["brain_types"],
                base_volume=_vol_cfg("barrier_12bar_meta"),
                max_volume=_cfg("barrier_12bar_meta", "max_volume", 0.0),
                base_sl_atr_mult=_cfg("barrier_12bar_meta", "sl", {}).get("base_atr_mult", 3.0),
                base_tp_atr_mult=_cfg("barrier_12bar_meta", "tp", {}).get("base_atr_mult", 1.5),
                hard_sl_ratio=_cfg("barrier_12bar_meta", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("barrier_12bar_meta", "sl", {}).get("min_sl_distance", 8.0),
                min_rr_ratio=_cfg("barrier_12bar_meta", "sl", {}).get("min_rr_ratio", 0.5),
                confidence_threshold=_cfg("barrier_12bar_meta", "confidence_threshold", 0.40),
                spread_points=_cfg("barrier_12bar_meta", "spread_points", 0.0),
                max_spread_points=_cfg("barrier_12bar_meta", "max_spread_points", 0.0),
                long_bias_discount=_cfg("barrier_12bar_meta", "direction_balance", {}).get(
                    "long_bias_discount", 0.05
                ),
                exit_flip_enabled=_exit_cfg("barrier_12bar_meta", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("barrier_12bar_meta", "time_exit_cycles", 60),
                exit_zscore_enabled=_exit_cfg("barrier_12bar_meta", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("barrier_12bar_meta", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("barrier_12bar_meta", "min_valid_brains", 1),
                timeframe=_cfg("barrier_12bar_meta", "timeframe", "M5"),
                exit_hesitation_cycles=_exit_cfg("barrier_12bar_meta", "hesitation_cycles", 12),
            ),
            barrier_12bar_meta_brains,
            budget=StrategyBudget(
                "barrier_12bar_meta",
                daily_loss_limit_pct=_cfg("barrier_12bar_meta", "budget", {}).get(
                    "daily_loss_limit_pct", -0.03
                ),
                max_consecutive_losses=_cfg("barrier_12bar_meta", "budget", {}).get(
                    "max_consecutive_losses", 5
                ),
            ),
        )

    # ── Swing strategies (D1 features, TF-specific barrier contracts) ──
    if daily_swing_brains:
        strategies["daily_swing"] = SwingStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="daily_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("daily_swing", "trend_following"),
                magic=90301,
                brain_types=DAILY_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("daily_swing"),
                max_volume=_cfg("daily_swing", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("daily_swing", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("daily_swing", "tp", {}).get("base_atr_mult", 3.5),
                hard_sl_ratio=_cfg("daily_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("daily_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("daily_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("daily_swing", "confidence_threshold", 0.45),
                spread_points=_cfg("daily_swing", "spread_points", 0.0),
                max_spread_points=_cfg("daily_swing", "max_spread_points", 0.0),
                long_bias_discount=_cfg("daily_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("daily_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("daily_swing", "time_exit_cycles", 1440),
                exit_zscore_enabled=_exit_cfg("daily_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("daily_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("daily_swing", "min_valid_brains", 2),
                timeframe=_cfg("daily_swing", "timeframe", "D1"),
                exit_hesitation_cycles=_exit_cfg("daily_swing", "hesitation_cycles", 0),
            ),
            daily_swing_brains,
            budget=StrategyBudget(
                "daily_swing",
                daily_loss_limit_pct=_cfg("daily_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.02
                ),
                max_consecutive_losses=_cfg("daily_swing", "budget", {}).get(
                    "max_consecutive_losses", 3
                ),
            ),
        )

    if m15_swing_brains:
        strategies["m15_swing"] = SwingStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="m15_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("m15_swing", "trend_following"),
                magic=90310,
                brain_types=M15_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("m15_swing"),
                max_volume=_cfg("m15_swing", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("m15_swing", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("m15_swing", "tp", {}).get("base_atr_mult", 1.5),
                hard_sl_ratio=_cfg("m15_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("m15_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("m15_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("m15_swing", "confidence_threshold", 0.45),
                spread_points=_cfg("m15_swing", "spread_points", 0.0),
                max_spread_points=_cfg("m15_swing", "max_spread_points", 0.0),
                min_p_win=_cfg("m15_swing", "min_p_win", 0.50),
                long_bias_discount=_cfg("m15_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("m15_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("m15_swing", "time_exit_cycles", 72),
                exit_zscore_enabled=_exit_cfg("m15_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("m15_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("m15_swing", "min_valid_brains", 1),
                timeframe=_cfg("m15_swing", "timeframe", "M15"),
                exit_hesitation_cycles=_exit_cfg("m15_swing", "hesitation_cycles", 0),
            ),
            m15_swing_brains,
            budget=StrategyBudget(
                "m15_swing",
                daily_loss_limit_pct=_cfg("m15_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("m15_swing", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
            ),
        )

    if m30_swing_brains:
        strategies["m30_swing"] = SwingStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="m30_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("m30_swing", "trend_following"),
                magic=90320,
                brain_types=M30_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("m30_swing"),
                max_volume=_cfg("m30_swing", "max_volume", 0.03),
                base_sl_atr_mult=_cfg("m30_swing", "sl", {}).get("base_atr_mult", 1.5),
                base_tp_atr_mult=_cfg("m30_swing", "tp", {}).get("base_atr_mult", 1.5),
                hard_sl_ratio=_cfg("m30_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("m30_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("m30_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("m30_swing", "confidence_threshold", 0.45),
                spread_points=_cfg("m30_swing", "spread_points", 0.0),
                max_spread_points=_cfg("m30_swing", "max_spread_points", 0.0),
                min_p_win=_cfg("m30_swing", "min_p_win", 0.50),
                long_bias_discount=_cfg("m30_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("m30_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("m30_swing", "time_exit_cycles", 36),
                exit_zscore_enabled=_exit_cfg("m30_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("m30_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("m30_swing", "min_valid_brains", 1),
                timeframe=_cfg("m30_swing", "timeframe", "M30"),
                exit_hesitation_cycles=_exit_cfg("m30_swing", "hesitation_cycles", 0),
            ),
            m30_swing_brains,
            budget=StrategyBudget(
                "m30_swing",
                daily_loss_limit_pct=_cfg("m30_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("m30_swing", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
            ),
        )

    if btc_swing_brains:
        strategies["btc_swing"] = SwingStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="btc_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("btc_swing", "trend_following"),
                magic=90410,
                brain_types=BTC_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("btc_swing"),
                max_volume=_cfg("btc_swing", "max_volume", 0.05),
                base_sl_atr_mult=_cfg("btc_swing", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("btc_swing", "tp", {}).get("base_atr_mult", 2.5),
                hard_sl_ratio=_cfg("btc_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("btc_swing", "sl", {}).get("min_sl_distance", 200.0),
                min_rr_ratio=_cfg("btc_swing", "sl", {}).get("min_rr_ratio", 0.85),
                confidence_threshold=_cfg("btc_swing", "confidence_threshold", 0.35),
                spread_points=_cfg("btc_swing", "spread_points", 1400),
                max_spread_points=_cfg("btc_swing", "max_spread_points", 2500),
                min_p_win=_cfg("btc_swing", "min_p_win", 0.45),
                long_bias_discount=_cfg("btc_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("btc_swing", "flip_exit_enabled", True),
                exit_time_cycles=_exit_cfg("btc_swing", "time_exit_cycles", 36),
                exit_zscore_enabled=_exit_cfg("btc_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("btc_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("btc_swing", "min_valid_brains", 1),
                timeframe=_cfg("btc_swing", "timeframe", "M30"),
                exit_hesitation_cycles=_exit_cfg("btc_swing", "hesitation_cycles", 3),
            ),
            btc_swing_brains,
            budget=StrategyBudget(
                "btc_swing",
                daily_loss_limit_pct=_cfg("btc_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.03
                ),
                max_consecutive_losses=_cfg("btc_swing", "budget", {}).get(
                    "max_consecutive_losses", 5
                ),
                cooldown_minutes=_cfg("btc_swing", "budget", {}).get("cooldown_minutes", 0),
            ),
        )

    # ── DQAF-20260615-002: Dedicated Survival strategy for V9_H1 ──
    if btc_swing_h1_brains:
        strategies["btc_swing_h1"] = SwingStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="btc_swing_h1",
                strategy_family=_STRATEGY_FAMILY_MAP.get("btc_swing_h1", "trend_following"),
                magic=90411,
                brain_types={"lightgbm_v1"},
                base_volume=_vol_cfg("btc_swing_h1"),
                max_volume=_cfg("btc_swing_h1", "max_volume", 0.1),
                base_sl_atr_mult=_cfg("btc_swing_h1", "sl", {}).get("base_atr_mult", 3.0),
                base_tp_atr_mult=_cfg("btc_swing_h1", "tp", {}).get("base_atr_mult", 2.0),
                hard_sl_ratio=_cfg("btc_swing_h1", "sl", {}).get("hard_sl_ratio", 2.0),
                min_sl_distance=_cfg("btc_swing_h1", "sl", {}).get("min_sl_distance", 150.0),
                min_rr_ratio=_cfg("btc_swing_h1", "sl", {}).get("min_rr_ratio", 0.5),
                confidence_threshold=_cfg("btc_swing_h1", "confidence_threshold", 0.40),
                spread_points=_cfg("btc_swing_h1", "spread_points", 200),
                max_spread_points=_cfg("btc_swing_h1", "max_spread_points", 3000),
                min_p_win=_cfg("btc_swing_h1", "min_p_win", 0.55),
                long_bias_discount=_cfg("btc_swing_h1", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("btc_swing_h1", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("btc_swing_h1", "time_exit_cycles", 144),
                exit_zscore_enabled=_exit_cfg("btc_swing_h1", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("btc_swing_h1", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("btc_swing_h1", "min_valid_brains", 1),
                timeframe=_cfg("btc_swing_h1", "timeframe", "H1"),
                exit_hesitation_cycles=_exit_cfg("btc_swing_h1", "hesitation_cycles", 24),
            ),
            btc_swing_h1_brains,
            budget=StrategyBudget(
                "btc_swing_h1",
                daily_loss_limit_pct=_cfg("btc_swing_h1", "budget", {}).get(
                    "daily_loss_limit_pct", -0.04
                ),
                max_consecutive_losses=_cfg("btc_swing_h1", "budget", {}).get(
                    "max_consecutive_losses", 4
                ),
                cooldown_minutes=_cfg("btc_swing_h1", "budget", {}).get("cooldown_minutes", 0),
            ),
        )

    if h1_swing_brains:
        strategies["h1_swing"] = SwingStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="h1_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("h1_swing", "trend_following"),
                magic=90330,
                brain_types=H1_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("h1_swing"),
                max_volume=_cfg("h1_swing", "max_volume", 0.02),
                base_sl_atr_mult=_cfg("h1_swing", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("h1_swing", "tp", {}).get("base_atr_mult", 3.5),
                hard_sl_ratio=_cfg("h1_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("h1_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("h1_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("h1_swing", "confidence_threshold", 0.45),
                min_p_win=_cfg("h1_swing", "min_p_win", 0.50),
                spread_points=_cfg("h1_swing", "spread_points", 0.0),
                max_spread_points=_cfg("h1_swing", "max_spread_points", 0.0),
                long_bias_discount=_cfg("h1_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("h1_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("h1_swing", "time_exit_cycles", 288),
                exit_zscore_enabled=_exit_cfg("h1_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("h1_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("h1_swing", "min_valid_brains", 1),
                timeframe=_cfg("h1_swing", "timeframe", "H1"),
                exit_hesitation_cycles=_exit_cfg("h1_swing", "hesitation_cycles", 0),
            ),
            h1_swing_brains,
            budget=StrategyBudget(
                "h1_swing",
                daily_loss_limit_pct=_cfg("h1_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("h1_swing", "budget", {}).get(
                    "max_consecutive_losses", 3
                ),
            ),
        )

    if h4_swing_brains:
        strategies["h4_swing"] = SwingStrategy(
            StrategyLineConfig(
                symbol=config.symbol,
                base_dir=config.base_dir,
                contract_size=get_asset(config.symbol).contract_size,
                name="h4_swing",
                strategy_family=_STRATEGY_FAMILY_MAP.get("h4_swing", "trend_following"),
                magic=90340,
                brain_types=H4_SWING_GROUP["brain_types"],
                base_volume=_vol_cfg("h4_swing"),
                max_volume=_cfg("h4_swing", "max_volume", 0.02),
                base_sl_atr_mult=_cfg("h4_swing", "sl", {}).get("base_atr_mult", 2.0),
                base_tp_atr_mult=_cfg("h4_swing", "tp", {}).get("base_atr_mult", 4.0),
                hard_sl_ratio=_cfg("h4_swing", "sl", {}).get("hard_sl_ratio", 1.5),
                min_sl_distance=_cfg("h4_swing", "sl", {}).get("min_sl_distance", 0.0),
                min_rr_ratio=_cfg("h4_swing", "sl", {}).get("min_rr_ratio", 0.0),
                confidence_threshold=_cfg("h4_swing", "confidence_threshold", 0.45),
                min_p_win=_cfg("h4_swing", "min_p_win", 0.50),
                spread_points=_cfg("h4_swing", "spread_points", 0.0),
                max_spread_points=_cfg("h4_swing", "max_spread_points", 0.0),
                long_bias_discount=_cfg("h4_swing", "direction_balance", {}).get(
                    "long_bias_discount", 0.0
                ),
                exit_flip_enabled=_exit_cfg("h4_swing", "flip_exit_enabled", False),
                exit_time_cycles=_exit_cfg("h4_swing", "time_exit_cycles", 864),
                exit_zscore_enabled=_exit_cfg("h4_swing", "zscore_exit_enabled", False),
                exit_min_r=_exit_cfg("h4_swing", "min_r_for_hold", 0.3),
                min_valid_brains=_cfg("h4_swing", "min_valid_brains", 1),
                timeframe=_cfg("h4_swing", "timeframe", "H4"),
                exit_hesitation_cycles=_exit_cfg("h4_swing", "hesitation_cycles", 0),
            ),
            h4_swing_brains,
            budget=StrategyBudget(
                "h4_swing",
                daily_loss_limit_pct=_cfg("h4_swing", "budget", {}).get(
                    "daily_loss_limit_pct", -0.015
                ),
                max_consecutive_losses=_cfg("h4_swing", "budget", {}).get(
                    "max_consecutive_losses", 2
                ),
            ),
        )

    # ── Rule-engine strategies (no ML brains, pure math) ──────────────────
    # Blind Spot / Phase 1 (2026-06-13): StructuralSwingV1 was defined in
    # core/strategies/ and configured in live.yaml but never instantiated
    # because strategy_builder only knew about Barrier/Micro/StatArb/Swing.
    # Scan for rule_engine entries and create RuleEngineStrategyWrapper.
    _rule_engine_names: list[str] = []
    for _sname, _scfg in config.strategy_configs.items():
        if _scfg.get("rule_engine") and _cfg(_sname, "enabled", True):
            _rule_engine_names.append(_sname)

    for _sname in _rule_engine_names:
        _re_name = config.strategy_configs[_sname].get("rule_engine", "")
        if _re_name == "structural_swing_v1":
            from core.strategies.structural_swing_v1 import StructuralSwingV1

            _sl_cfg = _cfg(_sname, "sl", {})
            _tp_cfg = _cfg(_sname, "tp", {})
            _tick_size = 0.001 if config.symbol.startswith("XAU") else 0.01

            _engine = StructuralSwingV1(
                sl_atr_mult=float(_sl_cfg.get("base_atr_mult", 3.0)),
                tp_atr_mult=float(_tp_cfg.get("base_atr_mult", 1.5)),
                horizon_bars=int(_cfg(_sname, "exit", {}).get("time_exit_cycles", 12)),
                spread_points=float(_cfg(_sname, "spread_points", 30)),
                slippage_points=float(_cfg(_sname, "slippage_points", 10)),
                tick_size=_tick_size,
            )
            strategies[_sname] = RuleEngineStrategyWrapper(
                strategy_name=_sname,
                magic=int(_cfg(_sname, "magic", 90501)),
                rule_engine=_engine,
                cooldown_bars=int(_cfg(_sname, "cooldown_bars", 3)),
                max_positions_per_direction=int(_cfg(_sname, "max_positions_per_direction", 1)),
                base_volume=float(_cfg(_sname, "base_volume", 0.01)),
            )
            print(
                json.dumps(
                    {
                        "event": "rule_engine_strategy_created",
                        "time": _utc_iso(),
                        "strategy": _sname,
                        "rule_engine": _re_name,
                        "magic": _cfg(_sname, "magic", 90501),
                        "sl_atr_mult": _engine.sl_mult,
                        "tp_atr_mult": _engine.tp_mult,
                        "horizon_bars": _engine.horizon,
                        "message": "Zero-ML rule-based strategy integrated into live pipeline",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        else:
            print(
                json.dumps(
                    {
                        "event": "rule_engine_unknown",
                        "time": _utc_iso(),
                        "strategy": _sname,
                        "rule_engine": _re_name,
                        "warning": "Unknown rule_engine type — strategy will NOT be created",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    return strategies
