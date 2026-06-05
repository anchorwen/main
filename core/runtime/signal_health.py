"""Functional signal-health checks for the live trading cycle.

Detects degradation in data quality and model behaviour that could silently
erode strategy performance.  All checks are non-blocking — they produce
structured log events so the operator can investigate.

Checks:
  1. Data freshness   — is the feature snapshot stale vs its expected cadence?
  2. ATR anomaly      — is current ATR outside the historical interquartile range?
  3. Prediction drift — have brain prediction distributions shifted from baseline?
  4. Spread expansion — is the bid-ask spread abnormally wide relative to typical?
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any

# ── Feature gate ────────────────────────────────────────────────────────


class GateResult:
    """Result of a feature-gate check."""

    __slots__ = ("passed", "reason_code", "detail")

    def __init__(self, passed: bool, reason_code: str = "", detail: str = ""):
        self.passed = passed
        self.reason_code = reason_code
        self.detail = detail

    def __bool__(self) -> bool:
        return self.passed


class FeatureGate:
    """Pre-inference feature validation — blocks garbage-in before garbage-out.

    Usage::

        gate = FeatureGate.check(
            feature_vector=np.zeros(40),
            micro_vector=np.zeros(9),
            atr=6.2,
            mid_price=4700.0,
        )
        if not gate.passed:
            print(f"Blocked: {gate.reason_code}")
    """

    @staticmethod
    def check(
        feature_vector: Any = None,
        micro_vector: Any = None,
        atr: float = 0.0,
        mid_price: float = 0.0,
    ) -> GateResult:
        """Validate feature vectors before feeding them to brain inference.

        Returns GateResult with ``passed=True`` only when all checks pass.
        """
        import math as _math

        # ── Feature vector shape + NaN/Inf check ──
        fv = None
        if feature_vector is not None:
            try:
                fv = getattr(feature_vector, "ravel", None)
                if fv is not None:
                    fv = feature_vector.ravel()
                else:
                    fv = feature_vector
                # Removed hardcoded shape==(40,) check (FIX-20260528-017).
                # Downstream adapter-level dimension guards catch mismatches
                # on a per-brain basis without false-blocking non-40 brains.
                nan_count = 0
                inf_count = 0
                zero_count = 0
                total = 0
                for v in fv.flat:
                    total += 1
                    if _math.isnan(float(v)):
                        nan_count += 1
                    elif _math.isinf(float(v)):
                        inf_count += 1
                    elif float(v) == 0.0:
                        zero_count += 1
                if nan_count > 5:
                    return GateResult(False, "FEATURE_NAN", f"{nan_count} NaN values")
                if total > 0 and zero_count > total - 30:
                    return GateResult(
                        False,
                        "FEATURE_ZERO_VECTOR",
                        f"{zero_count}/{total} zero features (≥30 non-zero required)",
                    )
            except Exception:
                return GateResult(False, "FEATURE_ZERO_VECTOR", "feature vector validation failed")

        # ── Market data sanity ──
        if atr <= 0:
            return GateResult(False, "FEATURE_STALE", "ATR <= 0")
        if mid_price <= 0:
            return GateResult(False, "FEATURE_STALE", "mid_price <= 0")

        # ── Microstructure fallback-zero check ──
        if micro_vector is not None:
            try:
                mv = micro_vector.ravel() if hasattr(micro_vector, "ravel") else micro_vector
                all_zero = all(float(v) == 0.0 for v in mv.flat)
                if all_zero:
                    return GateResult(
                        False, "FEATURE_COLD_START", "micro vector is all zeros (fallback)"
                    )
            except Exception:
                logging.getLogger(__name__).warning("Feature vector cold-start check failed")

        return GateResult(True, "", "ok")


# ── Rolling-statistics helper ────────────────────────────────────────────


class _RollingStats:
    """Lightweight rolling window — no numpy needed."""

    def __init__(self, maxlen: int = 200):
        self._buf: deque[float] = deque(maxlen=maxlen)

    def push(self, value: float) -> None:
        self._buf.append(value)

    @property
    def count(self) -> int:
        return len(self._buf)

    def mean(self) -> float:
        if not self._buf:
            return 0.0
        return sum(self._buf) / len(self._buf)

    def std(self) -> float:
        if len(self._buf) < 2:
            return 0.0
        m = self.mean()
        return (sum((v - m) ** 2 for v in self._buf) / (len(self._buf) - 1)) ** 0.5

    def percentile(self, pct: float) -> float:
        """Linear-interpolated percentile (0-100)."""
        if not self._buf:
            return 0.0
        sorted_vals = sorted(self._buf)
        k = (pct / 100.0) * (len(sorted_vals) - 1)
        f = int(k)
        c = k - f
        if f + 1 >= len(sorted_vals):
            return sorted_vals[-1]
        return sorted_vals[f] + c * (sorted_vals[f + 1] - sorted_vals[f])

    def iqr(self) -> tuple[float, float, float]:
        """Returns (q25, q50, q75)."""
        return self.percentile(25), self.percentile(50), self.percentile(75)


# ── Health monitor ───────────────────────────────────────────────────────


class SignalHealthMonitor:
    """Per-cycle functional health checks for live trading signals."""

    def __init__(
        self,
        *,
        atr_history_len: int = 200,
        spread_history_len: int = 100,
        prediction_history_len: int = 500,
        freshness_max_age_seconds: float = 120.0,
        atr_iqr_mult: float = 3.0,
        spread_iqr_mult: float = 3.0,
        drift_warn_frac: float = 0.30,
    ):
        # Rolling buffers
        self._atr_stats = _RollingStats(maxlen=atr_history_len)
        self._spread_stats = _RollingStats(maxlen=spread_history_len)

        # Prediction distribution drift tracking — per direction
        self._up_prob_history: deque[float] = deque(maxlen=prediction_history_len)
        self._down_prob_history: deque[float] = deque(maxlen=prediction_history_len)
        self._confidence_history: deque[float] = deque(maxlen=prediction_history_len)

        # Thresholds
        self.freshness_max_age = freshness_max_age_seconds
        self.atr_iqr_mult = atr_iqr_mult
        self.spread_iqr_mult = spread_iqr_mult
        self.drift_warn_frac = drift_warn_frac

        # Last-seen tracking
        self._last_feature_time: datetime | None = None

    # ── Feed methods ─────────────────────────────────────────────────

    def feed_atr(self, atr: float) -> None:
        self._atr_stats.push(atr)

    def feed_spread(self, spread_pct: float) -> None:
        self._spread_stats.push(spread_pct)

    def feed_prediction(self, up_prob: float, down_prob: float, confidence: float) -> None:
        self._up_prob_history.append(up_prob)
        self._down_prob_history.append(down_prob)
        self._confidence_history.append(confidence)

    def mark_feature_received(self, event_time: datetime | None = None) -> None:
        self._last_feature_time = event_time or datetime.now(UTC)

    # ── Checks ───────────────────────────────────────────────────────

    def check_all(
        self,
        *,
        current_atr: float | None = None,
        current_spread_pct: float | None = None,
        symbol: str = "XAUUSDc",
    ) -> dict[str, Any]:
        """Run all health checks and return a combined report with actions."""
        results: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "healthy": True,
            "warnings": 0,
            "checks": {},
            "actions": [],  # actionable recommendations
        }

        for name, check_fn in [
            ("data_freshness", lambda: self.check_data_freshness()),
            ("atr_anomaly", lambda: self.check_atr_anomaly(current_atr)),
            ("prediction_drift", lambda: self.check_prediction_drift()),
            ("spread_expansion", lambda: self.check_spread_expansion(current_spread_pct)),
        ]:
            try:
                r = check_fn()
                results["checks"][name] = r
                if r.get("warning", False):
                    results["warnings"] += 1
                    results["healthy"] = False
            except Exception as exc:
                results["checks"][name] = {"warning": True, "reason": f"check_error: {exc}"}
                results["warnings"] += 1
                results["healthy"] = False

        # Derive actions from warnings
        results["actions"] = self._derive_actions(results["checks"])
        return results

    def _derive_actions(self, checks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate health warnings into actionable recommendations."""
        actions: list[dict[str, Any]] = []

        # Prediction drift → recommend brain freeze + size reduction
        drift = checks.get("prediction_drift", {})
        if drift.get("warning"):
            if "confidence_collapse" in drift.get("reason", ""):
                actions.append(
                    {
                        "action": "freeze_lowest_performing_brain",
                        "reason": f"confidence_collapse_{drift.get('recent_confidence_mean', 0):.3f}",
                        "urgency": "high",
                    }
                )
            if "up_prob_shift" in drift.get("reason", ""):
                actions.append(
                    {
                        "action": "reduce_all_position_sizes",
                        "multiplier": 0.70,
                        "reason": f"prediction_drift_{drift.get('up_prob_shift', 0):.3f}",
                        "urgency": "medium",
                    }
                )

        # Spread expansion → reduce position size
        spread = checks.get("spread_expansion", {})
        if spread.get("warning"):
            current = spread.get("current_spread_pct", 0)
            median = spread.get("spread_median_pct", 0)
            ratio = current / max(median, 1e-9)
            if ratio > 5:
                mult = 0.40
            elif ratio > 3:
                mult = 0.60
            else:
                mult = 0.80
            actions.append(
                {
                    "action": "reduce_new_position_sizes",
                    "multiplier": mult,
                    "reason": f"spread_expansion_{ratio:.1f}x_median",
                    "urgency": "high" if ratio > 5 else "medium",
                }
            )

        # Data freshness → skip cycle (don't trade on stale data)
        freshness = checks.get("data_freshness", {})
        if freshness.get("warning"):
            actions.append(
                {
                    "action": "skip_new_positions",
                    "reason": f"stale_data_{freshness.get('age_seconds', 0):.0f}s",
                    "urgency": "high",
                }
            )

        # ATR anomaly → reduce position size (uncertainty premium)
        atr = checks.get("atr_anomaly", {})
        if atr.get("warning"):
            z = abs(atr.get("z_score", 0))
            if z > 5:
                mult = 0.50
            elif z > 4:
                mult = 0.70
            else:
                mult = 0.85
            actions.append(
                {
                    "action": "reduce_new_position_sizes",
                    "multiplier": mult,
                    "reason": f"atr_outlier_z{atr.get('z_score', 0):.1f}",
                    "urgency": "medium",
                }
            )

        # Deduplicate: keep the most restrictive reduce_new_position_sizes
        reduce_actions = [a for a in actions if a["action"] == "reduce_new_position_sizes"]
        if len(reduce_actions) > 1:
            most_restrictive = min(reduce_actions, key=lambda a: a["multiplier"])
            actions = [a for a in actions if a["action"] != "reduce_new_position_sizes"]
            actions.append(most_restrictive)

        return actions

    def check_data_freshness(self) -> dict[str, Any]:
        """Check that the last feature snapshot is within acceptable age."""
        if self._last_feature_time is None:
            return {"warning": False, "age_seconds": None, "reason": "no_data_yet"}
        age = (datetime.now(UTC) - self._last_feature_time).total_seconds()
        if age > self.freshness_max_age:
            return {
                "warning": True,
                "age_seconds": round(age, 1),
                "max_age_seconds": self.freshness_max_age,
                "reason": f"stale_data_{age:.0f}s",
            }
        return {"warning": False, "age_seconds": round(age, 1)}

    def check_atr_anomaly(self, current_atr: float | None) -> dict[str, Any]:
        """Flag when current ATR is far outside historical IQR."""
        if current_atr is None:
            return {"warning": False, "reason": "no_atr_provided"}
        if self._atr_stats.count < 30:
            # Feed it and don't warn — still building baseline
            self.feed_atr(current_atr)
            return {
                "warning": False,
                "reason": "baseline_building",
                "samples": self._atr_stats.count,
            }
        q25, q50, q75 = self._atr_stats.iqr()
        iqr = q75 - q25
        if iqr <= 0:
            self.feed_atr(current_atr)
            return {"warning": False, "reason": "low_dispersion"}
        lower = q25 - self.atr_iqr_mult * iqr
        upper = q75 + self.atr_iqr_mult * iqr
        z_score = (current_atr - q50) / (self._atr_stats.std() or 1.0)
        self.feed_atr(current_atr)
        if current_atr < lower or current_atr > upper:
            return {
                "warning": True,
                "current_atr": round(current_atr, 4),
                "atr_median": round(q50, 4),
                "atr_iqr": [round(q25, 4), round(q75, 4)],
                "z_score": round(z_score, 2),
                "reason": "atr_outlier",
            }
        return {
            "warning": False,
            "current_atr": round(current_atr, 4),
            "atr_median": round(q50, 4),
            "z_score": round(z_score, 2),
        }

    def check_prediction_drift(self) -> dict[str, Any]:
        """Detect shifts in brain prediction distribution (mean/variance)."""
        n = len(self._up_prob_history)
        if n < 50:
            return {"warning": False, "reason": "insufficient_samples", "samples": n}

        # Split into two halves and compare means
        half = n // 2
        recent = list(self._up_prob_history)[-half:]
        earlier = list(self._up_prob_history)[:half]

        recent_mean = sum(recent) / half
        earlier_mean = sum(earlier) / half
        abs_diff = abs(recent_mean - earlier_mean)

        # Also check confidence collapse
        recent_conf = list(self._confidence_history)[-half:]
        recent_conf_mean = sum(recent_conf) / max(len(recent_conf), 1)

        warning = False
        reasons: list[str] = []

        if abs_diff > self.drift_warn_frac:
            warning = True
            reasons.append(f"up_prob_shift_{abs_diff:.3f}")
        if recent_conf_mean < 0.35:
            warning = True
            reasons.append(f"confidence_collapse_{recent_conf_mean:.3f}")

        return {
            "warning": warning,
            "up_prob_shift": round(abs_diff, 4),
            "recent_up_mean": round(recent_mean, 4),
            "earlier_up_mean": round(earlier_mean, 4),
            "recent_confidence_mean": round(recent_conf_mean, 4),
            "samples": n,
            "reason": "; ".join(reasons) if reasons else "ok",
        }

    def check_spread_expansion(self, current_spread_pct: float | None) -> dict[str, Any]:
        """Flag when bid-ask spread is abnormally wide."""
        if current_spread_pct is None:
            return {"warning": False, "reason": "no_spread_provided"}
        if self._spread_stats.count < 20:
            self.feed_spread(current_spread_pct)
            return {
                "warning": False,
                "reason": "baseline_building",
                "samples": self._spread_stats.count,
            }
        q25, q50, q75 = self._spread_stats.iqr()
        iqr = q75 - q25
        if iqr <= 0:
            self.feed_spread(current_spread_pct)
            return {"warning": False, "reason": "low_dispersion"}
        upper = q75 + self.spread_iqr_mult * iqr
        self.feed_spread(current_spread_pct)
        if current_spread_pct > upper:
            return {
                "warning": True,
                "current_spread_pct": round(current_spread_pct, 6),
                "spread_median_pct": round(q50, 6),
                "spread_upper_bound": round(upper, 6),
                "reason": "spread_expansion",
            }
        return {
            "warning": False,
            "current_spread_pct": round(current_spread_pct, 6),
            "spread_median_pct": round(q50, 6),
        }

    def as_summary(self) -> dict[str, Any]:
        """Lightweight snapshot suitable for journal logging."""
        return {
            "atr_samples": self._atr_stats.count,
            "atr_median": round(self._atr_stats.percentile(50), 4)
            if self._atr_stats.count > 0
            else None,
            "spread_samples": self._spread_stats.count,
            "prediction_samples": len(self._up_prob_history),
            "last_feature_age_s": (
                round((datetime.now(UTC) - self._last_feature_time).total_seconds(), 1)
                if self._last_feature_time
                else None
            ),
        }


# ── Convenience: run checks and log ──────────────────────────────────────


def run_signal_health_checks(
    monitor: SignalHealthMonitor,
    *,
    current_atr: float | None = None,
    current_spread_pct: float | None = None,
    symbol: str = "XAUUSDc",
) -> dict[str, Any]:
    """Run health checks and print warnings as structured JSON to stdout."""
    report = monitor.check_all(
        current_atr=current_atr,
        current_spread_pct=current_spread_pct,
        symbol=symbol,
    )
    if not report["healthy"]:
        print(
            json.dumps(
                {"event": "signal_health_warning", **report},
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )
    return report
