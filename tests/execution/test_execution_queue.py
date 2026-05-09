"""Tests for core/execution/execution_queue.py — staggered dispatch queue."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.execution.execution_queue import (
    ExecutionQueue,
)
from core.execution.portfolio_risk import RiskResult, RiskVerdict
from core.execution.strategy_line import StrategyDecision


def _make_decision(
    strategy: str = "barrier_12bar",
    magic: int = 90001,
    direction: str = "long",
    volume: float = 0.02,
    sl: float = 1990.0,
    tp: float = 2017.5,
) -> StrategyDecision:
    return StrategyDecision(
        strategy_name=strategy,
        magic=magic,
        should_trade=True,
        direction=direction,
        confidence=0.75,
        volume=volume,
        sl=sl,
        tp=tp,
        hard_sl=1985.0,
        brain_ids=["brain_01"],
    )


def _approved_risk(volume: float = 0.02) -> RiskResult:
    return RiskResult(RiskVerdict.APPROVED, adjusted_volume=volume)


def _rejected_risk(reason: str = "gross_exposure") -> RiskResult:
    return RiskResult(RiskVerdict.REJECTED, reason=reason)


class TestExecutionQueue:
    # ── Enqueue ──
    def test_enqueue_adds_to_queue(self):
        eq = ExecutionQueue()
        dec = _make_decision()
        risk = _approved_risk()
        eq.enqueue("barrier_12bar", dec, risk)
        assert eq.queue_size == 1

    def test_enqueue_assigns_priority(self):
        eq = ExecutionQueue()
        dec = _make_decision()
        risk = _approved_risk()
        eq.enqueue("micro_3bar", dec, risk)
        eq.enqueue("barrier_12bar", dec, risk)
        eq.enqueue("unknown_strategy", dec, risk)

        # micro=0, barrier=1, unknown=99
        priorities = [(q.strategy_name, q.priority) for q in eq._queue]
        assert ("micro_3bar", 0) in priorities
        assert ("barrier_12bar", 1) in priorities
        assert ("unknown_strategy", 99) in priorities

    # ── Flush ──
    def test_flush_empty_queue_returns_empty(self):
        eq = ExecutionQueue()
        results = eq.flush(lambda **kw: {})
        assert results == []

    def test_flush_dispatches_in_priority_order(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        eq = ExecutionQueue(stagger_seconds=0)
        dispatch_order: list[str] = []

        def _dispatch(**kw):
            dispatch_order.append(kw["magic"])
            return {}

        # Enqueue in reverse priority order
        dec_statarb = _make_decision(strategy="statarb_dynamic", magic=90003)
        dec_barrier = _make_decision(strategy="barrier_12bar", magic=90001)
        dec_micro = _make_decision(strategy="micro_3bar", magic=90002)

        eq.enqueue("statarb_dynamic", dec_statarb, _approved_risk())
        eq.enqueue("barrier_12bar", dec_barrier, _approved_risk())
        eq.enqueue("micro_3bar", dec_micro, _approved_risk())

        results = eq.flush(_dispatch)
        # micro (0) → barrier (1) → statarb (2)
        assert dispatch_order == [90002, 90001, 90003]
        assert all(r.dispatched for r in results)

    def test_flush_skips_rejected_risk(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        eq = ExecutionQueue(stagger_seconds=0)
        dec = _make_decision()
        eq.enqueue("barrier_12bar", dec, _rejected_risk("test_reject"))

        results = eq.flush(lambda **kw: {})
        assert len(results) == 1
        assert results[0].dispatched is False
        assert "test_reject" in results[0].reason

    def test_flush_calls_dispatch_fn_with_correct_args(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        eq = ExecutionQueue(stagger_seconds=0)
        dec = _make_decision(direction="long", volume=0.03, sl=1995.0, tp=2015.0)
        eq.enqueue("barrier_12bar", dec, _approved_risk(volume=0.03))

        captured: dict = {}

        def _capture(**kw):
            captured.update(kw)
            return {"order_id": 555}

        results = eq.flush(_capture, symbol="XAUUSDc", base_dir="test_data")
        assert captured["side"] == "long"
        assert captured["volume"] == 0.03
        assert captured["magic"] == 90001
        assert captured["stop_loss"] == 1995.0
        assert captured["take_profit"] == 2015.0
        assert captured["brain_ids"] == ["brain_01"]
        assert results[0].dispatched is True
        assert results[0].journal_entry == {"order_id": 555}

    def test_flush_catches_dispatch_error(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        eq = ExecutionQueue(stagger_seconds=0)
        dec = _make_decision()
        eq.enqueue("barrier_12bar", dec, _approved_risk())

        def _fail(**kw):
            raise RuntimeError("MT5 connection lost")

        results = eq.flush(_fail)
        assert len(results) == 1
        assert results[0].dispatched is False
        assert "MT5 connection lost" in results[0].reason

    def test_flush_clears_queue_after_flush(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        eq = ExecutionQueue(stagger_seconds=0)
        eq.enqueue("barrier_12bar", _make_decision(), _approved_risk())
        eq.flush(lambda **kw: {})
        assert eq.queue_size == 0

    def test_stagger_delay_applied(self, monkeypatch):
        sleep_calls: list[float] = []

        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

        eq = ExecutionQueue(stagger_seconds=20.0)
        eq.enqueue(
            "micro_3bar", _make_decision(strategy="micro_3bar", magic=90002), _approved_risk()
        )
        eq.enqueue(
            "barrier_12bar", _make_decision(strategy="barrier_12bar", magic=90001), _approved_risk()
        )

        eq.flush(lambda **kw: {})

        # First dispatch: no sleep. Second: should sleep.
        # With monotonic returning the same value, sleep(20 - 0) = sleep(20)
        assert len(sleep_calls) >= 1

    def test_flush_net_out_patches_dispatch_live_order(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        net_out_risk = RiskResult(
            RiskVerdict.NET_OUT,
            reason="net_out_against_barrier",
            adjusted_volume=0.01,
            net_out_ticket=123,
        )

        eq = ExecutionQueue(stagger_seconds=0)
        dec = _make_decision(strategy="micro_3bar", direction="long")
        eq.enqueue("micro_3bar", dec, net_out_risk)

        import sys

        # Create a mock for scripts.send_live_order.dispatch_live_order
        mock_dispatch_live = MagicMock()
        # Patch where it's imported (inside flush())
        with patch.dict(
            sys.modules,
            {"scripts.send_live_order": MagicMock(dispatch_live_order=mock_dispatch_live)},
        ):
            results = eq.flush(lambda **kw: {"ok": True})
            mock_dispatch_live.assert_called_once()

        assert results[0].dispatched is True

    def test_flush_reduced_handles_existing_before_dispatch(self, monkeypatch):
        monkeypatch.setattr("time.monotonic", lambda: 100.0)
        monkeypatch.setattr("time.sleep", lambda s: None)

        reduced_risk = RiskResult(
            RiskVerdict.REDUCED,
            reason="reduce_existing_barrier",
            adjusted_volume=0.02,
            net_out_ticket=456,
        )

        eq = ExecutionQueue(stagger_seconds=0)
        dec = _make_decision(strategy="micro_3bar")
        eq.enqueue("micro_3bar", dec, reduced_risk)

        import sys

        mock_dispatch_live = MagicMock()
        with patch.dict(
            sys.modules,
            {"scripts.send_live_order": MagicMock(dispatch_live_order=mock_dispatch_live)},
        ):
            results = eq.flush(lambda **kw: {"ok": True})
            net_out_call = mock_dispatch_live.call_args
            assert net_out_call is not None

        assert results[0].dispatched is True
