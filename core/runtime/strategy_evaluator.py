"""Multi-strategy evaluation — independent strategy evaluation + risk + execution queue.

Extracted from live_cycle.py per the Strangler Fig pattern (#7).
Runs each strategy line independently, applies regime gates, cooldown,
family spacing, portfolio risk checks, and √N correlation discount.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np

from core.execution.cross_strategy_coordinator import CrossStrategyCoordinator
from core.execution.execution_queue import ExecutionQueue
from core.execution.portfolio_risk import PortfolioRiskController, RiskVerdict
from core.execution.pre_trade_guards import check_feature_vector, repair_feature_vector
from core.execution.regime_gate import RegimeGate
from core.execution.strategy_context import StrategyEvaluationContext
from core.runtime.time_utils import _utc_iso  # consolidated

# ── R1 Gate silence protection state (FIX-20260613-083) ──
# Module-level dict tracks consecutive R1-blocked cycles to prevent
# persistent zero-open silence in trending markets (>4h → relax).
_r1_silence_state: dict[str, int] = {"consecutive_blocks": 0, "last_block_cycle": 0}


def _get_r1_silence_state() -> dict[str, int]:
    return _r1_silence_state


# ── OOD Gateway (DQAF-20260705-064 P2) ────────────────────────────────────
# Module-level singleton — OOD configs are loaded once and cached.
# Schema resolution maps strategy names to OOD schema identifiers.

_ood_gateway: Any = None  # OODGateway | None


def _get_ood_gateway() -> Any:
    """Return the module-level OOD gateway singleton, initialising on first call."""
    global _ood_gateway
    if _ood_gateway is None:
        from core.execution.ood_gateway import OODGateway

        _ood_gateway = OODGateway(data_dir="data_btc")
    return _ood_gateway


# ── FIX-20260724-001: ATR Ratio dead-market circuit breaker ──────────────────
# Replaces FIX-20260719-001 (M5_Vol_ZScore hard gate) which was structurally
# biased: CFD tick_volume has burst-decay distribution + frequent identical
# consecutive values → Vol_ZScore 94% non-positive over 39,714 records.
#
# New gate uses price-action-based ATR ratio:
#   atr_ratio = current_atr / mean(recent_atr_buffer_50)
#   BLOCK when atr_ratio < 0.5  (current volatility < 50% of recent baseline)
#
# The baseline is the 50-bar buffer_sample from regime_detector_state.json,
# NOT the 63-day EWMA atr_mean (which lags too much for regime-change detection).
_ATR_DEAD_MARKET_RATIO: float = 0.5  # block entries when current_atr/mean(buffer) < this
_ATR_BUFFER_MIN_SAMPLES: int = 10  # minimum buffer_sample entries required to arm gate

# M5_Vol_ZScore index in the V9 Institutional 40-dim feature vector.
# Still present in the vector (feature calc unchanged per IC veto on Feature Drift)
# but NO LONGER used as a circuit breaker — replaced by ATR ratio gate above.
_V9_M5_VOL_ZSCORE_IDX: int = 5  # 0-indexed, for OOD diagnostic logging only

# DQAF-20260807-001 (IC 雷霆裁决 — Option A Chop Filter): God's Eye HARD
# VETO threshold on NEW entries.  When health < this (Defensive) OR chop is
# detected, the entry is blocked outright (BLOCKED_BY_GODSEYE) instead of
# the previous fail-open reduce-only behaviour.  Calibrated from the 08-06
# XAU evidence: h1@09:44 entered with health=0.52 + chop and lost -77.9;
# m15@14:15 with chop=true lost -66.4.
_GODS_EYE_HARD_BLOCK_HEALTH: float = 0.55

# Module-level cache for ATR buffer_sample (refreshed every 60s from disk).
# regime_detector_state.json is updated each cycle by the RegimeDetector,
# so a short TTL keeps us in sync without re-reading on every strategy eval.
_atr_buffer_cache: dict[str, Any] = {"data": None, "ts": 0.0, "base_dir": ""}


def _load_atr_buffer_sample(base_dir: str) -> list[float] | None:
    """Load buffer_sample from regime_detector_state.json, cached for 60s.

    The buffer_sample is the RegimeDetector's rolling window of recent ATR
    values (last 50 bars).  Using this as the denominator in the ATR ratio
    gives us a "local relative dead market" detector that is NOT polluted
    by the 63-day EWMA (which lags months behind regime changes).
    """
    global _atr_buffer_cache
    _now = time.monotonic()
    if (
        _atr_buffer_cache["data"] is not None
        and _atr_buffer_cache["base_dir"] == base_dir
        and (_now - _atr_buffer_cache["ts"]) < 60.0
    ):
        return _atr_buffer_cache["data"]

    if not base_dir:
        return None

    try:
        from pathlib import Path as _Path

        _state_path = _Path(base_dir) / "regime_detector_state.json"
        if not _state_path.exists():
            _atr_buffer_cache = {"data": None, "ts": _now, "base_dir": base_dir}
            return None

        _state = json.loads(_state_path.read_text(encoding="utf-8"))
        _buf = _state.get("buffer_sample")
        if isinstance(_buf, list) and len(_buf) > 0:
            _buf_float = [float(v) for v in _buf]
            _atr_buffer_cache = {"data": _buf_float, "ts": _now, "base_dir": base_dir}
            return _buf_float
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass

    _atr_buffer_cache = {"data": None, "ts": _now, "base_dir": base_dir}
    return None


def _resolve_ood_schema(strategy_name: str, strategy: Any) -> str:
    """Resolve the OOD schema name for a given strategy.

    Most strategies use the canonical 40-dim V9 institutional schema.
    Specialised strategies override based on their brain configuration.
    """
    # Per-strategy overrides
    _overrides: dict[str, str] = {
        "micro_3bar": "v4.3_microstructure_9",
        "micro_m15": "v4.3_microstructure_9",
        "micro_h1": "v4.3_microstructure_9",
    }
    if strategy_name in _overrides:
        return _overrides[strategy_name]

    # Probe the strategy's brains for their feature schema
    try:
        brains = getattr(strategy, "brains", None) or []
        for b_info in brains:
            if isinstance(b_info, dict):
                schema = b_info.get("feature_schema", "") or b_info.get("feature_schema_id", "")
            else:
                schema = getattr(b_info, "feature_schema", "") or getattr(
                    b_info, "feature_schema_id", ""
                )
            if schema:
                # Map training schema → OOD schema
                # btc_macro_enhanced_41 uses v9_institutional_40 OOD (same feature space)
                # swing_enhanced_* schemas also share the V9 feature space
                if "v9" in schema or "macro" in schema or "swing" in schema:
                    return "v9_institutional_40"
                if "micro" in schema:
                    return "v4.3_microstructure_9"
                return schema
    except (RuntimeError, ValueError, AttributeError, TypeError):
        pass

    # Fallback: most strategies use V9 institutional
    return "v9_institutional_40"


# ── Regime Direction Gate (FIX-20260614-B2: Feature-Not-Gate complete) ──
# FIX-20260613-090: Wired the RegimeDirectionGate into the live pipeline.
# FIX-20260614-B2: ADX gating phased out — brains now learn regime awareness
# natively from OU/Hurst features.  Priority 0 physics override (Theta > P75
# AND Hurst < P25 → extreme mean-reversion → "ranging") remains as circuit breaker.
# The gate now provides diagnostic audit only — all signals pass through.
from core.execution.regime_direction_gate import RegimeDirectionGate

_direction_gate = RegimeDirectionGate(adx_threshold=25, stale_warn_cycles=48)


# ── DQAF-20260806-003 Option B2: GodsEye health deadband ramp ───────────────
# IC Approved 2026-08-06 (投委会终局裁决): 否决 B1 (mode 条件恒 1.0 — 执行端对
# normal 态 health 0.9→0.75 的降级零感知, 违背 GodsEye 连续微调初衷); 否决 B3
# (乘数层 floor 兜底 — 与 Ω 终门地板叠地板, 违背 FIX-20260730-010 单闸门哲学);
# 批准 B2 = Deadband + Proportional Control.
#
#   health >= 0.70  → 1.0   (健康死区: 环境安全, 不干预资金管理层数学 — 彻底解决
#                            0.02 阈值共振, 健康 GodsEye 不再把已收敛到 min_economic
#                            的 volume 再 shave 到 floor 之下)
#   0.25 <= health < 0.70 → 连续线性斜坡 0.25x..1.0x (比例控制带: volume 平滑降级,
#                           直至跌破 min_economic 被 Ω 终门物理绞杀)
#   health < 0.25   → 0.25  (钳位既有最坏乘数 floor — 红线: 所有 Floor/Multiplier 不变)
def _gods_eye_health_vol_mult(health: float) -> float:
    _HEALTH_DEADBAND = 0.70  # health >= deadband -> no volume intervention
    _HEALTH_VOL_FLOOR = 0.25  # worst-case multiplier (pre-existing floor, preserved)
    if health >= _HEALTH_DEADBAND:
        return 1.0
    if health <= _HEALTH_VOL_FLOOR:
        return _HEALTH_VOL_FLOOR
    _t = (health - _HEALTH_VOL_FLOOR) / (_HEALTH_DEADBAND - _HEALTH_VOL_FLOOR)
    return _HEALTH_VOL_FLOOR + (1.0 - _HEALTH_VOL_FLOOR) * _t


def evaluate_strategy_lines(
    *,
    strategy_lines: dict[str, Any],
    feature_vector: Any,
    micro_feature_vector: Any,
    mid_price: float | None,
    bid: float | None,
    ask: float | None,
    current_atr: float,
    tf_atr_map: dict[str, float] | None = None,  # FIX-20260706-027: per-TF ATR
    regime_info: dict[str, Any],
    regime_gate: RegimeGate | None,
    regime_modulation: Any = None,
    trend_direction: str = "neutral",
    trend_strength: float = 0.0,
    h4_trend_strength: float = 0.0,
    macro_regime: str = "mixed",
    risk_budget_usd: float = 0.0,
    sl_streak_blocked_until: dict[str, float] | None = None,
    portfolio_risk: PortfolioRiskController,
    execution_queue: ExecutionQueue,
    tracker: Any,
    pnl_ledger: Any,
    current_positions: dict[str, dict[str, Any]],
    session_volume_mult: float = 1.0,
    health_volume_mult: float = 1.0,
    micro_sequences: dict[str, Any] | None = None,
    daily_feature_vector: Any = None,
    account_equity: float | None = None,
    cycle_count: int = 0,
    meta_signal_filter: Any = None,
    meta_filter_gate: Any = None,
    conformal_ou_gate: Any = None,
    microstructure_gate: Any = None,
    micro_feature_dict: dict[str, float] | None = None,
    cooldown_registry: Any = None,
    family_entry_tracker: Any = None,
    mtf_price_service: Any = None,
    meta_feature_vector: Any = None,
    # ── FIX-20260607-007: trend maturity signals ──
    hurst: float | None = None,
    kalman_velocity_bps: float | None = None,
    # ── FIX-20260606-131: reentry guard front-placement (P2.6) ──
    reentry_states: dict[str, Any] | None = None,
    reentry_sl_cooldown: float | None = None,
    reentry_sl_penalty: float | None = None,
    reentry_bleed_cooldown: float | None = None,
    reentry_bleed_penalty: float | None = None,
    # ── FIX-20260606-138: bootstrap degraded flag (Fail-Closed) ──
    bootstrap_degraded: bool = False,
    btc_augment: Any = None,  # FIX-20260613-046: pre-computed 37-dim BTC vector
    # ── FIX-20260609-011: governance degradation gate ──
    governance_state: dict[str, Any] | None = None,
    # ── FIX-20260611-022: data-health degradation constraints ──
    degradation_constraints: Any | None = None,
    # ── P4-2: Cross-strategy coordinator (2026-06-13) ──
    cross_strategy_coordinator: CrossStrategyCoordinator | None = None,
    # ── FIX-20260625-090: God's Eye cross-instrument regime consensus ──
    gods_eye_verdict: Any = None,
    # ── FIX-20260615-006/C8: required — no default ──
    base_dir: str = "",
    # ── FIX-20260629-188 (P1-3): time-based session gating ──
    blocked_entry_hours: list[int] | None = None,
) -> dict[str, Any]:
    """Run independent strategy evaluations + portfolio risk + execution queue.

    Returns a summary dict for logging.

    When *bootstrap_degraded* is True (restart state restoration failed),
    ALL trades are blocked — the system defaults to Fail-Closed rather than
    silently allowing trades through empty guard state.
    """
    # ── FIX-20260606-138: Fail-Closed on bootstrap degradation ──
    if bootstrap_degraded:
        import json as _json_fc

        _blocked_summary: dict[str, Any] = {
            "event": "gate_chain_blocked",
            "reason": "bootstrap_degraded_fail_closed",
            "time": _utc_iso(),
            "message": (
                "Restart state bootstrap failed — reentry guard, cooldown, "
                "and budget state could not be verified.  All trades are "
                "blocked until manual intervention confirms state integrity."
            ),
            "action_required": (
                "Check journal file integrity and restart the system. "
                "If journal is intact, review bootstrap logs for errors."
            ),
            "strategies_blocked": sorted(strategy_lines.keys()),
        }
        print(_json_fc.dumps(_blocked_summary, ensure_ascii=False), flush=True)
        return {
            "decisions_map": {},
            "trade_decisions": 0,
            "queued": 0,  # FIX-20260630-198: ensure key parity with normal return
            "active_strategies": list(strategy_lines.keys()),
            "strategy_results": [
                {
                    "strategy": sname,
                    "should_trade": False,
                    "direction": "neutral",
                    "confidence": 0.0,
                    "reason": "bootstrap_degraded_fail_closed",
                }
                for sname in strategy_lines
            ],
        }

    # ── FIX-20260629-188 (P1-3): Time-based session gating ──
    # Block ALL new entries during statistically identified loss-making
    # hours (00:00-01:00 UTC -$44.32, 20:00-21:00 UTC -$43.83, n=506 XAU
    # swing closes).  These two hours account for -$88.15 out of -$89.03
    # total swing PnL.  Existing positions continue to be managed
    # (trailing stops, confidence decay exits) — only NEW entries are
    # blocked.
    if blocked_entry_hours:
        _current_utc_hour = datetime.now(UTC).hour
        if _current_utc_hour in blocked_entry_hours:
            import json as _json_sg

            _sg_summary: dict[str, Any] = {
                "event": "gate_chain_blocked",
                "reason": "session_time_blocked",
                "time": _utc_iso(),
                "blocked_hour_utc": _current_utc_hour,
                "blocked_hours_config": blocked_entry_hours,
                "message": (
                    f"UTC hour {_current_utc_hour:02d}:00 is in the "
                    f"blocked_entry_hours list {blocked_entry_hours}. "
                    "New entries blocked — existing positions continue "
                    "to be managed."
                ),
                "strategies_blocked": sorted(strategy_lines.keys()),
            }
            print(_json_sg.dumps(_sg_summary, ensure_ascii=False), flush=True)
            return {
                "decisions_map": {},
                "trade_decisions": 0,
                "queued": 0,  # FIX-20260630-198: ensure key parity with normal return
                "active_strategies": list(strategy_lines.keys()),
                "strategy_results": [
                    {
                        "strategy": sname,
                        "should_trade": False,
                        "direction": "neutral",
                        "confidence": 0.0,
                        "reason": f"session_time_blocked_utc_{_current_utc_hour:02d}",
                    }
                    for sname in strategy_lines
                ],
            }

    decisions: list[Any] = []
    _blocked = sl_streak_blocked_until or {}
    strategy_results: list[dict[str, Any]] = []

    for sname, strategy in strategy_lines.items():
        gate_mode = "full"
        if regime_gate is not None:
            base_mode = regime_gate.get_strategy_mode(sname)
            if regime_modulation is not None and hasattr(regime_modulation, "strategy_activation"):
                from core.execution.regime_gate import get_stricter_mode

                gate_mode = get_stricter_mode(base_mode, regime_modulation.strategy_activation)
            else:
                gate_mode = base_mode

        # ── Per-strategy SL streak block ──
        if sname in _blocked and time.time() < _blocked[sname]:
            strategy_results.append(
                {"strategy": sname, "action": "blocked_sl_streak", "blocked_until": _blocked[sname]}
            )
            continue

        # ── M15 bar-boundary gating ──
        # FIX-20260714-005: Only live-mode M15 strategies are gated to
        # M15 bar boundaries (0/15/30/45). Probation and shadow modes
        # evaluate every cycle — they need golden_master signal recording
        # for observability.  The original gate (FIX-20260713-007) only
        # bypassed shadow, which silently killed probation-mode M15.
        _tf = getattr(getattr(strategy, "config", None), "timeframe", "M5")
        if _tf == "M15" and mtf_price_service is not None:
            _utc_minute = datetime.now(UTC).minute
            if not mtf_price_service.is_m15_boundary(_utc_minute):
                _mode = getattr(getattr(strategy, "config", None), "mode", None)
                if _mode == "live":
                    continue
        _effective_mid = mid_price

        # ── Cut 1: Absolute Refractory Period (cooldown check) ──
        if cooldown_registry is not None:
            _cd_allowed, _cd_reason = cooldown_registry.check_cooldown(
                sname,
                "long",
            )
            if not _cd_allowed:
                pass

        # ── Cut 2: Family entry spacing check (pre-evaluate) ──
        if family_entry_tracker is not None:
            from core.execution.pre_trade_guards import strategy_to_family

            _fam = strategy_to_family(sname)
            if _fam != sname:
                pass

        # ── OU-augmented feature vector for meta-labeler strategy ──
        _fv = feature_vector
        if sname == "barrier_12bar_meta" and meta_feature_vector is not None:
            _fv = meta_feature_vector

        # ── Blind Spot 1: Sanity Bounds Gate ───────────────────────────
        # Repair NaN/Inf in feature vector before inference, then check
        # for extreme outliers (abs(Z) > 10.0).  A single poisoned feature
        # value can trigger spurious high-confidence signals from tree-based
        # models (XGB/LGB).  Drop the cycle if irreparable.
        _fv, _repair_log = repair_feature_vector(_fv)
        if _repair_log["repaired"]:
            print(
                json.dumps(
                    {
                        "event": "feature_vector_repaired",
                        "time": _utc_iso(),
                        "strategy": sname,
                        "nan_filled": _repair_log["nan_filled"],
                        "inf_filled": _repair_log["inf_filled"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        _fv_check = check_feature_vector(_fv, max_nan_ratio=0.0)
        if not _fv_check["passed"]:
            print(
                json.dumps(
                    {
                        "event": "feature_vector_blocked",
                        "time": _utc_iso(),
                        "strategy": sname,
                        "issues": _fv_check["issues"],
                        "reason": "sanity_bounds_gate",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            strategy_results.append(
                {
                    "strategy": sname,
                    "should_trade": False,
                    "direction": "neutral",
                    "confidence": 0.0,
                    "reason": "sanity_bounds_gate:" + ",".join(_fv_check["issues"]),
                }
            )
            continue

        # ── Extreme value gate: catch genuine data corruption (e.g. float
        # overflow, corrupted memory).  Threshold is deliberately high (1e6)
        # because non-normalized features (BTC co_ratio, tick_velocity, XAU
        # macro ratios) can legitimately reach 200-500.  Values > 1e6 are
        # almost certainly floating-point errors or memory corruption.
        # NaN/Inf are already handled by repair_feature_vector() above.
        # FIX-20260613-058: threshold raised from 10.0→1e6 after BTC false
        # positives on co_ratio=221.1 blocked all btc_swing trades.
        _fv_arr = np.asarray(_fv, dtype=np.float64).ravel()
        _fv_clean = _fv_arr[np.isfinite(_fv_arr)]
        if len(_fv_clean) > 0 and np.max(np.abs(_fv_clean)) > 1e6:
            _max_val = float(np.max(np.abs(_fv_clean)))
            _max_idx = int(np.argmax(np.abs(_fv_clean)))
            print(
                json.dumps(
                    {
                        "event": "extreme_feature_blocked",
                        "time": _utc_iso(),
                        "strategy": sname,
                        "max_abs_value": _max_val,
                        "feature_index": _max_idx,
                        "reason": "extreme_value_gate",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            strategy_results.append(
                {
                    "strategy": sname,
                    "should_trade": False,
                    "direction": "neutral",
                    "confidence": 0.0,
                    "reason": f"extreme_value_gate:max_abs={_max_val:.1f}_at_idx_{_max_idx}",
                }
            )
            continue

        # ── DQAF-20260705-064 P2: Feature-Space OOD Gateway ───────────
        # Mahalanobis distance regime-shift detection.  When the live
        # feature vector drifts beyond the training distribution, the
        # model must fall silent.  This is the immune system that makes
        # manual kill-switches (P0) obsolete.
        _ood_verdict = None
        if _fv is not None and len(list(_fv)) > 0:
            try:
                _ood_gate = _get_ood_gateway()
                _schema = _resolve_ood_schema(sname, strategy)
                _ood_verdict = _ood_gate.check(
                    np.asarray(_fv, dtype=np.float64).ravel(),
                    schema_name=_schema,
                )
                # ── FIX-20260719-001: extract diagnostic features for OOD events ──
                _fv_arr = np.asarray(_fv, dtype=np.float64).ravel()
                _m5_vol_z = (
                    float(_fv_arr[_V9_M5_VOL_ZSCORE_IDX])
                    if len(_fv_arr) > _V9_M5_VOL_ZSCORE_IDX
                    else None
                )

                if _ood_verdict.status == "blocked":
                    print(
                        json.dumps(
                            {
                                "event": "ood_gateway_blocked",
                                "time": _utc_iso(),
                                "strategy": sname,
                                "schema": _schema,
                                "distance": round(_ood_verdict.distance, 2),
                                "threshold_block": round(_ood_verdict.threshold_block, 2),
                                "m5_vol_zscore": round(_m5_vol_z, 4)
                                if _m5_vol_z is not None
                                else None,
                                "reason": _ood_verdict.reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    strategy_results.append(
                        {
                            "strategy": sname,
                            "should_trade": False,
                            "direction": "neutral",
                            "confidence": 0.0,
                            "reason": f"regime_ood_blocked:{_ood_verdict.reason}",
                        }
                    )
                    continue
                elif _ood_verdict.status == "cautious":
                    print(
                        json.dumps(
                            {
                                "event": "ood_gateway_cautious",
                                "time": _utc_iso(),
                                "strategy": sname,
                                "schema": _schema,
                                "distance": round(_ood_verdict.distance, 2),
                                "threshold_cautious": round(_ood_verdict.threshold_cautious, 2),
                                "m5_vol_zscore": round(_m5_vol_z, 4)
                                if _m5_vol_z is not None
                                else None,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    # Cautious: confidence dampening applied downstream via
                    # _ood_cautious_flag on the decision context.
            except (RuntimeError, ValueError, ImportError, OSError) as _ood_exc:
                # OOD is defense-in-depth — fail-open on gateway errors
                pass

        # ── FIX-20260724-001: ATR Ratio dead-market circuit breaker ──────────
        # Replaces FIX-20260719-001 (Vol_ZScore hard gate) which was structurally
        # biased: CFD tick_volume burst-decay distribution → Vol_ZScore 94%
        # non-positive over 39,714 records → persistent false-positive blockade.
        #
        # New gate: atr_ratio = current_atr / mean(recent_atr_buffer_50)
        # When current ATR drops below 50% of the recent 50-bar baseline,
        # the market is effectively dead — model predictions become unreliable.
        # Microstructure strategies (micro_*) are exempt (own MicrostructureGate).
        #
        # Defense layers (unchanged two-layer concept):
        #   Layer 1 (OOD):  Mahalanobis distance — "is the feature space normal?"
        #   Layer 2 (ATR):  ATR Ratio           — "is the market breathing?"
        # Both must pass for a trade to proceed.
        if not sname.startswith("micro_") and current_atr is not None and current_atr > 0:
            _atr_buf = _load_atr_buffer_sample(base_dir)
            if _atr_buf is not None and len(_atr_buf) >= _ATR_BUFFER_MIN_SAMPLES:
                _recent_atr_baseline = float(np.mean(_atr_buf))
                if _recent_atr_baseline > 0:
                    _atr_ratio = current_atr / _recent_atr_baseline
                    if _atr_ratio < _ATR_DEAD_MARKET_RATIO:
                        print(
                            json.dumps(
                                {
                                    "event": "atr_dead_market_blocked",
                                    "time": _utc_iso(),
                                    "strategy": sname,
                                    "current_atr": round(current_atr, 4),
                                    "atr_baseline": round(_recent_atr_baseline, 4),
                                    "atr_ratio": round(_atr_ratio, 4),
                                    "threshold": _ATR_DEAD_MARKET_RATIO,
                                    "buffer_samples": len(_atr_buf),
                                    "reason": "dead_market_atr_collapse",
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        strategy_results.append(
                            {
                                "strategy": sname,
                                "should_trade": False,
                                "direction": "neutral",
                                "confidence": 0.0,
                                "reason": (
                                    f"atr_dead_market_block:"
                                    f"atr_ratio_{_atr_ratio:.3f}_lt_{_ATR_DEAD_MARKET_RATIO}"
                                ),
                            }
                        )
                        continue

        # ── FIX-20260629-171: Strategy mode enforcement ──
        # When mode=probation, the strategy MUST have ≥1 brain with governance
        # status ≥ probation to trade real capital.  Otherwise the strategy is
        # downgraded to shadow mode (virtual signals only — no real orders).
        # This CLOSES the DQAF-20260609-011 gap where Cut 4 micro-volume
        # exploration allowed candidate/shadow brains to trade real capital
        # despite the strategy being explicitly marked probation.
        #
        # Cold-start path: virtual shadow signals accumulate → Rule 85
        # (auto_promote_shadow_to_probation) → brain reaches probation →
        # mode enforcement passes → Cut 4 micro-volume trading → Rule 75
        # (auto_promote_probation_to_live) → full trading.
        _strategy_mode = getattr(strategy.config, "mode", "live")
        if _strategy_mode == "probation" and governance_state is not None:
            _has_probation_plus = False
            _strategy_brains = getattr(strategy, "brains", [])
            for _b_info in _strategy_brains or []:
                _bid = (
                    _b_info.get("brain_id", "")
                    if isinstance(_b_info, dict)
                    else getattr(_b_info, "brain_id", "")
                )
                if _bid:
                    _gs = governance_state.get("brain_states", {}).get(_bid, {})
                    _status = _gs.get("status", "") if isinstance(_gs, dict) else ""
                    if _status in ("probation", "live"):
                        _has_probation_plus = True
                        break
            if not _has_probation_plus:
                gate_mode = "shadow"
                print(
                    json.dumps(
                        {
                            "event": "strategy_mode_enforcement",
                            "time": _utc_iso(),
                            "strategy": sname,
                            "mode": _strategy_mode,
                            "reason": "probation_mode_no_qualified_brain",
                            "detail": (
                                "Strategy mode=probation but no brain has governance "
                                "status >= probation — forcing shadow mode (virtual only)"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # Non-M5 strategies (M15/M30/H1/H4) were previously receiving M5 ATR,
        # causing SL/TP barriers 2.5–7× tighter than training labels.
        _strategy_tf = getattr(strategy.config, "timeframe", "M5")
        _strategy_atr: float | None = None
        if tf_atr_map and _strategy_tf in tf_atr_map:
            _strategy_atr = tf_atr_map[_strategy_tf]

        # ── L3 Interface Consolidation: StrategyEvaluationContext ──────────
        # Construct the frozen context once per strategy evaluation cycle.
        # Single-parameter contract — adding a field here never changes any
        # evaluate() signature downstream.
        _eval_ctx = StrategyEvaluationContext(
            feature_vector=_fv,
            micro_feature_vector=micro_feature_vector,
            mid_price=_effective_mid,
            bid=bid,
            ask=ask,
            current_atr=_strategy_atr if _strategy_atr is not None else current_atr,
            strategy_atr=_strategy_atr,
            regime_info=regime_info,
            regime_gate_mode=gate_mode,
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            h4_trend_strength=h4_trend_strength,
            hurst=hurst,  # FIX-20260607-007
            kalman_velocity_bps=kalman_velocity_bps,  # FIX-20260607-007
            macro_regime=macro_regime,
            risk_budget_usd=risk_budget_usd,
            tracker=tracker,
            pnl_ledger=pnl_ledger,
            pnl_store=pnl_ledger,
            micro_sequences=micro_sequences,
            daily_feature_vector=daily_feature_vector,
            meta_filter=meta_signal_filter,
            meta_filter_gate=meta_filter_gate,
            conformal_ou_gate=conformal_ou_gate,
            microstructure_gate=microstructure_gate,
            micro_feature_dict=micro_feature_dict,
            btc_augment=btc_augment,  # FIX-20260613-052: resolved placeholder
            governance_state=governance_state,  # DQAF-20260622-059: LIVE-brain filter
        )
        decision = strategy.evaluate(context=_eval_ctx)

        # ── Cut 1a: Regime Direction Gate (FIX-20260613-079 + FIX-20260613-083) ──
        # Counter-trend trades are penalised when trend is confirmed.
        # Ranging markets (trend_direction="neutral"/"") → full passthrough.
        # FIX-083: 4h silence protection — if R1 blocks ALL trades for >4h,
        # relax to penalty-only to prevent system-wide trading silence.
        #
        # FIX-20260613-090-wire: RegimeDirectionGate replaces inline physics check.
        # The gate's _resolve_trend() includes self-calibrating OU Theta + Hurst
        # mean-reversion override.  When it returns "ranging", counter-trend
        # signals pass through without penalty.
        _gate_trend = _direction_gate._resolve_trend(regime_info)
        if _gate_trend == "ranging":
            trend_direction = "neutral"  # bypass counter-trend penalty
        if decision.should_trade and trend_direction in ("long", "short"):
            _opposing = (trend_direction == "long" and decision.direction == "short") or (
                trend_direction == "short" and decision.direction == "long"
            )
            if _opposing:
                _orig_conf = decision.confidence
                decision.confidence = round(decision.confidence * 0.5, 4)
                if decision.confidence < 0.35:
                    # ── 4h silence protection ──
                    # Track consecutive R1 blocks.  If ALL trades have been
                    # blocked for >48 cycles (~4h at 5-min), relax to penalty-only
                    # to prevent zero-open silence in persistent trending markets.
                    _r1_state = _get_r1_silence_state()
                    _r1_state["consecutive_blocks"] += 1
                    _r1_state["last_block_cycle"] = cycle_count
                    if _r1_state["consecutive_blocks"] > 48:
                        decision.should_trade = True  # override block
                        decision.reason = (
                            f"regime_direction_gate:silence_protection"
                            f"_counter_trend_{decision.direction}_vs_{trend_direction}"
                            f"_conf_{_orig_conf:.3f}_relaxed_to_penalty_only"
                            f"_silence_{_r1_state['consecutive_blocks']}_cycles"
                        )
                    else:
                        decision.should_trade = False
                        decision.reason = (
                            f"regime_direction_gate:counter_trend"
                            f"_{decision.direction}_vs_{trend_direction}"
                            f"_conf_{_orig_conf:.3f}_penalised_to_{decision.confidence:.3f}"
                        )
                else:
                    _r1_state = _get_r1_silence_state()
                    _r1_state["consecutive_blocks"] = 0  # reset: trade passed penalty
                    decision.reason = (
                        f"{decision.reason or 'ok'}" f"+regime_dir_penalty:{trend_direction}"
                    )
            else:
                # Trade is trend-aligned — reset silence counter
                _r1_state = _get_r1_silence_state()
                _r1_state["consecutive_blocks"] = 0

        # ── Cut 1: Post-evaluate cooldown check (direction known) ──
        if decision.should_trade and cooldown_registry is not None:
            _cd_allowed, _cd_reason = cooldown_registry.check_cooldown(sname, decision.direction)
            if not _cd_allowed:
                decision.should_trade = False
                decision.reason = _cd_reason
                print(
                    json.dumps(
                        {
                            "event": "cooldown_blocked",
                            "time": _utc_iso(),
                            "strategy": sname,
                            "direction": decision.direction,
                            "reason": _cd_reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Cut 2: Post-evaluate family spacing check (direction known) ──
        if decision.should_trade and family_entry_tracker is not None:
            from core.execution.pre_trade_guards import (
                _STRATEGY_FAMILY_GAP_SEC,
                _SWING_FAMILY_MIN_TF_SEC,
                strategy_to_family,
            )

            _fam = strategy_to_family(sname)
            if _fam != sname:
                # DQAF-20260615-011: per-strategy gap override —
                # H1 models need ≥1 full bar (3600s) between entries
                _min_gap = _STRATEGY_FAMILY_GAP_SEC.get(sname, _SWING_FAMILY_MIN_TF_SEC)
                _fs_allowed, _fs_reason = family_entry_tracker.check_spacing(
                    _fam, decision.direction, sname, min_gap_sec=_min_gap
                )
                if not _fs_allowed:
                    decision.should_trade = False
                    decision.reason = _fs_reason
                    print(
                        json.dumps(
                            {
                                "event": "family_spacing_blocked",
                                "time": _utc_iso(),
                                "strategy": sname,
                                "family": _fam,
                                "direction": decision.direction,
                                "reason": _fs_reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                # ── FIX-20260714-006: decision-time family_entry_tracker.record_entry REMOVED ──
                # The decision-time record_entry (FIX-20260609-002) was called BOTH at
                # decision time (here) AND at dispatch time (dispatch_post.py:56).
                # Every trade reset the 900s timer twice, causing ~1800s effective
                # cooldown instead of 900s.  With 4 XAU swing strategies all predicting
                # the same direction, this doubled blocking caused 83.3% signal rejection.
                #
                # The dispatch-time record_entry (dispatch_post.py:56) is the SSOT —
                # only a REAL filled order should start the family cooldown clock.
                # Same-cycle cluster entries remain mitigated by the reentry guard (Cut 3)
                # and CrossStrategyCoordinator, which operate independently.

        # ── Cut 3: Reentry quality guard (FIX-20260606-131, P2.6 front-placement) ──
        if decision.should_trade and reentry_states is not None:
            from core.execution.reentry_guard import ensure_reentry_state

            # ── DQAF-20260616-001/P2: Detect rule-based strategies ──────────
            # Strategies with no ML brains (brain_types=[], min_valid_brains=0)
            # have fixed confidence that cannot improve.  The reentry guard
            # must use time-based cooldown instead of ML confidence thresholds,
            # otherwise the strategy is permanently deadlocked.
            _strategy_cfg = getattr(strategy, "config", None)
            _is_rule_based = (
                _strategy_cfg is not None
                and len(getattr(_strategy_cfg, "brain_types", [None])) == 0
                and getattr(_strategy_cfg, "min_valid_brains", 1) == 0
            )

            _rs = ensure_reentry_state(reentry_states, sname)
            _allowed, _rr_reason, _cons_count_f = _rs.check_and_record_entry(
                direction=decision.direction,
                confidence=decision.confidence,
                mid=mid_price or 0.0,
                entry_half_life=getattr(decision, "entry_half_life", 0.0),
                timeframe_minutes=5.0,
                sl_cooldown=reentry_sl_cooldown,
                sl_penalty=reentry_sl_penalty,
                bleed_cooldown=reentry_bleed_cooldown,
                bleed_penalty=reentry_bleed_penalty,
                is_rule_based=_is_rule_based,
            )
            if not _allowed:
                decision.should_trade = False
                decision.reason = _rr_reason
                print(
                    json.dumps(
                        {
                            "event": "reentry_blocked",
                            "time": _utc_iso(),
                            "strategy": sname,
                            "direction": decision.direction,
                            "confidence": round(decision.confidence, 4),
                            "reason": _rr_reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Cut 4: Governance degradation gate (FIX-20260609-011) ──────────
        # When NO brain in this strategy has achieved "live" OR "probation"
        # status, the strategy is trading with unproven (candidate/shadow) or
        # permanently-degraded (frozen/retired) models.  Degrade to minimum
        # exploration volume and require higher confidence to prevent "cadet
        # brains driving heavy mechs".
        #
        # FIX-20260703-061 (DQAF-20260703-061): Expanded active-brain filter
        # from status == "live" to status in ("live", "probation").  Probation
        # brains are actively trading with statistically-valid PnL data (per
        # FIX-060 rationale); vote_weight penalty at signal level already
        # handles governance trust discount.  Excluding them creates a
        # self-inflicted deadlock when a strategy's ONLY brain is probation
        # (e.g. btc_swing_h1 → V12_H1_15) → _live_count=0 every cycle →
        # permanent [degraded: no_live_brains] tag.
        #
        # Historical: FIX-20260629-174 also missed strategy_evaluator.py
        # (L307 + L548) — this file has systemic incomplete-fix risk.
        if decision.should_trade and governance_state is not None:
            # ── DQAF-20260612-002 / FIX-20260612-006: SSOT fix ──
            # Bypass legacy strategy.brains nested-dict lookup (fragile: depends
            # on "brain_id" key convention and is vulnerable to registry→governance
            # status skew).  Use the flat list[str] from the decision object —
            # these are the brain IDs that actually voted in this cycle, freshly
            # resolved by strategy.evaluate().
            _voted_brain_ids = getattr(decision, "brain_ids", [])
            _live_count = sum(
                1
                for bid in _voted_brain_ids
                if governance_state.get("brain_states", {}).get(bid, {}).get("status")
                in ("live", "probation")
            )
            _total_voters = len(_voted_brain_ids)
            if _live_count == 0:
                _degraded_confidence_floor = 0.50
                # FIX-20260730-010: Proportional reduction replaces hard cap at 0.01.
                # Ω Phase 2 — all degradation gates use multiplicative factors;
                # the only floor is MIN_ECONOMIC_VOLUME at the final settlement gate.
                _degraded_vol_factor = 0.25
                if decision.confidence < _degraded_confidence_floor:
                    decision.should_trade = False
                    decision.reason = "no_live_brains_and_low_confidence"
                    print(
                        json.dumps(
                            {
                                "event": "governance_degraded_blocked",
                                "time": _utc_iso(),
                                "strategy": sname,
                                "direction": decision.direction,
                                "confidence": round(decision.confidence, 4),
                                "live_brains": 0,
                                "reason": decision.reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    decision.volume = round(decision.volume * _degraded_vol_factor, 4)
                    decision.reason = (
                        decision.reason or ""
                    ) + f" [degraded: no_live_brains x{_degraded_vol_factor}]"
                    print(
                        json.dumps(
                            {
                                "event": "governance_degraded_volume",
                                "time": _utc_iso(),
                                "strategy": sname,
                                "direction": decision.direction,
                                "confidence": round(decision.confidence, 4),
                                "volume": decision.volume,
                                "live_brains": 0,
                                "vol_factor": _degraded_vol_factor,
                                "reason": decision.reason,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            # ── Cut 4-bis: Non-active brain dominance gate ──────────────────
            # FIX-20260623-083 + FIX-20260703-061: When active brains
            # (live+probation) exist but are a MINORITY of voters, lower-status
            # brains (candidate/shadow/frozen) can collectively dominate the
            # consensus.  This caused BTC_Swing_V12_H1_Survival (live, 16% of
            # opens) to be outvoted by lower-status brains generating 62% of
            # opens — $57 profit bled to -$20 while governance correctly kept
            # them on probation.
            #
            # Gate: if active brains < 50% of voters → require higher confidence
            # and cap volume.  This prevents "cadet fleet" from driving
            # heavy exposure while active brains are the minority voice.
            #
            # Note: probation brains count as "active" (FIX-20260703-061) —
            # they are actively trading with valid PnL data and the vote_weight
            # penalty already handles the governance trust discount.
            elif _total_voters > 0:
                _live_ratio = _live_count / _total_voters
                if _live_ratio < 0.5:
                    _nonlive_confidence_floor = 0.55
                    # FIX-20260730-010: Proportional reduction replaces hard cap at 0.01.
                    # Ω Phase 2 — all degradation gates use multiplicative factors.
                    _nonlive_vol_factor = 0.25
                    if decision.confidence < _nonlive_confidence_floor:
                        decision.should_trade = False
                        decision.reason = "non_live_dominance_low_confidence"
                        print(
                            json.dumps(
                                {
                                    "event": "governance_non_live_dominance_blocked",
                                    "time": _utc_iso(),
                                    "strategy": sname,
                                    "direction": decision.direction,
                                    "confidence": round(decision.confidence, 4),
                                    "live_brains": _live_count,
                                    "total_voters": _total_voters,
                                    "live_ratio": round(_live_ratio, 3),
                                    "reason": decision.reason,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    else:
                        decision.volume = round(decision.volume * _nonlive_vol_factor, 4)
                        decision.reason = (
                            decision.reason or ""
                        ) + f" [degraded: non_live_dominance x{_nonlive_vol_factor}]"
                        print(
                            json.dumps(
                                {
                                    "event": "governance_non_live_dominance_volume",
                                    "time": _utc_iso(),
                                    "strategy": sname,
                                    "direction": decision.direction,
                                    "confidence": round(decision.confidence, 4),
                                    "volume": decision.volume,
                                    "live_brains": _live_count,
                                    "total_voters": _total_voters,
                                    "live_ratio": round(_live_ratio, 3),
                                    "vol_factor": _nonlive_vol_factor,
                                    "reason": decision.reason,
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )

        # ── Cut 5: Fail-Closed SL/TP assertion (FIX-20260611-020) ───────
        # Reject ANY trade decision that lacks valid SL/TP, regardless of
        # confidence or brain votes.  SL=0 means "unlimited risk" which is
        # never acceptable for automated trading.  Shadow-mode decisions
        # are exempt (virtual tracking, no real order).
        #
        # Historical: DQAF-20260607-005 (FIX-140/141/142) established
        # Fail-Closed dispatch after UnboundLocalError caused orphan
        # positions.  This extends the pattern to the SL/TP dimension:
        # FIX-20260611-017 fixed premature-breakeven from uninitialized
        # lowest_low=0.0, but the symmetric risk (SL/TP uninitialized = 0)
        # was left unprotected until now.
        if (
            decision.should_trade
            and gate_mode != "shadow"
            and (decision.sl <= 0 or decision.tp <= 0)
        ):
            decision.should_trade = False
            decision.reason = f"fail_closed_sltp_missing(sl={decision.sl:.1f}_tp={decision.tp:.1f})"
            print(
                json.dumps(
                    {
                        "event": "fail_closed_sltp_rejected",
                        "time": _utc_iso(),
                        "strategy": sname,
                        "direction": decision.direction,
                        "sl": decision.sl,
                        "tp": decision.tp,
                        "hard_sl": decision.hard_sl,
                        "confidence": round(decision.confidence, 4),
                        "live_brains": _live_count if governance_state is not None else -1,
                        "reason": decision.reason,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ── Cut 6: Data-health degradation → progressive risk reduction ──
        # FIX-20260611-022: Computed upstream from DataHealthService output.
        # NORMAL(100%) → YELLOW(40%) → ORANGE(15%,no new) → RED(0%,close-only).
        if decision.should_trade and gate_mode != "shadow" and degradation_constraints is not None:
            try:
                from core.observability.degradation import apply_degradation_to_decision

                _dv, _dt, _dr = apply_degradation_to_decision(
                    degradation_constraints,
                    decision.volume,
                    decision.should_trade,
                )
                if _dr:
                    decision.volume = _dv
                    decision.should_trade = _dt
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass
            decision.reason = (decision.reason or "") + _dr
            if not _dt:
                print(
                    json.dumps(
                        {
                            "event": "degradation_blocked",
                            "time": _utc_iso(),
                            "strategy": sname,
                            "direction": decision.direction,
                            "reason": decision.reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        # ── Cut 7: God's Eye cross-instrument consensus gate ────────────
        # FIX-20260625-090: Modulates confidence and volume based on
        # multi-TF alignment, cross-instrument consistency, chop, and
        # anomaly scores.
        # DQAF-20260807-001 (IC 雷霆裁决 — Option A Chop Filter): the old
        # "God's Eye NEVER blocks a trade outright (fail-open for entries)"
        # contract was abolished.  08-06 实证 — 3 XAU longs entered at
        # 24h-range high percentiles (80.5%/73.2%/56.6%) under chop or
        # health=0.52, all exited at a loss (-77.9/-66.4/m30 无账) — proved
        # fail-open lets trend-following signals "buy the top".  New
        # contract: when health < 0.55 (Defensive) OR chop is detected,
        # pass an explicit BLOCKED_BY_GODSEYE status downstream
        # (should_trade=False, volume=0).  Risk-control floor only; signal
        # generation untouched.  "shadow" mode still forces shadow.
        if gods_eye_verdict is not None and decision.should_trade:
            _gev = gods_eye_verdict
            _ge_health = getattr(_gev, "health_score", 1.0)
            _ge_conf_mod = getattr(_gev, "confidence_modifier", 1.0)
            _ge_mode = getattr(_gev, "recommended_mode", "normal")
            _ge_chop = getattr(_gev, "chop_detected", False)
            _ge_anomaly = getattr(_gev, "anomaly_score", 0.0)
            _ge_macro = getattr(_gev, "macro_bias", "neutral")

            # ── Option A: God's Eye HARD VETO on new entries (IC Order) ──
            if _ge_health < _GODS_EYE_HARD_BLOCK_HEALTH or _ge_chop:
                decision.should_trade = False
                decision.volume = 0.0
                decision.reason = (
                    decision.reason or ""
                ) + f"gods_eye:blocked_by_gods_eye" f"(h={_ge_health:.2f},chop={_ge_chop})"
                print(
                    json.dumps(
                        {
                            "event": "gods_eye_blocked_entry",
                            "time": _utc_iso(),
                            "strategy": sname,
                            "direction": decision.direction,
                            "gods_eye_health": round(_ge_health, 4),
                            "chop_detected": _ge_chop,
                            "mode": _ge_mode,
                            "confidence": round(decision.confidence, 4),
                            "volume": 0.0,
                            "reason": decision.reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            else:
                # ── Mode-based gating ──
                if _ge_mode == "shadow":
                    # God's Eye in shadow: force shadow mode (no real money)
                    from core.execution.regime_gate import get_stricter_mode

                    gate_mode = get_stricter_mode(gate_mode, "shadow")
                    decision.confidence = round(decision.confidence * _ge_conf_mod, 4)
                elif _ge_mode == "defensive":
                    # Defensive: require higher confidence floor
                    if decision.confidence < 0.50:
                        decision.should_trade = False
                        decision.reason = (
                            f"gods_eye:defensive_confidence_floor"
                            f"(conf={decision.confidence:.3f}<0.50)"
                        )
                    else:
                        decision.confidence = round(decision.confidence * _ge_conf_mod, 4)
                elif _ge_mode == "cautious":
                    # Cautious: modest confidence reduction
                    decision.confidence = round(decision.confidence * _ge_conf_mod, 4)
                # "normal": no modification

                # ── Health-based volume modulation ──
                # FIX-20260730-010: Removed max(0.01, ...) floor — Ω Phase 2.
                # Volume floor is now enforced ONLY at the final settlement gate.
                # DQAF-20260806-003 Option B2 (IC Approved): _health_vol via
                # deadband ramp — healthy GodsEye (health >= 0.70) no longer
                # shaves volume, so a healthy eye cannot push an economically
                # viable volume below the min_economic floor (threshold resonance).
                if decision.should_trade:
                    _health_vol = _gods_eye_health_vol_mult(_ge_health)
                    decision.volume = round(decision.volume * _health_vol, 4)
                    # Append God's Eye diagnostic to reason
                    _ge_tag = (
                        f"+gods_eye:{_ge_mode}" f"_h={_ge_health:.2f}" f"_cm={_ge_conf_mod:.2f}"
                    )
                    if _ge_chop:
                        _ge_tag += "_chop"
                    if _ge_anomaly > 0.3:
                        _ge_tag += f"_anom={_ge_anomaly:.2f}"
                    if _ge_macro != "neutral":
                        _ge_tag += f"_macro={_ge_macro}"
                    decision.reason = (decision.reason or "") + _ge_tag

        # Apply session + health volume multipliers
        # FIX-20260730-010: Removed max(0.01, ...) floor — Ω Phase 2.
        if decision.should_trade:
            combined_mult = session_volume_mult * health_volume_mult
            if combined_mult != 1.0:
                decision.volume = round(decision.volume * combined_mult, 4)

        # ═══════════════════════════════════════════════════════════════
        # Ω Final Settlement Gate: Minimum Economic Volume
        # FIX-20260730-010 — Phase 2 Architecture Refactoring.
        # TECH_DEBT-006 (DQAF-20260803-001 / IC 最高执行令, 2026-08-03):
        # per-symbol floor — explicit config wins, otherwise symbol-aware
        # default via StrategyLineConfig.resolved_min_economic_volume
        # (BTC → own base_volume floor 0.01; XAU → 2× lot_step = 0.02).
        #
        # This is the ONLY volume floor in the entire pipeline.  All
        # upstream degradation gates (governance, God's Eye health,
        # session multiplier) apply multiplicative factors WITHOUT
        # individual floors.  The raw product flows through to here.
        #
        # Below the per-symbol floor the trade is KILLED — not silently
        # forced to an uneconomic lot size.  The system honestly reports
        # "conditions too degraded to trade at viable size."
        # ═══════════════════════════════════════════════════════════════
        _floor = 0.02  # last-resort fallback (XAU-calibrated, never lower)
        _cfg_ = getattr(strategy, "config", None)
        # RuleEngineStrategyWrapper.config is a plain dict ({name, timeframe})
        # — pure-rule strategies bypass the Kelly pipeline; keep XAU fallback.
        if _cfg_ is not None and hasattr(_cfg_, "resolved_min_economic_volume"):
            _floor = _cfg_.resolved_min_economic_volume
        if decision.should_trade and decision.volume < _floor:
            decision.should_trade = False
            decision.reason = (
                (decision.reason or "")
                + " [volume_degraded_below_economic_minimum: "
                + f"{decision.volume:.4f} < {_floor}]"
            )
            print(
                json.dumps(
                    {
                        "event": "min_economic_volume_blocked",
                        "time": _utc_iso(),
                        "strategy": sname,
                        "direction": decision.direction,
                        "volume": decision.volume,
                        "min_economic": _floor,
                        "confidence": round(decision.confidence, 4),
                        "reason": decision.reason,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        elif decision.should_trade:
            # Final rounding to nearest lot_step (0.01) — the only place
            # this happens after all degradation factors.  No floor needed:
            # the per-symbol floor already guarantees volume >= _floor.
            _ticks = int(decision.volume / 0.01 + 0.5)
            decision.volume = round(_ticks * 0.01, 2)

        strategy_results.append(
            {
                "strategy": sname,
                "should_trade": decision.should_trade,
                "direction": decision.direction,
                "confidence": decision.confidence,
                "volume": decision.volume,
                "p_win": getattr(decision, "p_win", 0.5),
                "kelly_mult": getattr(decision, "kelly_mult", 1.0),
                "regime_mode": gate_mode,
                "venue": getattr(decision, "venue", "live"),
                "reason": decision.reason,
                "supporting": decision.supporting_count,
                "total": decision.total_count,
                # FIX-20260704-006: SL/TP for golden_master observability
                "sl": decision.sl,
                "tp": decision.tp,
                "hard_sl": decision.hard_sl,
            }
        )

        if not decision.should_trade:
            try:
                from core.runtime.gate_audit_recorder import record_gate_block

                record_gate_block(
                    strategy_name=sname,
                    direction=decision.direction,
                    reason=decision.reason,
                    gate_diag=getattr(decision, "gate_diag", None) or None,
                    base_dir=base_dir,
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass
            continue

        # Portfolio risk check
        risk_result = portfolio_risk.check(
            decision,
            current_positions,
            current_price=mid_price,
            account_equity=account_equity,
            current_cycle=cycle_count,
        )

        if risk_result.verdict.value == "rejected":
            strategy_results[-1]["risk"] = "rejected"
            strategy_results[-1]["risk_reason"] = risk_result.reason
            continue

        # ── Blind Spot 3: Entry-in-flight lock ────────────────────────
        # If an open order is still awaiting ACK from MT5, block the
        # next cycle from dispatching a duplicate.  Without this, a slow
        # MT5 response (>5s) causes the next cycle to see "no position"
        # and dispatch a second open — doubling exposure.
        if execution_queue.is_pending_open(sname):
            strategy_results[-1]["should_trade"] = False
            strategy_results[-1]["reason"] = "blocked_entry_in_flight"
            print(
                json.dumps(
                    {
                        "event": "entry_in_flight_blocked",
                        "time": _utc_iso(),
                        "strategy": sname,
                        "direction": decision.direction,
                        "reason": "pending_open_order_not_yet_acked",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        # ── DQAF-20260622-059 / P2: UnattributedOrderRejected self-protection ──
        # If a strategy previously triggered UnattributedOrderRejected (sentinel
        # magic 90401), it is PERMANENTLY BLOCKED until system restart.  Do NOT
        # enqueue further orders — they would fail with the same fatal error.
        if execution_queue.is_unattributed_blocked(sname):
            strategy_results[-1]["should_trade"] = False
            strategy_results[-1]["reason"] = "blocked_unattributed_magic_90401"
            print(
                json.dumps(
                    {
                        "event": "unattributed_blocked_strategy_skipped",
                        "time": _utc_iso(),
                        "strategy": sname,
                        "direction": decision.direction,
                        "reason": "strategy_permanently_blocked_due_to_sentinel_magic_90401",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        # ── P4-2: Cross-strategy coordinator ──────────────────────────
        # Block if another strategy already holds an opposing position.
        # Opposing positions cancel each other's edge while paying
        # spread+slippage twice — a guaranteed net loss.
        if cross_strategy_coordinator is not None:
            _conflict = cross_strategy_coordinator.check(
                pending_strategy=sname,
                pending_direction=decision.direction,
                current_positions=current_positions,
            )
            if _conflict.blocked:
                strategy_results[-1]["should_trade"] = False
                strategy_results[-1]["reason"] = _conflict.reason
                strategy_results[-1]["conflict"] = [
                    {"strategy": o.strategy_name, "direction": o.direction, "ticket": o.ticket}
                    for o in _conflict.opposing_positions
                ]
                print(
                    json.dumps(
                        {
                            "event": "cross_strategy_blocked",
                            "time": _utc_iso(),
                            "strategy": sname,
                            "direction": decision.direction,
                            "opposing": [o.strategy_name for o in _conflict.opposing_positions],
                            "reason": _conflict.reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                continue

        # Queue for execution
        execution_queue.enqueue(sname, decision, risk_result)
        decisions.append(decision)
        strategy_results[-1]["risk"] = risk_result.verdict.value
        if risk_result.adjusted_volume != decision.volume:
            strategy_results[-1]["adjusted_volume"] = risk_result.adjusted_volume

        # Update current_positions snapshot
        current_positions[sname] = {
            "strategy": sname,
            "direction": decision.direction,
            "volume": risk_result.adjusted_volume
            if risk_result.adjusted_volume > 0
            else decision.volume,
            "ticket": 0,
            "entry_cycle": cycle_count,
            "brain_ids": getattr(decision, "brain_ids", []),
        }

    # ── Tier 3: √N correlation discount ──
    from core.execution.correlation_sizer import apply_sqrt_n_discount

    _, sqrt_n_clusters = apply_sqrt_n_discount(decisions)

    dropped_names = {
        d.strategy_name
        for d in decisions
        if not d.should_trade and "sqrt_n_dropped" in getattr(d, "reason", "")
    }
    if dropped_names:
        for qd in execution_queue._queue:
            if qd.strategy_name in dropped_names:
                qd.risk_result.verdict = RiskVerdict.REJECTED
                qd.risk_result.reason = getattr(qd.decision, "reason", "sqrt_n_dropped")
        for sname in list(current_positions.keys()):
            if sname in dropped_names:
                del current_positions[sname]
        for sr in strategy_results:
            if sr.get("strategy", "") in dropped_names:
                for d in decisions:
                    if d.strategy_name == sr["strategy"]:
                        sr["should_trade"] = False
                        sr["reason"] = d.reason
                        sr["volume"] = 0.0
                        break

    for cluster in sqrt_n_clusters:
        if cluster.dropped_strategies:
            print(
                json.dumps(
                    {
                        "event": "sqrt_n_discount",
                        "time": _utc_iso(),
                        "direction": cluster.direction,
                        "n_same_direction": cluster.n_same_direction,
                        "raw_total": cluster.raw_total_volume,
                        "discounted_total": cluster.discounted_volume,
                        "dropped": cluster.dropped_strategies,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    return {
        "strategy_results": strategy_results,
        "trade_decisions": len(decisions),
        "queued": execution_queue.queue_size,
        "active_strategies": list(strategy_lines.keys()),
        "decisions_map": {d.strategy_name: d for d in decisions},
    }
