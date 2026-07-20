"""StrategyEvaluationContext — frozen SSOT for strategy evaluate() input.

Institutional mandate (L3 — Interface Contract Consolidation):
    The 28-parameter evaluate() signature was a recurring source of TypeError
    crashes (strategy_atr → governance_state → microstructure_gate).
    Consolidating into a frozen dataclass achieves:

    1. Single-Parameter Contract: evaluate() signature is permanent — adding
       a field here never changes any function signature.
    2. Frozen = Immutable: context cannot be mutated mid-evaluation, ensuring
       input purity through the entire decision chain.
    3. O(1) extension cost: new gate/parameter = one field here = zero
       downstream signature changes.

    This is the Parameter Object Pattern applied at institutional grade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StrategyEvaluationContext:
    """All input state for one strategy evaluation cycle.

    Frozen (immutable) — context is read-only throughout evaluate().
    Local overrides use Python's local variable shadowing, not mutation.

    Fields are ordered: required (no default) first, then optional.
    All fields have defaults for test ergonomics; the production call site
    always provides every field explicitly.
    """

    # ── Feature vectors (ML input) ──
    feature_vector: Any = None
    micro_feature_vector: Any = None

    # ── Prices ──
    mid_price: float | None = None
    bid: float | None = None
    ask: float | None = None

    # ── ATR (volatility anchor) ──
    current_atr: float = 5.0  # non-optional — call site always provides concrete value
    strategy_atr: float | None = None  # per-TF ATR (FIX-20260706-027)

    # ── Regime ──
    regime_info: dict[str, Any] | None = None
    regime_gate_mode: str = "full"
    trend_direction: str = "neutral"
    trend_strength: float = 0.0
    h4_trend_strength: float = 0.0
    macro_regime: str = "mixed"

    # ── Advanced signals ──
    hurst: float | None = None  # FIX-20260607-007: M5 Hurst for trend maturity
    kalman_velocity_bps: float | None = None  # FIX-20260607-007: H1 Kalman velocity (bps)

    # ── Risk ──
    risk_budget_usd: float = 0.0

    # ── Infrastructure ──
    tracker: Any = None
    pnl_ledger: Any = None
    pnl_store: Any = None

    # ── ML feature data ──
    micro_sequences: dict[str, Any] | None = None
    daily_feature_vector: Any = None
    micro_feature_dict: dict[str, float] | None = None
    btc_augment: Any = None  # FIX-20260613-046: pre-computed BTC vector

    # ── Gates ──
    meta_filter: Any = None
    meta_filter_gate: Any = None
    conformal_ou_gate: Any = None
    microstructure_gate: Any = None  # FIX-20260720-002

    # ── Governance ──
    governance_state: dict[str, Any] | None = None  # DQAF-20260622-059


__all__ = ["StrategyEvaluationContext"]
