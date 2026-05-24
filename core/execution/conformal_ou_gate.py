"""Conformal OU Gate — physics-based signal quality gate for OU mean-reversion strategies.

Replaces the generic 47-dim LightGBM MetaFilterGate for ``statarb_dynamic`` (M5)
and ``statarb_m15`` (M15) strategy lines.  Instead of a learned model, this gate
computes a physics-grounded quality score from OU process diagnostics and applies
a conformal-calibrated adaptive threshold (Q10, FIFO window).

**Design Principle**: OU signals must be validated against their own physics —
Z-Depth (how deep in signal territory), Z-Velocity (signal momentum), Half-life
(mean-reversion speed), Theta strength (reversion evidence), and ADX alignment
(trend contamination).  Any factor can independently degrade the score.

**Score Formula (multiplicative)**:
    score = z_depth_q * hl_q * theta_q * adx_q * vel_q
Each component is clamped [0.1, 1.0] (vel: [0.3, 1.5]) so no single factor can
zero the score, but weak factors cumulatively suppress it.

**Integration**: The existing :class:`ConformalCalibrator` provides the adaptive
threshold — the gate calls ``calibrator.compute_threshold()`` on each evaluation
and ``calibrator.update(score, label)`` when a trade closes.  This is the same
Track 3d calibration infrastructure already wired for MetaFilterGate.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ── Constants ──
DEFAULT_BASE_THRESHOLD = 0.35  # lenient base — physics score is conservative
DEFAULT_MIN_THRESHOLD = 0.25
DEFAULT_MAX_THRESHOLD = 0.65
DEFAULT_OU_CONFIGS_PATH = "configs/brains/"


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
    except Exception:
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
    ) -> None:
        self._calibrator = calibrator
        self._base_threshold = base_threshold
        self._min_threshold = min_threshold
        self._max_threshold = max_threshold

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

        # ── 3. Multiplicative composite score ──
        score = z_depth_q * hl_q * theta_q * adx_q * vel_q
        score = float(np.clip(score, 0.0, 1.0))

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
        }

        # ── 4. Conformal threshold ──
        if self._calibrator is not None:
            effective_threshold = self._calibrator.compute_threshold()
            # Clamp to gate's own bounds
            effective_threshold = float(
                np.clip(effective_threshold, self._min_threshold, self._max_threshold)
            )
            threshold_source = "conformal_q10"
        else:
            effective_threshold = self._base_threshold
            threshold_source = "fixed"

        passed = score >= effective_threshold

        return {
            "passed": passed,
            "score": round(score, 4),
            "threshold": round(effective_threshold, 4),
            "threshold_source": threshold_source,
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
        or None if no OU brain signal is found.
        """
        for p in proposals:
            brain_id = str(getattr(p, "brain_id", ""))
            brain_type = str(getattr(p, "brain_type", "") or "")

            # Identify OU brains by type prefix
            if not brain_type.startswith("ou_"):
                # Fallback: check diagnostics for OU indicators
                diag = getattr(p, "diagnostics", None) or {}
                if isinstance(diag, dict) and "theta" not in diag:
                    continue
                if not isinstance(diag, dict):
                    continue
            else:
                diag = getattr(p, "diagnostics", None) or {}
                if not isinstance(diag, dict):
                    diag = {}

            z_score = float(getattr(p, "raw_score", 0.0) or 0.0)
            theta = float(diag.get("theta", 0.0))
            half_life = float(diag.get("half_life", float("inf")))
            if half_life == float("inf") or half_life > 10000:
                half_life = 100.0  # cap for scoring

            direction = str(getattr(p, "direction", "neutral"))

            return {
                "brain_id": brain_id,
                "z_score": z_score,
                "theta": theta,
                "half_life": half_life,
                "direction": direction,
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
