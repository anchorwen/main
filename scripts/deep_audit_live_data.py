#!/usr/bin/env python
"""Deep Audit: Institutional-Grade Live Data Scan — DQAF-20260621-043
====================================================================
Covers: BTC + XAU live trade journals, position snapshots, ledger events,
golden master, governance state, brain performance, calibrator state,
and cross-source consistency.

Statistical conventions (declared upfront):
  - Dedup: by position_ticket (open+close pair = 1 trade)
  - Win rate: wins / (wins + losses), breakeven excluded from denominator
  - PnL: R-units (risk-normalized), from journal pnl field
  - Open-only positions: excluded from trade counts

Usage:
  python scripts/deep_audit_live_data.py [--data-dir data_btc] [--full]
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

# -- Helpers --------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    """Load JSONL file, skipping empty/malformed lines."""
    records = []
    if not path.exists():
        print(f"  [WARN] File not found: {path}")
        return records
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  [WARN] JSON decode error at {path}:{i}: {e}")
    return records


def load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fmt_pnl(v: float) -> str:
    return f"{v:+.2f}R"


def pct(v: float, n: int) -> str:
    if n == 0:
        return "N/A"
    return f"{v/n*100:.1f}%"


# -- Section 1: Live Trade Journal ----------------------------------------

def audit_journal(data_dir: Path, label: str) -> dict:
    """Full audit of live_trade_journal.jsonl."""
    print(f"\n{'='*72}")
    print(f"  SECTION 1: Live Trade Journal Audit — {label}")
    print(f"{'='*72}")

    journal_path = data_dir / "live_trade_journal.jsonl"
    records = load_jsonl(journal_path)
    print(f"  Total records: {len(records):,}")

    # Separate open/close
    opens = [r for r in records if r.get("action") == "open"]
    closes = [r for r in records if r.get("action") == "close"]
    other = [r for r in records if r.get("action") not in ("open", "close")]
    print(f"  Open records:  {len(opens):,}")
    print(f"  Close records: {len(closes):,}")
    if other:
        print(f"  Other actions: {len(other):,} -> {Counter(r.get('action') for r in other).most_common()}")

    # Dedup by position_ticket
    close_by_ticket: dict[int, dict] = {}
    for c in closes:
        ticket = c.get("position_ticket")
        if ticket is not None:
            if ticket in close_by_ticket:
                # Duplicate close — keep latest
                existing_ts = close_by_ticket[ticket].get("recorded_at", "")
                new_ts = c.get("recorded_at", "")
                if new_ts > existing_ts:
                    close_by_ticket[ticket] = c
            else:
                close_by_ticket[ticket] = c

    open_by_ticket: dict[int, dict] = {}
    for o in opens:
        ticket = o.get("position_ticket")
        if ticket is not None:
            open_by_ticket[ticket] = o

    # Match open-close pairs
    matched_tickets = set(open_by_ticket.keys()) & set(close_by_ticket.keys())
    open_only = set(open_by_ticket.keys()) - set(close_by_ticket.keys())
    close_only = set(close_by_ticket.keys()) - set(open_by_ticket.keys())

    print("\n  -- Dedup & Matching --")
    print(f"  Unique open tickets:  {len(open_by_ticket):,}")
    print(f"  Unique close tickets: {len(close_by_ticket):,}")
    print(f"  Matched pairs:        {len(matched_tickets):,}")
    print(f"  Open-only (no close): {len(open_only):,}")
    print(f"  Close-only (no open): {len(close_only):,}")

    # Duplicate closes
    dup_close_tickets = [t for t, c in close_by_ticket.items() if sum(1 for x in closes if x.get("position_ticket") == t) > 1]
    if dup_close_tickets:
        print(f"  [WARN] Duplicate close records: {len(dup_close_tickets)} tickets")
        for t in dup_close_tickets[:5]:
            count = sum(1 for x in closes if x.get("position_ticket") == t)
            print(f"      ticket={t}: {count} close records")

    # Trade stats
    trades = []
    for ticket in matched_tickets:
        o = open_by_ticket[ticket]
        c = close_by_ticket[ticket]
        pnl = c.get("pnl", 0) or 0
        label = c.get("label", "unknown")
        brain_ids = o.get("brain_ids") or ["unknown"]
        if isinstance(brain_ids, str):
            brain_ids = [brain_ids]
        if brain_ids is None:
            brain_ids = ["unknown"]
        side = o.get("side", "unknown")
        trades.append({
            "ticket": ticket,
            "pnl": pnl,
            "label": label,
            "brain_ids": brain_ids,
            "side": side,
            "open_time": o.get("recorded_at", ""),
            "close_time": c.get("recorded_at", ""),
        })

    if not trades:
        print("  [WARN] No matched trades to analyze!")
        return {"trade_count": 0}

    # Win/Loss/BE
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    bes = [t for t in trades if t["pnl"] == 0]

    total_pnl = sum(t["pnl"] for t in trades)
    wr = len(wins) / (len(wins) + len(losses)) if (len(wins) + len(losses)) > 0 else 0

    print("\n  -- Trade Statistics --")
    print(f"  Total trades:    {len(trades):,}")
    print(f"  Wins:            {len(wins):,} ({pct(len(wins), len(trades))})")
    print(f"  Losses:          {len(losses):,} ({pct(len(losses), len(trades))})")
    print(f"  Breakevens:      {len(bes):,} ({pct(len(bes), len(trades))})")
    print(f"  Win rate (ex BE): {wr*100:.1f}%")
    print(f"  Total PnL:       {fmt_pnl(total_pnl)}")
    print(f"  Avg Win:         {fmt_pnl(sum(t['pnl'] for t in wins) / len(wins)) if wins else 'N/A'}")
    print(f"  Avg Loss:        {fmt_pnl(sum(t['pnl'] for t in losses) / len(losses)) if losses else 'N/A'}")
    if wins and losses:
        print(f"  Profit Factor:   {abs(sum(t['pnl'] for t in wins) / sum(t['pnl'] for t in losses)):.2f}")

    # Label distribution
    print("\n  -- Exit Label Distribution --")
    label_counts = Counter(t["label"] for t in trades)
    for lbl, cnt in label_counts.most_common(20):
        lbl_pnl = sum(t["pnl"] for t in trades if t["label"] == lbl)
        lbl_wr = sum(1 for t in trades if t["label"] == lbl and t["pnl"] > 0)
        lbl_n = sum(1 for t in trades if t["label"] == lbl)
        lbl_loss = sum(1 for t in trades if t["label"] == lbl and t["pnl"] < 0)
        lbl_wr_pct = lbl_wr / (lbl_wr + lbl_loss) * 100 if (lbl_wr + lbl_loss) > 0 else 0
        print(f"  {lbl:30s} -> {cnt:4d} trades, PnL={fmt_pnl(lbl_pnl)}, WR={lbl_wr_pct:.1f}%")

    # Brain performance
    print("\n  -- Brain Performance --")
    brain_stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0, "bes": 0})
    for t in trades:
        for bid in t["brain_ids"]:
            bs = brain_stats[bid]
            bs["count"] += 1
            bs["pnl"] += t["pnl"]
            if t["pnl"] > 0:
                bs["wins"] += 1
            elif t["pnl"] < 0:
                bs["losses"] += 1
            else:
                bs["bes"] += 1

    for bid in sorted(brain_stats.keys()):
        bs = brain_stats[bid]
        wr_b = bs["wins"] / (bs["wins"] + bs["losses"]) * 100 if (bs["wins"] + bs["losses"]) > 0 else 0
        print(f"  {bid:25s} -> {bs['count']:4d} trades, PnL={fmt_pnl(bs['pnl'])}, WR={wr_b:.1f}%")

    # Side distribution
    print("\n  -- Side Distribution --")
    side_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
    for t in trades:
        s = side_stats[t["side"]]
        s["count"] += 1
        s["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            s["wins"] += 1
        elif t["pnl"] < 0:
            s["losses"] += 1
    for side, s in sorted(side_stats.items()):
        swr = s["wins"] / (s["wins"] + s["losses"]) * 100 if (s["wins"] + s["losses"]) > 0 else 0
        print(f"  {side:10s} -> {s['count']:4d} trades, PnL={fmt_pnl(s['pnl'])}, WR={swr:.1f}%")

    # Data quality checks
    print("\n  -- Data Quality Flags --")
    flags = []

    # Check for missing brain_ids
    no_brain = [t for t in trades if not t["brain_ids"] or t["brain_ids"] == ["unknown"]]
    if no_brain:
        flags.append(f"[WARN] {len(no_brain)} trades with missing/unknown brain_ids")

    # Check for null PnL
    null_pnl = [t for t in trades if t["pnl"] is None]
    if null_pnl:
        flags.append(f"[WARN] {len(null_pnl)} trades with null pnl")

    # Check for duplicate tickets in opens
    dup_open = [t for t, c in Counter(r.get("position_ticket") for r in opens).items() if c > 1]
    if dup_open:
        flags.append(f"[WARN] {len(dup_open)} duplicate open tickets")

    # Check for MIA (missing-in-action) label
    mia_trades = [t for t in trades if "mia" in str(t.get("label", "")).lower()]
    if mia_trades:
        flags.append(f"[WARN] {len(mia_trades)} MIA-labeled trades ({pct(len(mia_trades), len(trades))})")

    # Check label gap: close records without label
    no_label = [c for c in closes if not c.get("label")]
    if no_label:
        flags.append(f"[WARN] {len(no_label)} close records without label")

    if not flags:
        print("  [OK] No quality flags raised")
    else:
        for flag in flags:
            print(f"  {flag}")

    # Time range
    times = [t["close_time"] for t in trades if t["close_time"]]
    if times:
        times.sort()
        print("\n  -- Time Range --")
        print(f"  First trade: {times[0]}")
        print(f"  Last trade:  {times[-1]}")

    return {
        "trade_count": len(trades),
        "total_pnl": total_pnl,
        "win_rate": wr,
        "label_counts": dict(label_counts),
        "brain_stats": {k: dict(v) for k, v in brain_stats.items()},
        "flags": flags,
        "open_only": len(open_only),
        "close_only": len(close_only),
    }


# -- Section 2: Position Snapshots ----------------------------------------

def audit_snapshots(data_dir: Path, label: str) -> dict:
    """Audit position_snapshots.jsonl for trailing SL coverage."""
    print(f"\n{'='*72}")
    print(f"  SECTION 2: Position Snapshots Audit — {label}")
    print(f"{'='*72}")

    snap_path = data_dir / "position_snapshots.jsonl"
    snaps = load_jsonl(snap_path)
    print(f"  Total snapshots: {len(snaps):,}")

    if not snaps:
        return {"snapshot_count": 0}

    # Group by ticket (BTC uses "ticket", XAU may use "position_ticket")
    by_ticket: dict[int, list[dict]] = defaultdict(list)
    for s in snaps:
        ticket = s.get("ticket") or s.get("position_ticket")
        if ticket is not None:
            by_ticket[ticket].append(s)

    print(f"  Unique tickets:  {len(by_ticket):,}")

    # Trailing SL analysis
    # BTC snapshots use: ticket, time, bars_held, unrealized_pnl_r, trailing_sl_distance
    # XAU snapshots may use different fields
    trail_stats = []
    for ticket, snap_list in by_ticket.items():
        snap_list.sort(key=lambda x: x.get("time", x.get("recorded_at", "")))
        first = snap_list[0]
        last = snap_list[-1]

        # Try multiple possible field names for SL
        sl_values = []
        for s in snap_list:
            sl = s.get("sl_price") or s.get("trailing_sl_distance") or s.get("sl")
            if sl is not None:
                sl_values.append(sl)

        tp_values = [s.get("tp_price", 0) or 0 for s in snap_list if s.get("tp_price") is not None]

        sl_moved = len(set(sl_values)) > 1 if sl_values else False
        tp_moved = len(set(tp_values)) > 1 if tp_values else False
        snap_count = len(snap_list)

        trail_stats.append({
            "ticket": ticket,
            "snap_count": snap_count,
            "sl_moved": sl_moved,
            "tp_moved": tp_moved,
            "first_sl": sl_values[0] if sl_values else None,
            "last_sl": sl_values[-1] if sl_values else None,
        })

    no_snaps = 0
    trail_moved = 0
    trail_not_moved = 0
    single_snap = 0

    for ts in trail_stats:
        if ts["snap_count"] == 1:
            single_snap += 1
        if ts["sl_moved"]:
            trail_moved += 1
        else:
            trail_not_moved += 1

    print("\n  -- Trailing SL Coverage --")
    print(f"  Positions with snapshots:     {len(trail_stats):,}")
    print(f"  SL moved (trail active):      {trail_moved} ({pct(trail_moved, len(trail_stats))})")
    print(f"  SL NOT moved:                 {trail_not_moved} ({pct(trail_not_moved, len(trail_stats))})")
    print(f"  Single-snapshot positions:    {single_snap} ({pct(single_snap, len(trail_stats))})")

    return {
        "snapshot_count": len(snaps),
        "unique_tickets": len(by_ticket),
        "trail_moved": trail_moved,
        "trail_not_moved": trail_not_moved,
        "single_snapshot": single_snap,
    }


# -- Section 3: Ledger Events ---------------------------------------------

def audit_ledger(data_dir: Path, label: str) -> dict:
    """Audit ledger_events.jsonl for system health signals."""
    print(f"\n{'='*72}")
    print(f"  SECTION 3: Ledger Events Audit — {label}")
    print(f"{'='*72}")

    ledger_path = data_dir / "ledger_events.jsonl"
    events = load_jsonl(ledger_path)
    print(f"  Total events: {len(events):,}")

    if not events:
        return {"event_count": 0}

    # Event type distribution
    event_types = Counter(e.get("event_type", e.get("type", "unknown")) for e in events)
    print("\n  -- Event Type Distribution --")
    for etype, cnt in event_types.most_common(15):
        print(f"  {etype:40s} -> {cnt:,}")

    # Error/critical events
    error_events = [e for e in events if any(kw in str(e).lower() for kw in ["error", "critical", "fail", "exception"])]
    if error_events:
        print("\n  -- Error/Critical Events --")
        print(f"  Count: {len(error_events):,}")
        # Show most recent 5
        error_events.sort(key=lambda x: x.get("recorded_at", x.get("timestamp", "")), reverse=True)
        for e in error_events[:5]:
            ts = e.get("recorded_at", e.get("timestamp", ""))
            msg = str(e.get("message", e.get("event_type", str(e))))[:120]
            print(f"  [{ts}] {msg}")

    # Time range
    timestamps = [e.get("recorded_at", e.get("timestamp", "")) for e in events if e.get("recorded_at") or e.get("timestamp")]
    if timestamps:
        timestamps.sort()
        print(f"\n  First event: {timestamps[0]}")
        print(f"  Last event:  {timestamps[-1]}")

    return {"event_count": len(events), "event_types": dict(event_types)}


# -- Section 4: Golden Master ---------------------------------------------

def audit_golden_master(data_dir: Path, label: str) -> dict:
    """Audit golden_master.jsonl for label quality."""
    print(f"\n{'='*72}")
    print(f"  SECTION 4: Golden Master Audit — {label}")
    print(f"{'='*72}")

    gm_path = data_dir / "golden_master.jsonl"
    records = load_jsonl(gm_path)
    print(f"  Total records: {len(records):,}")

    if not records:
        return {"record_count": 0}

    # Check sorting
    timestamps = []
    for r in records:
        ts = r.get("timestamp", r.get("recorded_at", r.get("bar_time", "")))
        timestamps.append(str(ts))

    is_sorted = all(timestamps[i] <= timestamps[i+1] for i in range(len(timestamps)-1))
    print(f"  Chronologically sorted: {is_sorted}")

    if not is_sorted:
        # Find first out-of-order
        for i in range(len(timestamps)-1):
            if timestamps[i] > timestamps[i+1]:
                print(f"  [WARN] First out-of-order at index {i}: {timestamps[i]} > {timestamps[i+1]}")
                break

    # Label distribution — handle BTC format (outputs.<strategy>.direction)
    # and XAU format (flat "label"/"target" field)
    labels = []
    for r in records:
        label = r.get("label") or r.get("target")
        if label is None and "outputs" in r:
            # BTC format: outputs = {"btc_swing": {"direction": "short", "confidence": 0.586, ...}}
            outputs = r.get("outputs", {})
            if isinstance(outputs, dict):
                for strategy, sdata in outputs.items():
                    if isinstance(sdata, dict):
                        direction = sdata.get("direction", "")
                        conf = sdata.get("confidence", "")
                        should = sdata.get("should_trade", "")
                        label = f"{strategy}:{direction}:conf={conf:.2f}:trade={should}" if conf else f"{strategy}:{direction}"
        labels.append(str(label) if label else "")

    label_counts = Counter(str(l) for l in labels)
    print("\n  -- Label Distribution --")
    for lbl, cnt in label_counts.most_common(15):
        print(f"  {lbl:30s} -> {cnt:6d} ({pct(cnt, len(records))})")

    # Check for empty labels
    empty_labels = sum(1 for l in labels if not l or l in ("", "None", "null"))
    if empty_labels:
        print(f"  [WARN] {empty_labels} records with empty/null labels")

    # Feature dimension check
    if records:
        first = records[0]
        feature_keys = [k for k in first.keys() if k not in ("timestamp", "recorded_at", "bar_time", "label", "target", "symbol", "timeframe")]
        print(f"\n  Feature dimensions: {len(feature_keys)}")
        print(f"  Sample features: {feature_keys[:10]}...")

    return {"record_count": len(records), "is_sorted": is_sorted, "label_counts": dict(label_counts)}


# -- Section 5: Governance State ------------------------------------------

def audit_governance(data_dir: Path, label: str) -> dict:
    """Audit governance_state.json for brain health."""
    print(f"\n{'='*72}")
    print(f"  SECTION 5: Governance State Audit — {label}")
    print(f"{'='*72}")

    gov_path = data_dir / "governance_state.json"
    gov = load_json(gov_path)
    if gov is None:
        print("  [WARN] governance_state.json not found!")
        return {}

    # Brain inventory — handle both "brain_states" (BTC) and "brains" (XAU)
    brains = gov.get("brain_states", gov.get("brains", {}))
    if isinstance(brains, dict):
        brain_list = list(brains.values())
    elif isinstance(brains, list):
        brain_list = brains
    else:
        brain_list = []

    print(f"  Total brains in governance: {len(brain_list)}")

    live_brains = [b for b in brain_list if b.get("status") == "live"]
    candidate_brains = [b for b in brain_list if b.get("status") == "candidate"]
    retired_brains = [b for b in brain_list if b.get("status") == "retired"]
    probation_brains = [b for b in brain_list if b.get("status") == "probation"]
    frozen_brains = [b for b in brain_list if b.get("status") == "frozen"]

    print(f"  Live:      {len(live_brains)}")
    print(f"  Candidate: {len(candidate_brains)}")
    print(f"  Probation: {len(probation_brains)}")
    print(f"  Frozen:    {len(frozen_brains)}")
    print(f"  Retired:   {len(retired_brains)}")

    if live_brains:
        print("\n  -- Live Brains --")
        for b in live_brains:
            bid = b.get("brain_id", b.get("id", "?"))
            vw = b.get("vote_weight", b.get("weight", "?"))
            perf = b.get("performance_metrics", {})
            pnl = perf.get("pnl_r", "?") if isinstance(perf, dict) else "?"
            wr = perf.get("win_rate", "?") if isinstance(perf, dict) else "?"
            trades = perf.get("total_trades", "?") if isinstance(perf, dict) else "?"
            print(f"  {bid:30s} vote_weight={vw}, trades={trades}, PnL={pnl}, WR={wr}")

    # Flag brains with suspiciously high trade counts (backtest contamination)
    for b in brain_list:
        bid = b.get("brain_id", b.get("id", "?"))
        perf = b.get("performance_metrics")
        if isinstance(perf, dict):
            trades = perf.get("total_trades", 0) or 0
            pnl = perf.get("pnl_r", 0) or 0
            sharpe = perf.get("sharpe_ratio", 0)
            if trades > 1000:
                print(f"\n  [WARN] {bid}: {trades} trades — possible backtest data contamination!")
                print(f"         PnL={pnl}, Sharpe={sharpe}")
            if sharpe is not None and isinstance(sharpe, int | float) and sharpe < -10:
                print(f"  [WARN] {bid}: Sharpe={sharpe:.1f} — severely negative, data quality suspect")

    # Total decisions count
    total_decisions = gov.get("total_decisions", gov.get("total_trades", 0))
    print(f"\n  Total decisions in governance: {total_decisions}")

    # Journal brain vs governance brain mismatch
    # This was DQAF-042 finding #6 — governance brain count != journal brain count
    transition_log = gov.get("transition_log", [])
    print(f"  Transition log entries: {len(transition_log)}")

    # Track all brains ever registered
    all_registered_brains = set()
    for t in transition_log:
        all_registered_brains.add(t.get("brain_id", ""))
    all_registered_brains.discard("")
    print(f"  Unique brains in transition log: {len(all_registered_brains)}")

    return {
        "brain_count": len(brain_list),
        "live_count": len(live_brains),
        "candidate_count": len(candidate_brains),
        "retired_count": len(retired_brains),
        "total_decisions": total_decisions,
        "transition_log_count": len(transition_log),
        "registered_brains": len(all_registered_brains),
        "suspicious_brains": [b.get("brain_id", "?") for b in brain_list
            if (isinstance(b.get("performance_metrics"), dict) and
                (b.get("performance_metrics", {}).get("total_trades", 0) or 0) > 1000)],
    }


# -- Section 6: Calibrator State ------------------------------------------

def audit_calibrator(data_dir: Path, label: str) -> dict:
    """Audit calibrator state for probability calibration health."""
    print(f"\n{'='*72}")
    print(f"  SECTION 6: Calibrator State Audit — {label}")
    print(f"{'='*72}")

    cal_path = data_dir / "conformal_calibrator_state.json"
    cal = load_json(cal_path)
    if cal is None:
        print("  [WARN] conformal_calibrator_state.json not found!")
        return {}

    # Key metrics
    if isinstance(cal, dict):
        p_win = cal.get("p_win", cal.get("current_p_win", None))
        if p_win is not None:
            print(f"  Current p_win: {p_win:.4f}")

        n_samples = cal.get("n_samples", cal.get("sample_count", 0))
        print(f"  Sample count:  {n_samples}")

        # Check for cold-start (p_win stuck at 0.5)
        if p_win is not None and abs(p_win - 0.5) < 0.01:
            print("  [WARN] p_win near 0.5 — possible cold-start or non-calibrating")

        # Temperature
        temp = cal.get("temperature", cal.get("T", None))
        if temp is not None:
            print(f"  Temperature:   {temp:.4f}")

    return {"p_win": p_win, "n_samples": n_samples}


# -- Section 7: Cross-Source Consistency ----------------------------------

def audit_cross_consistency(data_dir: Path, journal_stats: dict, snap_stats: dict, gov_stats: dict) -> dict:
    """Cross-reference journal trades vs snapshots vs governance."""
    print(f"\n{'='*72}")
    print(f"  SECTION 7: Cross-Source Consistency Audit — {data_dir.name}")
    print(f"{'='*72}")

    issues = []

    # Journal trade count vs governance total_decisions
    jt_count = journal_stats.get("trade_count", 0)
    gov_count = gov_stats.get("total_decisions", 0)
    if jt_count > 0 and gov_count > 0:
        ratio = jt_count / gov_count if gov_count > 0 else 0
        print(f"\n  Journal trades:       {jt_count:,}")
        print(f"  Governance decisions: {gov_count:,}")
        print(f"  Ratio (j/g):          {ratio:.2f}")
        if ratio < 0.5:
            issues.append(f"Governance has {gov_count:,} decisions but journal only {jt_count:,} trades (ratio {ratio:.2f})")
        elif ratio > 2.0:
            issues.append(f"Journal has {jt_count:,} trades but governance only {gov_count:,} decisions")

    # Snapshot coverage vs journal trades
    snap_tickets = snap_stats.get("unique_tickets", 0)
    if jt_count > 0 and snap_tickets > 0:
        coverage = snap_tickets / jt_count if jt_count > 0 else 0
        print(f"  Journal trades:       {jt_count:,}")
        print(f"  Snapshot tickets:     {snap_tickets:,}")
        print(f"  Snapshot coverage:    {coverage*100:.1f}%")
        if coverage < 0.5:
            issues.append(f"Low snapshot coverage: {coverage*100:.1f}% of trades have snapshots")

    # Label gap (DQAF-042 finding #2)
    label_gap_trades = journal_stats.get("close_only", 0)
    if label_gap_trades > 0:
        print(f"  Close-only (no open match): {label_gap_trades:,}")
        if label_gap_trades > jt_count * 0.1:
            issues.append(f"Large close-only gap: {label_gap_trades} closes without matching opens")

    print("\n  -- Consistency Issues --")
    if not issues:
        print("  [OK] No cross-source consistency issues")
    else:
        for issue in issues:
            print(f"  [WARN] {issue}")

    return {"issues": issues}


# -- Section 8: Alpha Signal Audit ----------------------------------------

def audit_alpha(data_dir: Path, label: str) -> dict:
    """Audit alpha feed and performance."""
    print(f"\n{'='*72}")
    print(f"  SECTION 8: Alpha Signal Audit — {label}")
    print(f"{'='*72}")

    reg_path = data_dir / "alpha_registry.json"
    perf_path = data_dir / "alpha_performance.json"
    feed_path = data_dir / "alpha_feed_state.json"

    reg = load_json(reg_path)
    perf = load_json(perf_path)
    feed = load_json(feed_path)

    if reg:
        if isinstance(reg, dict):
            alphas = reg.get("alphas", reg.get("entries", []))
            if isinstance(alphas, dict):
                alphas = list(alphas.values())
            print(f"  Registered alphas: {len(alphas)}")
            # Count active
            active = [a for a in alphas if a.get("status") in ("active", "live", True)]
            print(f"  Active: {len(active)}")
            inactive = [a for a in alphas if a.get("status") in ("inactive", "retired", "disabled", False)]
            print(f"  Inactive: {len(inactive)}")

    if feed:
        print(f"  Feed state: {json.dumps(feed, default=str)[:200]}")

    if perf:
        if isinstance(perf, dict):
            entries = perf.get("entries", perf.get("performance", []))
            if isinstance(entries, dict):
                entries = list(entries.values())
            print(f"  Performance entries: {len(entries)}")

    return {}


# -- Section 9: DQAF-042 Regression Check ---------------------------------

def audit_dqaf042_regression(data_dir: Path, journal_stats: dict, gov_stats: dict) -> dict:
    """Check if any of the 10 DQAF-042 findings have regressed."""
    print(f"\n{'='*72}")
    print(f"  SECTION 9: DQAF-042 Regression Check — {data_dir.name}")
    print(f"{'='*72}")

    regressions = []
    passes = []

    # Finding #1: Leaderboard crash — check if leaderboard.json exists and has data
    lb_path = data_dir / "reports" / "leaderboard.json"
    if lb_path.exists():
        lb = load_json(lb_path)
        if lb and lb.get("brains"):
            passes.append("#1 Leaderboard: present with brains")
        else:
            regressions.append("#1 Leaderboard: present but empty/missing brains")
    else:
        regressions.append("#1 Leaderboard: file missing")

    # Finding #2: Journal vs Labels 38% gap — check close-only ratio
    jt_count = journal_stats.get("trade_count", 0)
    co_count = journal_stats.get("close_only", 0)
    if jt_count > 0:
        gap_pct = co_count / (jt_count + co_count) * 100
        if gap_pct > 20:
            regressions.append(f"#2 Label gap: {gap_pct:.1f}% close-only ({co_count}/{jt_count+co_count})")
        else:
            passes.append(f"#2 Label gap: {gap_pct:.1f}% (acceptable)")

    # Finding #3: Governance backtest contamination
    susp = gov_stats.get("suspicious_brains", [])
    if susp:
        regressions.append(f"#3 Backtest contamination: {len(susp)} brains with >1000 trades in governance: {susp}")
    else:
        passes.append("#3 Backtest contamination: none detected")

    # Finding #4: Calibrator p_win degradation — checked in calibrator section
    # (handled by audit_calibrator)

    # Finding #5: brain_performance dimensions empty
    bp_path = data_dir / "brain_performance.json"
    if bp_path.exists():
        bp = load_json(bp_path)
        if bp:
            passes.append(f"#5 brain_performance.json: present ({len(bp)} entries)")
        else:
            regressions.append("#5 brain_performance.json: empty or invalid")
    else:
        regressions.append("#5 brain_performance.json: file missing")

    # Finding #8: golden_master sorting
    gm_path = data_dir / "golden_master.jsonl"
    if gm_path.exists():
        gm = load_jsonl(gm_path)
        if gm:
            timestamps = [str(r.get("timestamp_utc", r.get("timestamp", r.get("recorded_at", "")))) for r in gm]
            is_sorted = all(timestamps[i] <= timestamps[i+1] for i in range(len(timestamps)-1))
            if is_sorted:
                passes.append("#8 Golden Master: sorted correctly")
            else:
                regressions.append("#8 Golden Master: NOT sorted")

    # Finding #10: State files in git index (check .gitignore enforcement)
    # This is checked via git ls-files, not runtime

    print("\n  -- Regression Status --")
    if regressions:
        for r in regressions:
            print(f"  [REGRESSION] {r}")
    for p in passes:
        print(f"  [PASS] {p}")

    if not regressions:
        print("\n  [OK] No DQAF-042 regressions detected")
    else:
        print(f"\n  [WARN] {len(regressions)} DQAF-042 regression(s) found!")

    return {"regressions": len(regressions), "passes": len(passes), "details": regressions + passes}


# -- Section 10: Data Health Report Cross-Check ---------------------------

def audit_data_health(data_dir: Path, label: str) -> dict:
    """Audit the data health self-report for internal consistency."""
    print(f"\n{'='*72}")
    print(f"  SECTION 10: Data Health Self-Report Audit — {label}")
    print(f"{'='*72}")

    dh_path = data_dir / "state" / "data_health_state.json"
    dh = load_json(dh_path)
    if dh is None:
        print("  [WARN] data_health_state.json not found!")
        return {}

    # Check for PASS/FAIL/WARN counts
    checks = dh.get("checks", dh.get("results", {}))
    if isinstance(checks, list):
        print(f"  Total checks: {len(checks)}")
        by_status = Counter(c.get("status", "unknown") for c in checks)
        for status, cnt in by_status.most_common():
            print(f"  {status}: {cnt}")
    elif isinstance(checks, dict):
        print(f"  Total checks: {len(checks)}")
        by_status = Counter(
            v.get("status", "unknown") if isinstance(v, dict) else str(v)
            for v in checks.values()
        )
        for status, cnt in by_status.most_common():
            print(f"  {status}: {cnt}")

    # Overall status
    overall = dh.get("overall", dh.get("status", "unknown"))
    print(f"\n  Overall status: {overall}")

    # Timestamp
    ts = dh.get("generated_at", dh.get("timestamp", dh.get("recorded_at", "")))
    if ts:
        print(f"  Generated at: {ts}")

    return {"overall": overall}


# -- Main -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Institutional-grade live data deep audit")
    parser.add_argument("--data-dir", default="data_btc", help="Data directory (default: data_btc)")
    parser.add_argument("--full", action="store_true", help="Run on both BTC and XAU")
    args = parser.parse_args()

    data_dirs = []
    if args.full:
        data_dirs = [Path("data_btc"), Path("data")]
    else:
        data_dirs = [Path(args.data_dir)]

    print("=" * 72)
    print("  DEEP AUDIT: Institutional-Grade Live Data Scan")
    print("  Docket: DQAF-20260621-043")
    print(f"  Time:   {datetime.now(UTC).isoformat()}")
    print(f"  Targets: {[str(d) for d in data_dirs]}")
    print("=" * 72)

    all_results = {}
    for dd in data_dirs:
        label = "BTC" if "btc" in str(dd).lower() else "XAU"
        print(f"\n{'#'*72}")
        print(f"  TARGET: {label} ({dd})")
        print(f"{'#'*72}")

        journal_stats = audit_journal(dd, label)
        snap_stats = audit_snapshots(dd, label)
        ledger_stats = audit_ledger(dd, label)
        gm_stats = audit_golden_master(dd, label)
        gov_stats = audit_governance(dd, label)
        cal_stats = audit_calibrator(dd, label)
        cross_stats = audit_cross_consistency(dd, journal_stats, snap_stats, gov_stats)
        alpha_stats = audit_alpha(dd, label)
        dqaf042_stats = audit_dqaf042_regression(dd, journal_stats, gov_stats)
        dh_stats = audit_data_health(dd, label)

        all_results[str(dd)] = {
            "journal": journal_stats,
            "snapshots": snap_stats,
            "ledger": ledger_stats,
            "golden_master": gm_stats,
            "governance": gov_stats,
            "calibrator": cal_stats,
            "cross_consistency": cross_stats,
            "dqaf042_regression": dqaf042_stats,
            "data_health": dh_stats,
        }

    # Summary
    print(f"\n{'='*72}")
    print("  AUDIT COMPLETE")
    print(f"{'='*72}")
    print("\n[DONE] All statistics above are the sole source of truth.")
    return all_results


if __name__ == "__main__":
    main()
