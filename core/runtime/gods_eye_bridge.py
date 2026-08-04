"""God's Eye bridge — Strangler Fig extraction from live_cycle.py.

Encapsulates the translation layer between RegimeGate.classify() output
and GodsEye.update_instrument() input.  Keeps God's Eye wiring out of
the live_cycle monolith — only a single ``feed_gods_eye()`` call remains.

FIX-20260625-090: God's Eye Phase 2 — live trading pipeline integration.
"""

from __future__ import annotations

import json
from typing import Any

from core.runtime.time_utils import _utc_iso


def extract_gods_eye_snapshot(
    regime_gate_result: dict[str, Any], symbol: str
) -> dict[str, dict[str, Any]]:
    """Extract multi-TF regime snapshot for God's Eye ingestion.

    Converts RegimeGate.classify() output into the format expected by
    ``GodsEye.update_instrument()``::

        {"M5": {"regime": str, "direction": str, "strength": float}, ...}

    Direction mapping: RegimeGate "long"/"short"/"neutral" → God's Eye
    "up"/"down"/"flat".  M15 and M30 are not available from the current
    RegimeGate (no intermediate-TF TrendDetectors); God's Eye dynamically
    contracts the TF ladder to exclude missing TFs (FIX-20260731-004),
    preventing the NaN-as-zero anti-pattern that previously caused
    permanent multi_tf_alignment=0.5.
    """
    _DIR_MAP = {"long": "up", "short": "down", "neutral": "flat"}
    snapshot: dict[str, dict[str, Any]] = {}

    # M5 — from RegimeGate's M5 TrendDetector
    _m5_regime = regime_gate_result.get("regime", "normal")
    _m5_dir = regime_gate_result.get("trend_direction", "neutral")
    _m5_strength = float(regime_gate_result.get("m5_trend_strength", 0.5) or 0.5)
    snapshot["M5"] = {
        "regime": _m5_regime,
        "direction": _DIR_MAP.get(_m5_dir, "flat"),
        "strength": max(0.0, min(1.0, _m5_strength)),
    }

    # H1 — from RegimeGate's H1 TrendDetector
    _h1_dir = regime_gate_result.get("h1_trend_direction", "neutral")
    _h1_strength = float(regime_gate_result.get("h1_trend_strength", 0.5) or 0.5)
    snapshot["H1"] = {
        "regime": "trending" if _h1_dir in ("long", "short") else "ranging",
        "direction": _DIR_MAP.get(_h1_dir, "flat"),
        "strength": max(0.0, min(1.0, _h1_strength)),
    }

    # H4 — from RegimeGate's H4 TrendDetector (only if ready)
    _h4_dir = regime_gate_result.get("h4_trend_direction", "neutral")
    _h4_strength = float(regime_gate_result.get("h4_trend_strength", 0.5) or 0.5)
    if regime_gate_result.get("h4_is_ready", False):
        snapshot["H4"] = {
            "regime": "trending" if _h4_dir in ("long", "short") else "ranging",
            "direction": _DIR_MAP.get(_h4_dir, "flat"),
            "strength": max(0.0, min(1.0, _h4_strength)),
        }

    # D1 — from RegimeGate's D1 TrendDetector (only if ready)
    _d1_dir = regime_gate_result.get("d1_trend_direction", "neutral")
    _d1_strength = float(regime_gate_result.get("d1_trend_strength", 0.5) or 0.5)
    if regime_gate_result.get("d1_is_ready", False):
        snapshot["D1"] = {
            "regime": "trending" if _d1_dir in ("long", "short") else "ranging",
            "direction": _DIR_MAP.get(_d1_dir, "flat"),
            "strength": max(0.0, min(1.0, _d1_strength)),
        }

    return snapshot


def feed_gods_eye(
    state: Any,
    regime_gate_result: dict[str, Any],
    config: Any,
) -> Any:
    """Feed God's Eye with the current RegimeGate output and return a verdict.

    Handles lazy initialization, snapshot extraction, ingestion, and
    diagnostic logging.  Returns ``None`` on any failure (God's Eye is
    advisory — it never fails a cycle).

    Args:
        state: ``LiveCycleState`` — must have a ``gods_eye`` attribute.
        regime_gate_result: Dict from ``RegimeGate.classify()``.
        config: ``LiveCycleConfig`` — must have a ``symbol`` attribute.

    Returns:
        ``GodsEyeVerdict`` or ``None``.
    """
    try:
        # ── Lazy init ──
        if state.gods_eye is None:
            from core.execution.gods_eye import GodsEye  # noqa: I001

            state.gods_eye = GodsEye(primary_instrument=config.symbol)
            print(
                json.dumps(
                    {
                        "event": "gods_eye_init",
                        "time": _utc_iso(),
                        "primary_instrument": config.symbol,
                        "correlation_pairs": [
                            f"{p['pair'][0]}/{p['pair'][1]}" for p in state.gods_eye._correlations
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ── Ingest ──
        snapshot = extract_gods_eye_snapshot(regime_gate_result, config.symbol)
        state.gods_eye.update_instrument(config.symbol, snapshot)
        verdict = state.gods_eye.verdict()

        # ── Diagnostic ──
        print(
            json.dumps(
                {
                    "event": "gods_eye_cycle",
                    "time": _utc_iso(),
                    "health_score": round(verdict.health_score, 4),
                    "confidence_modifier": round(verdict.confidence_modifier, 4),
                    "recommended_mode": verdict.recommended_mode,
                    "multi_tf_alignment": round(verdict.multi_tf_alignment, 4),
                    "tf_alignment_detail": verdict.tf_alignment_detail,
                    "cross_instrument_consensus": round(verdict.cross_instrument_consensus, 4),
                    "chop_detected": verdict.chop_detected,
                    "chop_score": round(verdict.chop_score, 4),
                    "macro_bias": verdict.macro_bias,
                    "macro_conviction": round(verdict.macro_conviction, 4),
                    "anomaly_score": round(verdict.anomaly_score, 4),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return verdict

    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        # God's Eye is advisory — never fail the cycle
        return None
