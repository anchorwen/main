"""Conformal OU Gate — physics-based signal quality gate for OU mean-reversion strategies.

Replaces the generic 47-dim LightGBM MetaFilterGate for ``statarb_dynamic`` (M5)
and ``statarb_m15`` (M15) strategy lines.  Instead of a learned model, this gate
computes a physics-grounded quality score from OU process diagnostics and applies
a conformal-calibrated adaptive threshold (Q10, FIFO window).

**Design Principle**: OU signals must be validated against their own physics —
Z-Depth (how deep in signal territory), Z-Velocity (signal momentum), Half-life
(mean-reversion speed), Theta strength (reversion evidence), and ADX alignment
(trend contamination).  Any factor can independently degrade the score.

**Score Formula (geometric mean, default)**:
    score = (∏ clip(component, 0.0, 1.0))^(1/5)
Each component is clamped [0.0, 1.0] before the geometric mean so any component
at zero (e.g. theta ≤ 0, no mean-reversion) vetoes the score.  The geometric
mean is the correct central tendency for ratio-scale quality metrics — it
preserves the veto property without the dimensional collapse of a raw product.

**Warmup-Driven Threshold Schedule (Explore-then-Commit)**:
    Phase COLD  (samples < 50):  threshold = 0.20, volume = 0.01 (exploration)
    Phase WARM  (50 ≤ n < 100):  threshold = max(0.20, calibrator.Q10)
    Phase HOT   (n ≥ 100):       threshold = calibrator.Q10, clamp [0.25, 0.65]
During COLD phase, position sizing is force-capped at min lot to cap
exploration risk while the calibrator accumulates samples.  This breaks
the chicken-and-egg deadlock where the gate blocks trades → no samples →
calibrator never warms up.

**Integration**: The existing :class:`ConformalCalibrator` provides the adaptive
threshold — the gate calls ``calibrator.compute_threshold()`` on each evaluation
and ``calibrator.update(score, label)`` when a trade closes.  This is the same
Track 3d calibration infrastructure already wired for MetaFilterGate.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from core.runtime.fault_handler import fail_open_guard

logger = logging.getLogger(__name__)

# ── Constants ──
DEFAULT_BASE_THRESHOLD = 0.35  # lenient base — physics score is conservative
DEFAULT_MIN_THRESHOLD = 0.25
DEFAULT_MAX_THRESHOLD = 0.65
DEFAULT_OU_CONFIGS_PATH = "configs/brains/"

# Scoring mode
DEFAULT_SCORING_MODE = "geometric_mean"  # "geometric_mean" | "product"

# Warmup-driven threshold schedule (Explore-then-Commit)
COLD_PHASE_THRESHOLD = 0.20  # lenient fixed threshold for sample collection
COLD_PHASE_MAX_SAMPLES = 50  # calibrator warmup boundary
WARM_PHASE_MAX_SAMPLES = 100  # transition to fully adaptive Q10
COLD_PHASE_FORCE_VOLUME = 0.01  # cent-account min lot during exploration


# ── OU parameter loading ────────────────────────────────────────────────────


def _load_ou_params(artifact_path: str) -> dict[str, float]:
    """Load OU optimal_params from a brain artifact JSON."""
    try:
        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)
        opt = artifact.get("optimal_params", {})
        return {
            "window": float(opt.get("window", 100)),
            "z_entry": float(opt.get("z_entry", 2.0)),
            "z_exit": float(opt.get("z_exit", 0.5)),
            "max_half_life": float(opt.get("max_half_life", 20)),
            "theta_min": float(opt.get("theta_min", 0.005)),
        }
    except Exception:  # BLE001:FOG_WRAPPED
        with fail_open_guard("ConformalOUGate:Compute"):
            raise
        logger.warning("ConformalOUGate: cannot load OU params from %s", artifact_path)
        return {
            "window": 100.0,
            "z_entry": 2.0,
            "z_exit": 0.5,
            "max_half_life": 20.0,
            "theta_min": 0.005,
        }


def _build_ou_configs(brains_dir: str = DEFAULT_OU_CONFIGS_PATH) -> dict[str, dict[str, float]]:
    """Scan brain configs for OU-type brains and extract strategy→params mapping.

    Returns ``{strategy_name: {z_entry, z_exit, max_half_life, theta_min}}``
    keyed by ``contract_group`` (which maps 1:1 to strategy line names).
    """
    configs: dict[str, dict[str, float]] = {}
    bp = Path(brains_dir)
    if not bp.exists():
        return configs

    for cfg_path in sorted(bp.glob("*.json")):
        if "normalization" in cfg_path.name.lower():
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        btype = cfg.get("brain_type", "")
        if not btype.startswith("ou_"):
            continue

        contract_group = cfg.get("contract_group", "")
        artifact = cfg.get("artifact_path", "")
        if not contract_group or not artifact:
            continue

        params = _load_ou_params(artifact)
        configs[contract_group] = params
        logger.info(
            "ConformalOUGate: registered %s -> z_entry=%.2f max_hl=%.0f theta_min=%.4f",
            contract_group,
            params["z_entry"],
            params["max_half_life"],
            params["theta_min"],
        )

    return configs


# ── Physics scoring ─────────────────────────────────────────────────────────


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + np.exp(-x))
    exp_x = np.exp(x)
    return exp_x / (1.0 + exp_x)


def _compute_z_depth_quality(z_score: float, z_entry: float) -> float:
    """Quality of Z-Score depth relative to entry threshold.

    Peaks at 2.0× z_entry (strong but not extreme).  Drops off for marginal
    signals (barely above entry) and extreme deviations (possible structural
    break rather than mean-reversion).

    Returns value in [0.1, 1.0].
    """
    if z_entry <= 0:
        return 0.5
    depth = abs(z_score) / z_entry
    if depth < 1.0:
        return float(np.clip(depth, 0.1, 1.0))  # linear below entry — weak
    # Quadratic decay from peak at 2.0
    quality = 1.0 - ((depth - 2.0) ** 2) / 16.0
    return float(np.clip(quality, 0.1, 1.0))


def _compute_half_life_quality(half_life: float, max_half_life: float) -> float:
    """Quality from mean-reversion speed.

    Short half-life (fast reversion) → high quality.
    half_life >= max_half_life → minimum quality.

    Mirrors the hl_discount in ParamsBrainAdapter._z_to_direction().
    """
    if max_half_life <= 0:
        return 0.5
    ratio = half_life / max_half_life
    quality = 1.0 - ratio
    return float(np.clip(quality, 0.1, 1.0))


def _compute_theta_quality(theta: float, theta_min: float) -> float:
    """Quality from mean-reversion strength evidence.

    theta >> theta_min → strong evidence for OU dynamics.
    theta near theta_min → weak — barely above noise threshold.
    theta <= 0 → no mean-reversion (trending / random walk).
    """
    if theta <= 0 or theta_min <= 0:
        return 0.1
    ratio = theta / theta_min
    # Logarithmic scale: ratio=1 → 0.3, ratio=3 → 0.6, ratio=10 → 0.9
    quality = 0.3 + 0.7 * float(np.clip(np.log1p(ratio) / np.log1p(5.0), 0.0, 1.0))
    return float(np.clip(quality, 0.1, 1.0))


def _compute_adx_quality(adx_value: float) -> float:
    """Trend contamination penalty.

    ADX < 20 → no penalty (range-bound / weak trend)
    ADX 25 → 0.8  (mild penalty)
    ADX 35 → 0.57 (moderate — OU struggles in trending)
    ADX 50 → 0.4  (heavy penalty)
    ADX > 60 → floor 0.2 (mean-reversion is dead in strong trends)
    """
    if adx_value <= 20:
        return 1.0
    excess = adx_value - 20.0
    quality = 1.0 / (1.0 + excess / 15.0)
    return float(np.clip(quality, 0.2, 1.0))


# ── Composite scoring ────────────────────────────────────────────────────────


def _compute_composite_score(components: dict[str, float], mode: str = "geometric_mean") -> float:
    """Combine physics quality components into a single score.

    **Geometric mean** (default): ``(∏ clip(c, 0, 1))^(1/n)``.
    Every component is strictly clamped to [0.0, 1.0] before multiplication
    to prevent negative values from causing complex roots or NaN propagation.
    If any component is ≤ 0, the entire score is 0 (hard veto).

    **Product** (legacy): ``∏ clip(c, 0.1, 1.0)`` with per-component floors.
    Retained for A/B comparison; the geometric mean is the recommended mode
    because it does not collapse with dimensionality.
    """
    if not components:
        return 0.0

    if mode == "geometric_mean":
        # Safety: clip every component to [0.0, 1.0] before computing.
        # This prevents negative numbers → complex roots, and NaN → infection.
        vals = [float(np.clip(v, 0.0, 1.0)) for v in components.values()]
        prod = 1.0
        for v in vals:
            prod *= v
        if prod <= 0.0:
            return 0.0
        return float(prod ** (1.0 / len(vals)))

    # Legacy product mode
    score = 1.0
    for v in components.values():
        score *= float(np.clip(v, 0.1, 1.0))
    return float(np.clip(score, 0.0, 1.0))


# ── Gate ────────────────────────────────────────────────────────────────────


class ConformalOUGate:
    """Physics-based OU signal quality gate with conformal threshold calibration.

    Replaces the 47-dim LightGBM :class:`MetaFilterGate` for OU strategies.
    Computes a multiplicative quality score from OU process diagnostics and
    applies an adaptive threshold from the shared :class:`ConformalCalibrator`.

    The gate is **strategy-aware**: it loads OU parameters per contract_group
    so M5 (statarb_dynamic, z_entry=3.9) and M15 (statarb_m15, z_entry=1.2)
    are evaluated against their own physics.

    Usage::

        gate = ConformalOUGate(calibrator=cal)
        gate.load_ou_configs()  # auto-discovers from configs/brains/

        result = gate.filter(
            strategy_name="statarb_dynamic",
            proposals=proposals,
            adx_value=adx,
        )
        if result["passed"]:
            dispatch_trade(...)
    """

    def __init__(
        self,
        calibrator: Any | None = None,  # ConformalCalibrator
        *,
        base_threshold: float = DEFAULT_BASE_THRESHOLD,
        min_threshold: float = DEFAULT_MIN_THRESHOLD,
        max_threshold: float = DEFAULT_MAX_THRESHOLD,
        scoring_mode: str = DEFAULT_SCORING_MODE,
    ) -> None:
        if scoring_mode not in ("geometric_mean", "product"):
            raise ValueError(
                f"scoring_mode must be 'geometric_mean' or 'product', got {scoring_mode!r}"
            )
        self._calibrator = calibrator
        self._base_threshold = base_threshold
        self._min_threshold = min_threshold
        self._max_threshold = max_threshold
        self._scoring_mode = scoring_mode

        # Strategy → OU params: populated by load_ou_configs()
        self._ou_configs: dict[str, dict[str, float]] = {}

        # Z-Score history per brain_id for Z-Velocity: brain_id → deque(z_score)
        self._z_history: dict[str, deque[float]] = {}

        self._loaded = False

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def load_ou_configs(self, brains_dir: str = DEFAULT_OU_CONFIGS_PATH) -> None:
        """Auto-discover OU brain configs and load their optimal parameters.

        Called once at startup.  After this, :meth:`filter` uses the correct
        OU physics parameters for each strategy line.
        """
        self._ou_configs = _build_ou_configs(brains_dir)
        self._loaded = len(self._ou_configs) > 0
        if self._loaded:
            logger.info(
                "ConformalOUGate: loaded %d OU strategy configs",
                len(self._ou_configs),
            )
        else:
            logger.warning("ConformalOUGate: no OU brain configs found — gate disabled")

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    # ------------------------------------------------------------------
    # Warmup-driven threshold schedule
    # ------------------------------------------------------------------

    def _resolve_warmup_threshold(self) -> dict[str, Any]:
        """Resolve effective threshold and volume policy from calibrator state.

        Explore-then-Commit schedule:

        * **COLD**  (samples < 50):  fixed 0.20 threshold, force_min_volume=True.
          The gate is intentionally lenient — we need trades to collect samples.
          Volume is capped at min lot (0.01) to bound exploration risk.
        * **WARM**  (50 ≤ n < 100):  Q10 from calibrator, floored at 0.20.
          The calibrator is warm but the distribution may still be unstable.
        * **HOT**   (n ≥ 100):       full Q10 from calibrator, clamped [0.25, 0.65].
          Distribution is stable — adaptive threshold fully in control.

        When no calibrator is attached, returns the fixed base threshold with
        no volume override (backward-compatible behaviour).
        """
        if self._calibrator is None:
            return {
                "threshold": self._base_threshold,
                "phase": "no_calibrator",
                "force_min_volume": False,
            }

        n = self._calibrator.sample_count

        if n < COLD_PHASE_MAX_SAMPLES:
            return {
                "threshold": COLD_PHASE_THRESHOLD,
                "phase": "cold",
                "force_min_volume": True,
                "samples": n,
                "warmup_target": COLD_PHASE_MAX_SAMPLES,
            }

        q10 = float(self._calibrator.compute_threshold())

        if n < WARM_PHASE_MAX_SAMPLES:
            return {
                "threshold": max(COLD_PHASE_THRESHOLD, q10),
                "phase": "warm",
                "force_min_volume": False,
                "samples": n,
            }

        # HOT: full Q10, bounded to gate's own safety range
        return {
            "threshold": float(np.clip(q10, self._min_threshold, self._max_threshold)),
            "phase": "hot",
            "force_min_volume": False,
            "samples": n,
        }

    # ------------------------------------------------------------------
    # Core filtering
    # ------------------------------------------------------------------

    def filter(
        self,
        *,
        strategy_name: str,
        proposals: list[Any],
        adx_value: float = 20.0,
    ) -> dict[str, Any]:
        """Evaluate OU signal quality from physics features + conformal threshold.

        Args:
            strategy_name: ``"statarb_dynamic"`` or ``"statarb_m15"``.
            proposals: List of ``BrainSignal`` objects from this cycle.
            adx_value: Current ADX(14) value for trend contamination penalty.

        Returns:
            Dict with ``passed``, ``score``, ``threshold``, ``threshold_source``,
            ``features`` (physics diagnostics dict), and ``reason``.
        """
        if not self._loaded:
            return {
                "passed": True,
                "score": 0.0,
                "threshold": self._base_threshold,
                "threshold_source": "gate_not_loaded",
                "features": {},
                "reason": "conformal_ou_gate_not_loaded",
            }

        ou_params = self._ou_configs.get(strategy_name)
        if ou_params is None:
            return {
                "passed": True,
                "score": 0.0,
                "threshold": self._base_threshold,
                "threshold_source": "no_ou_config",
                "features": {},
                "reason": f"no_ou_config_for_{strategy_name}",
            }

        # ── 1. Extract OU diagnostics from proposals ──
        ou_diag = self._extract_ou_diagnostics(proposals, strategy_name)
        if ou_diag is None:
            return {
                "passed": True,
                "score": 0.0,
                "threshold": self._base_threshold,
                "threshold_source": "no_ou_signal",
                "features": {},
                "reason": "no_ou_brain_signal_in_proposals",
            }

        # ── 2. Compute physics features ──
        z_entry = float(ou_params["z_entry"])
        max_hl = float(ou_params["max_half_life"])
        theta_min = float(ou_params["theta_min"])

        z_depth_q = _compute_z_depth_quality(ou_diag["z_score"], z_entry)
        hl_q = _compute_half_life_quality(ou_diag["half_life"], max_hl)
        theta_q = _compute_theta_quality(ou_diag["theta"], theta_min)
        adx_q = _compute_adx_quality(adx_value)
        vel_q = self._compute_velocity_quality(
            ou_diag["brain_id"], ou_diag["z_score"], ou_diag["direction"], z_entry
        )

        # ── 3. z_depth hard veto (FIX-20260526-031) ──
        # Mean-reversion physics demands price DEVIATION.  When |z| is tiny
        # (price hugs the mean), theta and velocity are irrelevant — there is
        # no profit space to revert through.  z_depth_q < 0.25 means the
        # actual |z| is less than 0.25× z_entry: pure noise territory.
        # The veto is applied BEFORE composite scoring so that no other
        # dimension can "rescue" a signal whose physical basis is absent.
        if z_depth_q < 0.25:
            score = 0.0
        else:
            # ── 4. Composite score via geometric mean (default) or product ──
            _components = {
                "z_depth_q": z_depth_q,
                "hl_q": hl_q,
                "theta_q": theta_q,
                "adx_q": adx_q,
                "vel_q": vel_q,
            }
            score = _compute_composite_score(_components, mode=self._scoring_mode)

        features = {
            "z_score": round(ou_diag["z_score"], 4),
            "z_entry": z_entry,
            "z_depth": round(abs(ou_diag["z_score"]) / max(z_entry, 0.01), 2),
            "z_depth_q": round(z_depth_q, 4),
            "half_life": round(ou_diag["half_life"], 1),
            "max_half_life": max_hl,
            "hl_q": round(hl_q, 4),
            "theta": round(ou_diag["theta"], 6),
            "theta_min": theta_min,
            "theta_q": round(theta_q, 4),
            "adx": round(adx_value, 1),
            "adx_q": round(adx_q, 4),
            "vel_q": round(vel_q, 4),
            "ou_confidence": round(ou_diag.get("ou_confidence", 0.5), 4),
        }

        # ── 4. Warmup-driven threshold schedule ──
        _warmup = self._resolve_warmup_threshold()
        effective_threshold = float(_warmup["threshold"])
        warmup_phase = str(_warmup["phase"])
        force_min_volume = bool(_warmup.get("force_min_volume", False))

        passed = score >= effective_threshold

        return {
            "passed": passed,
            "score": round(score, 4),
            "threshold": round(effective_threshold, 4),
            "threshold_source": f"conformal_{warmup_phase}",
            "warmup_phase": warmup_phase,
            "force_min_volume": force_min_volume,
            "features": features,
            "reason": "ok"
            if passed
            else f"score_{score:.4f}_lt_threshold_{effective_threshold:.4f}",
        }

    # ------------------------------------------------------------------
    # OU diagnostics extraction
    # ------------------------------------------------------------------

    def _extract_ou_diagnostics(
        self, proposals: list[Any], strategy_name: str
    ) -> dict[str, Any] | None:
        """Find the OU brain's signal in proposals and extract physics diagnostics.

        Returns dict with brain_id, z_score, theta, half_life, direction,
        or None if no OU brain signal is found for this strategy.
        """
        for p in proposals:
            brain_id = str(getattr(p, "brain_id", ""))
            brain_type = str(getattr(p, "brain_type", "") or "")

            # Identify OU brains by type prefix
            if brain_type.startswith("ou_"):
                diag = getattr(p, "diagnostics", None) or {}
                if not isinstance(diag, dict):
                    diag = {}
            else:
                # Fallback: check diagnostics for OU indicators (theta + half_life)
                diag = getattr(p, "diagnostics", None) or {}
                if not isinstance(diag, dict):
                    continue
                if "theta" not in diag or "half_life" not in diag:
                    continue

            # ── Verify contract_group matches strategy via BrainRegistry ──
            if strategy_name and brain_id:
                try:
                    from core.brains.brain_registry import BrainRegistry

                    entry = BrainRegistry.instance().get(brain_id)
                    if entry is not None and entry.contract_group != strategy_name:
                        continue  # brain is for a different strategy line
                except Exception:  # BLE001:FOG
                    with fail_open_guard("conformal_ou_gate:_extract_ou_diagnostics"):
                        pass  # registry resolve failure is non-blocking
            z_score = float(getattr(p, "raw_score", 0.0) or 0.0)
            theta = float(diag.get("theta", 0.0))
            half_life = float(diag.get("half_life", float("inf")))
            if half_life == float("inf") or half_life > 10000:
                half_life = 100.0  # cap for scoring

            direction = str(getattr(p, "direction", "neutral"))
            ou_confidence = float(getattr(p, "confidence", 0.5) or 0.5)

            return {
                "brain_id": brain_id,
                "z_score": z_score,
                "theta": theta,
                "half_life": half_life,
                "direction": direction,
                "ou_confidence": ou_confidence,
            }

        return None

    # ------------------------------------------------------------------
    # Z-Velocity tracking
    # ------------------------------------------------------------------

    def _compute_velocity_quality(
        self, brain_id: str, z_score: float, direction: str, z_entry: float
    ) -> float:
        """Compute Z-Velocity quality from rolling z_score history.

        Z-Velocity = (z_t - z_{t-1}) / z_entry — how fast is z_score moving?
        Quality depends on whether the movement is aligned with signal direction:
          - LONG  signal (z < 0): dz < 0 = strengthening, dz > 0 = fading
          - SHORT signal (z > 0): dz > 0 = strengthening, dz < 0 = fading
          - NEUTRAL: 1.0 (no penalty/reward)

        Maintains a per-brain deque of recent z_score values.
        """
        if brain_id not in self._z_history:
            self._z_history[brain_id] = deque(maxlen=10)

        history = self._z_history[brain_id]

        # First observation — no velocity yet
        if len(history) == 0:
            history.append(z_score)
            return 1.0

        prev_z = history[-1]
        history.append(z_score)

        if z_entry <= 0:
            return 1.0

        dz_norm = (z_score - prev_z) / z_entry

        if direction == "long":
            # For long: z is negative, dz < 0 means z is getting MORE negative → strengthening
            alignment = -dz_norm  # dz=-0.5 → alignment=+0.5 (good)
        elif direction == "short":
            # For short: z is positive, dz > 0 means z is getting MORE positive → strengthening
            alignment = dz_norm  # dz=+0.5 → alignment=+0.5 (good)
        else:
            return 1.0

        # Map alignment to quality factor [0.3, 1.5]
        # alignment > 0: signal strengthening → bonus
        # alignment < 0: signal fading → penalty
        quality = 1.0 + _sigmoid(alignment * 3.0) - 0.5
        return float(np.clip(quality, 0.3, 1.5))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "loaded": self._loaded,
            "base_threshold": self._base_threshold,
            "min_threshold": self._min_threshold,
            "max_threshold": self._max_threshold,
            "strategies": list(self._ou_configs.keys()),
            "ou_configs": {
                k: {
                    "z_entry": v["z_entry"],
                    "z_exit": v["z_exit"],
                    "max_half_life": v["max_half_life"],
                    "theta_min": v["theta_min"],
                }
                for k, v in self._ou_configs.items()
            },
            "calibrator": self._calibrator.describe() if self._calibrator is not None else None,
        }


# ── Strategy line integration ────────────────────────────────────────────────
# FIX-20260620-016: Extracted from strategy_line.evaluate() lines 868-997.
# The function encapsulates the OU physics gate + MetaFilter fallback logic
# and returns a blocked StrategyDecision (or None) + the raw ou_result dict
# for downstream COLD phase volume override via _last_ou_result.


def apply_conformal_ou_gate(
    *,
    strategy_name: str,
    conformal_ou_gate: Any,
    meta_filter_gate: Any,
    proposals: list[Any],
    trend_strength: float,
    feature_vector: Any,
    micro_feature_dict: dict[str, float] | None,
    direction: str,
    confidence: float,
    brain_ids: list[str],
    support_count: int,
    total_count: int,
    regime_gate_mode: str,
    make_decision: Callable[..., Any],
) -> tuple[Any | None, dict[str, Any] | None]:
    """Apply OU signal quality gate for statarb strategies (M5/M15).

    Replaces the 47-dim LightGBM MetaFilterGate with OU-specific physics
    features: Z-Depth, Z-Velocity, Half-life quality, Theta strength,
    and ADX trend penalty.  Falls back to MetaFilterGate if the OU gate
    is not available.

    COLD phase exploration bypass: when ``force_min_volume=True``
    (ConformalOU calibrator < 50 samples), gate rejection is overridden
    so exploration trades can collect PIT samples.

    Args:
        strategy_name: ``"statarb_dynamic"`` or ``"statarb_m15"``.
        conformal_ou_gate: :class:`ConformalOUGate` instance (or None).
        meta_filter_gate: :class:`MetaFilterGate` instance (or None).
        proposals: List of ``BrainSignal`` objects.
        trend_strength: [0, 1] H1 trend strength for ADX approximation.
        feature_vector: Feature vector for MetaFilter fallback.
        micro_feature_dict: Optional micro features for MetaFilter.
        direction: Consensus direction (``"long"`` / ``"short"`` / ``"neutral"``).
        confidence: Consensus confidence.
        brain_ids: Brain IDs from consensus.
        support_count: Supporting voter count.
        total_count: Total voter count.
        regime_gate_mode: Current regime mode (``"full"`` / ``"reduced"`` / ``"shadow"``).
        make_decision: Callable that creates a ``StrategyDecision`` from keyword
                       arguments.  Typically ``StrategyLine._make_decision()``.

    Returns:
        ``(blocked_decision, ou_result)`` tuple:
        - ``blocked_decision`` is a ``StrategyDecision`` to return immediately,
          or ``None`` if the trade should proceed.
        - ``ou_result`` is the raw gate output dict (for ``_last_ou_result``
          downstream COLD phase volume override), or ``None``.
    """
    # ── Track 3d: Conformal OU Gate (OU physics-based signal quality) ──
    if conformal_ou_gate is not None and conformal_ou_gate.is_loaded:
        try:
            adx_approx = 15.0 + trend_strength * 40.0
            ou_result = conformal_ou_gate.filter(
                strategy_name=strategy_name,
                proposals=proposals,
                adx_value=adx_approx,
            )
            if not ou_result["passed"] and not ou_result.get("force_min_volume"):
                # FIX-20260527-006: COLD phase exploration bypass.
                # When force_min_volume=True (calibrator < 50 samples),
                # the gate rejection is overridden — fall through to
                # downstream COLD exploration logic (p_win=0.50, 0.01 lot).
                _gd: dict[str, Any] = {}
                _feat = ou_result.get("features", {})
                if _feat:
                    _gd = {
                        "gate": "conformal_ou",
                        "composite_score": ou_result.get("score"),
                        "threshold": ou_result.get("threshold"),
                        "z_score": _feat.get("z_score"),
                        "z_entry": _feat.get("z_entry"),
                        "z_depth_q": _feat.get("z_depth_q"),
                        "half_life": _feat.get("half_life"),
                        "hl_q": _feat.get("hl_q"),
                        "theta": _feat.get("theta"),
                        "theta_q": _feat.get("theta_q"),
                        "adx": _feat.get("adx"),
                        "adx_q": _feat.get("adx_q"),
                        "vel_q": _feat.get("vel_q"),
                    }
                blocked = make_decision(
                    should_trade=False,
                    direction=direction,
                    confidence=confidence,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    brain_ids=brain_ids,
                    supporting_count=support_count,
                    total_count=total_count,
                    regime_mode=regime_gate_mode,
                    reason=ou_result["reason"],
                    gate_diag=_gd,
                )
                return (blocked, ou_result)
            # Passed or COLD exploration bypass → proceed
            return (None, ou_result)
        except Exception:  # BLE001:FOG
            with fail_open_guard("strategy_line:evaluate"):
                _sl_logger = logging.getLogger(__name__)
                _sl_logger.warning(
                    "OU gate evaluation failed for strategy=%s — BLOCKING trade",
                    strategy_name,
                    exc_info=True,
                )
                blocked = make_decision(
                    should_trade=False,
                    direction=direction,
                    confidence=confidence,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    brain_ids=brain_ids,
                    supporting_count=support_count,
                    total_count=total_count,
                    regime_mode=regime_gate_mode,
                    reason="ou_gate_exception_blocked",
                )
            return (blocked, None)

    # ── MetaFilter fallback ──
    if (
        meta_filter_gate is not None
        and meta_filter_gate.is_loaded
        and feature_vector is not None
    ):
        try:
            mf_result = meta_filter_gate.filter(
                feature_vector=feature_vector,
                micro_features=micro_feature_dict or {},
            )
            if not mf_result["passed"]:
                blocked = make_decision(
                    should_trade=False,
                    direction=direction,
                    confidence=confidence,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    brain_ids=brain_ids,
                    supporting_count=support_count,
                    total_count=total_count,
                    regime_mode=regime_gate_mode,
                    reason=mf_result["reason"],
                )
                return (blocked, None)
        except Exception:  # BLE001:FOG
            with fail_open_guard("strategy_line:evaluate"):
                _sl_logger = logging.getLogger(__name__)
                _sl_logger.warning(
                    "Meta-filter gate evaluation failed for strategy=%s — BLOCKING trade",
                    strategy_name,
                    exc_info=True,
                )
                blocked = make_decision(
                    should_trade=False,
                    direction=direction,
                    confidence=confidence,
                    volume=0.0,
                    sl=0.0,
                    tp=0.0,
                    hard_sl=0.0,
                    brain_ids=brain_ids,
                    supporting_count=support_count,
                    total_count=total_count,
                    regime_mode=regime_gate_mode,
                    reason="meta_filter_gate_exception_blocked",
                )
            return (blocked, None)

    # Neither gate available or loaded → pass through
    return (None, None)
