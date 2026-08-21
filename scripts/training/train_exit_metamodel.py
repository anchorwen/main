#!/usr/bin/env python3
"""Train a meta-model for dynamic exit management.

Two data sources:
  --data-source snapshots  (default, RECOMMENDED)  Read 20-dim ExitFeatureSnapshot
                          from ``data/meta_exit_snapshots.jsonl``, pair with live
                          trade journal for win/loss labels.
  --data-source journal    (legacy, DEPRECATED)    Read 8 journal-level features
                          from open→close trade pairs.

Architecture Committee Directive (2026-06-28):
  The snapshots path MUST produce exactly 20 features matching
  ExitFeatureSnapshot runtime fields.  Any 8-dim journal feature
  contamination triggers ``sys.exit(1)`` — no silent degradation.

Usage:
  python scripts/training/train_exit_metamodel.py --symbol xau
  python scripts/training/train_exit_metamodel.py --symbol btc
  python scripts/training/train_exit_metamodel.py --data-source journal --journal <path>
  python scripts/training/train_exit_metamodel.py --snapshots <path> --journal <path> --output <path>

Output (derived from --symbol, or explicit --output):
  data/models/meta_exit_model_v3_xau.txt        (XAU LightGBM booster)
  data/models/meta_exit_model_v3_xau.meta.json  (feature names + training stats)
  data_btc/models/meta_exit_model_v3_btc.txt    (BTC LightGBM booster)
  data_btc/models/meta_exit_model_v3_btc.meta.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

# ── Architecture Committee: canonical 20-dim ExitFeatureSnapshot field list ──
# These MUST stay in sync with core/execution/meta_exit_engine.py ExitFeatureSnapshot
# and core/execution/position_manager.py _write_meta_exit_telemetry().
EXIT_SNAPSHOT_FEATURE_NAMES: list[str] = [
    # PnL state (5)
    "current_r",
    "prev_r",
    "peak_r",
    "drawdown_r",
    "pnl_pct",
    # Time state (3)
    "cycles_held",
    "expected_horizon",
    "time_ratio",
    # Regime state (5)
    "regime_confidence",
    "trend_aligned",
    "atr_current",
    "atr_entry",
    "atr_expansion",
    # Brain consensus state (4)
    "entry_consensus_score",
    "entry_supporting_count",
    "current_supporting_count",
    "consensus_drift",
    # Context / derived (2)
    "side_short",  # derived: 1.0 if side=="short" else 0.0
    "symbol_btc",  # derived: 1.0 if BTC else 0.0
]

EXIT_SNAPSHOT_FEATURE_DIM = len(EXIT_SNAPSHOT_FEATURE_NAMES)  # MUST == 19

# Corresponding runtime fields in ExitFeatureSnapshot (for cross-reference):
#  5 PnL + 3 Time + 4 Regime(numeric) + 4 Consensus + 2 Context + 1 label = 19
#  This matches meta_exit_engine._runtime_feature_map() output (17 base + 2 derived).

# Journal-level feature names (deprecated — kept for --data-source journal backward compat)
_JOURNAL_FEATURE_NAMES: list[str] = [
    "side_short",
    "sl_distance",
    "tp_distance",
    "rr_ratio",
    "volume",
    "accepted",
    "entry_hour",
    "entry_dow",
]
_JOURNAL_FEATURE_DIM = len(_JOURNAL_FEATURE_NAMES)  # 8

# ── FIX-20260821-008 (The Consistency Guard): per-asset path SSOT ──
# MetaExit snapshot telemetry, the live trade journal, and the model output are
# per-asset. The pre-fix defaults (XAU snapshots + BTC journal) silently mispaired
# assets: 311 clean XAU tickets (86.6% of the universe) had no BTC close, so only
# 31 BTC fragments survived and the retrain was rejected as "insufficient samples"
# — a plausible business excuse masking a physical routing defect (ReB
# CROSS_ASSET_DEFAULT_SILENT_MISPAIR). Paths are now derived from --symbol at
# argparse time; explicit --snapshots/--journal/--output always win.
_SYMBOL_PATHS: dict[str, dict[str, str]] = {
    "xau": {
        "snapshots": "data/meta_exit_snapshots.jsonl",
        "journal": "data/live_trade_journal.jsonl",
        "output": "data/models/meta_exit_model_v3_xau.txt",
    },
    "btc": {
        "snapshots": "data_btc/meta_exit_snapshots.jsonl",
        "journal": "data_btc/live_trade_journal.jsonl",
        "output": "data_btc/models/meta_exit_model_v3_btc.txt",
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# Architecture Committee Hard Assertion: feature dimension gate
# ═══════════════════════════════════════════════════════════════════════════════


def _assert_feature_dimension(feature_names: list[str], source_label: str) -> None:
    """Hard assertion: feature dimension must be 20, not 8.

    If 8-dim journal features are detected, the model would suffer from
    the train-serve feature gap that caused the original June 8 shadow-mode
    demotion.  We refuse to train with contaminated features.
    """
    ndim = len(feature_names)
    if ndim == EXIT_SNAPSHOT_FEATURE_DIM:
        return  # correct
    if ndim == _JOURNAL_FEATURE_DIM:
        print(
            f"\n{'='*70}\n"
            f"  ARCHITECTURE COMMITTEE HALT: Feature Dimension Contamination\n"
            f"  Source: {source_label}\n"
            f"  Detected: {ndim} features (journal-level)\n"
            f"  Required: {EXIT_SNAPSHOT_FEATURE_DIM} features (ExitFeatureSnapshot)\n"
            f"\n"
            f"  These {ndim} journal features have zero predictive value at\n"
            f"  inference time.  Training on them creates the same train-serve\n"
            f"  feature gap that caused the MetaExit shadow-mode demotion on\n"
            f"  2026-06-08.  Use --data-source snapshots instead.\n"
            f"\n"
            f"  Contaminated features: {feature_names}\n"
            f"{'='*70}\n",
            file=sys.stderr,
        )
        sys.exit(1)
    # Unexpected dimension — warn but allow (exploratory)
    print(
        f"[WARNING] Unexpected feature dimension: {ndim} (expected {EXIT_SNAPSHOT_FEATURE_DIM}). "
        f"Proceeding but verify feature alignment manually.",
        file=sys.stderr,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Train meta-model for exit management (19-dim ExitFeatureSnapshot)"
    )
    p.add_argument(
        "--data-source",
        choices=["snapshots", "journal"],
        default="snapshots",
        help="Data source: snapshots (19-dim, recommended) or journal (8-dim, legacy)",
    )
    # ── FIX-20260821-008 (The Consistency Guard): NO hardcoded cross-asset defaults ──
    # The pre-fix defaults (XAU snapshots + BTC journal) silently mispaired assets.
    # Paths now derive from --symbol via the _SYMBOL_PATHS SSOT (or explicit flags).
    p.add_argument(
        "--symbol",
        choices=sorted(_SYMBOL_PATHS),
        default=None,
        help="Asset symbol (xau|btc). Derives --snapshots/--journal/--output defaults "
        "from the per-asset SSOT. Explicit path flags override.",
    )
    p.add_argument(
        "--snapshots",
        default=None,
        help="Path to ExitFeatureSnapshot telemetry JSONL (for --data-source snapshots). "
        "Default derived from --symbol.",
    )
    p.add_argument(
        "--journal",
        default=None,
        help="Path to live trade journal JSONL. Default derived from --symbol.",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Output model path. Default derived from --symbol.",
    )
    p.add_argument(
        "--min-trades",
        type=int,
        default=15,
        help="Minimum trades required to train (>=15 per quality gate)",
    )
    p.add_argument(
        "--retention-threshold",
        type=float,
        default=0.50,
        help="Minimum snapshot→journal join retention (paired/distinct snapshot tickets) "
        "required to train. Below this the two universes are assumed CROSS-ASSET and "
        "training is REFUSED with a hard exit (IC FIX-20260821-008). Default 0.50.",
    )
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    """Derive --snapshots/--journal/--output from --symbol when not explicit.

    FIX-20260821-008 (The Consistency Guard): explicit path flags always win; the
    per-asset table only fills in whatever is still unset. Without --symbol AND
    without explicit paths, argparse errors out rather than guessing an asset.
    """
    if args.symbol:
        _s = _SYMBOL_PATHS[args.symbol]
        if not args.snapshots:
            args.snapshots = _s["snapshots"]
        if not args.journal:
            args.journal = _s["journal"]
        if not args.output:
            args.output = _s["output"]
    elif not args.snapshots or not args.journal:
        # Only the legacy --data-source journal path may run without snapshots.
        # Any snapshots-mode run without both paths refuses to guess an asset.
        _err = argparse.ArgumentParser(description="train_exit_metamodel", add_help=False)
        if args.data_source == "snapshots":
            _err.error(
                "--symbol (xau|btc) is required for --data-source snapshots, or pass "
                "--snapshots AND --journal explicitly. Refusing to guess an asset "
                "(prevents silent cross-asset training)."
            )
        if not args.journal:
            _err.error("--journal is required for --data-source journal.")
    if not args.output:
        # Legacy default preserved only for explicit-path / journal mode.
        args.output = "data/models/meta_exit_model_v2.txt"
    return args


# ═══════════════════════════════════════════════════════════════════════════════
# Data source: ExitFeatureSnapshot telemetry (RECOMMENDED)
# ═══════════════════════════════════════════════════════════════════════════════


def _load_snapshots(path: str) -> list[dict[str, Any]]:
    """Load ExitFeatureSnapshot records from telemetry JSONL."""
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _pair_snapshots_with_labels(
    snapshots: list[dict[str, Any]],
    journal_path: str,
) -> tuple[list[dict[str, Any]], int]:
    """Match snapshot tickets to journal close outcomes for labeling.

    For each unique ticket, uses the LAST snapshot (closest to close)
    and the journal's PnL outcome as the label.

    Returns (paired, n_distinct_snapshot_tickets) — the second value feeds the
    FIX-20260821-008 consistency guard (join-retention rate).
    """
    # Build journal outcome lookup: ticket → PnL
    outcomes: dict[int, float] = {}
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("action") != "close":
                continue
            ticket = rec.get("position_ticket")
            if ticket is None:
                continue
            pnl = float(rec.get("pnl") or 0)
            outcomes[ticket] = pnl

    # Group snapshots by ticket, keep last one
    ticket_snapshots: dict[int, dict[str, Any]] = {}
    for snap in snapshots:
        ticket = snap.get("ticket")
        if ticket is None:
            continue
        # Keep the latest snapshot per ticket (highest timestamp_utc)
        if ticket not in ticket_snapshots or snap.get("timestamp_utc", 0) > ticket_snapshots[
            ticket
        ].get("timestamp_utc", 0):
            ticket_snapshots[ticket] = snap

    # Pair: snapshot features + journal label
    paired: list[dict[str, Any]] = []
    skipped_no_label = 0
    for ticket, snap in ticket_snapshots.items():
        outcome_pnl: float | None = outcomes.get(ticket)
        if outcome_pnl is None:
            skipped_no_label += 1
            continue
        label = 1 if outcome_pnl > 0.01 else 0
        paired.append({"snapshot": snap, "ticket": ticket, "pnl": outcome_pnl, "label": label})

    if skipped_no_label:
        print(f"[INFO] {skipped_no_label} tickets in snapshots have no journal close — skipping.")

    return paired, len(ticket_snapshots)


def _assert_join_retention(
    n_paired: int,
    n_snapshot_tickets: int,
    retention_threshold: float,
    snapshots_path: str,
    journal_path: str,
) -> None:
    """Hard guard: refuse training when the snapshot→journal join is too thin.

    FIX-20260821-008 (The Consistency Guard, IC 雷霆裁决): if fewer than
    ``retention_threshold`` (default 50%) of distinct snapshot tickets pair with a
    journal close, the two universes are almost certainly CROSS-ASSET — e.g. XAU
    snapshots joined against the BTC journal. The pre-fix defaults did exactly
    this silently: 311 XAU tickets (86.6% of the universe) had no BTC close, so
    only 31 BTC fragments survived and the retrain was rejected as "insufficient
    samples" — a plausible business excuse masking a physical routing defect.
    A hard exit beats a silently degraded model (Iron Law: fail loudly).
    """
    if n_snapshot_tickets == 0:
        print(
            f"\n{'='*70}\n"
            f"  CONSISTENCY GUARD HALT: empty snapshot universe\n"
            f"  snapshots: {snapshots_path}\n"
            f"  distinct tickets: 0 — nothing to train.\n"
            f"{'='*70}\n",
            file=sys.stderr,
        )
        sys.exit(1)
    retention = n_paired / n_snapshot_tickets
    if retention < retention_threshold:
        print(
            f"\n{'='*70}\n"
            f"  CONSISTENCY GUARD HALT: cross-asset pairing suspected\n"
            f"  snapshots: {snapshots_path}\n"
            f"  journal   : {journal_path}\n"
            f"  snapshot tickets : {n_snapshot_tickets}\n"
            f"  paired with close: {n_paired}\n"
            f"  join retention   : {retention:.1%}  <  threshold {retention_threshold:.0%}\n"
            f"\n"
            f"  The snapshot and journal universes barely overlap — they are almost\n"
            f"  certainly DIFFERENT ASSETS. Training on this mispair silently produces\n"
            f"  a degraded model with a plausible 'insufficient samples' rejection.\n"
            f"  Refusing to train. Re-run with --symbol xau|btc (or explicit paths)\n"
            f"  that belong to the SAME asset.\n"
            f"{'='*70}\n",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        f"[OK] Join retention {retention:.1%} >= {retention_threshold:.0%} — "
        f"snapshots/journal same-asset confirmed."
    )


def _extract_snapshot_features(pair: dict[str, Any]) -> dict[str, Any]:
    """Extract 20-dim feature vector from an ExitFeatureSnapshot pair.

    Hard assertion: feature count MUST be 20 — rejects 8-dim journal contamination.
    """
    snap = pair["snapshot"].get("snapshot", pair["snapshot"])

    # Derive categorical features
    side_str = str(snap.get("side", "long")).lower()
    side_short = 1.0 if side_str == "short" else 0.0

    symbol_str = str(snap.get("symbol", "")).upper()
    symbol_btc = 1.0 if "BTC" in symbol_str else 0.0

    # trend_aligned is stored as bool in snapshots — convert to float
    ta = snap.get("trend_aligned", True)
    trend_aligned_f = 1.0 if ta in (True, "True", 1, "1") else 0.0

    features: dict[str, float] = {
        # PnL state
        "current_r": float(snap.get("current_r", 0)),
        "prev_r": float(snap.get("prev_r", 0)),
        "peak_r": float(snap.get("peak_r", 0)),
        "drawdown_r": float(snap.get("drawdown_r", 0)),
        "pnl_pct": float(snap.get("pnl_pct", 0)),
        # Time state
        "cycles_held": float(snap.get("cycles_held", 0)),
        "expected_horizon": float(snap.get("expected_horizon", 12)),
        "time_ratio": float(snap.get("time_ratio", 0)),
        # Regime state
        "regime_confidence": float(snap.get("regime_confidence", 0)),
        "trend_aligned": trend_aligned_f,
        "atr_current": float(snap.get("atr_current", 0)),
        "atr_entry": float(snap.get("atr_entry", 0)),
        "atr_expansion": float(snap.get("atr_expansion", 0)),
        # Brain consensus state
        "entry_consensus_score": float(snap.get("entry_consensus_score", 0)),
        "entry_supporting_count": float(snap.get("entry_supporting_count", 0)),
        "current_supporting_count": float(snap.get("current_supporting_count", 0)),
        "consensus_drift": float(snap.get("consensus_drift", 0)),
        # Context (derived)
        "side_short": side_short,
        "symbol_btc": symbol_btc,
    }

    # Architecture Committee hard assertion
    _assert_feature_dimension(list(features.keys()), f"snapshots ticket={pair['ticket']}")

    # PnL and label (from journal pairing)
    features["pnl"] = round(float(pair.get("pnl", 0)), 4)
    features["label"] = int(pair.get("label", 0))

    return features


# ═══════════════════════════════════════════════════════════════════════════════
# Data source: Journal open→close pairs (LEGACY, DEPRECATED)
# ═══════════════════════════════════════════════════════════════════════════════


def _load_journal(path: str) -> list[dict[str, Any]]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _pair_opens_to_closes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    opens: dict[str, dict[str, Any]] = {}
    for r in records:
        if r.get("action") == "open":
            opens[r["message_id"]] = r
    pairs: list[dict[str, Any]] = []
    for r in records:
        if r.get("action") != "close":
            continue
        open_id = r.get("open_message_id", "")
        open_rec = opens.get(open_id)
        if open_rec is None:
            continue
        pairs.append(
            {
                "open": open_rec,
                "close": r,
                "symbol": r.get("symbol", ""),
                "side": open_rec.get("side", ""),
            }
        )
    return pairs


def _extract_journal_features(pair: dict[str, Any]) -> dict[str, Any]:
    """Extract 8 journal-level features.  DEPRECATED — use snapshots path instead."""
    o = pair["open"]
    c = pair["close"]
    side = o.get("side", "long")
    side_short = 1.0 if side == "short" else 0.0
    sl = float(o.get("sl", 0) or 0)
    tp = float(o.get("tp", 0) or 0)
    if side == "long":
        sl_dist = abs(tp - sl) * 0.3636 if tp > sl else abs(tp - sl) * 0.5
        tp_dist = abs(tp - sl) * 0.6364 if tp > sl else abs(tp - sl) * 0.5
    else:
        sl_dist = abs(sl - tp) * 0.3636 if sl > tp else abs(sl - tp) * 0.5
        tp_dist = abs(sl - tp) * 0.6364 if sl > tp else abs(sl - tp) * 0.5
    rr_ratio = tp_dist / max(sl_dist, 0.001)
    volume = float(o.get("volume") or o.get("effective_volume_hint", 0.01) or 0.01)
    accepted = 1.0 if o.get("ack_status") == "accepted" else 0.0
    ts_str = o.get("recorded_at", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        entry_hour, entry_dow = float(ts.hour), float(ts.weekday())
    except (ValueError, OSError):
        entry_hour, entry_dow = 12.0, 3.0
    pnl = float(c.get("pnl") or 0)
    detail = c.get("detail")
    detail_pnl = detail.get("pnl") if isinstance(detail, dict) else None
    close_pnl_raw = float(detail_pnl or pnl or 0)
    label = 1 if close_pnl_raw > 0.01 else 0
    return {
        "side_short": side_short,
        "sl_distance": round(sl_dist, 4),
        "tp_distance": round(tp_dist, 4),
        "rr_ratio": round(rr_ratio, 4),
        "volume": volume,
        "accepted": accepted,
        "entry_hour": entry_hour,
        "entry_dow": entry_dow,
        "pnl": round(close_pnl_raw, 4),
        "label": label,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Training
# ═══════════════════════════════════════════════════════════════════════════════


def train_model(
    X: list[list[float]],
    y: list[int],
    feature_names: list[str],
    output_path: str,
    seed: int = 42,
) -> dict[str, Any]:
    """Train LightGBM binary classifier and save artifacts."""
    import lightgbm as lgb
    import numpy as np

    X_arr = np.array(X, dtype=np.float32)
    y_arr = np.array(y, dtype=np.int32)

    n_pos = int(y_arr.sum())
    n_neg = len(y_arr) - n_pos
    print(f"Training dataset: {len(y_arr)} samples, {n_pos} wins, {n_neg} losses")

    scale_pos_weight = n_neg / max(n_pos, 1)

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "boosting_type": "gbdt",
        "num_leaves": 15,
        "max_depth": 4,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "min_data_in_leaf": 2,
        "scale_pos_weight": scale_pos_weight,
        "verbosity": 1,
        "seed": seed,
        "deterministic": True,
    }

    train_data = lgb.Dataset(X_arr, label=y_arr, feature_name=feature_names)
    print("Training LightGBM...")
    booster = lgb.train(
        params, train_data, num_boost_round=100, valid_sets=[train_data], valid_names=["train"]
    )

    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)
    booster.save_model(output_path)
    print(f"Model saved to {output_path}")

    meta_path = output_path.replace(".txt", ".meta.json")
    importance = booster.feature_importance(importance_type="gain")
    importance_dict = {
        name: float(imp) for name, imp in zip(feature_names, importance, strict=False)
    }

    meta = {
        "trained_at": datetime.now(UTC).isoformat(),
        "n_samples": len(y_arr),
        "n_wins": n_pos,
        "n_losses": n_neg,
        "win_rate": round(n_pos / max(len(y_arr), 1), 4),
        "feature_names": feature_names,
        "feature_dim": len(feature_names),
        "feature_importance_gain": importance_dict,
        "scale_pos_weight": scale_pos_weight,
        "params": {k: v for k, v in params.items() if k != "seed"},
        "data_source": "ExitFeatureSnapshot"
        if len(feature_names) == EXIT_SNAPSHOT_FEATURE_DIM
        else "journal",
        "architecture_committee_assertion": "19-dim hard assertion PASSED (matches _runtime_feature_map)",
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    print(f"Metadata saved to {meta_path}")
    return meta


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    # FIX-20260821-008: GBK console crashes on the ✅ emoji below AFTER artifacts
    # are saved — producing a misleading exit code 1 on an otherwise-successful
    # retrain. Reconfigure stdout AND stderr to UTF-8 so the quality-gate verdict
    # and the CONSISTENCY GUARD HALT message print cleanly (no mojibake on GBK).
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

    args = parse_args()
    args = resolve_args(args)

    if args.data_source == "snapshots":
        # ── 19-dim ExitFeatureSnapshot path (RECOMMENDED) ──
        if not os.path.exists(args.snapshots):
            print(f"Snapshots file not found: {args.snapshots}")
            sys.exit(1)
        if not os.path.exists(args.journal):
            print(f"Journal file not found: {args.journal} (required for labeling)")
            sys.exit(1)

        snapshots = _load_snapshots(args.snapshots)
        print(f"Loaded {len(snapshots)} ExitFeatureSnapshot records from {args.snapshots}")

        pairs, n_snapshot_tickets = _pair_snapshots_with_labels(snapshots, args.journal)
        print(f"Paired {len(pairs)} tickets with journal outcomes")

        # FIX-20260821-008 (The Consistency Guard): hard-fail on cross-asset mispair
        # BEFORE the "insufficient samples" soft exit can mask a routing defect.
        _assert_join_retention(
            len(pairs),
            n_snapshot_tickets,
            args.retention_threshold,
            args.snapshots,
            args.journal,
        )

        if len(pairs) < args.min_trades:
            print(
                f"Not enough paired trades ({len(pairs)} < {args.min_trades}). Skipping training."
            )
            sys.exit(0)

        X: list[list[float]] = []
        y: list[int] = []
        feature_names = EXIT_SNAPSHOT_FEATURE_NAMES

        for pair in pairs:
            feats = _extract_snapshot_features(pair)
            row = [feats[name] for name in feature_names]
            X.append(row)
            y.append(feats["label"])

        # Architecture Committee hard assertion: feature dimension
        _assert_feature_dimension(feature_names, f"snapshots final ({len(pairs)} trades)")

    else:
        # ── 8-dim journal path (LEGACY, DEPRECATED) ──
        print(
            "\n  ⚠️  WARNING: --data-source journal is DEPRECATED.\n"
            "     This uses 8 journal-level features with a known train-serve gap.\n"
            "     Use --data-source snapshots for the 20-dim institutional path.\n",
            file=sys.stderr,
        )
        if not os.path.exists(args.journal):
            print(f"Journal not found: {args.journal}")
            sys.exit(1)

        records = _load_journal(args.journal)
        print(f"Loaded {len(records)} journal records")
        pairs = _pair_opens_to_closes(records)
        print(f"Paired {len(pairs)} open→close trades")

        if len(pairs) < args.min_trades:
            print(
                f"Not enough closed trades ({len(pairs)} < {args.min_trades}). Skipping training."
            )
            sys.exit(0)

        X = []
        y = []
        feature_names = _JOURNAL_FEATURE_NAMES
        for pair in pairs:
            feats = _extract_journal_features(pair)
            row = [feats[name] for name in feature_names]
            X.append(row)
            y.append(feats["label"])

        # This WILL fail for snapshots path — by design
        _assert_feature_dimension(feature_names, "journal (legacy)")

    win_count = sum(y)
    print(
        f"Feature matrix: {len(X)} rows × {len(feature_names)} cols, wins={win_count}, losses={len(y)-win_count}"
    )

    if win_count < 2:
        print(f"Insufficient winning trades (need >= 2, got {win_count}). Skipping training.")
        sys.exit(0)

    meta = train_model(X, y, feature_names, args.output, seed=args.seed)

    print(f"\n{'='*60}")
    if meta["n_wins"] >= 15 and meta["win_rate"] >= 0.20:
        print(
            f"✅ Quality gate PASSED: {meta['n_wins']} wins, {meta['win_rate']:.2%} WR, {len(feature_names)} features."
        )
    else:
        print(
            f"⚠️  Quality gate NOT MET: n_wins={meta['n_wins']} (need ≥15), win_rate={meta['win_rate']:.2%} (need ≥20%)."
        )
        print("   Model saved but will be REJECTED by MetaExitEngine.load_model().")
        print(
            f"   Current accumulation: {len(y)} paired trades. Retrain when >=15 wins accumulated."
        )
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
