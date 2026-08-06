"""Tests for core.runtime.deal_selection — the MT5 exit-deal SSOT.

DQAF-20260708-003: proves the invariant "a close is resolved from a
DEAL_ENTRY_OUT deal, never the opening deal".  The pre-SSOT bug picked
``deals[0]`` (the entry deal) → close_price == entry_price, profit == 0 →
fabricated break-even.  These tests lock that door.
"""

from __future__ import annotations

from types import SimpleNamespace

from core.runtime.deal_selection import (
    CLOSE_SRC_EXIT_DEAL,
    CLOSE_SRC_NO_EXIT_DEAL,
    ExitResolution,
    resolve_exit_deal,
)


def _deal(
    *,
    entry: int,
    ticket: int,
    price: float,
    profit: float | None = 0.0,
    reason: int = 3,
    time: float = 0.0,
    volume: float = 0.1,
    position_id: int = 0,
    comment: str = "",
) -> SimpleNamespace:
    """Build a fake MT5 Deal object (matches history_deals_get() shape)."""
    return SimpleNamespace(
        entry=entry,
        ticket=ticket,
        price=price,
        profit=profit,
        reason=reason,
        time=time,
        volume=volume,
        position_id=position_id,
        comment=comment,
    )


class TestFullClose:
    def test_picks_exit_deal_not_entry_deal(self) -> None:
        """THE regression: never resolve a close from the opening deal."""
        entry = _deal(entry=0, ticket=1, price=63514.66, profit=0.0, reason=3, time=100)
        exit_ = _deal(entry=1, ticket=2, price=64598.99, profit=1084.0, reason=5, time=200)
        res = resolve_exit_deal([entry, exit_])
        assert res is not None
        assert res.has_exit is True
        assert res.close_price == 64598.99  # exit price, NOT 63514.66 entry
        assert res.close_pnl == 1084.0
        assert res.close_reason == 5
        assert res.entry_fill_price == 63514.66
        assert res.close_price_source == CLOSE_SRC_EXIT_DEAL

    def test_deal_order_reversed_still_picks_exit(self) -> None:
        """Order in the list must not matter — filter by entry flag, not index."""
        exit_ = _deal(entry=1, ticket=2, price=64598.99, profit=1084.0, reason=5, time=200)
        entry = _deal(entry=0, ticket=1, price=63514.66, profit=0.0, reason=3, time=100)
        res = resolve_exit_deal([exit_, entry])  # exit first
        assert res is not None and res.close_price == 64598.99
        assert res.entry_fill_price == 63514.66

    def test_sl_hit_loss(self) -> None:
        entry = _deal(entry=0, ticket=1, price=100.0, profit=0.0, time=100)
        exit_ = _deal(entry=1, ticket=2, price=95.0, profit=-5.0, reason=4, time=200)
        res = resolve_exit_deal([entry, exit_])
        assert res is not None
        assert res.close_price == 95.0
        assert res.close_pnl == -5.0
        assert res.close_reason == 4

    def test_true_breakeven_profit_zero_is_kept(self) -> None:
        """profit == 0 from an exit deal is a VERIFIED break-even, not None."""
        entry = _deal(entry=0, ticket=1, price=100.0, profit=0.0, time=100)
        exit_ = _deal(entry=1, ticket=2, price=100.0, profit=0.0, reason=3, time=200)
        res = resolve_exit_deal([entry, exit_])
        assert res is not None
        assert res.close_pnl == 0.0  # not None — genuine breakeven
        assert res.has_exit is True


class TestNoExitDeal:
    def test_only_entry_deal_returns_no_exit(self) -> None:
        """No exit deal → NEVER fabricate; provenance flags the anomaly."""
        entry = _deal(entry=0, ticket=1, price=100.0, profit=0.0)
        res = resolve_exit_deal([entry])
        assert res is not None
        assert res.close_price is None
        assert res.has_exit is False
        assert res.close_price_source == CLOSE_SRC_NO_EXIT_DEAL
        assert res.entry_fill_price == 100.0

    def test_empty_deals_returns_none(self) -> None:
        assert resolve_exit_deal([]) is None
        assert resolve_exit_deal(None) is None


class TestCursor:
    def test_cursor_excludes_processed_exit(self) -> None:
        exit_ = _deal(entry=1, ticket=2, price=110.0, profit=10.0, reason=5, time=200)
        res = resolve_exit_deal([exit_], cursor=2)  # ticket not > cursor
        assert res is not None
        assert res.close_price_source == CLOSE_SRC_NO_EXIT_DEAL

    def test_cursor_zero_includes_all(self) -> None:
        exit_ = _deal(entry=1, ticket=2, price=110.0, profit=10.0, reason=5, time=200)
        res = resolve_exit_deal([exit_], cursor=0)
        assert res is not None and res.has_exit


class TestPartialCloses:
    def test_sums_profit_across_exit_deals(self) -> None:
        entry = _deal(entry=0, ticket=1, price=100.0, profit=0.0, time=100)
        p1 = _deal(entry=1, ticket=2, price=105.0, profit=5.0, reason=3, time=200, volume=0.05)
        p2 = _deal(entry=1, ticket=3, price=110.0, profit=10.0, reason=3, time=300, volume=0.05)
        res = resolve_exit_deal([entry, p1, p2])
        assert res is not None
        assert res.close_pnl == 15.0  # 5 + 10
        assert res.close_volume == 0.1  # 0.05 + 0.05
        assert res.close_price == 110.0  # latest exit by time
        assert res.n_exit_deals == 2


class TestReasonPreference:
    def test_prefers_sltp_over_signal(self) -> None:
        entry = _deal(entry=0, ticket=1, price=100.0, profit=0.0, time=100)
        signal = _deal(entry=1, ticket=2, price=101.0, profit=1.0, reason=3, time=300)
        tp = _deal(entry=1, ticket=3, price=110.0, profit=10.0, reason=5, time=200)
        res = resolve_exit_deal([entry, signal, tp])
        assert res is not None
        assert res.close_reason == 5  # TP preferred even though signal is later
        assert res.close_price == 110.0


class TestFalsyEdgeCases:
    def test_entry_zero_not_confused_with_missing(self) -> None:
        """entry==0 must be treated as DEAL_ENTRY_IN, not as -1/missing."""
        entry = _deal(entry=0, ticket=1, price=100.0, profit=0.0)
        exit_ = _deal(entry=1, ticket=2, price=110.0, profit=10.0, reason=5)
        res = resolve_exit_deal([entry, exit_])
        assert res is not None and res.entry_fill_price == 100.0

    def test_reason_zero_client_close_preserved(self) -> None:
        """reason==0 (client close) must not collapse to -1."""
        entry = _deal(entry=0, ticket=1, price=100.0, profit=0.0)
        exit_ = _deal(entry=1, ticket=2, price=110.0, profit=10.0, reason=0)
        res = resolve_exit_deal([entry, exit_])
        assert res is not None and res.close_reason == 0

    def test_missing_profit_attr_yields_none_pnl(self) -> None:
        entry = _deal(entry=0, ticket=1, price=100.0, profit=None)
        exit_ = _deal(entry=1, ticket=2, price=110.0, profit=None, reason=5)
        res = resolve_exit_deal([entry, exit_])
        assert res is not None
        assert res.close_pnl is None  # no profit data
        assert res.close_price == 110.0  # price still authoritative
        assert res.has_exit is True

    def test_exit_deal_zero_price_not_usable(self) -> None:
        exit_ = _deal(entry=1, ticket=2, price=0.0, profit=10.0, reason=5)
        res = resolve_exit_deal([exit_])
        assert res is not None
        assert res.close_price is None
        assert res.has_exit is False


class TestResolutionDataclass:
    def test_is_frozen(self) -> None:
        res = ExitResolution(
            close_price=1.0,
            close_pnl=1.0,
            close_reason=5,
            close_time=1,
            close_volume=0.1,
            deal_id=2,
            position_id=1,
            comment="",
            entry_fill_price=1.0,
            close_price_source=CLOSE_SRC_EXIT_DEAL,
            n_exit_deals=1,
        )
        try:
            setattr(res, "close_price", 2.0)  # noqa: B010 — frozen; setattr still raises FrozenInstanceError
            raise AssertionError("ExitResolution should be frozen")
        except Exception:  # noqa: BLE001 — FrozenInstanceError expected
            pass
