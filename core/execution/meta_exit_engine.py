"""Meta-model exit engine — dynamic exit urgency scoring.

Replaces the old rule-based brain-flip exit with a multi-factor scoring
approach inspired by institutional meta-labeling frameworks.

Principles:
  1. PnL trajectory: current R, peak R, drawdown from peak
  2. Time decay: cycles held vs expected horizon (from model training contract)
  3. Regime alignment: is the trade still aligned with market regime?
  4. Brain consensus drift: has brain agreement weakened since entry?
  5. Volatility expansion: is ATR expanding against the position?

The engine produces an exit_urgency ∈ [0, 1].  The caller compares
this against a configurable threshold (default 0.65) to decide exit.

When a trained LightGBM model is available, it replaces the heuristic
scoring with P(win | features) inference.  The same feature vector
feeds both paths.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# ── Feature snapshot for one evaluation cycle ──


@dataclass
class ExitFeatureSnapshot:
    """Features captured at exit evaluation time.

    These mirror the inputs a meta-model would consume:
      entry features (40-dim) + pnl state + regime + time + brain consensus.
    """

    # PnL state
    current_r: float = 0.0  # current R-multiple (pnl / entry_risk)
    prev_r: float = 0.0  # previous cycle's R (for trajectory scoring)
    peak_r: float = 0.0  # highest R achieved
    drawdown_r: float = 0.0  # drawdown from peak R (always >= 0)
    pnl_pct: float = 0.0  # raw PnL as fraction of balance

    # Time state
    cycles_held: int = 0  # evaluation cycles since open
    expected_horizon: int = 12  # from model training contract
    time_ratio: float = 0.0  # cycles_held / expected_horizon

    # Regime state
    regime: str = "normal"  # low / normal / high
    regime_confidence: float = 0.0
    trend_aligned: bool = True  # is position direction aligned with trend?
    atr_current: float = 0.0
    atr_entry: float = 0.0
    atr_expansion: float = 0.0  # (atr_current - atr_entry) / atr_entry

    # Brain consensus state
    entry_consensus_score: float = 0.0
    entry_supporting_count: int = 0
    current_supporting_count: int = 0
    consensus_drift: float = 0.0  # entry_score - current_score (positive = weakening)

    # Context
    side: str = "long"
    symbol: str = ""


# ── Exit evaluation result ──


@dataclass
class ExitEvaluation:
    """Result of one exit evaluation cycle."""

    exit_urgency: float  # [0, 1], higher = more urgent
    should_exit: bool  # urgency >= threshold
    exit_reason: str = ""  # primary reason for exit recommendation
    factor_breakdown: dict[str, float] = field(default_factory=dict)
    p_win: float | None = None  # ML model P(win) if available, else None


# ── Heuristic scoring engine ──


class MetaExitEngine:
    """Multi-factor exit urgency scorer.

    When a trained model_path is provided and the model file exists,
    uses LightGBM inference.  Otherwise falls back to heuristic scoring.
    """

    def __init__(
        self,
        *,
        model_path: str | None = None,
        urgency_threshold: float = 0.65,
        # Factor weights (for heuristic mode)
        w_pnl: float = 0.30,
        w_time: float = 0.20,
        w_regime: float = 0.15,
        w_consensus: float = 0.25,
        w_volatility: float = 0.10,
    ) -> None:
        self.model_path = model_path
        self.urgency_threshold = urgency_threshold
        self.w_pnl = w_pnl
        self.w_time = w_time
        self.w_regime = w_regime
        self.w_consensus = w_consensus
        self.w_volatility = w_volatility
        self._model = None
        self._feature_names: list[str] = []

    # ── Public API ──

    def evaluate(self, snapshot: ExitFeatureSnapshot) -> ExitEvaluation:
        """Score exit urgency for one evaluation cycle."""
        if self._model is not None:
            return self._ml_evaluate(snapshot)

        return self._heuristic_evaluate(snapshot)

    _FORBIDDEN_FEATURES = {"is_sl_hit", "is_tp_hit"}
    _MIN_WINS = 15  # minimum winning trades for a useful model
    _MIN_WIN_RATE = 0.20  # minimum win rate for model to add value

    def load_model(self) -> bool:
        """Try to load a trained LightGBM model.  Returns True if loaded.

        Refuses models with:
          - Post-trade features (data leakage)
          - Too few winning samples (< MIN_WINS)
          - Win rate too low (< MIN_WIN_RATE, model would always predict loss)
        """
        if not self.model_path or not os.path.exists(self.model_path):
            return False
        try:
            import lightgbm as lgb

            # Load metadata for quality checks
            meta_path = self.model_path.replace(".txt", ".meta.json")
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                feature_names = meta.get("feature_names", [])
                n_wins = meta.get("n_wins", 0)
                win_rate = meta.get("win_rate", 0)

                # Check for data leakage features
                forbidden = self._FORBIDDEN_FEATURES & set(feature_names)
                if forbidden:
                    print(
                        json.dumps(
                            {
                                "event": "meta_exit_model_rejected",
                                "reason": f"data_leakage_features_{forbidden}",
                                "model_path": self.model_path,
                                "action": "delete_and_retrain_with_clean_features",
                            }
                        ),
                        flush=True,
                    )
                    return False

                # Check model quality — weak models provide no value
                if n_wins < self._MIN_WINS or win_rate < self._MIN_WIN_RATE:
                    print(
                        json.dumps(
                            {
                                "event": "meta_exit_model_rejected",
                                "reason": "insufficient_quality",
                                "n_wins": n_wins,
                                "win_rate": round(win_rate, 4),
                                "min_wins": self._MIN_WINS,
                                "min_win_rate": self._MIN_WIN_RATE,
                                "action": "retrain_after_collecting_more_trades",
                            }
                        ),
                        flush=True,
                    )
                    return False

                self._feature_names = feature_names

            self._model = lgb.Booster(model_file=self.model_path)
            return True
        except Exception:
            return False

    # ── Heuristic scoring ──

    def _heuristic_evaluate(self, snap: ExitFeatureSnapshot) -> ExitEvaluation:
        """Compute exit urgency from configurable factor scores."""
        factors: dict[str, float] = {}

        # 1. PnL factor — losing trades get higher urgency
        pnl_score = self._score_pnl(snap)
        factors["pnl"] = pnl_score

        # 2. Time factor — urgency rises as trade ages past expected horizon
        time_score = self._score_time(snap)
        factors["time"] = time_score

        # 3. Regime factor — counter-trend positions in strong regimes get higher urgency
        regime_score = self._score_regime(snap)
        factors["regime"] = regime_score

        # 4. Consensus drift factor — weakening brain agreement = higher urgency
        consensus_score = self._score_consensus(snap)
        factors["consensus"] = consensus_score

        # 5. Volatility factor — expanding ATR against position = higher urgency
        vol_score = self._score_volatility(snap)
        factors["volatility"] = vol_score

        # Weighted sum
        urgency = (
            self.w_pnl * pnl_score
            + self.w_time * time_score
            + self.w_regime * regime_score
            + self.w_consensus * consensus_score
            + self.w_volatility * vol_score
        )

        # Clamp and identify primary reason
        urgency = max(0.0, min(1.0, urgency))
        primary = max(factors, key=factors.get)

        reason_map = {
            "pnl": f"pnl_urgency_r_{snap.current_r:.2f}",
            "time": f"time_decay_{snap.time_ratio:.1%}_horizon",
            "regime": f"regime_misalignment_{snap.regime}",
            "consensus": f"consensus_drift_{snap.consensus_drift:.2f}",
            "volatility": f"vol_expansion_{snap.atr_expansion:.1%}",
        }

        return ExitEvaluation(
            exit_urgency=round(urgency, 4),
            should_exit=urgency >= self.urgency_threshold,
            exit_reason=reason_map.get(primary, primary),
            factor_breakdown=factors,
        )

    # ── Factor scorers (each returns [0, 1]) ──

    @staticmethod
    def _score_pnl(snap: ExitFeatureSnapshot) -> float:
        """Score PnL distress with R-trajectory awareness.

        Base score from current R level, adjusted by trajectory:
          - Stuck below 0.3R after 30+ cycles: +0.15 (trade going nowhere)
          - R improving (current > prev): -0.05 (trade is working)
          - R deteriorating (current < prev): +0.05 (trade is failing)

        0.0 = winning comfortably (R >= 2.0)
        0.5 = at breakeven
        1.0 = deep loss (R <= -1.5)
        """
        r = snap.current_r

        if r >= 1.5:
            base = 0.0  # winning, no urgency
        elif r >= 0.5:
            base = 0.15  # modest win, low urgency
        elif r >= 0.0:
            base = 0.30  # breakeven area
        elif r >= -0.5:
            base = 0.55  # small loss
        elif r >= -1.0:
            base = 0.75  # material loss
        else:
            base = 1.0  # deep loss

        # R-trajectory adjustments
        if snap.cycles_held >= 30 and r < 0.3:
            base += 0.15  # stuck — trade hasn't gone anywhere in 30+ cycles

        if snap.prev_r != 0.0:
            r_delta = r - snap.prev_r
            if r_delta > 0.05:
                base -= 0.05  # R improving — reduce urgency
            elif r_delta < -0.05:
                base += 0.05  # R deteriorating — increase urgency

        return max(0.0, min(1.0, base))

    @staticmethod
    def _score_time(snap: ExitFeatureSnapshot) -> float:
        """Score time decay with exponential ramp in the 80-100% zone.

        Phased urgency:
          - 0-50% of horizon: 0.0 (let trade develop, no time pressure)
          - 50-80%: 0.0 → 0.30 (linear, start watching)
          - 80-100%: 0.30 → 0.60 (exponential, exit pressure builds)
          - >100%: 0.80 (overtime, capped — PnL and other factors
                   should drive the final exit decision)
        """
        ratio = snap.time_ratio
        if ratio < 0.5:
            return 0.0
        if ratio < 0.8:
            # Linear: 0.0 at 50% → 0.30 at 80%
            return 0.30 * (ratio - 0.5) / 0.3
        if ratio <= 1.0:
            # Exponential: 0.30 at 80% → 0.60 at 100%
            t = (ratio - 0.8) / 0.2  # normalized [0, 1] within zone
            return 0.30 + 0.30 * (t**2)  # quadratic ramp
        return 0.80  # overtime, flat — don't force-close on time alone

    @staticmethod
    def _score_regime(snap: ExitFeatureSnapshot) -> float:
        """Score regime misalignment.

        0.0 = aligned with strong trend
        0.5 = neutral regime or low confidence
        1.0 = strongly counter-trend
        """
        if not snap.regime or snap.regime_confidence < 0.3:
            return 0.2

        if snap.trend_aligned:
            # In trade's favor but check regime confidence
            if snap.regime == "high":
                return 0.05  # high vol but aligned — still ok
            return 0.0  # aligned in normal/low vol

        # Counter-trend
        base = 0.5
        if snap.regime == "high":
            base = 0.7  # high vol counter-trend = dangerous
        return base + 0.3 * snap.regime_confidence

    @staticmethod
    def _score_consensus(snap: ExitFeatureSnapshot) -> float:
        """Score brain consensus deterioration.

        0.0 = consensus strengthened since entry
        0.5 = consensus unchanged
        1.0 = complete consensus collapse (all supporting brains flipped)
        """
        drift = snap.consensus_drift
        if drift <= 0:
            return 0.0  # consensus improved
        if drift < 0.1:
            return 0.15
        if drift < 0.2:
            return 0.35
        if drift < 0.4:
            return 0.60
        return 0.85

    @staticmethod
    def _score_volatility(snap: ExitFeatureSnapshot) -> float:
        """Score volatility expansion risk.

        0.0 = ATR contracting (favorable)
        0.5 = ATR unchanged
        1.0 = ATR expanding rapidly (>50% above entry)
        """
        expansion = snap.atr_expansion
        if expansion <= -0.1:
            return 0.0  # ATR contracting — good
        if expansion <= 0.0:
            return 0.1
        if expansion <= 0.2:
            return 0.3
        if expansion <= 0.5:
            return 0.6
        return 0.9

    # ── ML inference (future) ──

    def _ml_evaluate(self, snap: ExitFeatureSnapshot) -> ExitEvaluation:
        """Run LightGBM inference to get P(win).

        Falls back to heuristic if model inference fails.
        """
        try:
            features = self._build_feature_vector(snap)
            p_win = float(self._model.predict([features])[0])
            urgency = 1.0 - p_win  # P(loss) ≈ urgency

            return ExitEvaluation(
                exit_urgency=round(urgency, 4),
                should_exit=urgency >= self.urgency_threshold,
                exit_reason=f"ml_p_win_{p_win:.3f}",
                factor_breakdown={"p_win": round(p_win, 4)},
                p_win=round(p_win, 4),
            )
        except Exception:
            return self._heuristic_evaluate(snap)

    def _build_feature_vector(self, snap: ExitFeatureSnapshot) -> list[float]:
        """Build feature vector matching training schema.

        Produces features in the order specified by ``self._feature_names``
        (loaded from the model metadata).  Falls back to the 16-dim runtime
        vector if no names are available.
        """
        if self._feature_names:
            fmap = self._runtime_feature_map(snap)
            return [fmap.get(name, 0.0) for name in self._feature_names]

        # Fallback: 17-dim runtime vector (no trained model loaded)
        return [
            snap.current_r,
            snap.prev_r,
            snap.peak_r,
            snap.drawdown_r,
            snap.pnl_pct,
            float(snap.cycles_held),
            float(snap.expected_horizon),
            snap.time_ratio,
            snap.regime_confidence,
            float(snap.trend_aligned),
            snap.atr_current,
            snap.atr_entry,
            snap.atr_expansion,
            snap.entry_consensus_score,
            float(snap.entry_supporting_count),
            float(snap.current_supporting_count),
            snap.consensus_drift,
        ]

    @staticmethod
    def _runtime_feature_map(snap: ExitFeatureSnapshot) -> dict[str, float]:
        """Map every known feature name to its runtime value.

        This is the single source of truth for feature name → value
        mapping.  Both the engine and the training script should
        reference this canonical list.
        """
        return {
            # PnL state
            "current_r": snap.current_r,
            "prev_r": snap.prev_r,
            "peak_r": snap.peak_r,
            "drawdown_r": snap.drawdown_r,
            "pnl_pct": snap.pnl_pct,
            # Time state
            "cycles_held": float(snap.cycles_held),
            "expected_horizon": float(snap.expected_horizon),
            "time_ratio": snap.time_ratio,
            # Regime state
            "regime_confidence": snap.regime_confidence,
            "trend_aligned": float(snap.trend_aligned),
            "atr_current": snap.atr_current,
            "atr_entry": snap.atr_entry,
            "atr_expansion": snap.atr_expansion,
            # Brain consensus state
            "entry_consensus_score": snap.entry_consensus_score,
            "entry_supporting_count": float(snap.entry_supporting_count),
            "current_supporting_count": float(snap.current_supporting_count),
            "consensus_drift": snap.consensus_drift,
            # Entry characteristics (journal-level, constant during trade)
            "side_short": 1.0 if snap.side == "short" else 0.0,
            "sl_distance": snap.atr_entry * 2.0,
            "tp_distance": snap.atr_entry * 3.5,
            "rr_ratio": 3.5 / 2.0,
            "volume": 0.01,
            "accepted": 1.0,
            "entry_hour": 12.0,
            "entry_dow": 3.0,
            # NOTE: is_sl_hit/is_tp_hit removed — they are post-trade outcomes
            # (data leakage).  The old model trained with them is tainted;
            # retrain with the clean feature set before enabling ML mode.
        }


# ── Factory ──


def create_exit_engine(
    model_path: str | None = None,
    urgency_threshold: float = 0.65,
    **kwargs,
) -> MetaExitEngine | None:
    """Create a MetaExitEngine, loading model if available.

    When the trained model cannot be loaded (insufficient quality, data leakage,
    or missing), returns None so that Layer 2.5 is gracefully disabled and the
    existing trailing stop (Layer 1) + flip exit (Layer 2) + time exit (Layer 3)
    handle exit management without a worse-than-random heuristic.
    """
    engine = MetaExitEngine(
        model_path=model_path,
        urgency_threshold=urgency_threshold,
        **kwargs,
    )
    if model_path:
        loaded = engine.load_model()
        if not loaded:
            print(
                json.dumps(
                    {
                        "event": "meta_exit_model_unavailable",
                        "time": "",
                        "model_path": model_path,
                        "fallback": "atr_trailing_stop_layer1",
                        "action": "disabled_layer_2_5_using_layer_1_trail",
                    },
                ),
                flush=True,
            )
            return None
    return engine
