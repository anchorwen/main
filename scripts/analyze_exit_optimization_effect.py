"""
Analyze Exit Logic Optimization Effect (2026-06-10 ~ 06-13)
=============================================================
Iron Law #11 compliant: all statistics from structured data, not text-snippet reading.

Exit optimization timeline:
  - Baseline: before 2026-06-10 (pre-optimization)
  - Transition: 2026-06-10 to 2026-06-13 (deployment window)
  - Optimized: after 2026-06-13 (post-optimization)

Optimizations analyzed:
  - FIX-20260610-008: _classify_exit_reason() 12→31 patterns, New categories: kalman_flip,
    meta_exit(6 subtypes), net_out, watchdog, emergency_close, partial_tp→tp_hit
  - FIX-20260611-017: Trail parameter adjustment (BTC swing trail 0.5→1.0)
  - FIX-20260613-081: trail_activation_atr 0.5→0.3, breakeven_threshold_atr 1.5→1.0
  - FIX-20260613-086: Watchdog multi-dimensional evaluator (time_decay + price_decay)

De-duplication logic:
  - Group by position_ticket
  - If multiple close events for same ticket, take the one with non-null pnl
  - Shadow signals (position_ticket=0) excluded from exit analysis

PnL definition: pnl_r (R-multiple) from close event's "pnl" field.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Date cutoffs ──
BASELINE_END = "2026-06-10T00:00:00"
TRANSITION_END = "2026-06-14T00:00:00"  # day after last fix


def parse_date(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # Handle various ISO formats
        ts = ts.replace("Z", "+00:00")
        if "T" in ts:
            return datetime.fromisoformat(ts)
        return None
    except (ValueError, TypeError):
        return None


def get_period(ts: str | None) -> str:
    """Assign a trade to baseline / transition / optimized period."""
    if not ts:
        return "unknown"
    if ts < BASELINE_END:
        return "baseline"
    elif ts < TRANSITION_END:
        return "transition"
    else:
        return "optimized"


def _classify_exit_detail(label: str | None, detail: dict | None) -> str:
    """Classify exit reason from journal close event (label + detail.reason).

    Uses the DETAIL REASON as primary source of truth when available,
    because the label can be misleading:
      - label="breakeven" + detail.reason="mt5_deal_reason_3" → MIA, not breakeven
      - label="close_accepted" + detail.reason="sl_hit" → SL hit via managed close

    Updated with real journal taxonomy discovered 2026-06-21.
    """
    reason = ""
    if isinstance(detail, dict):
        reason = detail.get("reason", "")

    # ── Layer 0: Detail reason FIRST (most specific, broker/operator ground truth) ──
    if reason == "tp_hit":
        return "tp_hit"
    if reason == "sl_hit":
        return "sl_hit"
    if reason in ("mia_close", "mt5_deal_reason_3"):
        return "mia_close"  # DQAF-033: broker-side closes with unknown trigger
    if reason in ("unknown_close", "position_not_found"):
        return "unknown_close"
    if reason == "client_close":
        return "client_close"
    if reason in ("auto_orphan_rejected", "auto_orphan_stale"):
        return "orphan_sweeper"

    # ── Layer 1: Label patterns (managed/system exits) ──
    if label in ("tp_hit_first", "tp_hit", "win"):
        return "tp_hit"
    if label == "sl_hit_first":
        return "sl_hit"
    if label in ("sl_hit_trailed", "sl_hit"):
        return "sl_hit"
    if label == "loss":
        # "loss" label without detail reason → unclassified SL/pnl exit
        return "sl_hit"
    if label and label.startswith("exit_watchdog:"):
        # Extract watchdog subtype for finer analysis
        subtype = label.split(":", 1)[1] if ":" in label else ""
        if "confidence_decay" in subtype:
            return "watchdog_confidence_decay"
        if "bleed_stop" in subtype:
            return "watchdog_bleed_stop"
        if "hesitation" in subtype:
            return "watchdog_hesitation"
        if "meta_exit" in subtype:
            return "meta_exit"
        if "signal_reversal" in subtype:
            return "watchdog_signal_reversal"
        return "watchdog_other"
    if label and label.startswith("exit_meta:"):
        return "meta_exit"
    if label == "breakeven":
        return "breakeven"
    if label == "close_accepted":
        # Managed close accepted by broker — check if we have more specific info
        # Without detail.reason, it's an intentional managed exit
        return "managed_close"
    if label == "brain_flip":
        return "brain_flip"
    if label == "confidence_decay" or label == "confidence_drop":
        return "confidence_decay"
    if label == "momentum_pause":
        return "momentum_pause"
    if label == "time_exit":
        return "time_exit"

    # ── Layer 2: Meta exit subtypes from detail (FIX-20260610-008 sub-fix D) ──
    if reason in ("pnl_urgency", "time_decay", "regime_misalignment",
                  "consensus_drift", "vol_expansion", "ml_p_win"):
        return "meta_exit"
    if reason == "kalman_velocity_flip":
        return "kalman_flip"
    if reason == "net_out":
        return "net_out"
    if reason == "grace_period_emergency":
        return "emergency_close"

    return "other"


def compute_exit_analysis(data_dir: str) -> dict[str, Any]:
    """Main analysis entry point."""
    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return {"error": f"Journal not found: {journal_path}"}

    # ── 1. Load and deduplicate trades ──
    records: list[dict] = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Build trade map: ticket -> {open, close}
    trades: dict[int, dict] = defaultdict(
        lambda: {"open": None, "close_pnl": None, "close_label": None,
                 "close_detail": None, "close_ts": None, "brain_ids": [],
                 "side": "unknown"}
    )

    for rec in records:
        ticket = rec.get("position_ticket")
        if ticket is None or ticket == 0:
            continue  # exclude shadow signals
        action = rec.get("action", "")
        if action == "open":
            trades[ticket]["open"] = rec
            trades[ticket]["brain_ids"] = rec.get("brain_ids") or ["unknown"]
            trades[ticket]["side"] = rec.get("side", "unknown")
        elif action == "close":
            # Dedup: prefer non-null pnl
            pnl = rec.get("pnl")
            existing_pnl = trades[ticket]["close_pnl"]
            if pnl is not None and (existing_pnl is None or abs(pnl) > 0):
                trades[ticket]["close_pnl"] = pnl
                trades[ticket]["close_label"] = rec.get("label")
                trades[ticket]["close_detail"] = rec.get("detail")
                trades[ticket]["close_ts"] = rec.get("recorded_at")

    # ── 2. Classify each trade ──
    classified: list[dict] = []
    for ticket, td in trades.items():
        open_rec = td["open"]
        pnl = td["close_pnl"]
        if open_rec is None or pnl is None:
            continue

        open_ts = open_rec.get("recorded_at", "")
        close_ts = td.get("close_ts", "")
        period = get_period(close_ts)
        exit_cat = _classify_exit_detail(td["close_label"], td["close_detail"])
        label = td["close_label"] or "unknown"
        detail_reason = ""
        if isinstance(td["close_detail"], dict):
            detail_reason = td["close_detail"].get("reason", "")

        classified.append({
            "ticket": ticket,
            "open_ts": open_ts,
            "close_ts": close_ts,
            "period": period,
            "pnl": pnl,
            "exit_cat": exit_cat,
            "label": label,
            "detail_reason": detail_reason,
            "brain_ids": td["brain_ids"],
            "side": td["side"],
        })

    if not classified:
        return {"error": "No completed trades found"}

    # ── 3. Period-level aggregates ──
    periods = ["baseline", "transition", "optimized"]
    period_stats: dict[str, Any] = {}

    for period in periods:
        trades_p = [t for t in classified if t["period"] == period]
        if not trades_p:
            period_stats[period] = {"n_trades": 0, "note": "no trades in this period"}
            continue

        n = len(trades_p)
        wins = [t for t in trades_p if t["pnl"] > 0]
        losses = [t for t in trades_p if t["pnl"] < 0]
        bes = [t for t in trades_p if t["pnl"] == 0]

        total_pnl = sum(t["pnl"] for t in trades_p)
        wr = len(wins) / max(n - len(bes), 1)
        avg_win = sum(t["pnl"] for t in wins) / max(len(wins), 1)
        avg_loss = sum(t["pnl"] for t in losses) / max(len(losses), 1)
        profit_factor = sum(t["pnl"] for t in wins) / max(abs(sum(t["pnl"] for t in losses)), 0.01)

        # Exit reason distribution
        exit_dist: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl_sum": 0.0, "pnls": []})
        for t in trades_p:
            cat = t["exit_cat"]
            exit_dist[cat]["count"] += 1
            exit_dist[cat]["pnl_sum"] += t["pnl"]
            exit_dist[cat]["pnls"].append(t["pnl"])

        exit_stats = {}
        for cat, data in sorted(exit_dist.items(), key=lambda x: -x[1]["count"]):
            pnls = data["pnls"]
            exit_stats[cat] = {
                "count": data["count"],
                "pct": round(data["count"] / n * 100, 1),
                "pnl_total": round(data["pnl_sum"], 2),
                "pnl_avg": round(data["pnl_sum"] / data["count"], 2),
                "win_rate": round(sum(1 for p in pnls if p > 0) / max(len(pnls), 1) * 100, 1),
                "median_pnl": round(sorted(pnls)[len(pnls) // 2], 3),
            }

        # PnL distribution quartiles
        pnl_sorted = sorted(t["pnl"] for t in trades_p)
        q25_idx = max(0, len(pnl_sorted) // 4)
        q75_idx = max(0, 3 * len(pnl_sorted) // 4)

        period_stats[period] = {
            "n_trades": n,
            "total_pnl_r": round(total_pnl, 2),
            "avg_pnl_per_trade": round(total_pnl / n, 3),
            "win_rate_pct": round(wr * 100, 1),
            "profit_factor": round(profit_factor, 4),
            "avg_win": round(avg_win, 3),
            "avg_loss": round(avg_loss, 3),
            "breakeven_count": len(bes),
            "breakeven_pct": round(len(bes) / n * 100, 1),
            "pnl_median": round(pnl_sorted[len(pnl_sorted) // 2], 3),
            "pnl_q25": round(pnl_sorted[q25_idx], 3) if q25_idx < len(pnl_sorted) else 0,
            "pnl_q75": round(pnl_sorted[q75_idx], 3) if q75_idx < len(pnl_sorted) else 0,
            "exit_reasons": exit_stats,
        }

    # ── 4. Exit category pre/post comparison ──
    baseline_cats = period_stats.get("baseline", {}).get("exit_reasons", {})
    optimized_cats = period_stats.get("optimized", {}).get("exit_reasons", {})

    exit_comparison: dict[str, dict] = {}
    all_cats = sorted(set(list(baseline_cats.keys()) + list(optimized_cats.keys())))
    for cat in all_cats:
        b = baseline_cats.get(cat, {"count": 0, "pct": 0, "pnl_total": 0, "win_rate": 0})
        o = optimized_cats.get(cat, {"count": 0, "pct": 0, "pnl_total": 0, "win_rate": 0})
        exit_comparison[cat] = {
            "baseline_count": b["count"],
            "baseline_pct": b["pct"],
            "baseline_win_rate": b["win_rate"],
            "optimized_count": o["count"],
            "optimized_pct": o["pct"],
            "optimized_win_rate": o["win_rate"],
        }

    # ── 5. Exit category PnL contribution ──
    pnl_by_exit: dict[str, dict] = {}
    for period in periods:
        trades_p = [t for t in classified if t["period"] == period]
        by_cat: dict[str, list[float]] = defaultdict(list)
        for t in trades_p:
            by_cat[t["exit_cat"]].append(t["pnl"])
        cat_total = {}
        for cat, pnls in by_cat.items():
            cat_total[cat] = {
                "count": len(pnls),
                "pnl_sum": round(sum(pnls), 2),
                "pnl_avg": round(sum(pnls) / len(pnls), 3),
            }
        pnl_by_exit[period] = cat_total

    # ── 6. MIA/Unknown close timeline ──
    mia_trades = [t for t in classified if t["exit_cat"] in ("mia_close", "unknown_close")]
    mia_by_period: dict[str, Any] = {}
    for period in periods:
        trades_p = [t for t in mia_trades if t["period"] == period]
        mia_by_period[period] = {
            "count": len(trades_p),
            "pnl_total": round(sum(t["pnl"] for t in trades_p), 2),
        }

    # ── 7. Meta_exit usage pre/post (new category from FIX-20260610-008) ──
    meta_exit_trades = [t for t in classified if t["exit_cat"] == "meta_exit"]
    meta_by_period: dict[str, Any] = {}
    for period in periods:
        trades_p = [t for t in meta_exit_trades if t["period"] == period]
        meta_by_period[period] = {
            "count": len(trades_p),
            "pnl_total": round(sum(t["pnl"] for t in trades_p), 2),
        }

    # ── 8. Trailing SL snapshots analysis from position_snapshots.jsonl ──
    snap_path = Path(data_dir) / "position_snapshots.jsonl"
    trail_stats: dict[str, Any] = {"note": "no snapshot data"}
    if snap_path.exists():
        snaps: list[dict] = []
        with open(snap_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    snaps.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        # Map ticket -> snapshot count + trail_advances
        ticket_snaps: dict[int, list[dict]] = defaultdict(list)
        for s in snaps:
            ticket = s.get("ticket") or s.get("position_ticket")
            if ticket:
                ticket_snaps[ticket].append(s)

        trail_summary: dict[str, list[int]] = defaultdict(list)
        for ticket, snap_list in ticket_snaps.items():
            n_snaps = len(snap_list)
            # Check for trail activation
            trail_active = any(
                s.get("trailing_sl_distance", 0) > 0 for s in snap_list
            )
            trail_summary["snap_counts"].append(n_snaps)
            trail_summary["trail_active"].append(1 if trail_active else 0)

        n_with_snaps = len(ticket_snaps)
        n_trail_active = sum(trail_summary["trail_active"])

        # Correlate snapshot coverage with PnL
        trail_pnl: dict[str, list[float]] = {"active": [], "inactive": []}
        for ticket, snap_list in ticket_snaps.items():
            # Find matching classified trade
            ct = next((t for t in classified if t["ticket"] == ticket), None)
            if ct is None:
                continue
            trail_active_t = any(
                s.get("trailing_sl_distance", 0) > 0 for s in snap_list
            )
            if trail_active_t:
                trail_pnl["active"].append(ct["pnl"])
            else:
                trail_pnl["inactive"].append(ct["pnl"])

        trail_stats = {
            "total_tickets_with_snaps": n_with_snaps,
            "n_trail_active": n_trail_active,
            "trail_active_pct": round(n_trail_active / max(n_with_snaps, 1) * 100, 1),
            "snap_count_distribution": {
                "0": sum(1 for c in trail_summary["snap_counts"] if c == 0),
                "1": sum(1 for c in trail_summary["snap_counts"] if c == 1),
                "2-5": sum(1 for c in trail_summary["snap_counts"] if 2 <= c <= 5),
                "6-10": sum(1 for c in trail_summary["snap_counts"] if 6 <= c <= 10),
                "11+": sum(1 for c in trail_summary["snap_counts"] if c > 10),
            },
            "trail_active_pnl": {
                "count": len(trail_pnl["active"]),
                "total": round(sum(trail_pnl["active"]), 2),
                "avg": round(sum(trail_pnl["active"]) / max(len(trail_pnl["active"]), 1), 3),
            },
            "trail_inactive_pnl": {
                "count": len(trail_pnl["inactive"]),
                "total": round(sum(trail_pnl["inactive"]), 2),
                "avg": round(sum(trail_pnl["inactive"]) / max(len(trail_pnl["inactive"]), 1), 3),
            },
        }

    # ── 9. Per-brain exit reason breakdown (post-optimization only) ──
    opt_trades = [t for t in classified if t["period"] == "optimized"]
    brain_exit: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"count": 0, "pnl": 0.0}))
    for t in opt_trades:
        for bid in t["brain_ids"]:
            brain_exit[bid][t["exit_cat"]]["count"] += 1
            brain_exit[bid][t["exit_cat"]]["pnl"] += t["pnl"]

    brain_summary = {}
    for bid, exits in sorted(brain_exit.items()):
        total_n = sum(e["count"] for e in exits.values())
        total_pnl = sum(e["pnl"] for e in exits.values())
        top_exit = max(exits.items(), key=lambda x: x[1]["count"])
        mia_count = exits.get("mia_close", {}).get("count", 0) + exits.get("unknown_close", {}).get("count", 0)
        brain_summary[bid] = {
            "total_trades": total_n,
            "total_pnl": round(total_pnl, 2),
            "top_exit_reason": top_exit[0],
            "top_exit_pct": round(top_exit[1]["count"] / max(total_n, 1) * 100, 1),
            "mia_unknown_pct": round(mia_count / max(total_n, 1) * 100, 1),
        }

    # ── 10. Error vs profit exit efficiency score ──
    # "Good" exits = tp_hit, meta_exit, net_out (intentional management)
    # "Bad" exits = sl_hit, watchdog, mia_close, unknown_close (forced/reactive)
    good_cats = {"tp_hit", "meta_exit", "net_out", "partial_tp"}
    bad_cats = {"sl_hit", "watchdog", "mia_close", "unknown_close", "emergency_close"}

    efficiency: dict[str, Any] = {}
    for period in periods:
        trades_p = [t for t in classified if t["period"] == period]
        if not trades_p:
            continue
        good = [t for t in trades_p if t["exit_cat"] in good_cats]
        bad = [t for t in trades_p if t["exit_cat"] in bad_cats]
        neutral = [t for t in trades_p if t["exit_cat"] not in good_cats and t["exit_cat"] not in bad_cats]
        efficiency[period] = {
            "good_exit_pct": round(len(good) / len(trades_p) * 100, 1),
            "good_exit_pnl": round(sum(t["pnl"] for t in good), 2),
            "good_exit_wr": round(sum(1 for t in good if t["pnl"] > 0) / max(len(good), 1) * 100, 1),
            "bad_exit_pct": round(len(bad) / len(trades_p) * 100, 1),
            "bad_exit_pnl": round(sum(t["pnl"] for t in bad), 2),
            "bad_exit_wr": round(sum(1 for t in bad if t["pnl"] > 0) / max(len(bad), 1) * 100, 1),
            "neutral_exit_pct": round(len(neutral) / len(trades_p) * 100, 1),
            "neutral_exit_pnl": round(sum(t["pnl"] for t in neutral), 2),
        }

    return {
        "data_dir": data_dir,
        "total_trades": len(classified),
        "period_summary": period_stats,
        "exit_comparison": exit_comparison,
        "pnl_by_exit_period": pnl_by_exit,
        "mia_by_period": mia_by_period,
        "meta_exit_by_period": meta_by_period,
        "trail_stats": trail_stats,
        "brain_exit_summary": brain_summary,
        "exit_efficiency": efficiency,
    }


def print_report(results: dict) -> None:
    """Print the analysis report to stdout (Iron Law #11 compliance)."""
    if "error" in results:
        print(f"ERROR: {results['error']}")
        return

    print("=" * 90)
    print("  离场逻辑优化效果分析 — 机构级审计报告")
    print("  Exit Logic Optimization Effectiveness Audit")
    print(f"  优化窗口: 2026-06-10 ~ 2026-06-13 (FIX-008, FIX-017, FIX-081, FIX-086)")
    print(f"  数据源: {results['data_dir']}/live_trade_journal.jsonl")
    print(f"  总交易数: {results['total_trades']}")
    print("=" * 90)

    # ── SECTION A: Period-over-period comparison ──
    print("\n" + "─" * 90)
    print("SECTION A: 分期绩效对比 (Period Performance Comparison)")
    print("─" * 90)
    print(f"{'指标':<30} {'Baseline(<6/10)':<20} {'Transition':<20} {'Optimized(>6/13)':<20} {'Delta':<10}")
    print("-" * 90)

    ps = results["period_summary"]
    bl = ps.get("baseline", {})
    opt = ps.get("optimized", {})

    metrics = [
        ("n_trades", "交易数", "d"),
        ("total_pnl_r", "总PnL (R)", ".2f"),
        ("avg_pnl_per_trade", "均PnL/笔 (R)", ".3f"),
        ("win_rate_pct", "胜率 (%)", ".1f"),
        ("profit_factor", "盈亏比", ".4f"),
        ("avg_win", "平均盈利 (R)", ".3f"),
        ("avg_loss", "平均亏损 (R)", ".3f"),
        ("breakeven_pct", "Breakeven (%)", ".1f"),
        ("pnl_median", "PnL 中位数 (R)", ".3f"),
    ]

    for key, label, fmt in metrics:
        b_val = bl.get(key, 0) if bl else 0
        o_val = opt.get(key, 0) if opt else 0
        t_val = ps.get("transition", {}).get(key, 0)
        if isinstance(b_val, (int, float)) and isinstance(o_val, (int, float)):
            delta = o_val - b_val
            delta_str = f"{delta:+{fmt}}"
        else:
            delta_str = "N/A"
        print(f"{label:<30} {b_val:{fmt}}".ljust(45) + f"{t_val:{fmt}}".ljust(26) + f"{o_val:{fmt}}".ljust(16) + delta_str)

    # ── SECTION B: Exit reason distribution shift ──
    print("\n" + "─" * 90)
    print("SECTION B: 出场原因分布变迁 (Exit Reason Distribution Shift)")
    print("─" * 90)
    ec = results["exit_comparison"]
    if ec:
        print(f"{'Exit Category':<22} {'BL Count':>8} {'BL%':>7} {'OPT Count':>9} {'OPT%':>7} {'BL WR%':>7} {'OPT WR%':>7}")
        print("-" * 90)
        for cat, data in ec.items():
            print(f"{cat:<22} {data['baseline_count']:>8} {data['baseline_pct']:>6.1f}% {data['optimized_count']:>9} {data['optimized_pct']:>6.1f}% {data['baseline_win_rate']:>6.1f}% {data['optimized_win_rate']:>6.1f}%")

    # ── SECTION C: Exit Efficiency Score ──
    print("\n" + "─" * 90)
    print("SECTION C: 出场效率评分 (Exit Efficiency Score)")
    print("─" * 90)
    print("Good exits = tp_hit + meta_exit + net_out (intentional profit management)")
    print("Bad exits  = sl_hit + watchdog + mia_close + unknown_close (forced/reactive)")
    print()
    ee = results["exit_efficiency"]
    if ee:
        print(f"{'Period':<15} {'Good%':>7} {'Good PnL':>10} {'Good WR%':>8} {'Bad%':>7} {'Bad PnL':>10} {'Bad WR%':>8} {'Neutral%':>8}")
        print("-" * 90)
        for period in ["baseline", "transition", "optimized"]:
            e = ee.get(period, {})
            if e:
                print(f"{period:<15} {e['good_exit_pct']:>6.1f}% {e['good_exit_pnl']:>9.2f} {e['good_exit_wr']:>7.1f}% {e['bad_exit_pct']:>6.1f}% {e['bad_exit_pnl']:>9.2f} {e['bad_exit_wr']:>7.1f}% {e['neutral_exit_pct']:>7.1f}%")

    # ── SECTION D: MIA/Unknown Close Trend ──
    print("\n" + "─" * 90)
    print("SECTION D: MIA/Unknown Close 趋势 (DQAF-033 context)")
    print("─" * 90)
    mia = results["mia_by_period"]
    if mia:
        for period in ["baseline", "transition", "optimized"]:
            m = mia.get(period, {})
            n_t = ps.get(period, {}).get("n_trades", 1)
            pct = m.get("count", 0) / max(n_t, 1) * 100
            print(f"  {period:<15}: {m.get('count', 0):>3} trades ({pct:>5.1f}%), PnL={m.get('pnl_total', 0):>+.2f}R")

    # ── SECTION E: MetaExit usage (new category) ──
    print("\n" + "─" * 90)
    print("SECTION E: MetaExit 新分类采用 (FIX-20260610-008 产出)")
    print("─" * 90)
    me = results["meta_exit_by_period"]
    if me:
        for period in ["baseline", "transition", "optimized"]:
            m = me.get(period, {})
            n_t = ps.get(period, {}).get("n_trades", 1)
            pct = m.get("count", 0) / max(n_t, 1) * 100
            print(f"  {period:<15}: {m.get('count', 0):>3} trades ({pct:>5.1f}%), PnL={m.get('pnl_total', 0):>+.2f}R")

    # ── SECTION F: Trailing SL Effectiveness ──
    print("\n" + "─" * 90)
    print("SECTION F: Trailing SL 激活效果 (DQAF-034 context)")
    print("─" * 90)
    ts = results["trail_stats"]
    if ts and "total_tickets_with_snaps" in ts:
        print(f"  Total tickets with snapshots: {ts['total_tickets_with_snaps']}")
        print(f"  Trail active: {ts['n_trail_active']} ({ts['trail_active_pct']}%)")
        print(f"  Snap distribution: {ts.get('snap_count_distribution', {})}")
        tap = ts.get("trail_active_pnl", {})
        tip = ts.get("trail_inactive_pnl", {})
        print(f"  Trail ACTIVE trades:   count={tap.get('count', 0)}, total={tap.get('total', 0):+.2f}R, avg={tap.get('avg', 0):.3f}R")
        print(f"  Trail INACTIVE trades: count={tip.get('count', 0)}, total={tip.get('total', 0):+.2f}R, avg={tip.get('avg', 0):.3f}R")

    # ── SECTION G: Per-Brain Exit Profile (post-optimization) ──
    print("\n" + "─" * 90)
    print("SECTION G: 活跃大脑出场画像 (Per-Brain Exit Profile, post-optimization)")
    print("─" * 90)
    bs = results["brain_exit_summary"]
    if bs:
        print(f"{'Brain':<32} {'Trades':>6} {'PnL(R)':>8} {'Top Exit':>18} {'Top%':>6} {'MIA%':>6}")
        print("-" * 90)
        for bid, bd in sorted(bs.items(), key=lambda x: -x[1]["total_trades"]):
            print(f"{bid:<32} {bd['total_trades']:>6} {bd['total_pnl']:>+8.2f} {bd['top_exit_reason']:>18} {bd['top_exit_pct']:>5.1f}% {bd['mia_unknown_pct']:>5.1f}%")

    # ── SECTION H: Recommendations ──
    print("\n" + "─" * 90)
    print("SECTION H: 深度优化建议 (Deeper Optimization Recommendations)")
    print("─" * 90)

    # Auto-detect issues from data
    issues = []

    # Check if MIA rate improved
    bl_mia_pct = 0
    opt_mia_pct = 0
    if ps.get("baseline", {}).get("n_trades", 0) > 0:
        bl_mia = mia.get("baseline", {}).get("count", 0)
        bl_mia_pct = bl_mia / ps["baseline"]["n_trades"] * 100
    if ps.get("optimized", {}).get("n_trades", 0) > 0:
        opt_mia = mia.get("optimized", {}).get("count", 0)
        opt_mia_pct = opt_mia / ps["optimized"]["n_trades"] * 100

    if opt_mia_pct > 5:
        issues.append(f"[HIGH] MIA/Unknown Close 仍占 {opt_mia_pct:.1f}% — DQAF-033 需尽快启动调查")

    # Check if bad exit ratio improved
    if ee:
        bl_bad = ee.get("baseline", {}).get("bad_exit_pct", 0)
        opt_bad = ee.get("optimized", {}).get("bad_exit_pct", 0)
        if opt_bad > 40:
            issues.append(f"[MEDIUM] Bad exit 占比 {opt_bad:.1f}% (>40%) — SL/止损出场仍是主导退场方式")

    # Check win rate delta
    bl_wr = ps.get("baseline", {}).get("win_rate_pct", 0)
    opt_wr = ps.get("optimized", {}).get("win_rate_pct", 0)
    if opt_wr < bl_wr:
        issues.append(f"[INFO] 胜率 {bl_wr:.1f}%→{opt_wr:.1f}% 下降 — 需检查市场体制转换效应 vs 优化退化")

    # Check trail stats
    if ts and "total_tickets_with_snaps" in ts:
        trail_pct = ts.get("trail_active_pct", 0)
        if trail_pct < 60:
            issues.append(f"[HIGH] Trailing SL 激活率仅 {trail_pct:.1f}% — DQAF-034 锁/时序审计排上日程")

    if not issues:
        issues.append("[PASS] 所有指标在健康范围内")

    for issue in issues:
        print(f"  {issue}")

    print("\n" + "=" * 90)
    print("  [DONE] All statistics above are the sole source of truth.")
    print("=" * 90)


def main():
    parser = argparse.ArgumentParser(description="Exit optimization effect analysis")
    parser.add_argument("--data-dir", default="data_btc", help="Data directory (default: data_btc)")
    args = parser.parse_args()

    results = compute_exit_analysis(args.data_dir)
    print_report(results)


if __name__ == "__main__":
    main()
