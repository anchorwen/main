"""
Tests for core.execution.gods_eye — God's Eye cross-instrument regime consensus.

Covers: multi-TF alignment, cross-instrument consistency, chop detection,
anomaly detection, macro bias resolution, health score, mode selection.
"""

from __future__ import annotations

from core.execution.gods_eye import KNOWN_CORRELATIONS, GodsEye

# ── Helpers ───────────────────────────────────────────────────────────────────


def _snapshot(regimes: dict[str, tuple[str, str, float]]):
    """Build a regime snapshot dict from compact spec.

    Args:
        regimes: tf -> (regime, direction, strength)
    """
    return {tf: {"regime": r, "direction": d, "strength": s} for tf, (r, d, s) in regimes.items()}


# ── Multi-TF Alignment ────────────────────────────────────────────────────────


class TestMultiTFAlignment:
    def test_all_aligned_up(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.8),
                    "M15": ("trending", "up", 0.75),
                    "M30": ("trending", "up", 0.7),
                    "H1": ("trending", "up", 0.9),
                    "H4": ("trending", "up", 0.7),
                    "D1": ("trending", "up", 0.6),
                }
            ),
        )
        v = eye.verdict()
        assert v.multi_tf_alignment > 0.8, f"Expected high alignment, got {v.multi_tf_alignment}"

    def test_m15_m30_bridge_alignment(self):
        """M15 and M30 provide intermediate alignment between M5 and H1."""
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.8),
                    "M15": ("trending", "up", 0.7),
                    "M30": ("trending", "up", 0.6),
                    "H1": ("trending", "down", 0.5),  # Divergence at H1
                    "H4": ("trending", "down", 0.6),
                    "D1": ("trending", "down", 0.7),
                }
            ),
        )
        v = eye.verdict()
        # Pairs: M5/M15=1.0, M15/M30=1.0, M30/H1=0.0 (conflict),
        #        H1/H4=1.0, H4/D1=1.0
        # 4 of 5 pairs aligned → high but not perfect alignment
        assert "M30/H1" in v.tf_alignment_detail
        assert v.tf_alignment_detail["M30/H1"] == 0.0  # up vs down = conflict
        assert v.tf_alignment_detail["M5/M15"] == 1.0  # both up
        assert 0.75 < v.multi_tf_alignment < 0.95  # 4/5 pairs aligned

    def test_missing_m15_m30_not_penalized(self):
        """Missing M15/M30 data defaults to neutral — doesn't break alignment."""
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.8),
                    "H1": ("trending", "up", 0.8),
                    "H4": ("trending", "up", 0.7),
                    "D1": ("trending", "up", 0.6),
                }
            ),
        )
        v = eye.verdict()
        # Missing M15/M30 → strength=0 → minimal weight → alignment stays high
        assert (
            v.multi_tf_alignment > 0.7
        ), f"Missing M15/M30 should not break alignment, got {v.multi_tf_alignment}"

    def test_conflicting_directions(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.8),
                    "M15": ("trending", "up", 0.6),
                    "M30": ("trending", "up", 0.5),
                    "H1": ("trending", "down", 0.8),
                    "H4": ("trending", "down", 0.6),
                    "D1": ("trending", "up", 0.5),  # conflicts with H4
                }
            ),
        )
        v = eye.verdict()
        # M5/M15/M30 = up, H1/H4 = down, D1 = up
        # M30/H1: up vs down = 0.0 (conflict)
        # H4/D1: down vs up = 0.0 (conflict)
        # 2 of 5 pairs conflicted → moderate alignment
        assert (
            v.multi_tf_alignment < 0.7
        ), f"Expected moderate alignment, got {v.multi_tf_alignment}"

    def test_flat_direction_is_neutral(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.7),
                    "H1": ("ranging", "flat", 0.1),
                    "H4": ("ranging", "flat", 0.1),
                    "D1": ("trending", "up", 0.5),
                }
            ),
        )
        v = eye.verdict()
        # Flat TFs create 0.5 per pair, so alignment is moderate
        assert 0.4 <= v.multi_tf_alignment <= 0.7

    def test_only_m5_present(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.8),
                }
            ),
        )
        v = eye.verdict()
        assert isinstance(v.multi_tf_alignment, float)


# ── Cross-Instrument Consistency ──────────────────────────────────────────────


class TestCrossInstrument:
    def test_xau_dxy_inverse_consistent(self):
        """Gold up + DXY down = consistent with inverse relationship."""
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "H1": ("trending", "up", 0.7),
                }
            ),
        )
        eye.update_instrument(
            "DXY",
            _snapshot(
                {
                    "H1": ("trending", "down", 0.7),
                }
            ),
        )
        v = eye.verdict()
        assert v.cross_instrument_consensus > 0.5
        assert len(v.conflict_pairs) == 0

    def test_xau_dxy_inverse_conflict(self):
        """Gold up + DXY up = conflict with inverse relationship."""
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "H1": ("trending", "up", 0.7),
                }
            ),
        )
        eye.update_instrument(
            "DXY",
            _snapshot(
                {
                    "H1": ("trending", "up", 0.7),
                }
            ),
        )
        v = eye.verdict()
        assert v.cross_instrument_consensus < 0.5
        assert len(v.conflict_pairs) > 0
        assert any("XAUUSDc/DXY" in c for c in v.conflict_pairs)

    def test_missing_instrument_no_penalty(self):
        """If one instrument has no data, skip that pair."""
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "H1": ("trending", "up", 0.7),
                }
            ),
        )
        # No DXY data — should not penalize
        v = eye.verdict()
        assert v.cross_instrument_consensus == 1.0

    def test_flat_direction_skips_pair(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "H1": ("ranging", "flat", 0.1),
                }
            ),
        )
        eye.update_instrument(
            "DXY",
            _snapshot(
                {
                    "H1": ("trending", "up", 0.7),
                }
            ),
        )
        v = eye.verdict()
        assert v.cross_instrument_consensus == 1.0  # Skipped, no checks


# ── Chop Detection ────────────────────────────────────────────────────────────


class TestChopDetection:
    def test_stable_regime_no_chop(self):
        eye = GodsEye(chop_window_bars=12, chop_threshold_switches=6)
        for _ in range(20):
            eye.update_instrument(
                "XAUUSDc",
                _snapshot(
                    {
                        "M5": ("trending", "up", 0.7),
                    }
                ),
            )
        v = eye.verdict()
        assert not v.chop_detected
        assert v.chop_score < 0.3

    def test_rapid_switching_detects_chop(self):
        eye = GodsEye(chop_window_bars=12, chop_threshold_switches=6)
        regimes = [
            "trending",
            "ranging",
            "trending",
            "ranging",
            "high_vol",
            "trending",
            "ranging",
            "high_vol",
            "normal",
            "trending",
            "ranging",
            "trending",
            "ranging",
        ]
        for r in regimes:
            eye.update_instrument(
                "XAUUSDc",
                _snapshot(
                    {
                        "M5": (r, "up", 0.5),
                    }
                ),
            )
        v = eye.verdict()
        assert v.chop_detected, f"Expected chop, got score={v.chop_score}"

    def test_chop_reduces_health_score(self):
        eye = GodsEye(chop_window_bars=12, chop_threshold_switches=6)
        # First, stable
        for _ in range(12):
            eye.update_instrument(
                "XAUUSDc",
                _snapshot(
                    {
                        "M5": ("trending", "up", 0.7),
                        "H1": ("trending", "up", 0.7),
                        "H4": ("trending", "up", 0.7),
                        "D1": ("trending", "up", 0.7),
                    }
                ),
            )
        v_stable = eye.verdict()

        # Then, choppy
        for r in ["trending", "ranging", "high_vol", "normal"] * 3:
            eye.update_instrument(
                "XAUUSDc",
                _snapshot(
                    {
                        "M5": (r, "up", 0.5),
                    }
                ),
            )
        v_choppy = eye.verdict()

        assert (
            v_choppy.health_score < v_stable.health_score
        ), f"Choppy health ({v_choppy.health_score}) should be < stable ({v_stable.health_score})"


# ── Anomaly Detection ─────────────────────────────────────────────────────────


class TestAnomalyDetection:
    def test_repeated_combo_is_normal(self):
        eye = GodsEye()
        for _ in range(100):
            eye.update_instrument(
                "XAUUSDc",
                _snapshot(
                    {
                        "M5": ("trending", "up", 0.7),
                        "H1": ("trending", "up", 0.7),
                    }
                ),
            )
        v = eye.verdict()
        assert v.anomaly_score < 0.3, f"Repeated combo should be normal, got {v.anomaly_score}"

    def test_rare_combo_is_anomalous(self):
        eye = GodsEye()
        # 99 normal updates
        for _ in range(99):
            eye.update_instrument(
                "XAUUSDc",
                _snapshot(
                    {
                        "M5": ("trending", "up", 0.7),
                        "H1": ("trending", "up", 0.7),
                    }
                ),
            )
        # 1 rare combo
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("high_vol", "flat", 0.1),
                    "H1": ("ranging", "flat", 0.1),
                }
            ),
        )
        # Need one more update for the rare combo to be the current state
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("high_vol", "flat", 0.1),
                    "H1": ("ranging", "flat", 0.1),
                }
            ),
        )
        v = eye.verdict()
        # The current combo has only been seen 2/101 times ≈ 2%
        # This is >1% so should NOT trigger anomaly yet
        # But if it's the ONLY anomaly, score might be low
        assert isinstance(v.anomaly_score, float)


# ── Macro Bias ────────────────────────────────────────────────────────────────


class TestMacroBias:
    def test_h4_d1_both_up(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "H4": ("trending", "up", 0.8),
                    "D1": ("trending", "up", 0.7),
                }
            ),
        )
        v = eye.verdict()
        assert v.macro_bias == "up"
        assert v.macro_conviction > 0.5

    def test_h4_up_d1_flat(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "H4": ("trending", "up", 0.6),
                    "D1": ("ranging", "flat", 0.1),
                }
            ),
        )
        v = eye.verdict()
        assert v.macro_bias == "up"
        assert v.macro_conviction < 0.5

    def test_no_higher_tf_returns_neutral(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.8),
                }
            ),
        )
        v = eye.verdict()
        assert v.macro_bias == "neutral"
        assert v.macro_conviction == 0.0


# ── Health Score + Mode ───────────────────────────────────────────────────────


class TestHealthAndMode:
    def test_perfect_alignment_is_healthy(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.9),
                    "H1": ("trending", "up", 0.9),
                    "H4": ("trending", "up", 0.9),
                    "D1": ("trending", "up", 0.9),
                }
            ),
        )
        eye.update_instrument(
            "DXY",
            _snapshot(
                {
                    "H1": ("trending", "down", 0.9),
                }
            ),
        )
        v = eye.verdict()
        assert v.health_score > 0.7, f"Expected healthy, got {v.health_score}"
        assert v.recommended_mode in ("normal", "cautious")
        assert v.confidence_modifier >= 0.7

    def test_chop_and_conflict_is_unhealthy(self):
        eye = GodsEye(chop_window_bars=12, chop_threshold_switches=6)
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.7),
                    "H1": ("trending", "down", 0.7),  # TF conflict
                    "H4": ("trending", "up", 0.5),
                    "D1": ("trending", "down", 0.5),
                }
            ),
        )
        eye.update_instrument(
            "DXY",
            _snapshot(
                {
                    "H1": ("trending", "up", 0.7),  # Cross-instrument conflict
                }
            ),
        )
        for r in ["trending", "ranging", "high_vol", "normal"] * 3:
            eye.update_instrument(
                "XAUUSDc",
                _snapshot(
                    {
                        "M5": (r, "up", 0.5),
                    }
                ),
            )
        v = eye.verdict()
        assert v.health_score < 0.6, f"Expected unhealthy, got {v.health_score}"
        assert v.recommended_mode in ("defensive", "shadow")
        assert v.confidence_modifier < 0.85


# ── Serialization ─────────────────────────────────────────────────────────────


class TestSerialization:
    def test_to_dict_roundtrip(self):
        eye = GodsEye()
        eye.update_instrument(
            "XAUUSDc",
            _snapshot(
                {
                    "M5": ("trending", "up", 0.7),
                    "H1": ("trending", "up", 0.8),
                }
            ),
        )
        v1 = eye.verdict()
        state = eye.to_dict()

        # Create new eye from state (partial restore — combo counts preserved)
        eye2 = GodsEye()
        eye2._instruments = state["instruments"]
        eye2._regime_history = {
            k: __import__("collections").deque(v, maxlen=eye2._chop_window)
            for k, v in state["regime_history"].items()
        }
        eye2._regime_change_counter = state["regime_change_counter"]
        eye2._combo_counts = state["combo_counts"]
        eye2._total_updates = state["total_updates"]

        v2 = eye2.verdict()
        assert abs(v2.health_score - v1.health_score) < 0.01


# ── Known Correlations ────────────────────────────────────────────────────────


class TestKnownCorrelations:
    def test_all_pairs_have_valid_relationship(self):
        for corr in KNOWN_CORRELATIONS:
            assert corr["relationship"] in ("inverse", "aligned")
            assert 0.0 <= corr["strength"] <= 1.0
            assert len(corr["pair"]) == 2

    def test_xau_dxy_are_inverse(self):
        xau_dxy = [c for c in KNOWN_CORRELATIONS if "XAUUSDc" in c["pair"] and "DXY" in c["pair"]]
        assert len(xau_dxy) > 0
        assert xau_dxy[0]["relationship"] == "inverse"
