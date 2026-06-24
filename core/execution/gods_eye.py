"""
God's Eye — cross-instrument, multi-timeframe regime consensus engine.

Sits ABOVE per-instrument RegimeGate instances. While RegimeGate answers
"what regime is THIS instrument in right now?", God's Eye answers:
"across all instruments and timeframes, is the market structure healthy
enough to trade?"

Three pillars:
  1. Multi-TF alignment — do M5/M15/H1/H4 trends point the same way?
  2. Cross-instrument consistency — do correlated instruments agree?
  3. Regime anomaly detection — is the current regime combination unusual?

Output: a GodsEyeVerdict that modifies per-instrument strategy activation
and confidence thresholds. God's Eye NEVER silences a strategy — it only
reduces or restores confidence (fail-open for exits, cautious for entries).

Architecture:
  RegimeGate (per-instrument) → GodsEye (cross-instrument) → Execution Layer
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# ── Known inter-market relationships ──────────────────────────────────────────
# Format: (instrument_A, instrument_B, relationship, strength)
# relationship: "inverse" (typically move opposite), "aligned" (typically move together)
# strength: 0.0-1.0, how reliable the relationship is

KNOWN_CORRELATIONS: list[dict[str, Any]] = [
    {"pair": ("XAUUSDc", "DXY"), "relationship": "inverse", "strength": 0.75},
    {"pair": ("XAUUSDc", "XAGUSDc"), "relationship": "aligned", "strength": 0.70},
    {"pair": ("XAUUSDc", "EURUSDc"), "relationship": "aligned", "strength": 0.50},
    {"pair": ("EURUSDc", "DXY"), "relationship": "inverse", "strength": 0.85},
    {"pair": ("USDJPYc", "DXY"), "relationship": "aligned", "strength": 0.60},
    {"pair": ("XAGUSDc", "EURUSDc"), "relationship": "aligned", "strength": 0.40},
]


@dataclass
class GodsEyeVerdict:
    """Unified cross-instrument regime assessment.

    All scores in [0, 1] unless noted otherwise. Higher = healthier.
    """

    # Multi-TF alignment
    multi_tf_alignment: float = 0.5  # 0=conflicting, 1=all TFs aligned
    tf_alignment_detail: dict[str, float] = field(default_factory=dict)

    # Cross-instrument consistency
    cross_instrument_consensus: float = 1.0  # 0=conflict, 1=harmony
    conflict_pairs: list[str] = field(default_factory=list)  # e.g. ["XAUUSDc/DXY"]

    # Regime stability
    chop_detected: bool = False
    chop_score: float = 0.0  # 0=stable, 1=extreme chop
    regime_switches_per_hour: float = 0.0

    # Anomaly
    anomaly_score: float = 0.0  # 0=normal, 1=highly anomalous
    anomaly_reasons: list[str] = field(default_factory=list)

    # Combined
    health_score: float = 1.0  # 0=toxic, 1=healthy — product of above
    confidence_modifier: float = 1.0  # 0.5-1.5, multiplier for brain confidence
    recommended_mode: str = "normal"  # "normal" | "cautious" | "defensive" | "shadow"

    # Macro bias (highest-TF direction)
    macro_bias: str = "neutral"  # "bullish", "bearish", "neutral"
    macro_conviction: float = 0.0  # 0-1, strength of macro bias


class GodsEye:
    """Cross-instrument, multi-timeframe regime consensus engine.

    Usage::

        eye = GodsEye(primary_instrument="XAUUSDc")
        eye.update_instrument("XAUUSDc", {
            "M5": {"regime": "trending", "direction": "up", "strength": 0.6},
            "H1": {"regime": "trending", "direction": "up", "strength": 0.7},
            "H4": {"regime": "trending", "direction": "up", "strength": 0.5},
            "D1": {"regime": "ranging",  "direction": "flat", "strength": 0.1},
        })
        eye.update_instrument("DXY", {
            "H1": {"regime": "trending", "direction": "down", "strength": 0.6},
            "H4": {"regime": "trending", "direction": "down", "strength": 0.7},
        })
        verdict = eye.verdict()
        print(f"Health: {verdict.health_score:.2f}, Mode: {verdict.recommended_mode}")
    """

    def __init__(
        self,
        primary_instrument: str = "XAUUSDc",
        *,
        chop_window_bars: int = 24,
        chop_threshold_switches: int = 6,
        min_tf_alignment: float = 0.5,
        correlations: list[dict[str, Any]] | None = None,
    ):
        self._primary = primary_instrument
        self._chop_window = chop_window_bars
        self._chop_threshold = chop_threshold_switches
        self._min_tf_alignment = min_tf_alignment
        self._correlations = correlations or KNOWN_CORRELATIONS

        # Per-instrument regime state
        self._instruments: dict[str, dict[str, dict[str, Any]]] = {}

        # Chop detection: sliding window of regime labels per instrument
        self._regime_history: dict[str, deque[str]] = {}
        self._regime_change_counter: dict[str, int] = {}

        # Anomaly: track regime combination frequencies
        self._combo_counts: dict[str, int] = {}
        self._total_updates: int = 0

    # ── Data ingestion ────────────────────────────────────────────────────────

    def update_instrument(self, symbol: str, regime_map: dict[str, dict[str, Any]]) -> None:
        """Ingest a multi-timeframe regime snapshot for one instrument.

        Args:
            symbol: Instrument identifier, e.g. "XAUUSDc", "DXY".
            regime_map: Dict of timeframe → regime info.
                Each regime info should have at least:
                - "regime": str — "trending", "ranging", "high_vol", "normal", etc.
                - "direction": str — "up", "down", "flat"
                - "strength": float — 0.0-1.0 trend strength
        """
        self._instruments[symbol] = regime_map
        self._total_updates += 1

        # Track regime changes for chop detection
        if symbol == self._primary:
            self._track_regime_history(symbol, regime_map)

        # Track regime combinations for anomaly detection
        self._track_combo(symbol, regime_map)

    def _track_regime_history(self, symbol: str, regime_map: dict[str, dict[str, Any]]) -> None:
        """Track M5 regime label history for chop detection."""
        if symbol not in self._regime_history:
            self._regime_history[symbol] = deque(maxlen=self._chop_window)
            self._regime_change_counter[symbol] = 0

        m5 = regime_map.get("M5", {})
        current_label = m5.get("regime", "unknown")
        history = self._regime_history[symbol]

        if len(history) > 0 and history[-1] != current_label:
            self._regime_change_counter[symbol] += 1

        history.append(current_label)

    def _track_combo(self, symbol: str, regime_map: dict[str, dict[str, Any]]) -> None:
        """Track regime combination for anomaly detection."""
        # Build a compact combo key
        parts = []
        for tf in sorted(regime_map.keys()):
            r = regime_map[tf].get("regime", "?")
            parts.append(f"{tf}:{r[:2]}")  # e.g. "M5:tr", "H1:ra"
        key = f"{symbol}[{','.join(parts)}]"
        self._combo_counts[key] = self._combo_counts.get(key, 0) + 1

    # ── Verdict computation ───────────────────────────────────────────────────

    def verdict(self) -> GodsEyeVerdict:
        """Compute the current God's Eye verdict across all instruments."""
        v = GodsEyeVerdict()

        # 1. Multi-TF alignment
        v.multi_tf_alignment, v.tf_alignment_detail = self._check_multi_tf_alignment()

        # 2. Cross-instrument consistency
        v.cross_instrument_consensus, v.conflict_pairs = self._check_cross_instrument()

        # 3. Chop detection
        v.chop_detected, v.chop_score, v.regime_switches_per_hour = self._check_chop()

        # 4. Anomaly detection
        v.anomaly_score, v.anomaly_reasons = self._check_anomaly()

        # 5. Macro bias
        v.macro_bias, v.macro_conviction = self._resolve_macro_bias()

        # 6. Combined health score
        v.health_score = self._compute_health(v)

        # 7. Confidence modifier + recommended mode
        v.confidence_modifier, v.recommended_mode = self._resolve_mode(v)

        return v

    def _check_multi_tf_alignment(self) -> tuple[float, dict[str, float]]:
        """Check if M5→M15→M30→H1→H4→D1 trends point in the same direction.

        Uses the full standard timeframe ladder. M15/M30 bridge the gap between
        M5 microstructure and H1 tactical — their inclusion catches regime
        transitions earlier than the M5→H1 jump alone.

        Returns (alignment_score, per_pair_detail).
        """
        primary = self._instruments.get(self._primary, {})
        all_tfs = ["M5", "M15", "M30", "H1", "H4", "D1"]

        # Extract direction and strength for each TF
        tf_info: dict[str, dict[str, Any]] = {}
        for tf in all_tfs:
            info = primary.get(tf, {})
            direction = info.get("direction", "flat")
            strength = info.get("strength", 0.0)
            if direction in ("flat", "unknown", None):
                direction = "flat"
                strength = 0.0
            tf_info[tf] = {"direction": direction, "strength": strength}

        # Compare adjacent TF pairs on the standard ladder
        tf_ladder = ["M5", "M15", "M30", "H1", "H4", "D1"]
        alignments: dict[str, float] = {}
        total_weight = 0.0
        weighted_alignment = 0.0

        for i in range(len(tf_ladder) - 1):
            tf_high = tf_ladder[i]
            tf_low = tf_ladder[i + 1]
            d1 = tf_info[tf_high]["direction"]
            d2 = tf_info[tf_low]["direction"]
            s1 = tf_info[tf_high]["strength"]
            s2 = tf_info[tf_low]["strength"]

            # Alignment: 1.0 if same direction, 0.5 if one is flat, 0.0 if opposite
            if d1 == "flat" or d2 == "flat":
                pair_align = 0.5
            elif d1 == d2:
                pair_align = 1.0
            else:
                pair_align = 0.0

            # Weight by the average strength of the two TFs.
            # Missing TFs (not provided by caller) have strength=0.0,
            # contributing minimal weight — they don't skew the score.
            weight = (s1 + s2) / 2
            pair_key = f"{tf_high}/{tf_low}"
            alignments[pair_key] = pair_align
            weighted_alignment += pair_align * max(weight, 0.1)
            total_weight += max(weight, 0.1)

        overall = weighted_alignment / total_weight if total_weight > 0 else 0.5
        return round(overall, 3), alignments

    def _check_cross_instrument(self) -> tuple[float, list[str]]:
        """Check if correlated instruments have consistent trend directions.

        Returns (consensus_score, conflicting_pairs).
        """
        conflicts: list[str] = []
        total_checks = 0
        passed_checks = 0

        for corr in self._correlations:
            inst_a, inst_b = corr["pair"]
            expected = corr["relationship"]
            weight = corr["strength"]

            regime_a = self._instruments.get(inst_a, {})
            regime_b = self._instruments.get(inst_b, {})

            # Use H1 as the reference TF for cross-instrument comparison
            h1_a = regime_a.get("H1", {})
            h1_b = regime_b.get("H1", {})

            dir_a = h1_a.get("direction", "flat")
            dir_b = h1_b.get("direction", "flat")

            # Skip if either instrument has no data or is flat
            if not regime_a or not regime_b or dir_a == "flat" or dir_b == "flat":
                continue

            total_checks += 1

            same_direction = dir_a == dir_b
            if expected == "inverse":
                consistent = not same_direction
            else:  # aligned
                consistent = same_direction

            if consistent:
                passed_checks += 1
            else:
                conflicts.append(f"{inst_a}/{inst_b} ({dir_a} vs {dir_b}, expected {expected})")

        if total_checks == 0:
            return 1.0, []

        consensus = passed_checks / total_checks
        return round(consensus, 3), conflicts

    def _check_chop(self) -> tuple[bool, float, float]:
        """Detect excessive regime switching (chop/whipsaw).

        Returns (chop_detected, chop_score, switches_per_hour).
        """
        if self._primary not in self._regime_change_counter:
            return False, 0.0, 0.0

        switches = self._regime_change_counter[self._primary]
        history_len = len(self._regime_history.get(self._primary, []))

        if history_len < 6:  # Need minimum history
            return False, 0.0, 0.0

        # Switches per hour assuming ~12 M5 bars per hour per update
        bars_per_update = 1  # Each update is one bar
        hours = history_len * bars_per_update / 12
        switches_per_hour = switches / hours if hours > 0 else 0

        # Chop score: 0 (stable) to 1 (extreme chop)
        max_expected = self._chop_threshold
        chop_score = min(1.0, switches / max(self._chop_window, 1) * (24 / self._chop_window))
        chop_score = round(chop_score, 3)

        chop_detected = switches >= self._chop_threshold

        return chop_detected, chop_score, round(switches_per_hour, 1)

    def _check_anomaly(self) -> tuple[float, list[str]]:
        """Detect rare/unusual regime combinations.

        A regime combo that has been seen <1% of the time is flagged as anomalous.

        Returns (anomaly_score, reasons).
        """
        if self._total_updates < 50:
            return 0.0, []  # Not enough data

        reasons: list[str] = []
        total_anomaly_score = 0.0
        n_instruments = 0

        for symbol, regime_map in self._instruments.items():
            parts = []
            for tf in sorted(regime_map.keys()):
                r = regime_map[tf].get("regime", "?")
                parts.append(f"{tf}:{r[:2]}")
            key = f"{symbol}[{','.join(parts)}]"

            count = self._combo_counts.get(key, 0)
            frequency = count / self._total_updates

            if frequency < 0.01 and self._total_updates >= 100:
                reasons.append(f"{symbol} rare combo: {key} (seen {count}x, {frequency*100:.1f}%)")
                total_anomaly_score += 1.0 - frequency * 100
            n_instruments += 1

        anomaly_score = min(1.0, total_anomaly_score / max(n_instruments, 1))
        return round(anomaly_score, 3), reasons

    def _resolve_macro_bias(self) -> tuple[str, float]:
        """Determine macro bias from highest-TF regime directions.

        H4 and D1 provide the strategic bias. H1 is tactical.
        Returns (bias_direction, conviction).
        """
        primary = self._instruments.get(self._primary, {})

        # Collect higher-TF directions
        votes: dict[str, float] = {}
        for tf in ["H4", "D1"]:
            info = primary.get(tf, {})
            direction = info.get("direction", "flat")
            strength = info.get("strength", 0.0)
            if direction not in ("flat", "unknown", None):
                votes[direction] = votes.get(direction, 0.0) + strength

        if not votes:
            return "neutral", 0.0

        # Winner takes all
        bias = max(votes, key=lambda k: votes[k])
        conviction = min(1.0, votes[bias] / 2.0)  # Two TFs max, normalize

        return bias, round(conviction, 3)

    def _compute_health(self, v: GodsEyeVerdict) -> float:
        """Combine all signals into a single health score.

        Health = product of alignment, consensus, (1-chop), (1-anomaly).
        All factors must be healthy for a high score — multiplicative penalty.
        """
        factors = [
            max(0.1, v.multi_tf_alignment),
            max(0.1, v.cross_instrument_consensus),
            max(0.1, 1.0 - v.chop_score),
            max(0.1, 1.0 - v.anomaly_score),
        ]
        health = float(np.prod(factors))
        return round(health, 3)

    def _resolve_mode(self, v: GodsEyeVerdict) -> tuple[float, str]:
        """Map health score to confidence modifier and recommended mode.

        Returns (confidence_modifier, recommended_mode).
        """
        h = v.health_score

        if h >= 0.80:
            mode = "normal"
            modifier = 1.0
        elif h >= 0.60:
            mode = "cautious"
            modifier = 0.85
        elif h >= 0.40:
            mode = "defensive"
            modifier = 0.70
        else:
            mode = "shadow"
            modifier = 0.50

        # Additional modifiers
        reasons: list[str] = []

        # Chop penalty
        if v.chop_detected:
            modifier *= 0.80
            reasons.append("chop")

        # Cross-instrument conflict penalty
        if v.cross_instrument_consensus < 0.5 and v.conflict_pairs:
            modifier *= 0.85
            reasons.append("cross_conflict")

        # Strong macro bias bonus
        if v.macro_conviction > 0.7 and v.multi_tf_alignment > 0.7:
            modifier = min(1.2, modifier * 1.1)
            reasons.append("macro_aligned")

        modifier = round(max(0.40, min(1.50, modifier)), 3)

        # Determine mode
        if modifier < 0.50:
            mode = "shadow"
        elif modifier < 0.65:
            mode = "defensive"
        elif modifier < 0.85:
            mode = "cautious"
        else:
            mode = "normal"

        return modifier, mode

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def primary(self) -> str:
        return self._primary

    @property
    def instruments(self) -> list[str]:
        return list(self._instruments.keys())

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for checkpointing."""
        return {
            "schema_version": "gods_eye.v1",
            "primary_instrument": self._primary,
            "instruments": self._instruments,
            "regime_history": {k: list(v) for k, v in self._regime_history.items()},
            "regime_change_counter": dict(self._regime_change_counter),
            "combo_counts": dict(self._combo_counts),
            "total_updates": self._total_updates,
        }
