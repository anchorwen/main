# type: ignore
from __future__ import annotations

#!/usr/bin/env python3
"""
Live Trading Journal Auditor — Iron Law #11 compliant.

Statistical audit script for live trading data.
All conclusions must be based SOLELY on the stdout output of this script.

---
Dedup logic:
  - Trade journal: group by position identity (immutable position_identifier,
    falling back to the mutable position_ticket for legacy records), take the
    LAST close event with
    non-null PnL as the realized outcome.
  - Position snapshots: group by ticket, analyze per-snapshot trailing_sl_distance
    relative to current_atr.

Win rate definition:
  - PnL > 0 → win
  - PnL < 0 → loss
  - PnL == 0 → breakeven (excluded from win rate numerator and denominator unless
    explicitly noted)

PnL calculation:
  - Sum of PnL from realized (deduped) closes. USD.

Trailing SL:
  - trailing_sl_distance / current_atr per snapshot, averaged per ticket, then
    grand average across all tickets.
  - A snapshot with trailing_sl_distance=0 means SL was inactive (not trailing)
    at that bar.

Direction distribution:
  - From entry_context.brain_predictions[] of OPEN actions.
  - Counts per direction_bias: long/short/neutral.

Usage:
  python scripts/analyze_live_journal.py --data-dir data_btc
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.data.ticket_resolver import resolve_identity  # noqa: E402


def load_jsonl(path: Path) -> list[dict]:
    """Load a JSONL file, skipping blank lines."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _coalesce(mapping: dict, key: str, default: str) -> str:
    """Return ``mapping[key]``, treating a present-but-null value like a missing key.

    ``dict.get(key, default)`` substitutes *default* ONLY when *key* is absent;
    a key present with a ``None`` value still returns ``None``.  Live journal
    records legitimately carry ``label: null`` (e.g. the no-ticket orphan branch,
    pre FIX-20260626-144 write-side hardening), so a bare ``.get(k, default)`` let
    ``None`` propagate into ``pnl_by_label`` keys and formatted output, crashing
    ``format(None, ':s')`` (TypeError). Normalising categorical fields at this
    ingestion boundary keeps every downstream consumer null-free — the single
    root-cause point, versus re-guarding each print site (FIX-20260613-066).
    """
    value = mapping.get(key)
    return default if value is None else value


def analyze_journal(
    data_dir: Path, brain_filter: list[str] | None = None, exclusive: bool = False
) -> dict:
    """Analyze live_trade_journal.jsonl with ticket-level dedup.

    Args:
        data_dir: Path to data directory containing live_trade_journal.jsonl.
        brain_filter: If provided, only include trades where the open entry's
            brain_ids intersect with this list.
        exclusive: If True with brain_filter, only include trades where the
            filtered brain is the SOLE brain_id (no other brains also voted).
    """
    journal_path = data_dir / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return {"error": f"Journal not found: {journal_path}"}

    records = load_jsonl(journal_path)

    # ── Basic counts (raw, before dedup) ──
    raw_actions: dict[str, int] = defaultdict(int)
    raw_ack: dict[str, int] = defaultdict(int)
    for r in records:
        raw_actions[r.get("action", "?")] += 1
        raw_ack[r.get("ack_status", "?")] += 1

    # ── Group by IMMUTABLE position identity (not the mutable MT5 ticket) ──
    # FIX-20260708-001: MT5 re-tickets a position on partial-close/netting, so
    # grouping open/close legs by the mutable position_ticket manufactured a
    # false "orphan close" every time a ticket changed.  resolve_identity()
    # returns position_identifier (stable across re-ticketing), falling back to
    # the ticket only for legacy pre-identifier records.
    tickets = defaultdict(list)
    orphan_events = []
    for r in records:
        pt = resolve_identity(r)
        if pt:
            tickets[pt].append(r)
        else:
            orphan_events.append(r)

    # ── Dedup: for each ticket, find realized outcome ──
    realized_all = []  # all trades before brain filter (for orphan rate calculation)
    realized = []  # after brain filter

    for ticket, events in tickets.items():
        close_events = [
            e for e in events if e.get("action") == "close" and e.get("pnl") is not None
        ]
        if not close_events:
            continue
        # Use the LAST close with non-null PnL as the realized outcome
        final_close = close_events[-1]
        open_evt = next((e for e in events if e.get("action") == "open"), None)

        # ── Brain attribution from open entry (primary) or close (fallback) ──
        trade_brain_ids: list[str] = []
        if open_evt:
            trade_brain_ids = list(open_evt.get("brain_ids") or [])
        if not trade_brain_ids:
            trade_brain_ids = list(final_close.get("brain_ids") or [])

        # ── Orphan detection: close without matching open ──
        is_orphan = open_evt is None

        trade_data = {
            "ticket": ticket,
            "open_time": open_evt.get("recorded_at") if open_evt else None,
            "close_time": final_close.get("recorded_at"),
            "side": _coalesce(final_close, "side", "?"),
            "pnl": final_close.get("pnl", 0.0),
            "label": _coalesce(final_close, "label", "(unlabeled)"),
            "ack": _coalesce(final_close, "ack_status", "?"),
            "close_attempts": len(close_events),
            "total_events": len(events),
            "entry_confidence": open_evt.get("confidence") if open_evt else None,
            "entry_sl": open_evt.get("sl") if open_evt else None,
            "entry_tp": open_evt.get("tp") if open_evt else None,
            "entry_price": (
                ((open_evt.get("detail") or {}).get("request") or {}).get("price")
                if open_evt
                else None
            ),
            "entry_atr": ((open_evt.get("entry_context") or {}).get("atr") if open_evt else None),
            "brain_ids": trade_brain_ids,
            "is_orphan": is_orphan,
        }
        realized_all.append(trade_data)

        # ── Brain filter ──
        if brain_filter:
            trade_brain_set = set(trade_brain_ids)
            filter_set = set(brain_filter)
            if not trade_brain_set & filter_set:
                continue  # none of the requested brains voted
            if exclusive and not trade_brain_set.issubset(filter_set):
                continue  # other brains also voted — skip in exclusive mode

        realized.append(trade_data)

    # ── Orphan stats (computed from the applicable trade set) ──
    # When brain_filter is active, orphan stats reflect the filtered brain's trades.
    # When no filter, orphan stats reflect all trades.
    orphan_source = realized if brain_filter else realized_all
    orphan_closes = [r for r in orphan_source if r["is_orphan"]]
    orphan_close_count = len(orphan_closes)
    orphan_close_pnl = sum(r["pnl"] for r in orphan_closes)
    total_realized_all = len(realized_all)

    # ── Win/Loss/Breakeven ──
    wins = [r for r in realized if r["pnl"] > 0]
    losses = [r for r in realized if r["pnl"] < 0]
    breakevens = [r for r in realized if r["pnl"] == 0]

    total_pnl = sum(r["pnl"] for r in realized)
    wr = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0
    pf = (
        abs(sum(r["pnl"] for r in wins) / sum(r["pnl"] for r in losses))
        if wins and losses and sum(r["pnl"] for r in losses) != 0
        else float("inf")
    )

    # ── PnL by exit label ──
    pnl_by_label: dict[str, dict[str, float]] = defaultdict(
        lambda: {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
    )
    for r in realized:
        lbl = r["label"]
        pnl_by_label[lbl]["count"] += 1
        pnl_by_label[lbl]["pnl"] += r["pnl"]
        if r["pnl"] > 0:
            pnl_by_label[lbl]["wins"] += 1
        elif r["pnl"] < 0:
            pnl_by_label[lbl]["losses"] += 1

    # ── Direction distribution from OPEN events ──
    # FIX-20260624-061: Multi-source brain data with priority fallback.
    # entry_context.brain_predictions was removed from journal schema ~2026-06-20.
    # Priority: brain_votes (top-level, rich) > entry_context.brain_predictions
    #   (legacy, up_prob/down_prob) > brain_ids (top-level, bare IDs only).
    open_events = [r for r in records if r.get("action") == "open"]
    trade_directions: dict[str, int] = defaultdict(int)  # side of the trade (long/short)
    brain_directions: dict[str, int] = defaultdict(int)  # direction from brain predictions
    brain_detail = []  # per-open brain breakdown

    for o in open_events:
        trade_directions[o.get("side", "?")] += 1
        # ── Multi-source brain data extraction ──
        votes = o.get("brain_votes") or []
        ctx = o.get("entry_context", {}) or {}
        ctx_preds = ctx.get("brain_predictions", [])
        top_bids = o.get("brain_ids", [])

        if votes:
            preds = votes  # [{brain_id, direction_bias, confidence}]
        elif ctx_preds:
            preds = ctx_preds  # [{brain_id, direction_bias, up_prob, down_prob, confidence}]
        elif top_bids:
            preds = [{"brain_id": bid} for bid in top_bids]  # bare IDs only
        else:
            preds = []

        if not preds:
            brain_directions["no_predictions"] += 1
        for p in preds:
            db = p.get("direction_bias", "?")
            brain_directions[db] += 1
        brain_detail.append(
            {
                "time": o.get("recorded_at"),
                "trade_side": o.get("side"),
                "n_brains": len(preds),
                "brain_ids": [p.get("brain_id") for p in preds],
                "directions": [p.get("direction_bias", "?") for p in preds],
                "up_probs": [round(p.get("up_prob", 0), 3) for p in preds if "up_prob" in p]
                or None,
                "down_probs": [round(p.get("down_prob", 0), 3) for p in preds if "down_prob" in p]
                or None,
            }
        )

    # ── Entry timing / confidence analysis ──
    entry_confidences = [
        o.get("confidence") for o in open_events if o.get("confidence") is not None
    ]

    # ── SL/TP distances at entry ──
    sl_atr_list = []
    tp_atr_list = []
    rr_list = []
    for o in open_events:
        price = o.get("detail", {}).get("request", {}).get("price")
        sl = o.get("sl")
        tp = o.get("tp")
        atr = (o.get("entry_context", {}) or {}).get("atr")
        if price and sl and tp and atr and atr > 0:
            sl_dist = abs(price - sl)
            tp_dist = abs(tp - price)
            sl_atr = sl_dist / atr
            tp_atr = tp_dist / atr
            rr = tp_dist / sl_dist if sl_dist > 0 else 0
            sl_atr_list.append(sl_atr)
            tp_atr_list.append(tp_atr)
            rr_list.append(rr)

    # ── Per-brain aggregation (for --by-brain and orphan context) ──
    brain_index: dict[str, dict] = {}
    all_brain_ids: set[str] = set()
    for r in realized:
        for bid in r["brain_ids"]:
            all_brain_ids.add(bid)
            if bid not in brain_index:
                brain_index[bid] = {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}
            brain_index[bid]["trades"] += 1
            brain_index[bid]["pnl"] += r["pnl"]
            if r["pnl"] > 0:
                brain_index[bid]["wins"] += 1
            elif r["pnl"] < 0:
                brain_index[bid]["losses"] += 1

    # Compute per-brain WR/PF
    per_brain_stats = {}
    for bid, stats in brain_index.items():
        n_wins = stats["wins"]
        n_losses = stats["losses"]
        n_total = n_wins + n_losses
        wr_brain = n_wins / n_total * 100 if n_total else 0
        pf_brain = (
            abs(
                sum(r["pnl"] for r in realized if bid in r["brain_ids"] and r["pnl"] > 0)
                / sum(r["pnl"] for r in realized if bid in r["brain_ids"] and r["pnl"] < 0)
            )
            if n_wins
            and n_losses
            and sum(r["pnl"] for r in realized if bid in r["brain_ids"] and r["pnl"] < 0) != 0
            else float("inf")
        )
        per_brain_stats[bid] = {
            "trades": stats["trades"],
            "pnl_usd": round(stats["pnl"], 2),
            "wins": n_wins,
            "losses": n_losses,
            "win_rate_pct": round(wr_brain, 2),
            "profit_factor": round(pf_brain, 4),
        }

    return {
        "raw": {
            "total_entries": len(records),
            "actions": dict(raw_actions),
            "ack_status": dict(raw_ack),
            "orphan_events": len(orphan_events),
            "unique_tickets": len(tickets),
        },
        "realized": {
            "total_trades": len(realized),
            "wins": len(wins),
            "losses": len(losses),
            "breakevens": len(breakevens),
            "win_rate_pct": round(wr, 2),
            "total_pnl_usd": round(total_pnl, 2),
            "profit_factor": round(pf, 4),
            "avg_win_usd": round(sum(r["pnl"] for r in wins) / len(wins), 2) if wins else 0,
            "avg_loss_usd": round(sum(r["pnl"] for r in losses) / len(losses), 2) if losses else 0,
            "max_win_usd": round(max(r["pnl"] for r in wins), 2) if wins else 0,
            "max_loss_usd": round(min(r["pnl"] for r in losses), 2) if losses else 0,
        },
        "data_quality": {
            "orphan_close_count": orphan_close_count,
            "orphan_close_pnl_usd": round(orphan_close_pnl, 2),
            "orphan_close_pct": round(orphan_close_count / len(orphan_source) * 100, 1)
            if orphan_source
            else 0,
            "total_realized_all": total_realized_all,
            "brain_filter_applied": brain_filter if brain_filter else None,
            "distinct_brains": sorted(all_brain_ids),
            "per_brain": per_brain_stats,
        },
        "pnl_by_label": {
            lbl: {
                "count": s["count"],
                "pnl_usd": round(s["pnl"], 2),
                "wins": s["wins"],
                "losses": s["losses"],
            }
            for lbl, s in sorted(pnl_by_label.items(), key=lambda x: x[1]["pnl"])
        },
        "direction": {
            "trade_sides": dict(trade_directions),
            "brain_directions": dict(brain_directions),
            "brain_detail": brain_detail,
        },
        "entry_stats": {
            "confidence_min": round(min(entry_confidences), 4) if entry_confidences else None,
            "confidence_max": round(max(entry_confidences), 4) if entry_confidences else None,
            "confidence_avg": round(sum(entry_confidences) / len(entry_confidences), 4)
            if entry_confidences
            else None,
            "sl_atr_avg": round(sum(sl_atr_list) / len(sl_atr_list), 3) if sl_atr_list else None,
            "sl_atr_min": round(min(sl_atr_list), 3) if sl_atr_list else None,
            "sl_atr_max": round(max(sl_atr_list), 3) if sl_atr_list else None,
            "tp_atr_avg": round(sum(tp_atr_list) / len(tp_atr_list), 3) if tp_atr_list else None,
            "tp_atr_min": round(min(tp_atr_list), 3) if tp_atr_list else None,
            "tp_atr_max": round(max(tp_atr_list), 3) if tp_atr_list else None,
            "rr_avg": round(sum(rr_list) / len(rr_list), 3) if rr_list else None,
            "rr_min": round(min(rr_list), 3) if rr_list else None,
            "rr_max": round(max(rr_list), 3) if rr_list else None,
        },
        "close_attempts_distribution": _count_distribution([r["close_attempts"] for r in realized]),
    }


def analyze_position_snapshots(data_dir: Path) -> dict:
    """Analyze position_snapshots.jsonl for trailing SL behavior."""
    snap_path = data_dir / "position_snapshots.jsonl"
    if not snap_path.exists():
        return {"error": f"Snapshots not found: {snap_path}"}

    records = load_jsonl(snap_path)

    # Group by ticket
    tickets = defaultdict(list)
    for r in records:
        ticket = r.get("ticket")
        if ticket:
            tickets[ticket].append(r)

    # Per-ticket stats
    per_ticket = {}
    all_sl_atr_ratios = []  # all non-zero snapshots
    all_sl_distances = []
    tickets_with_inactive_sl = 0
    total_snapshots = 0
    tickets_never_tight = 0  # never reached <= 2x ATR
    tickets_never_moderate = 0  # never reached <= 3x ATR

    for ticket, snaps in tickets.items():
        total_snapshots += len(snaps)
        sl_ratios = []
        sl_dists = []
        has_inactive = False
        min_sl_ratio = float("inf")
        max_sl_ratio = 0.0
        pnl_trajectory = []

        for s in snaps:
            tsd = s.get("trailing_sl_distance", 0)
            atr = s.get("current_atr", 0)
            pnl_r = s.get("unrealized_pnl_r")
            bars = s.get("bars_held", 0)

            if tsd == 0:
                has_inactive = True
                # SL inactive at this bar
            elif atr and atr > 0:
                ratio = tsd / atr
                sl_ratios.append(ratio)
                sl_dists.append(tsd)
                all_sl_atr_ratios.append(ratio)
                all_sl_distances.append(tsd)
                min_sl_ratio = min(min_sl_ratio, ratio)
                max_sl_ratio = max(max_sl_ratio, ratio)

            pnl_trajectory.append(
                {
                    "bars_held": bars,
                    "unrealized_pnl_r": pnl_r,
                    "sl_ratio": tsd / atr if atr and atr > 0 and tsd > 0 else None,
                }
            )

        if has_inactive:
            tickets_with_inactive_sl += 1

        avg_sl_ratio = sum(sl_ratios) / len(sl_ratios) if sl_ratios else 0

        if min_sl_ratio > 2.0 or (not sl_ratios):
            tickets_never_tight += 1
        if min_sl_ratio > 3.0 or (not sl_ratios):
            tickets_never_moderate += 1

        per_ticket[ticket] = {
            "snapshots": len(snaps),
            "has_inactive_sl": has_inactive,
            "avg_sl_atr_ratio": round(avg_sl_ratio, 3),
            "min_sl_atr_ratio": round(min_sl_ratio, 3) if min_sl_ratio != float("inf") else None,
            "max_sl_atr_ratio": round(max_sl_ratio, 3),
            "first_pnl_r": pnl_trajectory[0]["unrealized_pnl_r"] if pnl_trajectory else None,
            "last_pnl_r": pnl_trajectory[-1]["unrealized_pnl_r"] if pnl_trajectory else None,
        }

    # Grand statistics
    if all_sl_atr_ratios:
        grand_avg_sl_atr = sum(all_sl_atr_ratios) / len(all_sl_atr_ratios)
        grand_median_sl_atr = sorted(all_sl_atr_ratios)[len(all_sl_atr_ratios) // 2]
    else:
        grand_avg_sl_atr = 0
        grand_median_sl_atr = 0

    total_tickets = len(tickets)

    return {
        "total_snapshots": total_snapshots,
        "total_tickets": total_tickets,
        "trailing_sl": {
            "grand_avg_sl_atr_ratio": round(grand_avg_sl_atr, 3),
            "grand_median_sl_atr_ratio": round(grand_median_sl_atr, 3),
            "tickets_with_inactive_sl_pct": round(tickets_with_inactive_sl / total_tickets * 100, 1)
            if total_tickets
            else 0,
            "tickets_never_tight_pct": round(tickets_never_tight / total_tickets * 100, 1)
            if total_tickets
            else 0,
            "tickets_never_moderate_pct": round(tickets_never_moderate / total_tickets * 100, 1)
            if total_tickets
            else 0,
        },
        "per_ticket": per_ticket,
    }


def _count_distribution(values: list) -> dict:
    """Return count distribution of a list of values."""
    dist: dict[str, int] = defaultdict(int)
    for v in values:
        dist[v] += 1
    return dict(sorted(dist.items()))


def print_report(journal: dict, snapshots: dict) -> None:
    """Print the formatted audit report to stdout."""
    sep = "=" * 70

    print(sep)
    print("  LIVE TRADING AUDIT REPORT")
    print("  Iron Law #11 — Script-Generated Statistics Only")
    print(sep)

    # ── Section 1: Raw Data Overview ──
    raw = journal.get("raw", {})
    dq = journal.get("data_quality", {})
    print("\n── 1. RAW DATA OVERVIEW ──")
    print(f"  Total journal entries:        {raw.get('total_entries', 'N/A')}")
    print(f"  Unique position tickets:      {raw.get('unique_tickets', 'N/A')}")
    print(f"  Orphan events (no ticket):    {raw.get('orphan_events', 'N/A')}")
    print(f"  Actions:                      {raw.get('actions', {})}")
    print(f"  Ack status distribution:      {raw.get('ack_status', {})}")

    # ── Data Quality & Brain Context ──
    bf = dq.get("brain_filter_applied")
    print(f"\n── 1.1 DATA QUALITY & CONTEXT ──")
    if bf:
        print(f"  Brain filter:                 {bf}")
    else:
        print(f"  Brain filter:                 (none — all brains aggregated)")
    print(f"  Distinct brains in data:      {dq.get('distinct_brains', [])}")
    print(f"  Orphan closes (close w/o open): {dq.get('orphan_close_count', 0)}")
    print(f"  Orphan close PnL:            ${dq.get('orphan_close_pnl_usd', 0):.2f}")
    if raw.get("unique_tickets", 1):
        print(
            f"  Orphan close rate:            {dq.get('orphan_close_pct', 0):.1f}% of all realized trades"
        )
        if dq.get("orphan_close_count", 0) > 0:
            print(
                f"  [!] NOTE: {dq.get('orphan_close_count', 0)} orphan closes (close without matching open)"
            )
            print(
                f"     are INCLUDED above. Their aggregate PnL: ${dq.get('orphan_close_pnl_usd', 0):.2f}."
            )
            print(
                f"     After FIX-20260708-001 (identity join) these are legacy records with"
                f" neither position_identifier nor a matching open ticket (pre-DQAF-033 anchor)."
            )

    # ── Section 2: Realized Trade Performance ──
    real = journal.get("realized", {})
    print("\n── 2. REALIZED TRADE PERFORMANCE (deduped by position identity) ──")
    print(f"  Total realized trades:        {real.get('total_trades', 0)}")
    print(f"  Wins (PnL > 0):               {real.get('wins', 0)}")
    print(f"  Losses (PnL < 0):             {real.get('losses', 0)}")
    print(f"  Breakevens (PnL = 0):         {real.get('breakevens', 0)}")
    print(f"  Win Rate (excl. breakeven):   {real.get('win_rate_pct', 0):.1f}%")
    print(f"  Total PnL:                   ${real.get('total_pnl_usd', 0):.2f}")
    print(f"  Profit Factor:                {real.get('profit_factor', 0):.2f}")
    print(f"  Avg Win:                     ${real.get('avg_win_usd', 0):.2f}")
    print(f"  Avg Loss:                    ${real.get('avg_loss_usd', 0):.2f}")
    print(f"  Max Win:                     ${real.get('max_win_usd', 0):.2f}")
    print(f"  Max Loss:                    ${real.get('max_loss_usd', 0):.2f}")

    # ── Section 3: PnL by Exit Label ──
    print("\n── 3. PnL BY EXIT LABEL ──")
    print(f"  {'Label':<55s} {'Count':>5s}  {'PnL($)':>10s}  {'W':>3s}  {'L':>3s}")
    print(f"  {'-'*55}  {'-'*5}  {'-'*10}  {'-'*3}  {'-'*3}")
    for lbl, s in journal.get("pnl_by_label", {}).items():
        print(
            f"  {lbl:<55s} {s['count']:>5d}  {s['pnl_usd']:>10.2f}  {s['wins']:>3d}  {s['losses']:>3d}"
        )

    # ── Section 3.1: Per-Brain Breakdown ──
    per_brain = dq.get("per_brain", {})
    if per_brain and len(per_brain) > 1:
        print(f"\n── 3.1 PER-BRAIN BREAKDOWN ──")
        print(
            f"  {'Brain ID':<35s} {'Trades':>7s} {'PnL($)':>10s} {'WR%':>7s} {'PF':>6s} {'W':>4s} {'L':>4s}"
        )
        print(f"  {'-'*35} {'-'*7} {'-'*10} {'-'*7} {'-'*6} {'-'*4} {'-'*4}")
        for bid in sorted(per_brain.keys()):
            s = per_brain[bid]
            wr_str = f"{s['win_rate_pct']:.1f}" if s["win_rate_pct"] != float("inf") else "N/A"
            pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "inf"
            print(
                f"  {bid:<35s} {s['trades']:>7d} {s['pnl_usd']:>10.2f} {wr_str:>7s} {pf_str:>6s} {s['wins']:>4d} {s['losses']:>4d}"
            )

    # ── Section 4: Direction Distribution ──
    direction = journal.get("direction", {})
    print("\n── 4. DIRECTION DISTRIBUTION ──")
    print(f"  Trade sides (opens):          {direction.get('trade_sides', {})}")
    print(f"  Brain prediction directions:  {direction.get('brain_directions', {})}")

    # Brain direction by unique brain_id per open
    print("\n  Per-open brain breakdown (most recent 15):")
    detail = direction.get("brain_detail", [])
    for d in detail[-15:]:
        time_str = d.get("time", "?")[:19] if d.get("time") else "?"
        trade_side = d.get("trade_side") or "?"
        print(
            f"    {time_str}  trade={trade_side:<6s}  "
            f"n_brains={d['n_brains']}  "
            f"ids={d['brain_ids']}  "
            f"dirs={d['directions']}"
        )
        if d["n_brains"] is not None and d["n_brains"] <= 3:
            print(f"      up_probs={d['up_probs']}  down_probs={d['down_probs']}")

    # ── Section 5: Entry Stats ──
    entry = journal.get("entry_stats", {})
    print("\n── 5. ENTRY STATISTICS ──")
    print(
        f"  Entry confidence: min={entry.get('confidence_min')}, "
        f"max={entry.get('confidence_max')}, avg={entry.get('confidence_avg')}"
    )
    print(
        f"  SL distance (x ATR): min={entry.get('sl_atr_avg')}, "
        f"avg={entry.get('sl_atr_avg')}, max={entry.get('sl_atr_max')}"
    )
    print(
        f"  TP distance (x ATR): min={entry.get('tp_atr_min')}, "
        f"avg={entry.get('tp_atr_avg')}, max={entry.get('tp_atr_max')}"
    )
    print(
        f"  R:R ratio:           min={entry.get('rr_min')}, "
        f"avg={entry.get('rr_avg')}, max={entry.get('rr_max')}"
    )

    # ── Section 6: Close Attempts Distribution ──
    print("\n── 6. CLOSE ATTEMPTS PER TICKET ──")
    print(f"  {journal.get('close_attempts_distribution', {})}")

    # ── Section 7: Trailing SL Analysis ──
    snap = snapshots.get("trailing_sl", {})
    print("\n── 7. TRAILING SL ANALYSIS (from position_snapshots.jsonl) ──")
    print(f"  Total snapshots:              {snapshots.get('total_snapshots', 0)}")
    print(f"  Total tickets with snapshots: {snapshots.get('total_tickets', 0)}")
    print(f"  Grand avg SL/ATR ratio:       {snap.get('grand_avg_sl_atr_ratio', 0):.3f}x")
    print(f"  Grand median SL/ATR ratio:    {snap.get('grand_median_sl_atr_ratio', 0):.3f}x")
    print(f"  Tickets w/ inactive SL:       {snap.get('tickets_with_inactive_sl_pct', 0):.1f}%")
    print(f"  Tickets NEVER <= 2x ATR:      {snap.get('tickets_never_tight_pct', 0):.1f}%")
    print(f"  Tickets NEVER <= 3x ATR:      {snap.get('tickets_never_moderate_pct', 0):.1f}%")

    # Per-ticket detail (top 10 by avg SL ratio, worst 10)
    per_ticket = snapshots.get("per_ticket", {})
    sorted_by_sl = sorted(
        [(t, s) for t, s in per_ticket.items() if s["avg_sl_atr_ratio"] > 0],
        key=lambda x: x[1]["avg_sl_atr_ratio"],
    )
    print("\n  Top 10 tightest SL (lowest avg ratio):")
    for ticket, s in sorted_by_sl[:10]:
        print(
            f"    ticket={ticket}: avg={s['avg_sl_atr_ratio']:.2f}x, "
            f"min={s['min_sl_atr_ratio']}, max={s['max_sl_atr_ratio']:.2f}x, "
            f"snapshots={s['snapshots']}, inactive={s['has_inactive_sl']}"
        )

    print("\n  Top 10 loosest SL (highest avg ratio):")
    for ticket, s in sorted_by_sl[-10:]:
        print(
            f"    ticket={ticket}: avg={s['avg_sl_atr_ratio']:.2f}x, "
            f"min={s['min_sl_atr_ratio']}, max={s['max_sl_atr_ratio']:.2f}x, "
            f"snapshots={s['snapshots']}, inactive={s['has_inactive_sl']}"
        )

    print("\n" + sep)
    print("  END OF AUDIT REPORT")
    print(sep)


def main():
    parser = argparse.ArgumentParser(description="Live Trading Journal Auditor")
    parser.add_argument(
        "--data-dir",
        default="data_btc",
        help="Path to data directory containing live_trade_journal.jsonl and position_snapshots.jsonl",
    )
    parser.add_argument(
        "--brain",
        action="append",
        dest="brains",
        default=None,
        help="Filter to trades attributed to this brain_id. Repeatable: --brain BTC_Swing_V4 --brain BTC_Swing_V7. "
        "Uses brain_ids from the open entry (or close entry as fallback). "
        "Without this flag, all brains are aggregated.",
    )
    parser.add_argument(
        "--exclusive",
        action="store_true",
        default=False,
        help="When used with --brain, only include trades where the specified brain is "
        "the SOLE brain_id (exclude multi-brain trades). Without this, all trades "
        "where the brain participated are included, even if other brains also voted.",
    )
    parser.add_argument(
        "--by-brain",
        action="store_true",
        default=False,
        help="Run a separate audit for each distinct brain_id found in the journal, "
        "then print a summary comparison table.",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        # Resolve relative to repo root (where this script lives under scripts/)
        script_dir = Path(__file__).resolve().parent
        repo_root = script_dir.parent
        data_dir = repo_root / data_dir

    if not data_dir.exists():
        print(f"ERROR: data directory not found: {data_dir}")
        sys.exit(1)

    if args.by_brain:
        # ── First pass: discover all distinct brain_ids ──
        journal_path = data_dir / "live_trade_journal.jsonl"
        all_brains: set[str] = set()
        with open(journal_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                bids = r.get("brain_ids") or []
                for bid in bids:
                    all_brains.add(bid)

        if not all_brains:
            print("ERROR: No brain_ids found in journal entries.")
            sys.exit(1)

        print("=" * 70)
        print("  PER-BRAIN AUDIT SUMMARY")
        print(f"  Data dir: {data_dir}")
        print(f"  Brains found: {len(all_brains)}")
        print("=" * 70)

        # ── Run per-brain audit ──
        all_results: dict[str, dict] = {}
        for bid in sorted(all_brains):
            result = analyze_journal(data_dir, brain_filter=[bid], exclusive=args.exclusive)
            if "error" in result:
                print(f"  {bid}: ERROR — {result['error']}")
                continue
            all_results[bid] = result

        # ── Print per-brain summary table ──
        print(
            f"\n{'Brain ID':<35s} {'Trades':>7s} {'PnL($)':>10s} {'WR%':>7s} {'PF':>6s} {'W':>4s} {'L':>4s} {'Orphans':>8s}"
        )
        print(f"{'-'*35} {'-'*7} {'-'*10} {'-'*7} {'-'*6} {'-'*4} {'-'*4} {'-'*8}")
        total_trades = 0
        total_pnl = 0.0
        total_wins = 0
        total_losses = 0
        total_orphans = 0
        for bid in sorted(all_results.keys()):
            r = all_results[bid]
            real = r.get("realized", {})
            dq = r.get("data_quality", {})
            trades = real.get("total_trades", 0)
            pnl = real.get("total_pnl_usd", 0)
            wr = real.get("win_rate_pct", 0)
            pf = real.get("profit_factor", 0)
            w = real.get("wins", 0)
            l = real.get("losses", 0)
            orphans = dq.get("orphan_close_count", 0)
            total_trades += trades
            total_pnl += pnl
            total_wins += w
            total_losses += l
            total_orphans += orphans
            wr_str = f"{wr:.1f}" if wr else "N/A"
            pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
            print(
                f"{bid:<35s} {trades:>7d} {pnl:>10.2f} {wr_str:>7s} {pf_str:>6s} {w:>4d} {l:>4d} {orphans:>8d}"
            )
        # Totals row
        total_wr = (
            total_wins / (total_wins + total_losses) * 100 if (total_wins + total_losses) else 0
        )
        total_pf = (
            abs(
                sum(
                    r.get("realized", {}).get("total_pnl_usd", 0)
                    for r in all_results.values()
                    if r.get("realized", {}).get("total_pnl_usd", 0) > 0
                )
                / sum(
                    r.get("realized", {}).get("total_pnl_usd", 0)
                    for r in all_results.values()
                    if r.get("realized", {}).get("total_pnl_usd", 0) < 0
                )
            )
            if total_losses
            else float("inf")
        )
        print(f"{'─'*35} {'─'*7} {'─'*10} {'─'*7} {'─'*6} {'─'*4} {'─'*4} {'─'*8}")
        print(
            f"{'TOTAL':<35s} {total_trades:>7d} {total_pnl:>10.2f} {total_wr:>7.1f} {total_pf:>6.2f} {total_wins:>4d} {total_losses:>4d} {total_orphans:>8d}"
        )
        print(f"\n  [!] NOTE: Totals may differ from no-filter run — trades attributed to")
        print(f"     multiple brains are counted once per brain above (not deduped).")
        print(f"     For strictly deduped aggregate, run without --by-brain.")
        print("=" * 70)
        return

    journal = analyze_journal(data_dir, brain_filter=args.brains, exclusive=args.exclusive)
    snapshots = analyze_position_snapshots(data_dir)

    if "error" in journal:
        print(f"ERROR: {journal['error']}")
    if "error" in snapshots:
        print(f"WARNING: {snapshots['error']}")

    print_report(journal, snapshots)


if __name__ == "__main__":
    main()
