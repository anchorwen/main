"""Meta Pipeline — config-driven regression probe → Stage-N filter chain.

Replaces the hardcoded ``_try_meta_pipeline()`` and inline probe extraction
in ``strategy_line.evaluate()`` with a declarative architecture:

  Brain JSON declares capability   →  ``"roles": ["meta_probe"]``
  live.yaml declares usage         →  ``meta_probes: [{brain_id, threshold}]``
  MetaPipeline orchestrates        →  find probe → extract raw_score → filter

All contracts are frozen dataclasses (Layer 1 compliant).

Design principles (per plan review):
  - Brain-id never hardcoded in code — discovered from config
  - Threshold per-probe, per-strategy configurable
  - Filter stage declarative (stage2, stage3, ...)
  - Backward-compatible: falls back to legacy extensions path for
    BrainDecisionProposal objects still in flight
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.schemas.trading_contracts import Direction, TradeDirection

# ── Contracts ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MetaProbeSpec:
    """Which brain serves as a regression probe and at what threshold.

    Declared in live.yaml per strategy line, or auto-discovered from
    brain config JSON ``"roles": ["meta_probe"]``.
    """

    brain_id: str
    threshold: float = 0.30
    filter_stage: str = "stage2"


@dataclass(frozen=True, slots=True)
class MetaProbeResult:
    """Output of a single meta-probe evaluation."""

    brain_id: str
    raw_score: float
    direction: Direction
    threshold: float
    passed: bool
    reason: str = ""


# ── Probe extraction (static, no side effects) ───────────────────────────


def extract_probe_score(proposals: list[Any], brain_id: str) -> float | None:
    """Extract the regression raw_score for *brain_id* from a list of proposals.

    Reads ``BrainSignal.raw_score`` directly (Layer 1 contract).
    Falls back to the legacy ``extensions.raw_outputs.raw_score`` path for
    ``BrainDecisionProposal`` objects still in flight (shadow / backtest).
    """
    for p in proposals:
        bid = getattr(p, "brain_id", None)
        if bid != brain_id:
            continue

        # Primary: BrainSignal.raw_score (Layer 1 immutable contract)
        raw = getattr(p, "raw_score", None)
        if raw is not None:
            return float(raw)

        # Fallback: legacy BrainDecisionProposal
        ext = getattr(p, "extensions", None)
        if ext and isinstance(ext, dict):
            ro = ext.get("raw_outputs", {})
            if isinstance(ro, dict):
                raw = ro.get("raw_score")
                if raw is not None:
                    return float(raw)

        return None

    return None


# ── Auto-discovery from brain config ─────────────────────────────────────


def discover_probe_specs(brains: list[dict[str, Any]]) -> list[MetaProbeSpec]:
    """Discover meta-probe specs from brain config JSON entries.

    Any brain with ``"roles": ["meta_probe"]`` (or ``"meta_probe"`` as a
    single string) is treated as a regression probe.  Threshold and filter
    stage are read from ``meta_probe_config`` or defaulted.

    Returns an empty list if no brains declare the role.
    """
    specs: list[MetaProbeSpec] = []
    for b in brains:
        roles = b.get("roles", [])
        if isinstance(roles, str):
            roles = [roles]
        if "meta_probe" not in roles:
            continue
        # Skip archived/frozen/zero-weight brains — they cannot serve as probes
        status = b.get("status", "")
        vw = b.get("vote_weight", 1.0)
        if status in ("archived", "frozen") or vw == 0.0:
            continue

        probe_cfg = b.get("meta_probe_config", {}) or {}
        specs.append(
            MetaProbeSpec(
                brain_id=b.get("brain_id", ""),
                threshold=float(probe_cfg.get("threshold", 0.30)),
                filter_stage=str(probe_cfg.get("filter_stage", "stage2")),
            )
        )
    return specs


# ── Orchestrator ─────────────────────────────────────────────────────────


class MetaPipeline:
    """Config-driven meta-probe evaluation engine.

    Replaces the monolithic ``_try_meta_pipeline()`` with a composition of
    small, testable steps:  extract → threshold → filter.

    Usage::

        pipeline = MetaPipeline(specs, filter_registry={"stage2": meta_filter})
        decision = pipeline.evaluate(
            proposals=proposals,
            feature_vector=feature_vector,
            micro_feature_vector=micro_feature_vector,
            direction=parliament_direction,
            ...
        )
    """

    def __init__(
        self,
        specs: list[MetaProbeSpec],
        filter_registry: dict[str, Any] | None = None,
    ):
        self._specs = specs
        self._filter_registry = filter_registry or {}

    @property
    def specs(self) -> list[MetaProbeSpec]:
        return list(self._specs)

    # ── Main entry point ─────────────────────────────────────────────────

    def evaluate(
        self,
        *,
        proposals: list[Any],
        feature_vector: Any,
        micro_feature_vector: Any,
        parliament_direction: str,
        current_atr: float,
        mid_price: float | None,
        entry_z_score: float,
        pnl_store: Any,
        risk_budget_usd: float,
        regime_info: dict[str, Any] | None,
        regime_gate_mode: str,
        brain_ids: list[str],
        support_count: int,
        total_count: int,
        config: Any,  # StrategyLineConfig
    ) -> Any | None:
        """Evaluate all meta-probe specs against the current proposals.

        Returns a ``StrategyDecision`` if any probe passes the full chain,
        or ``None`` if no probe fires.
        """
        # max_volume=0 means shadow-only — no real capital
        if getattr(config, "max_volume", 0.05) <= 0:
            return None

        for spec in self._specs:
            result = self._evaluate_one(
                spec=spec,
                proposals=proposals,
                feature_vector=feature_vector,
                micro_feature_vector=micro_feature_vector,
                parliament_direction=parliament_direction,
                current_atr=current_atr,
                mid_price=mid_price,
                entry_z_score=entry_z_score,
                pnl_store=pnl_store,
                risk_budget_usd=risk_budget_usd,
                regime_info=regime_info,
                regime_gate_mode=regime_gate_mode,
                brain_ids=brain_ids,
                support_count=support_count,
                total_count=total_count,
                config=config,
            )
            if result is not None:
                return result

        return None

    # ── Per-spec evaluation ──────────────────────────────────────────────

    def _evaluate_one(
        self,
        *,
        spec: MetaProbeSpec,
        proposals: list[Any],
        feature_vector: Any,
        micro_feature_vector: Any,
        parliament_direction: str,
        current_atr: float,
        mid_price: float | None,
        entry_z_score: float,
        pnl_store: Any,
        risk_budget_usd: float,
        regime_info: dict[str, Any] | None,
        regime_gate_mode: str,
        brain_ids: list[str],
        support_count: int,
        total_count: int,
        config: Any,
    ) -> Any | None:
        """Run one MetaProbeSpec through the full chain: extract → threshold → filter → SL/TP → RR → Kelly → volume."""
        from core.execution.dynamic_sl_tp import compute_dynamic_sl_tp, compute_sl_tp_levels
        from core.execution.kelly_sizer import compute_kelly_mult
        from core.schemas.trading_contracts import StrategyDecision

        # 1. Extract raw_score
        raw_score = extract_probe_score(proposals, spec.brain_id)
        if raw_score is None:
            return None

        # 2. Map to direction via threshold
        if raw_score < -spec.threshold:
            meta_dir: TradeDirection = "short"
        elif raw_score > spec.threshold:
            meta_dir = "long"
        else:
            return None  # no strong directional signal

        # 3. Run stage-N filter
        stage_filter = self._filter_registry.get(spec.filter_stage)
        if stage_filter is None:
            return None

        result = stage_filter.filter_arrays(
            direction=meta_dir,
            s1_prediction=raw_score,
            v9_array=feature_vector,
            micro_array=micro_feature_vector,
        )
        if not result.passed:
            _p = getattr(result, "p_win", None)
            print(
                json.dumps(
                    {
                        "event": "kelly_diag",
                        "time": _utc_iso(),
                        "strategy": getattr(config, "name", ""),
                        "stage": "meta_pipeline_rejected",
                        "source": "meta_pipeline",
                        "brain_id": spec.brain_id,
                        "s1_prediction": round(raw_score, 6),
                        "meta_dir": meta_dir,
                        "result_p_win": round(float(_p), 4) if _p is not None else None,
                        "passed": False,
                        "reason": result.reason if result.reason else "threshold",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return None

        meta_p_win = float(result.p_win)
        print(
            json.dumps(
                {
                    "event": "kelly_diag",
                    "time": _utc_iso(),
                    "strategy": getattr(config, "name", ""),
                    "stage": "meta_pipeline_approved",
                    "source": "meta_pipeline",
                    "brain_id": spec.brain_id,
                    "s1_prediction": round(raw_score, 6),
                    "meta_dir": meta_dir,
                    "result_p_win": round(meta_p_win, 4),
                    "passed": True,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # 4. SL/TP
        _spread_cost = float(getattr(config, "spread_points", 0.0)) * float(
            getattr(config, "tick_size", 0.01)
        )
        dsl = compute_dynamic_sl_tp(
            base_sl_mult=float(getattr(config, "base_sl_atr_mult", 2.0)),
            base_tp_mult=float(getattr(config, "base_tp_atr_mult", 3.5)),
            current_atr=current_atr,
            ref_atr=float(getattr(config, "ref_atr", 5.0)),
            hard_sl_ratio=float(getattr(config, "hard_sl_ratio", 1.5)),
            timeframe_mult=int(getattr(config, "timeframe_mult", 1)),
            min_sl_distance=float(getattr(config, "min_sl_distance", 0.0)),
            min_rr_ratio=float(getattr(config, "min_rr_ratio", 0.0)),
            spread_cost=_spread_cost,
        )
        entry_price = mid_price or 0.0
        if entry_price <= 0:
            return None
        levels = compute_sl_tp_levels(
            meta_dir,
            entry_price,
            dsl,
            spread_points=float(getattr(config, "spread_points", 0.0)),
            tick_size=float(getattr(config, "tick_size", 0.01)),
        )

        # 5. RR check
        sl_dist = abs(levels["stop_loss"] - entry_price)
        tp_dist = abs(levels["take_profit"] - entry_price)
        rr_ratio: float = 1.0
        if sl_dist > 0:
            rr_ratio = tp_dist / sl_dist
        min_rr = float(getattr(config, "min_rr_ratio", 0.0))
        if min_rr > 0 and rr_ratio < min_rr:
            return None

        # 6. Kelly sizing
        kelly_result = compute_kelly_mult(meta_p_win, rr_ratio)
        if kelly_result.fractional_mult == 0.0:
            return None  # negative EV
        kelly_mult = kelly_result.fractional_mult

        # 7. Volume
        entry_p = mid_price or 0.0
        volume = _compute_meta_volume(
            meta_p_win=meta_p_win,
            current_atr=current_atr,
            regime_info=regime_info,
            regime_gate_mode=regime_gate_mode,
            risk_budget_usd=risk_budget_usd,
            entry_z_score=entry_z_score,
            kelly_mult=kelly_mult,
            config=config,
        )
        lot_step = float(getattr(config, "lot_step", 0.01))
        ticks = round(volume / lot_step)
        volume = max(lot_step, round(ticks * lot_step, 2))

        # 8. Diagnostic log
        pre_kelly = getattr(config, "base_volume", 0.01)
        raw_target = pre_kelly * kelly_mult
        print(
            json.dumps(
                {
                    "event": "kelly_sizing",
                    "time": _utc_iso(),
                    "strategy": getattr(config, "name", ""),
                    "source": "meta_pipeline",
                    "brain_id": spec.brain_id,
                    "p_win": round(meta_p_win, 4),
                    "rr_ratio": round(rr_ratio, 4),
                    "kelly_mult": round(kelly_mult, 4),
                    "sizing_label": kelly_result.sizing_label,
                    "base_volume": round(pre_kelly, 4),
                    "raw_target_volume": round(raw_target, 4),
                    "final_stepped_volume": volume,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # 9. Build entry context
        brain_preds: list[dict[str, Any]] = []
        for p in proposals:
            _dir = getattr(p, "direction", None)
            _conf = float(getattr(p, "confidence", 0.5))
            if _dir is None:
                pred = getattr(p, "prediction", None) or {}
                _dir = pred.get("direction_bias", "neutral")
                _conf = float(pred.get("confidence", 0.5))
            if _dir == "long":
                _up, _down = _conf, round(1.0 - _conf, 4)
            elif _dir == "short":
                _up, _down = round(1.0 - _conf, 4), _conf
            else:
                _up, _down = 0.5, 0.5
            brain_preds.append(
                {
                    "brain_id": getattr(p, "brain_id", "unknown"),
                    "direction": _dir,
                    "up_probability": _up,
                    "down_probability": _down,
                    "confidence": _conf,
                }
            )

        return StrategyDecision(
            strategy_name=getattr(config, "name", ""),
            magic=int(getattr(config, "magic", 0)),
            direction=meta_dir,
            confidence=round(meta_p_win, 4),
            volume=volume,
            sl=levels["stop_loss"],
            tp=levels["take_profit"],
            hard_sl=levels["hard_sl"],
            brain_ids=brain_ids or [spec.brain_id],
            reason="meta_pipeline_approved",
            p_win=meta_p_win,
            kelly_mult=kelly_mult,
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def _utc_iso() -> str:
    from datetime import UTC
    from datetime import datetime as _dt

    return _dt.now(UTC).isoformat()


def _compute_meta_volume(
    *,
    meta_p_win: float,
    current_atr: float,
    regime_info: dict[str, Any] | None,
    regime_gate_mode: str,
    risk_budget_usd: float,
    entry_z_score: float,
    kelly_mult: float,
    config: Any,
) -> float:
    """Compute volume for a meta-pipeline trade.

    Canonical shared volume formula — keep in sync with
    StrategyLine._compute_volume in strategy_line.py.
    """
    import math

    base_volume = float(getattr(config, "base_volume", 0.01))
    max_volume = float(getattr(config, "max_volume", 0.05))
    lot_step = float(getattr(config, "lot_step", 0.01))
    ref_atr = float(getattr(config, "ref_atr", 5.0))

    # Vol-targeted sizing when risk_budget_usd > 0
    if risk_budget_usd > 0 and current_atr > 0:
        risk_per_lot = current_atr * 100.0
        if risk_per_lot > 0:
            target_risk_volume = risk_budget_usd / risk_per_lot
            vol_atr_factor = ref_atr / current_atr if current_atr > 0 else 1.0
        else:
            target_risk_volume = base_volume
            vol_atr_factor = 1.0
    else:
        target_risk_volume = base_volume
        vol_atr_factor = ref_atr / current_atr if current_atr > 0 else 1.0

    # Regime multiplier
    regime_mult = 1.0
    if regime_info and regime_gate_mode != "shadow":
        regime = regime_info.get("regime", "normal")
        if regime == "low_vol":
            regime_mult = float(getattr(config, "regime_vol_mult_low", 1.20))
        elif regime == "high_vol":
            regime_mult = float(getattr(config, "regime_vol_mult_high", 0.70))

    # z-depth penalty
    z_penalty = 0.55 + 0.45 / (1.0 + abs(entry_z_score) * 0.5) if entry_z_score != 0 else 1.0

    volume = target_risk_volume * vol_atr_factor * regime_mult * z_penalty * kelly_mult
    volume = min(volume, max_volume)
    volume = max(volume, lot_step)

    # Step to lot granularity
    ticks = math.floor(volume / lot_step + 0.5)
    return max(lot_step, round(ticks * lot_step, 2))
