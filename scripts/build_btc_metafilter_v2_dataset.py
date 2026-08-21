#!/usr/bin/env python3
"""Build BTC MetaFilter V2 training dataset with PIT-aligned features.

MLOps Iron Law #1 (Point-in-Time / ASOF Join):
  For each trade open time T, find the LAST feature record with
  event_time <= T.  Never use features from AFTER the trade opened.
  This prevents lookahead bias (data leakage).

MLOps Iron Law #2 (Label Engineering with Damping):
  y=1 only when PnL > spread_cost * 1.5.  Breakeven and micro-loss
  trades are labeled y=0 — the model learns to avoid friction noise.

MLOps Iron Law #3 (ASOF Tolerance — Stale Feature Rejection):
  If the best ASOF-matched feature is older than max_lookback_minutes,
  the trade is DROPPED.  Without this, a feature engine outage at 08:00
  would silently attach 6-hour-old features to a 14:00 trade — producing
  garbage training labels whose market context is unknowably stale.

FIX-20260621-028: Added --feature-contract (default v9_institutional_40),
  --max-lookback-minutes (default 15), removed micro-feature merge for
  40-dim contract (micro coverage is only 10% — mostly zeros that dilute
  signal), and added ASOF tolerance check between binary search and
  Column 3 knowledge-time filter.

Usage:
  python scripts/build_btc_metafilter_v2_dataset.py --data-dir data_btc
  python scripts/build_btc_metafilter_v2_dataset.py --data-dir data_btc --feature-contract v9_institutional_40
  python scripts/build_btc_metafilter_v2_dataset.py --data-dir data_btc --max-lookback-minutes 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any, NoReturn

import numpy as np

# Final close actions — a trade is "closed" when one of these records its last
# entry.  Module-level so load_journal_opens and journal_universe_stats agree
# on the SAME closed universe (TECH_DEBT-021).
FINAL_CLOSE_ACTIONS = {
    "close",
    "loss",
    "win",
    "close_accepted",
    "sl_hit_first",
    "tp_hit_first",
    "breakeven",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build BTC MetaFilter V2 PIT-aligned dataset")
    p.add_argument("--data-dir", default="data_btc", help="Data directory")
    p.add_argument("--symbol", default="BTCUSDc", help="Trading symbol")
    p.add_argument("--output", default=None, help="Output NPZ path")
    p.add_argument(
        "--spread-cost-usd",
        type=float,
        default=2.50,
        help="Estimated round-trip spread+slippage cost in USD",
    )
    p.add_argument(
        "--pnl-threshold-mult",
        type=float,
        default=1.5,
        help="Damping multiplier on spread cost for y=1",
    )
    p.add_argument(
        "--feature-contract",
        default="v9_institutional_40",
        help="Feature contract name (default: v9_institutional_40 for 40-dim V9)",
    )
    p.add_argument(
        "--max-lookback-minutes",
        type=int,
        default=15,
        help="Max minutes between trade open and matched feature (default: 15). "
        "Trades with older features are dropped to avoid stale-data contamination.",
    )
    return p.parse_args()


def _load_journal_by_ticket(data_dir: str) -> dict[int, list[dict]]:
    """Load journal grouped by position_ticket.

    Shared by load_journal_opens (trade extraction) and journal_universe_stats
    (TECH_DEBT-021 readiness denominators) so both count the SAME universe.
    """
    jl_path = os.path.join(data_dir, "live_trade_journal.jsonl")
    by_ticket: dict[int, list[dict]] = {}
    if os.path.exists(jl_path):
        with open(jl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tkt = rec.get("position_ticket")
                if tkt:
                    by_ticket.setdefault(int(tkt), []).append(rec)
    return by_ticket


def journal_universe_stats(data_dir: str) -> dict[str, Any]:
    """Distinct-ticket journal universe — SSOT for readiness metric denominators.

    TECH_DEBT-021 (The Denominator Alignment): readiness previously divided the
    asof_join_rate by raw ``ack_status=="closed"`` journal ENTRIES, which
    double-count duplicate/orphan closes and inflated the denominator ~3.7x
    (4697 entries vs 1262 distinct PnL-bearing trades → the false 22.3%).  These
    counts are written to the builder Report JSON sidecar and read back by
    check_training_readiness.py as the authoritative denominators.

    Returns:
        dict with journal_tickets / distinct_closed_trades / orphan_close_trades /
        manual_close_trades / valid_trades_count / real_closed_trades_count.
    """
    by_ticket = _load_journal_by_ticket(data_dir)
    n_tickets = len(by_ticket)
    distinct_closed = 0
    orphan_close = 0
    manual_close = 0
    valid_trades = 0
    real_closed = 0
    for recs in by_ticket.values():
        opens = [r for r in recs if r.get("action") == "open"]
        closes = [r for r in recs if r.get("action") in FINAL_CLOSE_ACTIONS]
        if not closes:
            continue
        distinct_closed += 1
        close_rec = max(closes, key=lambda r: r.get("recorded_at", "z"))
        has_open = bool(opens)
        is_manual = close_rec.get("label") == "manual_close"
        if not has_open:
            orphan_close += 1
        if is_manual:
            manual_close += 1
        if has_open and not is_manual:
            real_closed += 1
        pnl = close_rec.get("pnl")
        if pnl is None:
            pnl = close_rec.get("detail", {}).get("pnl")
        if pnl is not None:
            valid_trades += 1
    return {
        "journal_tickets": n_tickets,
        "distinct_closed_trades": distinct_closed,
        "orphan_close_trades": orphan_close,
        "manual_close_trades": manual_close,
        "valid_trades_count": valid_trades,
        "real_closed_trades_count": real_closed,
    }


def load_journal_opens(data_dir: str) -> list[dict[str, Any]]:
    """Extract open entries with close PnL from live_trade_journal.

    DQAF-20260614-004: Also propagate p_win and ou_z_entry from the open
    entry so they can be used as features in the MetaFilter dataset.
    """
    jl_path = os.path.join(data_dir, "live_trade_journal.jsonl")
    if not os.path.exists(jl_path):
        print(f"ERROR: {jl_path} not found")
        return []

    # First pass: collect all entries grouped by position_ticket
    by_ticket = _load_journal_by_ticket(data_dir)

    # Build open→p_win lookup from accepted entries
    open_pwin: dict[str, float] = {}  # message_id → p_win
    open_ou_z: dict[str, float] = {}  # message_id → ou_z_entry
    for recs in by_ticket.values():
        for r in recs:
            if r.get("action") == "open" and r.get("ack_status") == "accepted":
                mid = r.get("message_id", "")
                pw = r.get("p_win")
                if pw is not None and mid:
                    open_pwin[mid] = float(pw)
                # Extract ou_z_entry from entry_context
                ec = r.get("entry_context", {})
                if isinstance(ec, dict):
                    oz = ec.get("ou_z_entry", ec.get("ou_z_score", ec.get("z_score")))
                    if oz is not None and mid:
                        open_ou_z[mid] = float(oz)

    # Build trade records: open + final close
    trades = []
    for tkt, recs in by_ticket.items():
        opens = [r for r in recs if r.get("action") == "open"]
        if not opens:
            continue
        open_rec = opens[0]
        open_ts = open_rec.get("recorded_at", "")
        entry_price = open_rec.get("detail", {}).get("entry_price") or open_rec.get(
            "entry_price", 0
        )
        side = open_rec.get("side", "")
        volume = open_rec.get("volume", 0)
        open_mid = open_rec.get("message_id", "")

        # Find final close (module-level FINAL_CLOSE_ACTIONS = SSOT closed set)
        closes = [r for r in recs if r.get("action") in FINAL_CLOSE_ACTIONS]
        if not closes:
            continue
        # Use last close (in case of duplicates)
        close_rec = max(closes, key=lambda r: r.get("recorded_at", "z"))
        pnl = close_rec.get("pnl")
        if pnl is None:
            pnl = close_rec.get("detail", {}).get("pnl")
        if pnl is None:
            continue

        # Propagate p_win and ou_z_entry from open entry
        p_win = open_pwin.get(open_mid, 0.5)  # default neutral
        ou_z_entry = open_ou_z.get(open_mid, 0.0)

        trades.append(
            {
                "ticket": tkt,
                "open_time": open_ts,
                "entry_price": float(entry_price) if entry_price else 0,
                "side": side,
                "volume": float(volume) if volume else 0,
                "pnl": float(pnl),
                "close_label": close_rec.get("label", ""),
                "p_win": p_win,
                "ou_z_entry": ou_z_entry,
            }
        )

    pwin_ok = sum(1 for t in trades if t["p_win"] != 0.5)
    print(
        f"Journal: {len(by_ticket)} tickets, {len(trades)} with PnL "
        f"({pwin_ok} with signal p_win)"
    )
    return trades


def load_feature_store(
    data_dir: str,
    symbol: str = "BTCUSDc",
    feature_contract: str = "v9_institutional_40",
) -> list[dict[str, Any]]:
    """Load feature store records for the given contract.

    FIX-20260621-028: When contract is v9_institutional_40, only load v9
    records — skip micro-feature merge.  Micro features (v4.3_microstructure_9)
    have only ~10% coverage in the feature store, so 90% of values would be
    0.0 filler that dilutes the 40 real V9 features.

    When contract is the full 47-dim variant, merge v9 + micro at matching
    timestamps (legacy behavior, retained for backwards compatibility).
    """
    fs_path = os.path.join(
        data_dir,
        "feature_store",
        "records",
        f"symbol={symbol}",
        "timeframe=M5",
        "features.jsonl",
    )
    if not os.path.exists(fs_path):
        print(f"ERROR: {fs_path} not found")
        return []

    v9_records: dict[str, dict[str, Any]] = {}
    micro_records: dict[str, dict[str, Any]] = {}
    raw_count = 0
    with open(fs_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw_count += 1
            et = rec.get("event_time", "")
            schema = rec.get("schema_name", "")
            if schema == "v9_institutional_40":
                v9_records[et] = rec
            elif schema == "v4.3_microstructure_9":
                micro_records[et] = rec

    # ── FIX-20260621-028: 40-dim contract → skip micro merge ──
    if feature_contract == "v9_institutional_40":
        merged = []
        for et, v9_rec in sorted(v9_records.items()):
            v9_vals = v9_rec.get("values", {})
            if not isinstance(v9_vals, dict) or len(v9_vals) < 40:
                continue
            _ingested = v9_rec.get("ingested_at", "")
            merged.append(
                {
                    "event_time": et,
                    "ingested_at": _ingested,
                    "values": dict(v9_vals),
                }
            )
        print(
            f"Feature store: {raw_count} raw records → {len(v9_records)} v9 "
            f"(40-dim contract, micro skipped)"
        )
        return merged

    # ── 47-dim contract: merge v9 + micro at matching timestamps ──
    MICRO_FIELDS = ["tick_return", "hl_ratio", "co_ratio", "avg_spread", "OIM", "tick_velocity"]
    merged = []
    merged_count = 0
    for et, v9_rec in sorted(v9_records.items()):
        v9_vals = v9_rec.get("values", {})
        if not isinstance(v9_vals, dict) or len(v9_vals) < 40:
            continue
        merged_vals = dict(v9_vals)
        micro_rec = micro_records.get(et)
        if micro_rec is not None:
            micro_vals = micro_rec.get("values", {})
            if isinstance(micro_vals, dict):
                for mf in MICRO_FIELDS:
                    merged_vals[mf] = float(micro_vals.get(mf, 0.0))
                merged_count += 1
        for mf in MICRO_FIELDS:
            if mf not in merged_vals:
                merged_vals[mf] = 0.0
        _ingested = v9_rec.get("ingested_at", "")
        merged.append(
            {
                "event_time": et,
                "ingested_at": _ingested,
                "values": merged_vals,
            }
        )

    print(
        f"Feature store: {raw_count} raw records → {len(v9_records)} v9 + "
        f"{len(micro_records)} micro → {len(merged)} merged "
        f"({merged_count} with micro, {len(merged) - merged_count} without)"
    )
    return merged


def load_contract_feature_names(
    data_dir: str, feature_contract: str = "v9_institutional_40"
) -> list[str]:
    """Load feature names for the given contract.

    FIX-20260621-028: For v9_institutional_40, load directly from schema
    registry.  For 47-dim contracts, load from the training pipeline contract.
    """
    if feature_contract == "v9_institutional_40":
        return _load_v9_feature_names_from_registry()

    # Legacy: load from 47-dim contract
    contract_path = os.path.join("configs", "contracts", "training_pipeline_btc_metafilter_v3.json")
    if not os.path.exists(contract_path):
        print("WARNING: contract not found, falling back to v9 feature names")
        return _load_v9_feature_names_from_registry()
    with open(contract_path, encoding="utf-8") as f:
        contract = json.load(f)
    feature_names = contract.get("model_target", {}).get("feature_names_ssot", [])
    print(
        f"Contract features: {len(feature_names)} dim (from training_pipeline_btc_metafilter_v3.json)"
    )
    return list(feature_names)


def _load_v9_feature_names_from_registry() -> list[str]:
    """Load canonical v9_institutional_40 feature names from schema SSOT."""
    from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

    names = list(V9_INSTITUTIONAL_40_FEATURES)[:40]
    if len(names) == 40:
        print(f"Schema SSOT: {len(names)} features (v9_institutional_40)")
        return names
    print("WARNING: schema returned < 40 features, dataset may be incomplete")
    return names


def asof_join(
    trades: list[dict[str, Any]],
    features: list[dict[str, Any]],
    contract_feature_names: list[str],
    max_lookback_seconds: int = 900,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]], dict[str, Any]]:
    """PIT ASOF join with tolerance + knowledge-time filtering.

    MLOps Iron Law #1: backward-looking join only.  Never use future data.

    MLOps Iron Law #3 (FIX-20260621-028): ASOF tolerance.
      After binary search finds the best feature with event_time <= open_dt,
      check that the gap (open_dt - event_time) <= max_lookback_seconds.
      Without this check, a feature engine outage at 08:00 would silently
      match 6-hour-old features to a 14:00 trade — producing garbage labels.

    Column 3 — Knowledge-Time Filtering (Look-Ahead Bias Elimination):
      For each trade at time T, we can only use features whose:
        (a) event_time  <= T  (the feature describes a state at or before T)
        (b) ingested_at <= T  (the system KNEW about this feature at time T)
    """
    # ── Parse feature timestamps (event_time + ingested_at) ──
    feat_entries: list[tuple[datetime, datetime | None, int]] = []
    for i, f in enumerate(features):
        et = f.get("event_time", "")
        it = f.get("ingested_at", "")
        if not et:
            continue
        try:
            event_dt = datetime.fromisoformat(str(et)[:26])
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
        ingested_dt = None
        if it:
            try:
                ingested_dt = datetime.fromisoformat(str(it)[:26])
                if ingested_dt.tzinfo is None:
                    ingested_dt = ingested_dt.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                pass
        feat_entries.append((event_dt, ingested_dt, i))

    # Sort by event_time for binary search
    feat_entries.sort(key=lambda x: x[0])

    X_rows = []
    y_rows = []
    meta_rows = []
    matched = 0
    skipped_future = 0
    skipped_missing = 0
    skipped_not_known = 0
    skipped_stale = 0

    join_stats: dict[str, Any] = {}

    for trade in trades:
        open_ts = trade["open_time"]
        if not open_ts:
            skipped_missing += 1
            continue
        try:
            open_dt = datetime.fromisoformat(str(open_ts)[:26])
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            skipped_missing += 1
            continue

        # ── Binary search: find last feature with event_time <= open_dt ──
        lo, hi = 0, len(feat_entries) - 1
        best_idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if feat_entries[mid][0] <= open_dt:
                best_idx = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if best_idx < 0:
            skipped_future += 1
            continue

        # ── FIX-20260621-028: ASOF tolerance — reject stale features ──
        _best_event_dt = feat_entries[best_idx][0]
        _gap_seconds = (open_dt - _best_event_dt).total_seconds()
        if _gap_seconds > max_lookback_seconds:
            skipped_stale += 1
            continue

        # ── Column 3: Knowledge-time filter ──
        usable_idx = -1
        for candidate_idx in range(best_idx, -1, -1):
            _event_dt, _ingested_dt, _feat_i = feat_entries[candidate_idx]
            if _ingested_dt is None:
                usable_idx = candidate_idx
                break
            if _ingested_dt <= open_dt:
                usable_idx = candidate_idx
                break

        if usable_idx < 0:
            skipped_not_known += 1
            continue

        feat_idx = feat_entries[usable_idx][2]
        feat = features[feat_idx]
        values = feat.get("values", {})
        if not values or not isinstance(values, dict):
            skipped_missing += 1
            continue

        # Extract features in CONTRACT order
        feat_vec = []
        for fn in contract_feature_names:
            if fn == "ou_z_entry":
                feat_vec.append(float(trade.get("ou_z_entry", 0.0)))
            else:
                feat_vec.append(float(values.get(fn, 0.0)))

        if len(feat_vec) != len(contract_feature_names):
            skipped_missing += 1
            continue

        X_rows.append(feat_vec)
        y_rows.append(trade["pnl"])
        meta_rows.append(
            {
                "ticket": trade["ticket"],
                "open_time": open_ts,
                "feature_time": feat.get("event_time", ""),
                "gap_seconds": round(_gap_seconds, 1),
                "pnl": trade["pnl"],
                "side": trade["side"],
                "entry_price": trade["entry_price"],
                "volume": trade["volume"],
                "close_label": trade["close_label"],
                "p_win": trade.get("p_win", 0.5),
                "ou_z_entry": trade.get("ou_z_entry", 0.0),
            }
        )
        matched += 1

    print(
        f"ASOF join: {matched} matched, {skipped_future} no prior feature, "
        f"{skipped_stale} stale (gap > {max_lookback_seconds//60}min), "
        f"{skipped_not_known} not-yet-known, "
        f"{skipped_missing} missing data"
    )
    join_stats = {
        "matched_samples": matched,
        "skipped_future": skipped_future,
        "skipped_stale": skipped_stale,
        "skipped_not_known": skipped_not_known,
        "skipped_missing": skipped_missing,
        "max_lookback_seconds": max_lookback_seconds,
    }
    if matched == 0:
        return np.array([]), np.array([]), [], join_stats

    return np.array(X_rows), np.array(y_rows), meta_rows, join_stats


def apply_labels(
    y_pnl: np.ndarray,
    spread_cost: float,
    threshold_mult: float,
) -> np.ndarray:
    """MLOps Iron Law #2: Damping threshold for binary labels.

    y=1: PnL > spread_cost * threshold_mult  (clear winner)
    y=0: PnL <= spread_cost * threshold_mult  (loser or friction noise)
    """
    threshold = spread_cost * threshold_mult
    y_binary = (y_pnl > threshold).astype(np.int32)

    n_win = int(y_binary.sum())
    n_loss = len(y_binary) - n_win
    print(
        f"Labels: {n_win} wins (PnL > ${threshold:.2f}), {n_loss} non-wins "
        f"(WR={n_win/max(len(y_binary),1)*100:.1f}%)"
    )
    return y_binary


def _fail(msg: str) -> NoReturn:
    """Fail-fast generator (TECH_DEBT-020, The Fail-Fast Generator).

    The builder must NEVER exit 0 when it produced no usable dataset. A silent
    rc=0 leaves the output file empty (or missing), which downstream consumers
    (check_training_readiness.py) cannot distinguish from a real dataset —
    historically np.load on the empty NPZ raised EOFError.
    """
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    args = parse_args()
    data_dir = args.data_dir
    feature_contract = args.feature_contract
    max_lookback_seconds = args.max_lookback_minutes * 60

    print("=" * 60)
    print("  BTC MetaFilter V2 — PIT Dataset Builder")
    print(f"  Contract: {feature_contract}")
    print(f"  ASOF tolerance: {args.max_lookback_minutes} min")
    print("=" * 60)

    symbol = args.symbol
    trades = load_journal_opens(data_dir)
    if not trades:
        _fail(
            f"no open journal entries found in {data_dir}/live_trade_journal.jsonl — "
            "cannot build dataset"
        )

    features = load_feature_store(data_dir, symbol, feature_contract=feature_contract)
    if not features:
        _fail(
            f"no feature records found for symbol={symbol} in {data_dir}/feature_store — "
            "cannot build dataset (silent rc=0 was the TECH_DEBT-020 EOFError root cause)"
        )

    contract_names = load_contract_feature_names(data_dir, feature_contract=feature_contract)
    if not contract_names:
        _fail("no feature names available — cannot build dataset")
    print(f"Target features: {len(contract_names)} dim")

    X, y_pnl, meta, join_stats = asof_join(
        trades,
        features,
        contract_names,
        max_lookback_seconds=max_lookback_seconds,
    )
    if len(X) == 0:
        _fail("no samples after ASOF join — cannot build dataset")

    y = apply_labels(y_pnl, args.spread_cost_usd, args.pnl_threshold_mult)

    # ── Direction balance check ──
    n_long = sum(1 for m in meta if m["side"] == "long")
    n_short = sum(1 for m in meta if m["side"] == "short")
    if len(meta) > 0:
        print(
            f"Direction balance: LONG={n_long} ({n_long/len(meta)*100:.0f}%), "
            f"SHORT={n_short} ({n_short/len(meta)*100:.0f}%)"
        )

    # ── Save ──
    sym_tag = symbol.lower().replace("usdc", "").replace("usd", "")
    output = args.output or os.path.join(data_dir, "training", f"meta_features_{sym_tag}_v2.npz")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    np.savez_compressed(
        output,
        X=X,
        y=y,
        y_pnl=y_pnl,
        feature_names=np.array(contract_names, dtype=str),
        meta_tickets=np.array([m["ticket"] for m in meta]),
        spread_cost=args.spread_cost_usd,
        pnl_threshold=args.spread_cost_usd * args.pnl_threshold_mult,
    )
    print(f"\nDataset saved: {output}")
    print(f"  X shape: {X.shape}")
    print(f"  y distribution: {dict(zip(*np.unique(y, return_counts=True), strict=False))}")

    # ── TECH_DEBT-021: Report JSON sidecar (readiness denominator SSOT) ──
    # The builder's distinct-ticket universe is the authoritative denominator for
    # readiness's asof_join_rate / pnl_completeness.  Writing it to a sidecar next
    # to the NPZ (and reading it back in check_training_readiness.py) replaces the
    # old raw ack_status=="closed" entry count that inflated the denominator 3.7x
    # and fabricated the 22.3% asof_join_rate.
    report = {
        "symbol": symbol,
        "feature_contract": feature_contract,
        "built_at": datetime.now(UTC).isoformat(),
        "max_lookback_seconds": max_lookback_seconds,
        "journal": journal_universe_stats(data_dir),
        "asof": join_stats,
    }
    report_path = os.path.splitext(output)[0] + ".report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved: {report_path}")
    print(
        f"  valid_trades_count={report['journal']['valid_trades_count']} "
        f"(readiness asof_join_rate denominator, SSOT)"
    )

    # ── Dimension contract verification ──
    expected_dim = 40 if feature_contract == "v9_institutional_40" else 47
    if X.shape[1] != expected_dim:
        print(
            f"  [CONTRACT VIOLATION] Dataset has {X.shape[1]} dim, contract requires {expected_dim}!"
        )
    else:
        print(f"  [CONTRACT OK] Dataset dimension matches contract ({expected_dim} dim)")

    # Print PnL distribution for diagnostics
    pnl_sorted = sorted(y_pnl)
    print(
        f"\nPnL distribution: min={pnl_sorted[0]:+.2f}, "
        f"median={pnl_sorted[len(pnl_sorted)//2]:+.2f}, max={pnl_sorted[-1]:+.2f}"
    )
    print(
        f"  Wins above threshold: {int((y_pnl > args.spread_cost_usd * args.pnl_threshold_mult).sum())}"
    )
    print(
        f"  Breakeven zone (0 to threshold): "
        f"{int(((y_pnl > 0) & (y_pnl <= args.spread_cost_usd * args.pnl_threshold_mult)).sum())}"
    )
    print(f"  Losses: {int((y_pnl <= 0).sum())}")

    print("\n[DONE] All statistics above are the sole source of truth.")


if __name__ == "__main__":
    main()
