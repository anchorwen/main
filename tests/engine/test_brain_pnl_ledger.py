"""Tests for BrainPnLStore counterfactual P&L ledger."""

from pathlib import Path

import pytest

from core.feedback.brain_pnl_ledger import BrainPnLStore


class TestBrainPnLStore:
    """Core ledger behaviour."""

    def test_record_signal_returns_signal_id(self):
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "long", 4650.0, confidence=0.8)
        assert sid is not None
        assert sid.startswith("B1_")
        assert store.pending_count == 1

    def test_record_neutral_returns_none(self):
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "neutral", 4650.0)
        assert sid is None
        assert store.pending_count == 0

    def test_settle_long_win(self):
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "long", 4650.0, confidence=0.9)
        assert sid is not None
        outcome = store.settle_one(sid, 4660.0)
        assert outcome is not None

        assert outcome is not None
        assert outcome["is_win"] is True
        assert outcome["pnl_per_unit"] == 10.0
        assert outcome["pnl_bps"] == pytest.approx(21.51, abs=0.1)
        assert store.pending_count == 0

    def test_settle_long_loss(self):
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "long", 4650.0)
        assert sid is not None
        outcome = store.settle_one(sid, 4640.0)
        assert outcome is not None

        assert outcome["is_win"] is False
        assert outcome["pnl_per_unit"] == -10.0

    def test_settle_short_win(self):
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "short", 4650.0)
        assert sid is not None
        outcome = store.settle_one(sid, 4640.0)
        assert outcome is not None

        assert outcome["is_win"] is True
        assert outcome["pnl_per_unit"] == 10.0

    def test_settle_short_loss(self):
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "short", 4650.0)
        assert sid is not None
        outcome = store.settle_one(sid, 4660.0)
        assert outcome is not None

        assert outcome["is_win"] is False
        assert outcome["pnl_per_unit"] == -10.0

    def test_settle_all_ttl_gating(self):
        """Signals with TTL>0 should not settle (horizon-matched)."""
        store = BrainPnLStore()
        store.record_signal("B1", "XAUUSDc", "long", 100.0, expected_horizon=3)

        # Without update_pending, TTL=3 → nothing settles
        results = store.settle_all(105.0)
        assert len(results) == 0
        assert store.pending_count == 1

        # After 3 update_pending calls, TTL=0 → settles
        store.update_pending(102.0)
        store.update_pending(103.0)
        ready = store.update_pending(104.0)
        assert ready == 1
        results = store.settle_all(105.0)
        assert len(results) == 1
        assert results["B1"]["pnl_per_unit"] == 5.0

    def test_settle_all_force_all_backward_compat(self):
        """force_all=True settles everything regardless of TTL."""
        store = BrainPnLStore()
        store.record_signal("B1", "XAUUSDc", "long", 100.0, expected_horizon=12)

        results = store.settle_all(105.0, force_all=True)
        assert len(results) == 1
        assert store.pending_count == 0

    def test_mfe_mae_tracking(self):
        """update_pending should track best/worst prices during holding."""
        store = BrainPnLStore()
        store.record_signal("B1", "XAUUSDc", "long", 100.0, expected_horizon=3)

        # Simulate 3 cycles: price goes up then down
        store.update_pending(102.0)  # TTL=2, mfe=102, mae=100
        store.update_pending(98.0)  # TTL=1, mfe=102, mae=98
        store.update_pending(101.0)  # TTL=0, mfe=102, mae=98

        results = store.settle_all(101.0)
        outcome = results["B1"]
        assert outcome["pnl_per_unit"] == 1.0  # 101-100
        assert outcome["mfe_r"] > 0  # MFE from 102 (2% favorable)
        assert outcome["mae_r"] > 0  # MAE from 98 (2% adverse)

    def test_mfe_mae_short_direction(self):
        """MFE/MAE tracking for short trades."""
        store = BrainPnLStore()
        store.record_signal("B1", "XAUUSDc", "short", 100.0, expected_horizon=1)

        # Price drops (favorable for short) then spikes (adverse)
        store.update_pending(98.0)  # mfe=98 (lower=better for short)
        results = store.settle_all(99.0)
        outcome = results["B1"]
        assert outcome["pnl_per_unit"] == 1.0  # 100-99
        assert outcome["mfe_r"] > 0  # MFE: 100→98 = 2% favorable

    def test_settle_unknown_signal_returns_none(self):
        store = BrainPnLStore()
        assert store.settle_one("nonexistent", 100.0) is None

    def test_settle_all(self):
        store = BrainPnLStore()
        store.record_signal("B1", "XAUUSDc", "long", 100.0, expected_horizon=1)
        store.record_signal("B2", "XAUUSDc", "short", 100.0, expected_horizon=1)

        # TTL-based: must update_pending to decrement TTL before settle_all
        assert store.update_pending(100.0) == 2  # both ready (TTL: 1→0)
        results = store.settle_all(105.0)
        assert len(results) == 2
        assert results["B1"]["pnl_per_unit"] == 5.0  # long: 105-100
        assert results["B2"]["pnl_per_unit"] == -5.0  # short: 100-105
        assert store.pending_count == 0

    def test_pnl_values_per_unit(self):
        """P&L is per unit, not notional."""
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "long", 3000.0)
        assert sid is not None
        outcome = store.settle_one(sid, 3015.0)
        assert outcome is not None
        assert outcome["pnl_per_unit"] == 15.0


class TestMetrics:
    """Rolling metrics computation."""

    def test_insufficient_data(self):
        store = BrainPnLStore()
        m = store.get_metrics("unknown")
        assert m.sample_count == 0
        assert m.health_signal == "insufficient_data"

    def test_min_samples_for_health(self):
        store = BrainPnLStore()
        # 9 wins, 1 loss — needs 10 samples minimum for health signal
        for i in range(10):
            sid = store.record_signal("B1", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 101.0 if i < 9 else 99.0)

        m = store.get_metrics("B1")
        assert m.sample_count == 10
        assert m.win_rate == 0.9  # 9/10
        assert m.health_signal != "insufficient_data"

    def test_perfect_long_sharpe_positive(self):
        store = BrainPnLStore()
        # Varied returns so std > 0 for Sharpe calculation
        for i in range(20):
            sid = store.record_signal("B1", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 101.0 + i * 0.1)  # varied wins

        m = store.get_metrics("B1")
        assert m.sample_count == 20
        assert m.win_rate == 1.0
        assert m.cumulative_pnl > 0
        assert m.max_drawdown == 0.0
        assert m.profit_factor > 1.0

    def test_losing_brain_negative_sharpe(self):
        store = BrainPnLStore()
        # Varied returns so std > 0 for Sharpe calculation
        for i in range(20):
            sid = store.record_signal("B1", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 99.0 - i * 0.05)  # varied losses

        m = store.get_metrics("B1")
        assert m.win_rate == 0.0
        assert m.cumulative_pnl < 0
        assert m.sharpe_ratio < 0

    def test_directional_breakdown(self):
        store = BrainPnLStore()
        # 5 winning longs
        for _ in range(5):
            sid = store.record_signal("B1", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 101.0)
        # 3 losing shorts
        for _ in range(3):
            sid = store.record_signal("B1", "XAUUSDc", "short", 100.0)
            assert sid is not None
            store.settle_one(sid, 101.0)

        m = store.get_metrics("B1")
        assert m.long_count == 5
        assert m.short_count == 3
        assert m.long_win_rate == 1.0
        assert m.short_win_rate == 0.0

    def test_window_limits_outcomes(self):
        store = BrainPnLStore(window_size=10)
        for _ in range(15):
            sid = store.record_signal("B1", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 101.0)

        m = store.get_metrics("B1")
        assert m.sample_count == 10  # capped

    def test_get_all_metrics(self):
        store = BrainPnLStore()
        for b in ["B1", "B2"]:
            sid = store.record_signal(b, "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 101.0)

        all_m = store.get_all_metrics()
        assert "B1" in all_m
        assert "B2" in all_m

    def test_get_summary_table(self):
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "long", 100.0)
        assert sid is not None
        store.settle_one(sid, 102.0)
        sid = store.record_signal("B2", "XAUUSDc", "long", 100.0)
        assert sid is not None
        store.settle_one(sid, 101.0)

        table = store.get_summary_table()
        assert len(table) >= 1  # needs 5 samples for full metrics
        assert table[0]["brain_id"] == "B1"  # sorted by sharpe desc

    def test_health_signal_thresholds(self):
        """Verify health signal transitions at expected thresholds."""
        store = BrainPnLStore()

        # Winning brain → healthy
        for _ in range(15):
            sid = store.record_signal("winner", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 101.5)
        m = store.get_metrics("winner")
        assert m.health_signal in ("healthy", "stable")

        # Losing brain → critical or degraded
        for _ in range(15):
            sid = store.record_signal("loser", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 98.0)
        m2 = store.get_metrics("loser")
        assert m2.health_signal in ("critical", "degraded")


class TestPersistence:
    """Save / load round-trip."""

    def test_roundtrip(self, tmp_path: Path):
        store = BrainPnLStore()
        sid = store.record_signal("B1", "XAUUSDc", "long", 100.0, confidence=0.9)
        assert sid is not None
        store.settle_one(sid, 105.0)

        p = tmp_path / "ledger.json"
        store.save(p)
        assert p.exists()

        loaded = BrainPnLStore.load(p)
        assert loaded.total_settled == 1
        assert loaded.pending_count == 0

        m = loaded.get_metrics("B1")
        assert m.sample_count == 1
        assert m.cumulative_pnl == 5.0

    def test_roundtrip_with_pending(self, tmp_path: Path):
        store = BrainPnLStore()
        store.record_signal("B1", "XAUUSDc", "long", 100.0)
        store.record_signal("B2", "XAUUSDc", "short", 100.0)

        p = tmp_path / "ledger.json"
        store.save(p)

        loaded = BrainPnLStore.load(p)
        assert loaded.pending_count == 2
        assert loaded.total_settled == 0

    def test_load_missing_file(self):
        store = BrainPnLStore.load("nonexistent_file.json")
        assert store.total_settled == 0
        assert store.pending_count == 0


class TestDQAF060EventStreamFiltering:
    """DQAF-060: load_from_stream() must filter out SignalRecorded events."""

    def test_load_from_stream_filters_signal_recorded(self, tmp_path: Path):
        """Contract 1: Only SignalSettled events enter _settled."""
        from datetime import UTC, datetime

        from core.contracts.events import EventType, PnLEvent

        stream = tmp_path / "ledger_events.jsonl"
        _ts = datetime.now(UTC)
        events = [
            PnLEvent(
                event_type=EventType.SIGNAL_RECORDED,
                source="live",
                brain_id="test_brain",
                symbol="BTCUSDc",
                direction="long",
                entry_price=60000.0,
                pnl_r=0.0,
                confidence=0.8,
                generated_by="test",
                timestamp=_ts,
            ),
            PnLEvent(
                event_type=EventType.SIGNAL_SETTLED,
                source="live",
                brain_id="test_brain",
                symbol="BTCUSDc",
                direction="long",
                entry_price=60000.0,
                exit_price=61000.0,
                pnl_r=1.67,
                confidence=0.8,
                generated_by="test",
                timestamp=_ts,
            ),
        ]
        with open(stream, "w", encoding="utf-8") as f:
            for e in events:
                f.write(e.model_dump_json() + "\n")

        store = BrainPnLStore.load_from_stream(stream)
        metrics = store.get_metrics("test_brain")

        # Only the SignalSettled event should be in _settled
        assert metrics.sample_count == 1, (
            f"DQAF-060 VIOLATION: expected 1 settled trade, got {metrics.sample_count} "
            f"— SignalRecorded events leaked into _settled!"
        )
        assert metrics.win_rate == 1.0

    def test_load_from_stream_mixed_brains(self, tmp_path: Path):
        """SignalRecorded for brain A + SignalSettled for brain B — no cross-talk."""
        from datetime import UTC, datetime

        from core.contracts.events import EventType, PnLEvent

        stream = tmp_path / "ledger_events.jsonl"
        _ts = datetime.now(UTC)
        events = [
            PnLEvent(
                event_type=EventType.SIGNAL_RECORDED,
                source="live",
                brain_id="brain_with_signals_only",
                symbol="BTCUSDc",
                direction="long",
                entry_price=60000.0,
                pnl_r=0.0,
                confidence=0.8,
                generated_by="test",
                timestamp=_ts,
            ),
            PnLEvent(
                event_type=EventType.SIGNAL_RECORDED,
                source="live",
                brain_id="brain_with_signals_only",
                symbol="BTCUSDc",
                direction="short",
                entry_price=60000.0,
                pnl_r=0.0,
                confidence=0.7,
                generated_by="test",
                timestamp=_ts,
            ),
            PnLEvent(
                event_type=EventType.SIGNAL_SETTLED,
                source="live",
                brain_id="brain_with_trades",
                symbol="BTCUSDc",
                direction="long",
                entry_price=60000.0,
                exit_price=61000.0,
                pnl_r=1.67,
                confidence=0.8,
                generated_by="test",
                timestamp=_ts,
            ),
        ]
        with open(stream, "w", encoding="utf-8") as f:
            for e in events:
                f.write(e.model_dump_json() + "\n")

        store = BrainPnLStore.load_from_stream(stream)

        # Brain with only SignalRecorded events must have zero settled trades
        m_sig = store.get_metrics("brain_with_signals_only")
        assert m_sig.sample_count == 0, (
            f"DQAF-060: brain with only SignalRecorded should have 0 trades, "
            f"got {m_sig.sample_count}"
        )

        # Brain with SignalSettled must have exactly 1
        m_trade = store.get_metrics("brain_with_trades")
        assert m_trade.sample_count == 1


class TestDQAF060ProfitFactorInf:
    """DQAF-060: profit_factor must be inf (not 999.0) when gross_loss=0."""

    def test_profit_factor_inf_when_all_wins(self):
        """Contract 2: all-win brain → PF = inf, not 999.0."""
        import math

        store = BrainPnLStore()
        for _ in range(10):
            sid = store.record_signal("all_winner", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 102.0)

        m = store.get_metrics("all_winner")
        assert m.win_rate == 1.0
        assert math.isinf(
            m.profit_factor
        ), f"DQAF-060: all-win profit_factor should be inf, got {m.profit_factor}"
        assert m.profit_factor > 0  # positive infinity

    def test_profit_factor_to_dict_json_safe(self):
        """to_dict() must use None for inf profit_factor — JSON compatible."""
        store = BrainPnLStore()
        for _ in range(10):
            sid = store.record_signal("all_winner", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 102.0)

        m = store.get_metrics("all_winner")
        d = m.to_dict()
        assert (
            d["profit_factor"] is None
        ), f"DQAF-060: to_dict() should serialize inf as None, got {d['profit_factor']}"

    def test_profit_factor_finite_when_has_losses(self):
        """Normal brain with losses → finite PF."""
        store = BrainPnLStore()
        # 6 wins, 2 losses → PF = 12/2 = 6.0 (need >=5 for full metrics path)
        for _ in range(6):
            sid = store.record_signal("normal", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 102.0)
        for _ in range(2):
            sid = store.record_signal("normal", "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 99.0)

        m = store.get_metrics("normal")
        import math

        assert math.isfinite(
            m.profit_factor
        ), f"DQAF-060: brain with losses should have finite PF, got {m.profit_factor}"
        # PF = gross_profit / gross_loss = (6*2) / (2*1) = 12/2 = 6.0
        assert m.profit_factor == pytest.approx(6.0, abs=0.01)


class TestProperties:
    """Property accessors."""

    def test_brain_ids_sorted(self):
        store = BrainPnLStore()
        for b in ["C", "A", "B"]:
            sid = store.record_signal(b, "XAUUSDc", "long", 100.0)
            assert sid is not None
            store.settle_one(sid, 101.0)

        assert store.brain_ids == ["A", "B", "C"]

    def test_total_settled(self):
        store = BrainPnLStore()
        assert store.total_settled == 0

        sid = store.record_signal("B1", "XAUUSDc", "long", 100.0)
        assert sid is not None
        store.settle_one(sid, 101.0)
        assert store.total_settled == 1
