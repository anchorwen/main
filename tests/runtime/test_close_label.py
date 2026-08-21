"""Tests for core.runtime.close_label — the close-label SSOT (TECH_DEBT-007).

P6 (DQAF-20260821-001): the close-label decision now lives in one pure,
stdlib-only leaf.  These tests weld the FULL mapping matrix (P0-P6) so no
producer can silently diverge from the canonical label again.

FIX-20260821-002: SSOT extraction + four-producer convergence + settlement
trail-blindspot repair + MIA causality restoration + broker-fallback format
unification.
"""

from __future__ import annotations

import pytest

from core.runtime.close_label import (
    DEAL_REASON_MAP,
    resolve_close_label,
    resolve_close_reason_str,
    trail_active_from_sources,
    watchdog_shortcode,
)


class TestWatchdogShortcode:
    def test_two_part_canonical(self) -> None:
        """exit_watchdog:hesitation_18c_no_breakeven → watchdog:hesitation_18c."""
        assert (
            watchdog_shortcode("exit_watchdog:hesitation_18c_no_breakeven")
            == "watchdog:hesitation_18c"
        )

    def test_single_part(self) -> None:
        assert watchdog_shortcode("exit_watchdog:hesitation") == "watchdog:hesitation"

    def test_no_colon_uses_whole(self) -> None:
        assert watchdog_shortcode("hesitation") == "watchdog:hesitation"

    def test_long_single_token_truncated_to_30(self) -> None:
        _long = "x" * 50
        assert watchdog_shortcode(_long) == "watchdog:" + "x" * 30


class TestTrailActiveFromSources:
    def test_pm_advances_true(self) -> None:
        assert trail_active_from_sources(3, {}) is True

    def test_trail_contribution_true(self) -> None:
        assert trail_active_from_sources(0, {"trail_advances": 2}) is True

    def test_both_zero_false(self) -> None:
        assert trail_active_from_sources(0, {"trail_advances": 0}) is False

    def test_none_sources_false(self) -> None:
        assert trail_active_from_sources(None, None) is False

    def test_string_advances(self) -> None:
        assert trail_active_from_sources("3", {}) is True
        assert trail_active_from_sources(0, {"trail_advances": "2"}) is True

    def test_non_dict_contribution_false(self) -> None:
        assert trail_active_from_sources(0, "nope") is False


class TestResolveCloseReasonStr:
    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (0, "client_close"),
            (1, "mobile_close"),
            (2, "web_close"),
            (3, "signal_close"),
            (4, "sl_hit"),
            (5, "tp_hit"),
            (6, "stop_out"),
            (7, "risk_out"),
        ],
    )
    def test_canonical_taxonomy(self, reason: int, expected: str) -> None:
        assert resolve_close_reason_str(reason) == expected

    def test_none_is_honest_unknown(self) -> None:
        assert resolve_close_reason_str(None) == "unknown_close"

    def test_unknown_int(self) -> None:
        assert resolve_close_reason_str(99) == "unknown_99"


class TestResolveCloseLabel:
    """The full P0-P6 mapping matrix — the SSOT's entire contract."""

    def test_p0_watchdog_outranks_sl_trail(self) -> None:
        assert (
            resolve_close_label(4, "exit_watchdog:hesitation_18c_no_breakeven", True)
            == "watchdog:hesitation_18c"
        )

    def test_p1_sl_trailed(self) -> None:
        assert resolve_close_label(4, "", True) == "sl_hit_trailed"

    def test_p2_sl_first(self) -> None:
        assert resolve_close_label(4, "", False) == "sl_hit_first"

    def test_p3_tp_first_even_with_trail(self) -> None:
        assert resolve_close_label(5, "", True) == "tp_hit_first"

    def test_p4_managed_comment(self) -> None:
        assert resolve_close_label(3, "bleed_stop_r-0.5", False) == "managed:bleed_stop_r-0.5"

    def test_p4_comment_truncated_to_80(self) -> None:
        _long = "c" * 120
        assert resolve_close_label(3, _long, False) == "managed:" + "c" * 80

    def test_p5_broker_signal_close(self) -> None:
        assert resolve_close_label(3, "", False) == "broker:signal_close"

    def test_p5_broker_stop_out(self) -> None:
        assert resolve_close_label(6, "", False) == "broker:stop_out"

    def test_p5_broker_risk_out(self) -> None:
        assert resolve_close_label(7, "", False) == "broker:risk_out"

    def test_p5_broker_client_close(self) -> None:
        assert resolve_close_label(0, "", False) == "broker:client_close"

    def test_p6_none_reason_is_honest_unknown(self) -> None:
        """NEVER fabricate a broker:client_close for an unknown reason."""
        assert resolve_close_label(None, "", False) == "unknown_close"

    def test_p6_unknown_int(self) -> None:
        assert resolve_close_label(99, "", False) == "broker:unattributed_99"

    def test_sl_comment_does_not_shadow_sl_label(self) -> None:
        """A comment on a reason-4 deal still yields the SL label (comment
        loses only to watchdog, and SL/TP reasons outrank managed)."""
        assert resolve_close_label(4, "trail_advance_3", False) == "sl_hit_first"


class TestDealReasonMapIntegrity:
    def test_eight_canonical_entries(self) -> None:
        assert set(DEAL_REASON_MAP) == {0, 1, 2, 3, 4, 5, 6, 7}

    def test_map_roundtrip_with_reason_str(self) -> None:
        for _reason, _label in DEAL_REASON_MAP.items():
            assert resolve_close_reason_str(_reason) == _label
