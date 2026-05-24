"""Unified brain quality engine — single source of truth for all quality consumers.

All quality assessments flow through one engine so that DynamicBrainWeighter,
BrainLeaderboard, GovernanceScheduler, PositionManager, and live_cycle never
produce contradictory labels for the same brain.

The engine is brain-type-agnostic: it only reads BrainPnLMetrics and governance
status, never brain_type or contract_group.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ── Default tier thresholds (configurable) ──────────────────────────────
# Unified tier mapping:
#   score >= 85 → exceptional,  >= 70 → healthy,  >= 50 → stable,
#   >= 35 → warning,  >= 20 → degraded,  >= 10 → marginal,  < 10 → critical

DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "exceptional": {"score": 85.0},
    "healthy": {"score": 70.0},
    "stable": {"score": 50.0},
    "warning": {"score": 35.0},
    "degraded": {"score": 20.0},
    "marginal": {"score": 10.0},
    "critical": {"score": 0.0},  # < 10 implicit
}

DEFAULT_MIN_TRADES = 30  # below this → insufficient_data (statistically meaningful)


# ── Tier → weight mapping ───────────────────────────────────────────────

TIER_VOTE_WEIGHT: dict[str, float] = {
    "exceptional": 1.5,
    "healthy": 1.2,
    "stable": 1.0,
    "warning": 0.7,
    "degraded": 0.4,
    "marginal": 0.2,
    "critical": 0.0,
    "insufficient_data": 1.0,
}

# ── Tier → governance recommendation ────────────────────────────────────

TIER_GOVERNANCE_REC: dict[str, str] = {
    "exceptional": "promote",
    "healthy": "maintain",
    "stable": "maintain",
    "warning": "probation",
    "degraded": "freeze",
    "marginal": "freeze",
    "critical": "retire",
    "insufficient_data": "observe",
}


@dataclass
class BrainQualityVerdict:
    """Single authoritative quality verdict for one brain.

    Produced by BrainQualityEngine.assess() and consumed by all downstream
    systems (weighting, leaderboard, governance, position management).
    """

    brain_id: str
    quality_tier: str  # exceptional | healthy | stable | warning | degraded | marginal | critical | insufficient_data
    score: float  # 0–100 composite
    vote_weight: float  # recommended vote weight
    governance_rec: str  # promote | maintain | probation | freeze | retire | observe

    # Input metrics snapshot (for audit trail)
    sample_count: int = 0
    sharpe: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    cumulative_pnl: float = 0.0
    max_drawdown: float = 0.0

    # Computed sub-components (for debugging / divergence detection)
    sharpe_component: float = 0.0
    wr_component: float = 0.0
    pf_component: float = 0.0
    pnl_component: float = 0.0
    dd_component: float = 0.0

    # Governance-aware override
    governance_status: str = ""

    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "brain_id": self.brain_id,
            "quality_tier": self.quality_tier,
            "score": round(self.score, 2),
            "vote_weight": self.vote_weight,
            "governance_rec": self.governance_rec,
            "sample_count": self.sample_count,
            "sharpe": round(self.sharpe, 4),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4),
            "cumulative_pnl": round(self.cumulative_pnl, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "governance_status": self.governance_status,
            "components": {
                "sharpe": round(self.sharpe_component, 2),
                "win_rate": round(self.wr_component, 2),
                "profit_factor": round(self.pf_component, 2),
                "cumulative_pnl": round(self.pnl_component, 2),
                "drawdown": round(self.dd_component, 2),
            },
        }

    @property
    def is_active(self) -> bool:
        return self.quality_tier not in ("critical", "insufficient_data")

    @property
    def needs_attention(self) -> bool:
        return self.quality_tier in ("warning", "degraded", "critical")


class BrainQualityEngine:
    """Single authoritative source for brain quality assessment.

    Usage::

        # Direct instantiation:
        engine = BrainQualityEngine()
        verdict = engine.assess("xgboost_v9", metrics, governance_status="live")

        # Or via singleton (preferred for cross-system consistency):
        verdict = BrainQualityEngine.instance().assess(brain_id, metrics)
    """

    _instance: BrainQualityEngine | None = None

    def __init__(
        self,
        thresholds: dict[str, dict[str, float]] | None = None,
        min_trades: int = DEFAULT_MIN_TRADES,
    ):
        self._thresholds = thresholds or DEFAULT_THRESHOLDS
        self._min_trades = min_trades

    @classmethod
    def instance(cls) -> BrainQualityEngine:
        """Return the global singleton (lazy-init with defaults)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for tests)."""
        cls._instance = None

    # ── Public API ──────────────────────────────────────────────────────

    def assess(
        self,
        brain_id: str,
        metrics: Any,  # BrainPnLMetrics or duck-typed equivalent
        governance_status: str = "",
    ) -> BrainQualityVerdict:
        """Produce the single authoritative quality verdict for *brain_id*.

        Args:
            brain_id: Brain identifier.
            metrics: A BrainPnLMetrics instance or any object/dict with
                     sharpe_ratio, win_rate, profit_factor, cumulative_pnl,
                     max_drawdown, sample_count attributes/keys.
            governance_status: Current governance state (live, probation,
                               frozen, retired, shadow, candidate, etc.).
        """
        # Normalise input to dict
        if hasattr(metrics, "sharpe_ratio"):
            m = {
                "sample_count": getattr(metrics, "sample_count", 0),
                "sharpe_ratio": getattr(metrics, "sharpe_ratio", 0.0),
                "win_rate": getattr(metrics, "win_rate", 0.0),
                "profit_factor": getattr(metrics, "profit_factor", 0.0),
                "cumulative_pnl": getattr(metrics, "cumulative_pnl", 0.0),
                "max_drawdown": getattr(metrics, "max_drawdown", 0.0),
            }
        elif isinstance(metrics, dict):
            m = {
                "sample_count": metrics.get("sample_count", 0),
                "sharpe_ratio": metrics.get("sharpe_ratio", 0.0),
                "win_rate": metrics.get("win_rate", 0.0),
                "profit_factor": metrics.get("profit_factor", 0.0),
                "cumulative_pnl": metrics.get("cumulative_pnl", 0.0),
                "max_drawdown": metrics.get("max_drawdown", 0.0),
            }
        else:
            raise TypeError(f"metrics must be BrainPnLMetrics or dict, got {type(metrics)}")

        return self._compute(brain_id, m, governance_status)

    def assess_batch(
        self,
        metrics_map: dict[str, Any],
        governance_states: dict[str, str] | None = None,
    ) -> dict[str, BrainQualityVerdict]:
        """Assess all brains in *metrics_map* at once.

        Args:
            metrics_map: brain_id → BrainPnLMetrics (or dict).
            governance_states: brain_id → governance status string.
        """
        gov = governance_states or {}
        return {bid: self.assess(bid, m, gov.get(bid, "")) for bid, m in metrics_map.items()}

    def get_weight(self, brain_id: str, metrics: Any, governance_status: str = "") -> float:
        """Convenience: return only the vote weight."""
        return self.assess(brain_id, metrics, governance_status).vote_weight

    # ── Internal scoring ────────────────────────────────────────────────

    def _compute(
        self,
        brain_id: str,
        m: dict[str, float],
        governance_status: str,
    ) -> BrainQualityVerdict:
        n = int(m["sample_count"])
        sharpe = float(m["sharpe_ratio"])
        wr = float(m["win_rate"])
        pf = float(m["profit_factor"])
        cum_pnl = float(m["cumulative_pnl"])
        max_dd = float(m["max_drawdown"])

        # ── Insufficient data ───────────────────────────────────────────
        if n < self._min_trades:
            return BrainQualityVerdict(
                brain_id=brain_id,
                quality_tier="insufficient_data",
                score=0.0,
                vote_weight=TIER_VOTE_WEIGHT["insufficient_data"],
                governance_rec=TIER_GOVERNANCE_REC["insufficient_data"],
                sample_count=n,
                sharpe=sharpe,
                win_rate=wr,
                profit_factor=pf,
                cumulative_pnl=cum_pnl,
                max_drawdown=max_dd,
                governance_status=governance_status,
            )

        # ── Governed retirement override ────────────────────────────────
        if governance_status == "retired":
            return BrainQualityVerdict(
                brain_id=brain_id,
                quality_tier="critical",
                score=0.0,
                vote_weight=0.0,
                governance_rec="retire",
                sample_count=n,
                sharpe=sharpe,
                win_rate=wr,
                profit_factor=pf,
                cumulative_pnl=cum_pnl,
                max_drawdown=max_dd,
                governance_status=governance_status,
            )

        # ── Composite score (Leaderboard formula, 0–100) ────────────────
        sharpe_comp, wr_comp, pf_comp, pnl_comp, dd_comp = self._score_components(
            sharpe, wr, pf, cum_pnl, max_dd
        )
        score = max(0.0, sharpe_comp + wr_comp + pf_comp + pnl_comp + dd_comp)

        # ── Tier from score ─────────────────────────────────────────────
        tier = self._score_to_tier(score)

        # ── Governance-aware tier adjustment ────────────────────────────
        # Frozen brains can only be warning/degraded/marginal/critical — cannot be healthy
        if governance_status == "frozen" and tier in ("exceptional", "healthy", "stable"):
            tier = "warning"
            score = min(score, self._thresholds.get("warning", {}).get("score", 35.0) - 1.0)

        # ── Weight — with hard gates + continuous score mapping ─────────
        weight = self._compute_weight(
            score,
            tier,
            sharpe,
            n,
            wr=wr,
            pf=pf,
            cumulative_pnl=cum_pnl,
            max_drawdown=max_dd,
        )

        # ── Governance recommendation ───────────────────────────────────
        gov_rec = TIER_GOVERNANCE_REC.get(tier, "maintain")

        return BrainQualityVerdict(
            brain_id=brain_id,
            quality_tier=tier,
            score=round(score, 2),
            vote_weight=weight,
            governance_rec=gov_rec,
            sample_count=n,
            sharpe=sharpe,
            win_rate=wr,
            profit_factor=pf,
            cumulative_pnl=cum_pnl,
            max_drawdown=max_dd,
            sharpe_component=sharpe_comp,
            wr_component=wr_comp,
            pf_component=pf_comp,
            pnl_component=pnl_comp,
            dd_component=dd_comp,
            governance_status=governance_status,
        )

    @staticmethod
    def _score_components(
        sharpe: float,
        wr: float,
        pf: float,
        cum_pnl: float,
        max_dd: float,
    ) -> tuple[float, float, float, float, float]:
        """Compute the five score sub-components."""
        # Sharpe: tanh-squashed, 40% weight
        sharpe_comp = 40.0 * math.tanh(sharpe / 3.0)

        # Win rate: smooth ramp from 35% to 90%, 25% weight
        wr_comp = 25.0 * max(0.0, min(1.0, (wr - 0.35) / 0.55))

        # Profit factor: capped at 3.0, 15% weight
        pf_capped = min(pf if pf != float("inf") else 3.0, 3.0)
        pf_comp = 15.0 * (pf_capped / 3.0)

        # Cumulative PnL: tanh-squashed, 10% weight
        pnl_comp = 10.0 * math.tanh(cum_pnl / 50.0)

        # Drawdown penalty: 10% weight
        if cum_pnl > 0:
            dd_ratio = min(max_dd / max(abs(cum_pnl) + 0.01, 1.0), 1.0)
        else:
            dd_ratio = max_dd / (max_dd + abs(cum_pnl) + 1.0)
        dd_comp = 10.0 * (1.0 - dd_ratio)

        return sharpe_comp, wr_comp, pf_comp, pnl_comp, dd_comp

    def _score_to_tier(self, score: float) -> str:
        """Map composite score to quality tier (ordered best → worst)."""
        for tier in ("exceptional", "healthy", "stable", "warning", "degraded", "marginal"):
            if score >= self._thresholds.get(tier, {}).get("score", 0):
                return tier
        return "critical"

    @staticmethod
    def _compute_weight(
        score: float,
        tier: str,
        sharpe: float,
        sample_count: int,
        *,
        wr: float = 0.0,
        pf: float = 0.0,
        cumulative_pnl: float = 0.0,
        max_drawdown: float = 0.0,
    ) -> float:
        """Compute vote weight from quality verdict with hard gates.

        Hard gates (safety-critical, override tier):
          - trades >= 100, PnL < 0, AND (WR < 30% or PF < 0.60) → 0.0 (retired)
          - trades >= 100, PnL < 0 → probation floor 0.5

        Continuous weight mapping from score:
          score >= 85 → 1.50
          score >= 70 → 1.00 + (score-70)/15 * 0.50  → [1.00, 1.50]
          score >= 50 → 0.70 + (score-50)/20 * 0.30   → [0.70, 1.00]
          score >= 35 → 0.40 + (score-35)/15 * 0.30   → [0.40, 0.70]
          score >= 20 → 0.20 + (score-20)/15 * 0.20   → [0.20, 0.40]
          score >= 10 → 0.05 + (score-10)/10 * 0.15   → [0.05, 0.20]
          score  < 10 → 0.00
        """
        trades = sample_count

        # ── Hard gate: auto-retirement ──
        if trades >= 100 and cumulative_pnl < 0 and (wr < 0.30 or pf < 0.60):
            return 0.0

        tier_weight = TIER_VOTE_WEIGHT.get(tier, 1.0)

        # Insufficient_data / critical: return tier base directly
        if tier in ("insufficient_data", "critical"):
            return tier_weight

        # ── Continuous weight from score (smoothed within tier) ──
        if score >= 85:
            weight = 1.50
        elif score >= 70:
            weight = 1.00 + (score - 70) / 15.0 * 0.50
        elif score >= 50:
            weight = 0.70 + (score - 50) / 20.0 * 0.30
        elif score >= 35:
            weight = 0.40 + (score - 35) / 15.0 * 0.30
        elif score >= 20:
            weight = 0.20 + (score - 20) / 15.0 * 0.20
        elif score >= 10:
            weight = 0.05 + (score - 10) / 10.0 * 0.15
        else:
            weight = 0.0

        # Sharpe continuous adjustment: ±0.15 range
        sharpe_bend = math.tanh(sharpe / 3.0)
        weight += sharpe_bend * 0.15

        # Drawdown penalty
        if max_drawdown > 3.0:
            weight *= 0.85

        # ── Candidate gate: very low sample count → shadow vote ──
        if trades < 10:
            weight = min(weight, 0.5)

        # Probation floor: lots of trades but negative PnL (applies AFTER
        # sharpe/drawdown so adjustments can't push weight above the cap)
        if trades >= 100 and cumulative_pnl < 0:
            weight = min(weight, 0.5)

        return round(max(0.0, min(3.0, weight)), 2)
