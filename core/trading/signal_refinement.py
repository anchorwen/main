"""V6 Layer A: Signal Refinement Gate — entry signal quality filtering.

Sits AFTER brain ensemble consensus but BEFORE order dispatch.  Three sub-gates:
  A1: RegimeSuitability — "Should we trade this strategy in this regime?"
  A2: SignalQualityScorer — "How good is this specific signal?"
  A3: MultiTFConfirmation — "Do higher timeframes agree with the direction?"

Design (v6_integration_blueprint.pdf §2 Layer A):
  - All three gates are brain-agnostic — they consume standard contracts.
  - Geometric mean veto: any dimension at 0 → overall score = 0.
  - Size multiplier ∈ [0, 1] modulates position volume without fully blocking.
  - Config-gated: when disabled, returns pass-through RefinementResult.

Reference:
  - God's Eye V6.0: signal_quality.py (5-dim quality scorer)
  - d:\future: conformal_ou_gate.py (ConformalOUGate), regime_gate.py (RegimeGate),
    gods_eye.py (GodsEye cross-TF consensus), meta_signal_filter.py (MetaSignalFilter)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.trading.contracts import RefinementResult

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class RefinementConfig:
    """Per-symbol Signal Refinement Gate configuration.

    Loaded from configs/trading/signal_refinement.yaml.
    """

    enabled: bool = False
    shadow_mode: bool = False

    # A1: Regime Suitability
    min_regime_score: float = 0.25

    # A2: Signal Quality
    min_entry_quality: float = 0.35
    sizing_floor: float = 0.25
    quality_weights: dict[str, float] = field(
        default_factory=lambda: {
            "regime_confidence": 0.30,
            "estimation_quality": 0.25,
            "z_quality": 0.15,
            "consensus_strength": 0.20,
            "multi_tf_alignment": 0.10,
        }
    )

    # A3: Multi-TF
    min_alignment_score: float = 0.50


# ═══════════════════════════════════════════════════════════════════════
# A1: Regime Suitability
# ═══════════════════════════════════════════════════════════════════════

# Strategy type → regime state → suitability [0, 1]
# 1.0 = ideal regime, 0.0 = completely unsuitable.
# Regime states resolved by RegimeGate.classify().
_DEFAULT_REGIME_MATRIX: dict[str, dict[str, float]] = {
    "statarb_dynamic": {
        "mean_reversion": 1.0,
        "oscillation": 0.8,
        "low_volatility": 0.7,
        "mild_trend": 0.3,
        "trending": 0.0,
        "unknown": 0.3,
    },
    "barrier_12bar": {
        "mean_reversion": 0.9,
        "oscillation": 0.9,
        "mild_trend": 0.6,
        "trending": 0.4,
        "low_volatility": 0.5,
        "unknown": 0.5,
    },
    "swing": {
        "mean_reversion": 0.6,
        "oscillation": 0.7,
        "mild_trend": 0.8,
        "trending": 0.9,
        "low_volatility": 0.4,
        "unknown": 0.5,
    },
    # Default fallback for unlisted strategy types
    "default": {
        "mean_reversion": 0.5,
        "oscillation": 0.5,
        "mild_trend": 0.5,
        "trending": 0.5,
        "low_volatility": 0.5,
        "unknown": 0.5,
    },
}


def _score_regime_suitability(
    strategy_type: str,
    regime_state: str,
    matrix: dict[str, dict[str, float]] | None = None,
) -> float:
    """Score how suitable the current regime is for a given strategy type.

    Args:
        strategy_type: e.g. "statarb_dynamic", "barrier_12bar", "swing".
        regime_state: e.g. "mean_reversion", "trending", "oscillation".
        matrix: Override matrix.  Uses _DEFAULT_REGIME_MATRIX if None.

    Returns:
        Suitability score ∈ [0, 1].
    """
    m = matrix or _DEFAULT_REGIME_MATRIX
    strategy_row = m.get(strategy_type, m.get("default", {}))
    return strategy_row.get(regime_state, strategy_row.get("unknown", 0.5))


# ═══════════════════════════════════════════════════════════════════════
# A2: Signal Quality Scorer
# ═══════════════════════════════════════════════════════════════════════


def _score_signal_quality(
    *,
    regime_confidence: float = 0.5,
    estimation_quality: float = 0.5,
    z_quality: float = 0.5,
    consensus_strength: float = 0.5,
    multi_tf_alignment: float = 0.5,
    weights: dict[str, float] | None = None,
) -> tuple[float, dict[str, float]]:
    """Compute unified signal quality from 5 orthogonal dimensions.

    Uses geometric mean so any dimension at 0 → overall score = 0.
    This is a "veto by zero" design — a signal that fails completely
    on any single dimension is never traded.

    Returns:
        (overall_score ∈ [0, 1], component_scores dict).
    """
    w = weights or {
        "regime_confidence": 0.30,
        "estimation_quality": 0.25,
        "z_quality": 0.15,
        "consensus_strength": 0.20,
        "multi_tf_alignment": 0.10,
    }

    components = {
        "regime_confidence": max(0.0, min(1.0, regime_confidence)),
        "estimation_quality": max(0.0, min(1.0, estimation_quality)),
        "z_quality": max(0.0, min(1.0, z_quality)),
        "consensus_strength": max(0.0, min(1.0, consensus_strength)),
        "multi_tf_alignment": max(0.0, min(1.0, multi_tf_alignment)),
    }

    # Geometric mean with weights
    import math

    log_sum = 0.0
    total_weight = 0.0
    for dim, score in components.items():
        weight = w.get(dim, 0.0)
        if weight > 0:
            if score <= 0:
                return 0.0, components  # Geometric mean veto
            log_sum += weight * math.log(max(score, 1e-10))
            total_weight += weight

    if total_weight <= 0:
        return 0.0, components

    overall = math.exp(log_sum / total_weight)
    return float(overall), components


# ═══════════════════════════════════════════════════════════════════════
# A3: Multi-TF Confirmation
# ═══════════════════════════════════════════════════════════════════════


def _score_multi_tf_alignment(
    direction: str,
    tf_directions: dict[str, str],
    tf_weights: dict[str, float] | None = None,
) -> float:
    """Score how well higher timeframes align with the trade direction.

    Args:
        direction: "long" or "short".
        tf_directions: dict mapping TF name → direction ("long"/"short"/"neutral").
        tf_weights: dict mapping TF name → weight in alignment score.

    Returns:
        Alignment score ∈ [0, 1].  1.0 = all TFs agree, 0.0 = all oppose.
    """
    w = tf_weights or {
        "M5": 0.10,
        "M15": 0.15,
        "M30": 0.20,
        "H1": 0.25,
        "H4": 0.20,
        "D1": 0.10,
    }

    total_weight = 0.0
    aligned_weight = 0.0

    for tf_name, tf_weight in w.items():
        tf_dir = tf_directions.get(tf_name, "neutral")
        total_weight += tf_weight
        if tf_dir == direction:
            aligned_weight += tf_weight
        elif tf_dir == "neutral":
            aligned_weight += tf_weight * 0.5  # Neutral = half credit

    if total_weight <= 0:
        return 0.5  # No data → neutral

    return aligned_weight / total_weight


# ═══════════════════════════════════════════════════════════════════════
# Main Gate
# ═══════════════════════════════════════════════════════════════════════


class SignalRefinementGate:
    """Entry signal quality filter — Layer A of V6 Shared Infrastructure.

    Usage (in live_cycle.py, after StrategyLine.evaluate()):
        gate = SignalRefinementGate(config)
        result = gate.evaluate(
            decision=decision,
            regime_info=regime_info,
            gods_eye_snapshot=gods_eye_snapshot,
            ou_params=ou_params,
        )
        if not result.is_approved:
            log(f"Signal blocked: {result.suppression_reason}")
            return  # skip order dispatch
        decision.volume *= result.size_multiplier
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._cfg = config or {}
        self._global = self._cfg.get("global", {})

    @property
    def enabled(self) -> bool:
        return bool(self._global.get("enabled", False))

    @property
    def shadow_mode(self) -> bool:
        return bool(self._global.get("shadow_mode", False))

    def evaluate(
        self,
        *,
        decision: Any,  # StrategyDecision
        regime_info: dict[str, Any],
        gods_eye_snapshot: dict[str, Any] | None = None,
        ou_params: dict[str, Any] | None = None,
    ) -> RefinementResult:
        """Evaluate all three sub-gates.  Returns RefinementResult.

        When disabled, returns pass-through (is_approved=True, size_multiplier=1.0).
        """
        if not self.enabled:
            return RefinementResult(
                is_approved=True,
                size_multiplier=1.0,
                adjusted_confidence=getattr(decision, "confidence", 0.5),
                suppression_reason="disabled",
            )

        strategy_type = getattr(decision, "strategy_name", "default")
        direction = getattr(decision, "direction", "neutral")
        confidence = getattr(decision, "confidence", 0.5)

        # ── A1: Regime Suitability ──────────────────────────
        regime_state = regime_info.get("regime_state", "unknown")
        regime_matrix = self._cfg.get("regime_suitability", {}).get("matrix")
        regime_score = _score_regime_suitability(strategy_type, regime_state, regime_matrix)

        min_regime = self._cfg.get("regime_suitability", {}).get("min_score_for_entry", 0.25)

        # ── A2: Signal Quality ──────────────────────────────
        sq_cfg = self._cfg.get("signal_quality", {})
        weights = sq_cfg.get("weights")

        # Regime confidence from existing infrastructure
        regime_confidence = float(regime_info.get("ou_regime_prob", 0.5))

        # Estimation quality: 1/(1+KF_uncertainty)
        kf_uncertainty = float(regime_info.get("kf_uncertainty", 0.5))
        estimation_quality = 1.0 / (1.0 + kf_uncertainty)

        # Z-quality: distance from boundaries (sweet spot)
        entry_z = float(ou_params.get("z_score", 0)) if ou_params else 0.0
        z_quality = _compute_z_quality(abs(entry_z), min_z=2.5, max_z=3.0, sweet_spot=2.75)

        # Consensus strength from decision
        consensus_strength = float(min(confidence, 1.0))

        # Multi-TF alignment
        tf_directions = _extract_tf_directions(gods_eye_snapshot or {}, regime_info)
        tf_cfg = self._cfg.get("multi_tf", {})
        tf_weights = tf_cfg.get("tf_weights")
        alignment_score = _score_multi_tf_alignment(direction, tf_directions, tf_weights)

        quality_score, components = _score_signal_quality(
            regime_confidence=regime_confidence,
            estimation_quality=estimation_quality,
            z_quality=z_quality,
            consensus_strength=consensus_strength,
            multi_tf_alignment=alignment_score,
            weights=weights,
        )

        min_quality = sq_cfg.get("min_entry_quality", 0.35)
        sizing_floor = sq_cfg.get("sizing_floor", 0.25)

        # ── A3: Multi-TF Confirmation ───────────────────────
        min_alignment = self._cfg.get("multi_tf", {}).get("min_alignment_score", 0.50)

        # ── Gate verdict ────────────────────────────────────
        block_reasons: list[str] = []

        if regime_score < min_regime:
            block_reasons.append(
                f"regime_unsuitable({regime_state}:{regime_score:.2f}<{min_regime})"
            )

        if quality_score < min_quality:
            block_reasons.append(f"quality_low({quality_score:.2f}<{min_quality})")

        if alignment_score < min_alignment and direction != "neutral":
            block_reasons.append(f"tf_misaligned({alignment_score:.2f}<{min_alignment})")

        is_approved = len(block_reasons) == 0

        # Size multiplier: geometric mean of regime_score and quality_score,
        # floored at sizing_floor
        if is_approved:
            size_multiplier = max(
                sizing_floor,
                min(1.0, (regime_score * quality_score) ** 0.5),
            )
        else:
            size_multiplier = 0.0

        suppression_reason = "; ".join(block_reasons) if block_reasons else ""
        adjusted_confidence = min(confidence, quality_score) if is_approved else 0.0

        return RefinementResult(
            is_approved=is_approved,
            size_multiplier=round(size_multiplier, 4),
            adjusted_confidence=round(adjusted_confidence, 4),
            suppression_reason=suppression_reason,
            component_scores={
                "regime_score": round(regime_score, 4),
                "quality_score": round(quality_score, 4),
                "alignment_score": round(alignment_score, 4),
                **{f"q_{k}": round(v, 4) for k, v in components.items()},
            },
        )


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _compute_z_quality(
    abs_z: float,
    min_z: float = 2.5,
    max_z: float = 3.0,
    sweet_spot: float = 2.75,
) -> float:
    """Score how close |z| is to the sweet spot (not near boundaries).

    Returns 1.0 at sweet_spot, 0.0 at boundaries or outside [min_z, max_z].
    Triangular distribution with peak at sweet_spot.
    """
    if abs_z < min_z or abs_z > max_z:
        return 0.0

    if abs_z <= sweet_spot:
        # Rising from min_z to sweet_spot
        if sweet_spot > min_z:
            return (abs_z - min_z) / (sweet_spot - min_z)
        return 1.0
    else:
        # Falling from sweet_spot to max_z
        if max_z > sweet_spot:
            return (max_z - abs_z) / (max_z - sweet_spot)
        return 1.0


def _extract_tf_directions(
    gods_eye: dict[str, Any],
    regime_info: dict[str, Any],
) -> dict[str, str]:
    """Extract direction per timeframe from God's Eye snapshot and regime info.

    Returns dict like {"M5": "long", "M15": "short", "H1": "neutral", ...}.
    """
    tf_dirs: dict[str, str] = {}

    # From God's Eye (preferred source)
    for tf_name in ("M5", "M15", "M30", "H1", "H4", "D1"):
        tf_data = gods_eye.get(tf_name, {})
        if isinstance(tf_data, dict):
            direction = tf_data.get("direction", "")
            if direction in ("long", "short"):
                tf_dirs[tf_name] = direction
                continue

    # Fallback: from regime_info trend_detectors
    trend_detectors = regime_info.get("trend_detectors", {})
    for tf_name in ("M5", "H1", "H4", "D1"):
        if tf_name not in tf_dirs:
            td = trend_detectors.get(tf_name, {})
            if isinstance(td, dict):
                direction = td.get("direction", "")
                if direction in ("long", "short"):
                    tf_dirs[tf_name] = direction
                elif td.get("strength", 0) < 0.3:
                    tf_dirs[tf_name] = "neutral"

    # Fill missing TFs as neutral
    for tf_name in ("M5", "M15", "M30", "H1", "H4", "D1"):
        if tf_name not in tf_dirs:
            tf_dirs[tf_name] = "neutral"

    return tf_dirs
