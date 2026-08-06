from __future__ import annotations

#!/usr/bin/env python3
"""
PnL Data Integrity Cross-Validation — Iron Law #11 Compliant
============================================================
Cross-validates three PnL data sources per brain:
  1. brain_performance.json  — LIVE execution outcomes (SSOT per FIX-20260527-002)
  2. governance_state.json   — cumulative performance_metrics (may be backtest-contaminated)
  3. ledger_events.jsonl     — SignalSettled events (shadow/theoretical settlement)

Key FIX references:
  - FIX-20260527-002: brain_performance contamination root fix (per-strategy brain_ids)
  - FIX-20260615-012: Orphan Entry Alert Pollution (752 synthetic, pnl=0)
  - FIX-20260615-011: Ghost Brain Pollution + Unit Mixing (pnl_r vs pnl_per_unit)
  - FIX-20260616-005: Ledger pnl_r is NOT trade PnL (different ledger type)
  - FIX-20260530-056: performance_metrics injection double-silence fixed
  - FIX-20260518-042: entry_price from MT5 history_deals_get (94% had pnl=null)
  - FIX-20260602-058: MIA close PnL always $0 fix
  - FIX-20260517-013: slippage=0.10 added to all PnL paths

Usage:
  python scripts/verify_pnl_data_integrity.py [--data-dir data] [--data-dir-btc data_btc]
"""

import json
import sys
from collections import defaultdict
from pathlib import Path


def load_brain_performance(data_dir: str) -> dict[str, dict]:
    """Load brain_performance.json — the LIVE execution SSOT.

    Structure: {brain_id: [{execution_outcome, composite_score, timestamp, ...}, ...]}
    Window: 100 records per brain.
    """
    bp_path = Path(data_dir) / "brain_performance.json"
    if not bp_path.exists():
        return {}
    with open(bp_path, encoding="utf-8") as f:
        bp = json.load(f)

    # Data is under 'records' key: {brain_id: [{execution_outcome, ...}, ...]}
    records_dict = bp.get("records", {})
    if not isinstance(records_dict, dict):
        # Fallback: top-level iteration (legacy format)
        records_dict = {
            k: v
            for k, v in bp.items()
            if k not in ("schema_version", "window_size", "brain_ids", "records")
            and isinstance(v, list)
        }

    result = {}
    for bid, records in records_dict.items():
        if not isinstance(records, list):
            continue
        wins = sum(
            1 for r in records if isinstance(r, dict) and r.get("execution_outcome") == "win"
        )
        losses = sum(
            1 for r in records if isinstance(r, dict) and r.get("execution_outcome") == "loss"
        )
        total = wins + losses
        wr = wins / total if total > 0 else 0.0
        result[bid] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "wr": round(wr, 4),
            "source": "brain_performance.json (LIVE SSOT)",
        }
    return result


def load_governance_metrics(data_dir: str) -> dict[str, dict]:
    """Load governance performance_metrics (may be backtest-contaminated per 2026-06-11 audit)."""
    gov_path = Path(data_dir) / "governance_state.json"
    if not gov_path.exists():
        return {}
    try:
        with open(gov_path, encoding="utf-8") as f:
            gs = json.load(f)
    except UnicodeDecodeError:
        with open(gov_path, encoding="gbk") as f:
            gs = json.load(f)

    result = {}
    for bid, state in gs.get("brain_states", {}).items():
        pm = state.get("performance_metrics", {}) or {}
        trades = pm.get("total_trades", 0)
        if trades > 0:
            result[bid] = {
                "total": trades,
                "wr": pm.get("win_rate", 0) or 0,
                "pf": pm.get("profit_factor", 0) or 0,
                "pnl_r": pm.get("pnl_r", 0) or 0,
                "sharpe": pm.get("sharpe_ratio", 0) or 0,
                "status": state.get("status", "?"),
                "source": "governance_state.performance_metrics",
            }
    return result


def load_signalsettled_stats(data_dir: str) -> dict[str, dict]:
    """Aggregate SignalSettled events per brain from ledger_events.jsonl."""
    events_path = Path(data_dir) / "ledger_events.jsonl"
    if not events_path.exists():
        return {}

    brains: dict[str, dict] = defaultdict(lambda: {"pnl_list": [], "long": 0, "short": 0})
    with open(events_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event_type") != "SignalSettled":
                continue
            bid = ev.get("brain_id", "?")
            pnl_r = ev.get("pnl_r", 0) or 0
            brains[bid]["pnl_list"].append(pnl_r)
            d = ev.get("direction", "?")
            if d == "long":
                brains[bid]["long"] += 1
            elif d == "short":
                brains[bid]["short"] += 1

    result = {}
    for bid, b in brains.items():
        pnls = b["pnl_list"]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0.001)
        losses = sum(1 for p in pnls if p < -0.001)
        be = n - wins - losses
        wr = wins / (wins + losses) if (wins + losses) > 0 else 0.0
        total_pnl = sum(pnls)
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
        total_dec = b["long"] + b["short"]
        dir_bias = b["long"] / total_dec if total_dec > 0 else 0.5

        result[bid] = {
            "total": n,
            "wins": wins,
            "losses": losses,
            "be": be,
            "wr": round(wr, 4),
            "pf": round(pf, 2),
            "pnl_r_sum": round(total_pnl, 2),
            "dir_bias": round(dir_bias, 3),
            "source": "ledger_events.jsonl (SignalSettled)",
        }
    return result


def print_header(title: str):
    print()
    print("=" * 110)
    print(f"  {title}")
    print("=" * 110)


def main():
    data_dir = "data"
    data_dir_btc = "data_btc"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--data-dir" and i < len(sys.argv) - 1:
            data_dir = sys.argv[i + 1]
        if arg == "--data-dir-btc" and i < len(sys.argv) - 1:
            data_dir_btc = sys.argv[i + 1]

    for symbol, dd in [("XAU", data_dir), ("BTC", data_dir_btc)]:
        print_header(f"{symbol}/USD — PnL Data Source Cross-Validation")

        bp = load_brain_performance(dd)
        gov = load_governance_metrics(dd)
        ss = load_signalsettled_stats(dd)

        # Collect all brain IDs across all sources
        all_bids = set(bp.keys()) | set(gov.keys()) | set(ss.keys())

        print(
            f"\n  Sources found: brain_perf={len(bp)}, governance={len(gov)}, SignalSettled={len(ss)}"
        )
        print(f"  Total unique brains: {len(all_bids)}")

        # Table header
        hdr = (
            f"  {'Brain ID':<40} {'Live(BP)':>10} {'Gov(Trades)':>12} "
            f"{'Gov(WR)':>8} {'Gov(PF)':>8} {'SS(WR)':>8} {'SS(PF)':>8} "
            f"{'Discrepancy':>16}"
        )
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))

        discrepancies = []
        for bid in sorted(all_bids):
            bp_data = bp.get(bid, {})
            gov_data = gov.get(bid, {})
            ss_data = ss.get(bid, {})

            bp_trades = bp_data.get("total", 0)
            bp_wr = bp_data.get("wr", 0)
            gov_trades = gov_data.get("total", 0)
            gov_wr = gov_data.get("wr", 0)
            gov_pf = gov_data.get("pf", 0)
            ss_n = ss_data.get("total", 0)
            ss_wr = ss_data.get("wr", 0)
            ss_pf = ss_data.get("pf", 0)

            # Determine discrepancy
            disc = ""
            disc_sev = ""
            if bp_trades > 0 and gov_trades > 0:
                wr_diff = abs(bp_wr - gov_wr)
                if wr_diff > 0.15:
                    disc = f"BP_WR={bp_wr:.3f} vs Gov_WR={gov_wr:.3f} (diff={wr_diff:.3f})"
                    disc_sev = "!!"
            if bp_trades > 0 and gov_trades == 0:
                disc = f"Live has {bp_trades} trades, governance EMPTY"
                disc_sev = "!!"
            if gov_trades > 0 and bp_trades == 0:
                disc = f"Gov has {gov_trades} trades, live EMPTY (BACKTEST?)"
                disc_sev = "??"
            if bp_trades == 0 and gov_trades == 0 and ss_n > 0:
                disc = f"Only SignalSettled ({ss_n}), no live/governance"
                disc_sev = "--"
            if bp_trades == 0 and gov_trades == 0 and ss_n == 0:
                disc = "NO DATA in any source"
                disc_sev = "XX"

            if disc_sev in ("!!", "??"):
                discrepancies.append((bid, disc, disc_sev))

            bp_str = f"{bp_trades}t/{bp_wr:.3f}" if bp_trades > 0 else "-"
            gov_t_str = f"{gov_trades}t" if gov_trades > 0 else "-"
            gov_wr_str = f"{gov_wr:.3f}" if gov_trades > 0 else "-"
            gov_pf_str = f"{gov_pf:.2f}" if gov_trades > 0 else "-"
            ss_wr_str = f"{ss_wr:.3f}" if ss_n > 0 else "-"
            ss_pf_str = f"{ss_pf:.2f}" if ss_n > 0 else "-"

            print(
                f"  {bid:<40} {bp_str:>10} {gov_t_str:>12} "
                f"{gov_wr_str:>8} {gov_pf_str:>8} {ss_wr_str:>8} {ss_pf_str:>8} "
                f"{disc_sev:>4} {disc[:42]:<42}"
            )

        # Summary of discrepancies
        if discrepancies:
            print(f"\n  !! DISCREPANCIES FOUND ({len(discrepancies)}):")
            for bid, disc, sev in discrepancies:
                print(f"    [{sev}] {bid}: {disc}")

        # Source reliability assessment
        print("\n  --- Source Reliability Assessment ---")
        bp_with_data = sum(1 for b in bp.values() if b["total"] > 0)
        gov_with_data = sum(1 for b in gov.values() if b["total"] > 0)
        ss_with_data = sum(1 for b in ss.values() if b["total"] > 0)
        print(f"  brain_performance.json:  {bp_with_data} brains with LIVE trade data  <- SSOT")
        print(f"  governance_state.json:   {gov_with_data} brains with metrics  (may be backtest)")
        print(f"  ledger_events.jsonl:     {ss_with_data} brains with SignalSettled  (theoretical)")

        # Check for FIX-20260615-012 contamination (orphan entries with pnl=0)
        if ss:
            orphan_suspects = []
            for bid, s in ss.items():
                if s["total"] >= 50 and s["be"] > s["total"] * 0.5:
                    orphan_suspects.append((bid, s["total"], s["be"]))
            if orphan_suspects:
                print("\n  !! ORPHAN-CONTAMINATED BRAINS (>50% breakeven, per FIX-20260615-012):")
                for bid, n, be in orphan_suspects[:10]:
                    print(f"    {bid}: {be}/{n} breakeven ({be/n*100:.0f}%)")

        # Check for FIX-20260527-002 contamination (identical records across brains)
        if bp:
            bp_trade_counts = {bid: d["total"] for bid, d in bp.items() if d["total"] > 0}
            # Brains with identical trade counts may be contaminated
            count_groups = defaultdict(list)
            for bid, count in bp_trade_counts.items():
                count_groups[count].append(bid)
            contaminated_groups = {c: bids for c, bids in count_groups.items() if len(bids) >= 3}
            if contaminated_groups:
                print(
                    "\n  !! POTENTIAL CONTAMINATION (FIX-20260527-002: identical per-trade records):"
                )
                for count, bids in sorted(contaminated_groups.items()):
                    print(f"    {count} trades shared by: {', '.join(bids[:5])}")

    print()
    print("[DONE] All cross-validation data above is the sole source of truth.")
    print("Legend: BP=brain_performance(LIVE SSOT), Gov=governance, SS=SignalSettled")
    print("Discrepancy: !! = critical, ?? = suspect backtest, -- = shadow-only, XX = no data")


if __name__ == "__main__":
    main()
