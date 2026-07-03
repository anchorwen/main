#!/usr/bin/env python
"""T24: V6 Shadow Exit Priority Queue Analysis Script.

Reads ``v6_shadow_exits.jsonl`` (produced by management_phase.py telemetry)
and optionally cross-references with ``live_trade_journal.jsonl`` to
compare V6 exit recommendations against actual trade outcomes.

Usage:
  python scripts/analyze_shadow_exit.py --data-dir data              # XAU
  python scripts/analyze_shadow_exit.py --data-dir data_btc          # BTC
  python scripts/analyze_shadow_exit.py --data-dir data --since 2026-07-03
  python scripts/analyze_shadow_exit.py --data-dir data --summary-only

Output sections:
  1. Summary — total cycles, trigger rate, per-priority breakdown
  2. Per-Strategy — trigger counts by strategy line
  3. Ratchet Telemetry — breakeven/drawdown arming and firing rates
  4. PnL Trajectory — PnL distribution at evaluation time
  5. Cross-Reference — V6 shadow exit vs actual journal close (if journal exists)

Design principles:
  - position_ticket-based dedup — each shadow line is one cycle on one position
  - trigger = v6_triggered==true in telemetry record
  - journal cross-ref matches position_ticket to find actual close reason
  - all outputs are deterministic; no sampling or estimation
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="T24: Analyze V6 Exit Priority Queue shadow logs")
    p.add_argument(
        "--data-dir",
        default="data",
        help="Data directory containing reports/ and live_trade_journal.jsonl (default: data)",
    )
    p.add_argument(
        "--since",
        default="",
        help="Only analyze records on or after this date (YYYY-MM-DD)",
    )
    p.add_argument(
        "--summary-only",
        action="store_true",
        help="Output only the summary section (no per-ticket detail)",
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Show top N tickets in detail output (default: 20)",
    )
    return p.parse_args()


def _load_shadow_log(path: Path, since: str = "") -> list[dict]:
    """Load and parse v6_shadow_exits.jsonl."""
    records: list[dict] = []
    since_dt = datetime.fromisoformat(since).replace(tzinfo=UTC) if since else None

    if not path.exists():
        print(f"[WARN] Shadow log not found: {path}")
        return records

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since_dt:
                try:
                    t_str = rec.get("time", "")
                    if t_str:
                        t = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                        if t < since_dt:
                            continue
                except (ValueError, TypeError):
                    pass
            records.append(rec)
    return records


def _load_journal(path: Path) -> list[dict]:
    """Load live_trade_journal.jsonl, keeping only close entries with PnL."""
    entries: list[dict] = []
    if not path.exists():
        return entries
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") == "close" and e.get("pnl") is not None:
                entries.append(e)
    return entries


def _fmt_pct(n: int, d: int) -> str:
    if d == 0:
        return "N/A"
    return f"{100*n/d:.1f}%"


def _section(title: str) -> None:
    print()
    print(f"{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def _subsection(title: str) -> None:
    print(f"\n  --- {title} ---")


def analyze(data_dir: str, since: str = "", summary_only: bool = False, top_n: int = 20) -> int:
    data_path = Path(data_dir)
    shadow_path = data_path / "reports" / "v6_shadow_exits.jsonl"
    journal_path = data_path / "live_trade_journal.jsonl"

    records = _load_shadow_log(shadow_path, since)
    if not records:
        print(f"No shadow telemetry records found in {shadow_path}")
        print("Waiting for management phase cycles with active positions.")
        return 0

    journal_closes = _load_journal(journal_path)
    closes_by_ticket: dict[int, list[dict]] = defaultdict(list)
    for e in journal_closes:
        t = e.get("position_ticket")
        if t is not None:
            closes_by_ticket[t].append(e)

    # ── 1. Summary ──
    _section("1. SHADOW EVALUATION SUMMARY")

    n_total = len(records)
    n_triggered = sum(1 for r in records if r.get("v6_triggered"))
    n_not_triggered = n_total - n_triggered

    unique_tickets: set[int] = set()
    for r in records:
        t = r.get("ticket", 0)
        if t:
            unique_tickets.add(t)

    n_positions = len(unique_tickets)

    print(f"  Total evaluation cycles:  {n_total}")
    print(f"  Unique positions:         {n_positions}")
    print(f"  V6 exit triggered:        {n_triggered} ({_fmt_pct(n_triggered, n_total)})")
    print(f"  No trigger (evaluated):   {n_not_triggered} ({_fmt_pct(n_not_triggered, n_total)})")

    if not records:
        return 0

    # Date range
    times = []
    for r in records:
        try:
            t_str = r.get("time", "")
            if t_str:
                times.append(datetime.fromisoformat(t_str.replace("Z", "+00:00")))
        except (ValueError, TypeError):
            pass
    if times:
        print(f"  Time range:               {min(times).isoformat()} → {max(times).isoformat()}")

    # ── 2. Per-Priority Breakdown ──
    _subsection("2a. Exit Priority Breakdown")
    priority_counts: dict[str, int] = defaultdict(int)
    exit_code_counts: dict[str, int] = defaultdict(int)
    for r in records:
        if r.get("v6_triggered"):
            p = r.get("v6_priority", "UNKNOWN")
            priority_counts[str(p)] += 1
            ec = r.get("v6_exit_code", "UNKNOWN")
            exit_code_counts[str(ec)] += 1

    if priority_counts:
        print(f"  {'Priority':<12} {'Count':>6}  {'%':>7}")
        print(f"  {'-'*12} {'-'*6}  {'-'*7}")
        for p_name, cnt in sorted(priority_counts.items(), key=lambda x: -x[1]):
            print(f"  {p_name:<12} {cnt:>6}  {_fmt_pct(cnt, n_triggered):>7}")
        if exit_code_counts:
            print(f"\n  {'Exit Code':<30} {'Count':>6}")
            print(f"  {'-'*30} {'-'*6}")
            for ec, cnt in sorted(exit_code_counts.items(), key=lambda x: -x[1]):
                print(f"  {ec:<30} {cnt:>6}")
    else:
        print("  No V6 exits triggered yet. (Waiting for conditions to be met.)")
        print("  Active thresholds (from exit_priority.yaml):")
        print("    P6 breakeven: PnL > atr_mult * ATR * vol * 100  (currently atr_mult=0.3)")
        print(
            "    P6 drawdown:  peak PnL > activation_atr * ATR * vol * 100  (currently activation_atr=0.5)"
        )
        print("    P7 timestop:  bars_held > hold_mult * half_life  (currently hold_mult=3.0)")

    # ── 2b. Per-Strategy Breakdown ──
    _subsection("2b. Per-Strategy Breakdown")
    strategy_cycles: dict[str, int] = defaultdict(int)
    strategy_triggers: dict[str, int] = defaultdict(int)
    strategy_pnls: dict[str, list[float]] = defaultdict(list)
    for r in records:
        s = r.get("strategy", "unknown")
        strategy_cycles[s] += 1
        if r.get("v6_triggered"):
            strategy_triggers[s] += 1
        pnl = r.get("current_pnl")
        if pnl is not None:
            strategy_pnls[s].append(float(pnl))

    if strategy_cycles:
        print(f"  {'Strategy':<20} {'Cycles':>7} {'Triggers':>9} {'Trigger%':>9} {'Avg PnL':>10}")
        print(f"  {'-'*20} {'-'*7} {'-'*9} {'-'*9} {'-'*10}")
        for s_name in sorted(strategy_cycles.keys()):
            c = strategy_cycles[s_name]
            t = strategy_triggers.get(s_name, 0)
            pnls = strategy_pnls.get(s_name, [])
            avg_pnl = f"${sum(pnls)/len(pnls):.3f}" if pnls else "N/A"
            print(f"  {s_name:<20} {c:>7} {t:>9} {_fmt_pct(t, c):>9} {avg_pnl:>10}")

    # ── 3. Ratchet Telemetry ──
    _subsection("3. RATCHET RISK TELEMETRY")
    n_breakeven_armed = sum(1 for r in records if r.get("ratchet_breakeven_armed"))
    n_breakeven_fired = sum(1 for r in records if r.get("ratchet_breakeven_fired"))
    n_drawdown_armed = sum(1 for r in records if r.get("ratchet_drawdown_armed"))
    n_drawdown_fired = sum(1 for r in records if r.get("ratchet_drawdown_fired"))

    print(f"  Breakeven armed:    {n_breakeven_armed:>6}  ({_fmt_pct(n_breakeven_armed, n_total)})")
    print(f"  Breakeven fired:    {n_breakeven_fired:>6}  ({_fmt_pct(n_breakeven_fired, n_total)})")
    print(f"  Drawdown armed:     {n_drawdown_armed:>6}  ({_fmt_pct(n_drawdown_armed, n_total)})")
    print(f"  Drawdown fired:     {n_drawdown_fired:>6}  ({_fmt_pct(n_drawdown_fired, n_total)})")

    # ── 4. PnL Trajectory ──
    _subsection("4. PNL DISTRIBUTION AT EVALUATION TIME")
    all_pnls = [float(r.get("current_pnl", 0) or 0) for r in records]
    if all_pnls:
        import statistics

        all_pnls.sort()
        n_pnl = len(all_pnls)
        print(f"  Count:   {n_pnl}")
        print(f"  Min:     ${min(all_pnls):.4f}")
        print(f"  P25:     ${all_pnls[n_pnl//4]:.4f}")
        print(f"  Median:  ${all_pnls[n_pnl//2]:.4f}")
        print(f"  P75:     ${all_pnls[3*n_pnl//4]:.4f}")
        print(f"  Max:     ${max(all_pnls):.4f}")
        print(f"  Mean:    ${statistics.mean(all_pnls):.4f}")
        try:
            print(f"  StdDev:  ${statistics.stdev(all_pnls):.4f}")
        except statistics.StatisticsError:
            pass

        pos_pnl = [p for p in all_pnls if p > 0]
        neg_pnl = [p for p in all_pnls if p < 0]
        zero_pnl = [p for p in all_pnls if p == 0]
        print(f"  PnL>0:   {len(pos_pnl)} ({_fmt_pct(len(pos_pnl), n_pnl)})")
        print(f"  PnL<0:   {len(neg_pnl)} ({_fmt_pct(len(neg_pnl), n_pnl)})")
        print(f"  PnL=0:   {len(zero_pnl)} ({_fmt_pct(len(zero_pnl), n_pnl)})")

    # ── 5. Cross-Reference: V6 vs Actual ──
    if journal_closes:
        _subsection("5. CROSS-REFERENCE: V6 SHADOW vs ACTUAL EXIT")

        matched = 0
        v6_suggested: dict[int, str] = {}  # ticket → v6_exit_code
        for r in records:
            t = r.get("ticket", 0)
            if t and r.get("v6_triggered") and t not in v6_suggested:
                v6_suggested[t] = r.get("v6_exit_code", "UNKNOWN")

        actual_reasons: dict[str, int] = defaultdict(int)
        for _close_list in closes_by_ticket.values():
            for _entry in _close_list:
                _detail: dict[str, Any] = (
                    _entry.get("detail", {}) if isinstance(_entry.get("detail"), dict) else {}
                )
                reason: str = str(_detail.get("reason", _entry.get("label", "unknown")))
                actual_reasons[reason] += 1

        print(f"  Total closes in journal:    {len(journal_closes)}")
        print(f"  Tracks with V6 suggestion:  {len(v6_suggested)}")
        print(f"  Matched (V6→actual):        {matched}")

        if v6_suggested:
            print(f"\n  V6 Suggested Exits:")
            for ticket, code in sorted(v6_suggested.items()):
                close_entry = closes_by_ticket.get(ticket, [])
                actual_reason = (
                    "open"
                    if not close_entry
                    else close_entry[0]
                    .get("detail", {})
                    .get("reason", close_entry[0].get("label", "?"))
                )
                actual_pnl = close_entry[0].get("pnl", "?") if close_entry else "open"
                print(
                    f"    ticket={ticket}  V6={code:<25}  actual={str(actual_reason):<20}  PnL={actual_pnl}"
                )
                matched += 1
                if not summary_only and matched >= top_n:
                    print(f"    ... (showing top {top_n}, use --top-n for more)")
                    break

        print(f"\n  Actual Exit Reasons (all closes):")
        for reason, cnt in sorted(actual_reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:<30} {cnt:>5}")

    # ── 6. Recent Evaluation Samples ──
    if not summary_only:
        _subsection("6. RECENT EVALUATION SAMPLES (last 10)")
        for r in records[-10:]:
            t = r.get("ticket", "?")
            s = r.get("strategy", "?")
            pnl = r.get("current_pnl", 0)
            bar = r.get("bar", 0)
            trig = "FIRE" if r.get("v6_triggered") else "eval"
            code = r.get("v6_exit_code", "") if r.get("v6_triggered") else ""
            be = "BE" if r.get("ratchet_breakeven_armed") else ""
            dd = "DD" if r.get("ratchet_drawdown_armed") else ""
            flags = f"{be}{'/' if be and dd else ''}{dd}" or "-"
            # T23: M15/M30 regime data
            m15 = f"m15={r['m15_regime_prob']:.2f}" if r.get("m15_regime_prob") is not None else ""
            m30 = f"m30={r['m30_regime']:.2f}" if r.get("m30_regime") is not None else ""
            regime_str = f"{m15} {m30}".strip() or "-"
            print(
                f"  t={t} {s:<15} bar={bar:<5} PnL=${float(pnl):.4f}  "
                f"[{trig}] {code}  ratchet=[{flags}]  regime=[{regime_str}]"
            )

    # ── 6b. M15/M30 Regime Telemetry ──
    _subsection("6b. M15/M30 REGIME TELEMETRY (T23)")
    _m15_probs = [r["m15_regime_prob"] for r in records if r.get("m15_regime_prob") is not None]
    _m30_regimes = [r["m30_regime"] for r in records if r.get("m30_regime") is not None]
    if _m15_probs:
        import statistics as _st

        print(
            f"  M15 regime prob:  n={len(_m15_probs)}  "
            f"mean={_st.mean(_m15_probs):.3f}  "
            f"min={min(_m15_probs):.3f}  max={max(_m15_probs):.3f}"
        )
        _m15_low = sum(1 for p in _m15_probs if p < 0.30)
        _m15_high = sum(1 for p in _m15_probs if p > 0.60)
        print(
            f"    low (<0.30): {_m15_low} ({_fmt_pct(_m15_low, len(_m15_probs))})  "
            f"high (>0.60): {_m15_high} ({_fmt_pct(_m15_high, len(_m15_probs))})"
        )
    if _m30_regimes:
        import statistics as _st2

        print(
            f"  M30 regime:       n={len(_m30_regimes)}  "
            f"mean={_st2.mean(_m30_regimes):.3f}  "
            f"min={min(_m30_regimes):.3f}  max={max(_m30_regimes):.3f}"
        )
    if not _m15_probs and not _m30_regimes:
        print("  No M15/M30 regime data yet. Requires T23 code + process restart.")
    else:
        print("  T23 data feed: ACTIVE — StageGate + P3 RegimeCollapse have regime inputs")

    # ── 7. Readiness Assessment ──
    _subsection("7. T19/T24 READINESS ASSESSMENT")
    if n_triggered >= 100:
        print("  STATUS: READY for CP3 (>=100 shadow triggers)")
        print("  Next: review exit code distribution, check for false positives")
        print("  Then: CP4 — enable P6+P7 production execution")
    elif n_triggered >= 50:
        print(f"  STATUS: CP2 threshold met ({n_triggered} >= 50)")
        print(f"  Next: continue observation until 100 for CP3")
        print(
            f"  Rate: ~{n_triggered/max(1, (max(times)-min(times)).total_seconds()/3600):.1f} triggers/hr"
        )
    elif n_triggered >= 20:
        print(f"  STATUS: T24 script validated ({n_triggered} >= 20 triggers)")
        print(f"  Next: continue observation, target 50 for CP2")
    elif n_total > 0:
        print(f"  STATUS: Evaluating (0 triggers in {n_total} cycles)")
        print(f"  Positions observed: {n_positions}")
        print(f"  Avg cycles/position: {n_total/max(1, n_positions):.0f}")
        print(f"  Recommendation: verify process has restarted to pick up new config")
    else:
        print("  STATUS: No data — waiting for first management phase cycle")
        print("  Required: process restart to load V6 shadow telemetry code")

    return 0


if __name__ == "__main__":
    args = _parse_args()
    rc = analyze(
        data_dir=args.data_dir,
        since=args.since,
        summary_only=args.summary_only,
        top_n=args.top_n,
    )
    raise SystemExit(rc)
