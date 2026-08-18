"""Tests for core.runtime.position_registration — dispatch registration.

FIX-20260619-034: Tier 1 zero-coverage breakout #5.
Covers register_dispatched_positions with mocked dependencies.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.runtime.position_registration import register_dispatched_positions


def _make_dispatch_result(
    dispatched: bool = True,
    strategy_name: str = "test_swing",
    journal_entry: dict | None = None,
) -> MagicMock:
    dr = MagicMock()
    dr.dispatched = dispatched
    dr.strategy_name = strategy_name
    dr.journal_entry = journal_entry or {"intent_id": "intent_test_001"}
    return dr


def _make_decision(
    direction: str = "long",
    confidence: float = 0.75,
    volume: float = 0.1,
    sl: float = 4650.0,
    tp: float = 4800.0,
    brain_ids: list[str] | None = None,
    total_count: int = 3,
    supporting_count: int = 2,
    magic: int = 90001,
) -> MagicMock:
    d = MagicMock()
    d.direction = direction
    d.confidence = confidence
    d.volume = volume
    d.sl = sl
    d.tp = tp
    d.brain_ids = brain_ids or ["brain_1", "brain_2"]
    d.total_count = total_count
    d.supporting_count = supporting_count
    d.magic = magic
    d.entry_z_score = 0.5
    d.entry_half_life = 12.0
    d.cold_explore = False
    return d


class TestRegisterDispatchedPositions:
    """Tests for register_dispatched_positions."""

    def test_returns_zero_when_exit_management_disabled(self) -> None:
        config = SimpleNamespace(exit_management_enabled=False, no_mt5=True)
        result = register_dispatched_positions(
            config=config,
            position_manager=None,
            known_open_tickets={},
            loop_iteration=1,
            dispatch_results=[],
            eval_summary={},
            brains=[],
            journal_path=None,
            current_atr=6.0,
            mid_price=4700.0,
        )
        assert result["registered_count"] == 0

    def test_returns_zero_when_position_manager_none(self) -> None:
        config = SimpleNamespace(exit_management_enabled=True, no_mt5=True)
        result = register_dispatched_positions(
            config=config,
            position_manager=None,
            known_open_tickets={},
            loop_iteration=1,
            dispatch_results=[],
            eval_summary={},
            brains=[],
            journal_path=None,
            current_atr=6.0,
            mid_price=4700.0,
        )
        assert result["registered_count"] == 0

    def test_returns_zero_when_no_mt5(self) -> None:
        config = SimpleNamespace(exit_management_enabled=True, no_mt5=True)
        pm = MagicMock()
        result = register_dispatched_positions(
            config=config,
            position_manager=pm,
            known_open_tickets={},
            loop_iteration=1,
            dispatch_results=[],
            eval_summary={},
            brains=[],
            journal_path=None,
            current_atr=6.0,
            mid_price=4700.0,
        )
        assert result["registered_count"] == 0

    def test_skips_not_dispatched(self) -> None:
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={},
            position_state_path="/tmp/state.json",
        )
        pm = MagicMock()
        dr = _make_dispatch_result(dispatched=False)
        result = register_dispatched_positions(
            config=config,
            position_manager=pm,
            known_open_tickets={},
            loop_iteration=1,
            dispatch_results=[dr],
            eval_summary={"decisions_map": {}},
            brains=[],
            journal_path=None,
            current_atr=6.0,
            mid_price=4700.0,
        )
        assert result["registered_count"] == 0

    def test_skips_when_no_decision_in_map(self) -> None:
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={},
            position_state_path="/tmp/state.json",
        )
        pm = MagicMock()
        dr = _make_dispatch_result(strategy_name="unknown_strategy")
        result = register_dispatched_positions(
            config=config,
            position_manager=pm,
            known_open_tickets={},
            loop_iteration=1,
            dispatch_results=[dr],
            eval_summary={"decisions_map": {}},
            brains=[],
            journal_path=None,
            current_atr=6.0,
            mid_price=4700.0,
        )
        assert result["registered_count"] == 0

    def test_registers_position_with_valid_data(self) -> None:
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={
                "test_swing": {
                    "tp": {"partial_tp_enabled": False},
                    "exit": {
                        "trail_atr_mult": 2.0,
                        "trail_atr_mult_low": 1.5,
                        "trail_atr_mult_high": 3.0,
                        "breakeven_threshold_atr": 1.0,
                        "trail_activation_atr": 1.0,
                    },
                }
            },
            position_state_path="/tmp/state.json",
            exit_trail_activation_atr=1.0,
        )
        pm = MagicMock()
        decision = _make_decision()
        dr = _make_dispatch_result(journal_entry={"intent_id": "intent_001"})

        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = Path(tmpdir) / "live_trade_journal.jsonl"
            jpath.write_text(
                json.dumps(
                    {
                        "message_id": "intent_001",
                        "position_ticket": 5001,
                        "entry_price": 4700.0,
                        "side": "long",
                        "brain_votes": [{"brain_id": "b1", "direction": "long"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = register_dispatched_positions(
                config=config,
                position_manager=pm,
                known_open_tickets={},
                loop_iteration=5,
                dispatch_results=[dr],
                eval_summary={"decisions_map": {"test_swing": decision}},
                brains=[{"brain_id": "brain_1", "training_horizon": 24}],
                journal_path=jpath,
                current_atr=6.0,
                mid_price=4700.0,
            )

        assert result["registered_count"] == 1
        pm.register_position.assert_called_once()

    def test_injects_sl_min_rr_into_trail_policy(self) -> None:
        """TECH_DEBT-019: sl.min_rr_ratio from the strategy config must reach
        register_position's TrailPolicy.tp_min_rr_ratio — the single injection
        point for the RR contract (position_registration.py L256-259)."""
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={
                "test_swing": {
                    "sl": {"min_rr_ratio": 0.85},
                    "tp": {"partial_tp_enabled": False},
                    "exit": {
                        "trail_atr_mult": 2.0,
                        "trail_atr_mult_low": 1.5,
                        "trail_atr_mult_high": 3.0,
                        "breakeven_threshold_atr": 1.0,
                        "trail_activation_atr": 1.0,
                    },
                }
            },
            position_state_path="/tmp/state.json",
            exit_trail_activation_atr=1.0,
        )
        pm = MagicMock()
        decision = _make_decision()
        dr = _make_dispatch_result(journal_entry={"intent_id": "intent_rr_001"})

        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = Path(tmpdir) / "live_trade_journal.jsonl"
            jpath.write_text(
                json.dumps(
                    {
                        "message_id": "intent_rr_001",
                        "position_ticket": 6001,
                        "entry_price": 4700.0,
                        "side": "long",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = register_dispatched_positions(
                config=config,
                position_manager=pm,
                known_open_tickets={},
                loop_iteration=1,
                dispatch_results=[dr],
                eval_summary={"decisions_map": {"test_swing": decision}},
                brains=[],
                journal_path=jpath,
                current_atr=6.0,
                mid_price=4700.0,
            )

        assert result["registered_count"] == 1
        call_kwargs = pm.register_position.call_args.kwargs
        tp = call_kwargs["trail_policy"]
        assert tp.tp_min_rr_ratio == pytest.approx(0.85)

    def test_trail_policy_min_rr_defaults_zero(self) -> None:
        """No sl.min_rr_ratio in the config → 0.0 → RR contract disabled
        (zero-change for strategies without an RR contract)."""
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={
                "test_swing": {
                    "tp": {"partial_tp_enabled": False},
                    "exit": {
                        "trail_atr_mult": 2.0,
                        "trail_atr_mult_low": 1.5,
                        "trail_atr_mult_high": 3.0,
                        "breakeven_threshold_atr": 1.0,
                        "trail_activation_atr": 1.0,
                    },
                }
            },
            position_state_path="/tmp/state.json",
            exit_trail_activation_atr=1.0,
        )
        pm = MagicMock()
        decision = _make_decision()
        dr = _make_dispatch_result(journal_entry={"intent_id": "intent_rr_000"})

        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = Path(tmpdir) / "live_trade_journal.jsonl"
            jpath.write_text(
                json.dumps(
                    {
                        "message_id": "intent_rr_000",
                        "position_ticket": 6002,
                        "entry_price": 4700.0,
                        "side": "long",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            register_dispatched_positions(
                config=config,
                position_manager=pm,
                known_open_tickets={},
                loop_iteration=1,
                dispatch_results=[dr],
                eval_summary={"decisions_map": {"test_swing": decision}},
                brains=[],
                journal_path=jpath,
                current_atr=6.0,
                mid_price=4700.0,
            )

        call_kwargs = pm.register_position.call_args.kwargs
        assert call_kwargs["trail_policy"].tp_min_rr_ratio == 0.0

    def test_skip_when_ticket_not_in_journal(self) -> None:
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={},
            position_state_path="/tmp/state.json",
        )
        pm = MagicMock()
        decision = _make_decision()
        dr = _make_dispatch_result(journal_entry={"intent_id": "intent_missing"})

        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = Path(tmpdir) / "live_trade_journal.jsonl"
            jpath.write_text("", encoding="utf-8")

            # FIX-20260627-147: DQAF-20260614-008 retry loop
            # (60 × 0.5s = 30s).  Mock time.sleep so the retry
            # loop runs instantly instead of blocking CI for 30s.
            with patch("time.sleep", return_value=None):
                result = register_dispatched_positions(
                    config=config,
                    position_manager=pm,
                    known_open_tickets={},
                    loop_iteration=1,
                    dispatch_results=[dr],
                    eval_summary={"decisions_map": {"test_swing": decision}},
                    brains=[],
                    journal_path=jpath,
                    current_atr=6.0,
                    mid_price=4700.0,
                )

        assert result["registered_count"] == 0

    def test_known_open_tickets_updated(self) -> None:
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={
                "test_swing": {
                    "tp": {"partial_tp_enabled": False},
                    "exit": {
                        "trail_atr_mult": 2.0,
                        "trail_atr_mult_low": 1.5,
                        "trail_atr_mult_high": 3.0,
                        "breakeven_threshold_atr": 1.0,
                        "trail_activation_atr": 1.0,
                    },
                }
            },
            position_state_path="/tmp/state.json",
            exit_trail_activation_atr=1.0,
        )
        pm = MagicMock()
        decision = _make_decision()
        dr = _make_dispatch_result(journal_entry={"intent_id": "intent_002"})
        known_tickets: dict = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = Path(tmpdir) / "live_trade_journal.jsonl"
            jpath.write_text(
                json.dumps(
                    {
                        "message_id": "intent_002",
                        "position_ticket": 5002,
                        "entry_price": 4710.0,
                        "side": "long",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            register_dispatched_positions(
                config=config,
                position_manager=pm,
                known_open_tickets=known_tickets,
                loop_iteration=1,
                dispatch_results=[dr],
                eval_summary={"decisions_map": {"test_swing": decision}},
                brains=[],
                journal_path=jpath,
                current_atr=6.0,
                mid_price=4700.0,
            )

        assert 5002 in known_tickets
        assert known_tickets[5002]["strategy"] == "test_swing"

    def test_uses_dispatch_volume_not_decision_volume(self) -> None:
        """IC 2026-08-07 裁决 2a (Volume Single-Source): registration MUST consume
        DispatchResult.volume — the physical dispatch truth — even when reentry
        decay mutated decision.volume AFTER the risk snapshot.  This pins the
        split-brain root cause: dispatch sent 0.02 (risk.adjusted_volume) while
        registration recorded the decayed 0.01, leaving the book half-exposed.
        """
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={
                "test_swing": {
                    "tp": {"partial_tp_enabled": False},
                    "exit": {
                        "trail_atr_mult": 2.0,
                        "trail_atr_mult_low": 1.5,
                        "trail_atr_mult_high": 3.0,
                        "breakeven_threshold_atr": 1.0,
                        "trail_activation_atr": 1.0,
                    },
                }
            },
            position_state_path="/tmp/state.json",
            exit_trail_activation_atr=1.0,
        )
        pm = MagicMock()
        decision = _make_decision(volume=0.01)  # post-reentry-decay decision volume
        dr = _make_dispatch_result(journal_entry={"intent_id": "intent_split_brain"})
        dr.volume = 0.02  # physical dispatch truth (risk.adjusted_volume snapshot)
        known_tickets: dict = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = Path(tmpdir) / "live_trade_journal.jsonl"
            jpath.write_text(
                json.dumps(
                    {
                        "message_id": "intent_split_brain",
                        "position_ticket": 7777,
                        "entry_price": 4730.0,
                        "side": "long",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = register_dispatched_positions(
                config=config,
                position_manager=pm,
                known_open_tickets=known_tickets,
                loop_iteration=1,
                dispatch_results=[dr],
                eval_summary={"decisions_map": {"test_swing": decision}},
                brains=[],
                journal_path=jpath,
                current_atr=6.0,
                mid_price=4700.0,
            )

        assert result["registered_count"] == 1
        # register_position must receive the PHYSICAL dispatched volume (0.02),
        # not the decayed decision volume (0.01) that created the ghost position.
        call_kwargs = pm.register_position.call_args.kwargs
        assert call_kwargs["volume"] == 0.02
        # known_open_tickets (the reconciliation book) must also carry 0.02.
        assert known_tickets[7777]["volume"] == 0.02

    def test_handles_registration_failure_gracefully(self) -> None:
        config = SimpleNamespace(
            exit_management_enabled=True,
            no_mt5=False,
            strategy_configs={
                "test_swing": {
                    "tp": {"partial_tp_enabled": False},
                    "exit": {
                        "trail_atr_mult": 2.0,
                        "trail_atr_mult_low": 1.5,
                        "trail_atr_mult_high": 3.0,
                        "breakeven_threshold_atr": 1.0,
                        "trail_activation_atr": 1.0,
                    },
                }
            },
            position_state_path="/tmp/state.json",
            exit_trail_activation_atr=1.0,
        )
        pm = MagicMock()
        pm.register_position.side_effect = RuntimeError("registration failed")
        decision = _make_decision()
        dr = _make_dispatch_result(journal_entry={"intent_id": "intent_003"})

        with tempfile.TemporaryDirectory() as tmpdir:
            jpath = Path(tmpdir) / "live_trade_journal.jsonl"
            jpath.write_text(
                json.dumps(
                    {
                        "message_id": "intent_003",
                        "position_ticket": 5003,
                        "entry_price": 4720.0,
                        "side": "long",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = register_dispatched_positions(
                config=config,
                position_manager=pm,
                known_open_tickets={},
                loop_iteration=1,
                dispatch_results=[dr],
                eval_summary={"decisions_map": {"test_swing": decision}},
                brains=[],
                journal_path=jpath,
                current_atr=6.0,
                mid_price=4700.0,
            )

        assert result["registered_count"] == 0
