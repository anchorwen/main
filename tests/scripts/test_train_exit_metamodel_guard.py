"""TDD tests for FIX-20260821-008 (The Consistency Guard — P2 MetaExit).

Two-part fix per IC 雷霆裁决 (2026-08-21):
 ① No hardcoded cross-asset defaults — --snapshots/--journal/--output derive from
    --symbol (xau|btc) via the _SYMBOL_PATHS SSOT, or must be passed explicitly.
    The pre-fix defaults (XAU snapshots + BTC journal) silently mispaired assets:
    311 clean XAU tickets had no BTC close, so only 31 fragments survived and the
    retrain was rejected as "insufficient samples" — a plausible business excuse
    masking a physical routing defect.
 ② Join-retention hard assertion — if fewer than --retention-threshold (50%) of
    distinct snapshot tickets pair with a journal close, training REFUSES with a
    hard exit instead of producing a degraded model.

Only pure argparse/guard logic is tested here — no real data files, no LightGBM.
"""

from __future__ import annotations

import argparse

import pytest

from scripts.training.train_exit_metamodel import (
    _SYMBOL_PATHS,
    _assert_join_retention,
    resolve_args,
)


def _ns(**kwargs) -> argparse.Namespace:
    """Build a Namespace with the fields resolve_args touches."""
    base = {
        "symbol": None,
        "snapshots": None,
        "journal": None,
        "output": None,
        "data_source": "snapshots",
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


# ── ① --symbol derives per-asset paths (no hardcoded defaults) ──


def test_symbol_xau_derives_paths() -> None:
    args = resolve_args(_ns(symbol="xau"))
    assert args.snapshots == _SYMBOL_PATHS["xau"]["snapshots"] == "data/meta_exit_snapshots.jsonl"
    assert args.journal == _SYMBOL_PATHS["xau"]["journal"] == "data/live_trade_journal.jsonl"
    assert args.output == _SYMBOL_PATHS["xau"]["output"] == "data/models/meta_exit_model_v3_xau.txt"


def test_symbol_btc_derives_paths() -> None:
    args = resolve_args(_ns(symbol="btc"))
    assert args.snapshots == "data_btc/meta_exit_snapshots.jsonl"
    assert args.journal == "data_btc/live_trade_journal.jsonl"
    assert args.output == "data_btc/models/meta_exit_model_v3_btc.txt"


def test_explicit_paths_override_symbol() -> None:
    args = resolve_args(
        _ns(symbol="xau", snapshots="/custom/snaps.jsonl", journal="/custom/journal.jsonl")
    )
    assert args.snapshots == "/custom/snaps.jsonl"
    assert args.journal == "/custom/journal.jsonl"
    # output not given → derived from symbol
    assert args.output == _SYMBOL_PATHS["xau"]["output"]


def test_snapshots_mode_requires_symbol_or_paths() -> None:
    with pytest.raises(SystemExit):
        resolve_args(_ns())


def test_snapshots_mode_with_explicit_paths_allowed() -> None:
    args = resolve_args(_ns(snapshots="a.jsonl", journal="b.jsonl"))
    assert args.snapshots == "a.jsonl"
    assert args.journal == "b.jsonl"
    assert args.output == "data/models/meta_exit_model_v2.txt"  # legacy fallback


def test_journal_mode_requires_journal() -> None:
    with pytest.raises(SystemExit):
        resolve_args(_ns(data_source="journal"))


# ── ② Join-retention hard assertion ──


def test_retention_passes_aligned() -> None:
    # 311/359 = 86.6% — the real XAU-snapshots+XAU-journal pairing.
    _assert_join_retention(
        n_paired=311,
        n_snapshot_tickets=359,
        retention_threshold=0.5,
        snapshots_path="data/meta_exit_snapshots.jsonl",
        journal_path="data/live_trade_journal.jsonl",
    )  # no SystemExit


def test_retention_halts_cross_asset_mispair() -> None:
    # 31/359 = 8.6% — the pre-fix default (XAU snapshots + BTC journal).
    with pytest.raises(SystemExit):
        _assert_join_retention(
            n_paired=31,
            n_snapshot_tickets=359,
            retention_threshold=0.5,
            snapshots_path="data/meta_exit_snapshots.jsonl",
            journal_path="data_btc/live_trade_journal.jsonl",
        )


def test_retention_halts_empty_universe() -> None:
    with pytest.raises(SystemExit):
        _assert_join_retention(
            n_paired=0,
            n_snapshot_tickets=0,
            retention_threshold=0.5,
            snapshots_path="data/meta_exit_snapshots.jsonl",
            journal_path="data/live_trade_journal.jsonl",
        )


def test_retention_at_exact_threshold_passes() -> None:
    _assert_join_retention(
        n_paired=5,
        n_snapshot_tickets=10,
        retention_threshold=0.5,
        snapshots_path="a",
        journal_path="b",
    )  # 50% == threshold → allowed
