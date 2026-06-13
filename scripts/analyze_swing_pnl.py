#!/usr/bin/env python
"""Swing Strategy Deep-Dive Audit — 挖掘唯一盈利源。

Iron Law #11: 此脚本的 stdout 是唯一合法证据源。
所有统计数字必须来自此脚本输出，禁止在对话中补算。

Usage:
  python scripts/analyze_swing_pnl.py --data-dir data
  python scripts/analyze_swing_pnl.py --data-dir data_btc  # BTC
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

UTC = timezone.utc

# ═══════════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════════


def load_journal(data_dir: str) -> list[dict[str, Any]]:
    """Load trade journal, return all entries."""
    jp = Path(data_dir) / "live_trade_journal.jsonl"
    if not jp.exists():
        print(f"[ERROR] Journal not found: {jp}", file=sys.stderr)
        sys.exit(1)
    entries: list[dict[str, Any]] = []
    for line in jp.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def filter_swing_closes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter for swing strategy labeled closes (exclude auto_orphan)."""
    return [
        e for e in entries
        if e.get("action") == "close"
        and "swing" in str(e.get("strategy", "")).lower()
        and e.get("label") is not None
        and not str(e.get("label", "")).startswith("auto_orphan")
        and e.get("pnl") is not None
    ]


def filter_swing_opens(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter for swing strategy accepted opens."""
    return [
        e for e in entries
        if e.get("action") == "open"
        and "swing" in str(e.get("strategy", "")).lower()
        and e.get("ack_status") == "accepted"
    ]


def load_brain_votes(data_dir: str) -> list[dict[str, Any]]:
    """Load brain votes from all available daily files."""
    votes_dir = Path(data_dir) / "brain_votes"
    votes: list[dict[str, Any]] = []
    if votes_dir.exists():
        for vf in sorted(votes_dir.glob("*.jsonl")):
            for line in vf.read_text(encoding="utf-8").strip().split("\n"):
                if line.strip():
                    try:
                        votes.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return votes


def load_brain_performance(data_dir: str) -> dict[str, Any]:
    """Load brain performance tracker."""
    bp = Path(data_dir) / "brain_performance.json"
    if bp.exists():
        return json.loads(bp.read_text(encoding="utf-8"))
    return {}


def load_brain_pnl_ledger(data_dir: str) -> dict[str, Any]:
    """Load brain PnL ledger."""
    bp = Path(data_dir) / "brain_pnl_ledger.json"
    if bp.exists():
        return json.loads(bp.read_text(encoding="utf-8"))
    # BTC path
    bp = Path(data_dir) / "brain_pnl_ledger.json"
    if bp.exists():
        return json.loads(bp.read_text(encoding="utf-8"))
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1.1: Brain-level PnL attribution
# ═══════════════════════════════════════════════════════════════════════════════


def phase1_brain_attribution(closes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Attribute PnL to individual brains (from brain_ids field)."""
    brain_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "strategies": set()}
    )

    for e in closes:
        brain_ids = e.get("brain_ids") or []
        if isinstance(brain_ids, str):
            brain_ids = [brain_ids]
        pnl = float(e.get("pnl", 0) or 0)
        strategy = str(e.get("strategy", "unknown"))
        for bid in brain_ids:
            bs = brain_stats[bid]
            bs["pnl"] += pnl
            bs["trades"] += 1
            bs["strategies"].add(strategy)
            if pnl > 0:
                bs["wins"] += 1
            elif pnl < 0:
                bs["losses"] += 1

    # Convert sets to sorted lists for JSON compat
    for bs in brain_stats.values():
        bs["strategies"] = sorted(bs["strategies"])
        bs["win_rate"] = round(bs["wins"] / max(bs["trades"], 1), 4)

    return dict(brain_stats)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1.2: Exit label PnL attribution
# ═══════════════════════════════════════════════════════════════════════════════


def phase1_exit_attribution(closes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Attribute PnL by exit reason (label)."""
    label_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"pnl": 0.0, "count": 0, "avg_pnl": 0.0}
    )

    for e in closes:
        label = str(e.get("label", "unknown"))
        pnl = float(e.get("pnl", 0) or 0)
        ls = label_stats[label]
        ls["pnl"] += pnl
        ls["count"] += 1

    for ls in label_stats.values():
        ls["avg_pnl"] = round(ls["pnl"] / max(ls["count"], 1), 4)

    return dict(label_stats)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1.3: Bootstrap significance test
# ═══════════════════════════════════════════════════════════════════════════════


def phase1_bootstrap(closes: list[dict[str, Any]], iterations: int = 10000) -> dict[str, Any]:
    """Bootstrap significance: is the observed PnL distinguishable from random?"""
    pnls = np.array([abs(float(e.get("pnl", 0) or 0)) for e in closes])
    actual_total = sum(float(e.get("pnl", 0) or 0) for e in closes)

    if len(pnls) < 10:
        return {"error": "too few trades for bootstrap", "n_trades": len(pnls)}

    rng = np.random.RandomState(42)
    random_signs = rng.choice([-1, 1], size=(iterations, len(pnls)))
    random_paths = np.sum(random_signs * pnls, axis=1)

    p_value_right = float(np.mean(random_paths >= actual_total))  # P(random >= actual)
    p_value_left = float(np.mean(random_paths <= actual_total))   # P(random <= actual)
    p_value_two = 2.0 * min(p_value_right, p_value_left)

    return {
        "n_trades": len(pnls),
        "actual_pnl": round(actual_total, 4),
        "bootstrap_iterations": iterations,
        "random_mean": round(float(np.mean(random_paths)), 4),
        "random_std": round(float(np.std(random_paths)), 4),
        "random_p5": round(float(np.percentile(random_paths, 5)), 4),
        "random_p95": round(float(np.percentile(random_paths, 95)), 4),
        "p_value_right": round(p_value_right, 4),
        "p_value_left": round(p_value_left, 4),
        "p_value_two_sided": round(p_value_two, 4),
        "significant_at_5pct": p_value_two < 0.05,
        "interpretation": (
            "Statistically significant alpha (p<0.05)"
            if p_value_two < 0.05
            else "NOT statistically significant — could be luck"
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2.1: Time-of-day / Day-of-week analysis
# ═══════════════════════════════════════════════════════════════════════════════


def phase2_time_analysis(closes: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze PnL by UTC hour and weekday."""
    hour_pnl: dict[int, dict[str, float]] = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    dow_pnl: dict[int, dict[str, float]] = defaultdict(lambda: {"pnl": 0.0, "count": 0})

    for e in closes:
        ts = e.get("recorded_at", "")
        if not ts:
            continue
        try:
            # Handle various ISO formats
            ts = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue

        pnl = float(e.get("pnl", 0) or 0)
        hour = dt.hour
        dow = dt.weekday()  # 0=Monday

        hour_pnl[hour]["pnl"] += pnl
        hour_pnl[hour]["count"] += 1
        dow_pnl[dow]["pnl"] += pnl
        dow_pnl[dow]["count"] += 1

    # Format
    def fmt_hourly(d: dict) -> list[dict]:
        return sorted(
            [
                {"hour": h, "pnl": round(v["pnl"], 2), "count": int(v["count"]),
                 "avg_pnl": round(v["pnl"] / max(v["count"], 1), 4)}
                for h, v in d.items()
            ],
            key=lambda x: x["pnl"],
        )

    def fmt_dow(d: dict) -> list[dict]:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return sorted(
            [
                {"day": days[d], "pnl": round(v["pnl"], 2), "count": int(v["count"]),
                 "avg_pnl": round(v["pnl"] / max(v["count"], 1), 4)}
                for d, v in d.items()
            ],
            key=lambda x: x["pnl"],
        )

    return {
        "by_hour_utc": fmt_hourly(dict(hour_pnl)),
        "by_weekday": fmt_dow(dict(dow_pnl)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2.2: Per-strategy breakdown
# ═══════════════════════════════════════════════════════════════════════════════


def phase2_strategy_breakdown(closes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Break down PnL by individual swing strategy."""
    strat_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0, "breakeven": 0}
    )

    for e in closes:
        strategy = str(e.get("strategy", "unknown"))
        pnl = float(e.get("pnl", 0) or 0)
        ss = strat_stats[strategy]
        ss["pnl"] += pnl
        ss["trades"] += 1
        if pnl > 0.001:
            ss["wins"] += 1
        elif pnl < -0.001:
            ss["losses"] += 1
        else:
            ss["breakeven"] += 1

    for ss in strat_stats.values():
        n = max(ss["trades"], 1)
        ss["win_rate"] = round(ss["wins"] / n, 4)
        ss["avg_pnl"] = round(ss["pnl"] / n, 4)

    return dict(strat_stats)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2.3: Brain signal quality from brain_votes
# ═══════════════════════════════════════════════════════════════════════════════


def phase2_signal_quality(votes: list[dict[str, Any]], swing_brains: set[str]) -> dict[str, Any]:
    """Analyze signal quality for swing brains."""
    if not votes:
        return {"error": "no brain_votes data found"}

    brain_signals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "total_votes": 0, "long_signals": 0, "short_signals": 0,
            "neutral_signals": 0, "confidences": [],
        }
    )

    for v in votes:
        predictions = v.get("brain_predictions") or v.get("predictions") or []
        if isinstance(predictions, dict):
            predictions = [predictions]
        for p in predictions:
            if not isinstance(p, dict):
                continue
            bid = p.get("brain_id", "")
            if bid not in swing_brains:
                # Also check partial matches
                matched = False
                for sb in swing_brains:
                    if sb in bid or bid in sb:
                        matched = True
                        break
                if not matched:
                    continue

            bs = brain_signals[bid]
            bs["total_votes"] += 1
            direction = p.get("direction") or p.get("direction_bias", "")
            conf = float(p.get("confidence", 0) or 0)

            if direction in ("long", "up"):
                bs["long_signals"] += 1
            elif direction in ("short", "down"):
                bs["short_signals"] += 1
            else:
                bs["neutral_signals"] += 1
            bs["confidences"].append(conf)

    result: dict[str, Any] = {}
    for bid, bs in brain_signals.items():
        confs = np.array(bs["confidences"]) if bs["confidences"] else np.array([0.0])
        result[bid] = {
            "total_votes": bs["total_votes"],
            "long_pct": round(bs["long_signals"] / max(bs["total_votes"], 1) * 100, 1),
            "short_pct": round(bs["short_signals"] / max(bs["total_votes"], 1) * 100, 1),
            "neutral_pct": round(bs["neutral_signals"] / max(bs["total_votes"], 1) * 100, 1),
            "confidence_mean": round(float(np.mean(confs)), 4),
            "confidence_std": round(float(np.std(confs)), 4),
        }

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="analyze_swing_pnl")
    p.add_argument("--data-dir", default="data", help="Base data directory")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir

    print(f"[analyze_swing_pnl] Data dir: {data_dir}")
    print(f"[analyze_swing_pnl] Iron Law #11: All statistics below are from script stdout only.\n")

    # ── Load data ──
    entries = load_journal(data_dir)
    swing_closes = filter_swing_closes(entries)
    swing_opens = filter_swing_opens(entries)
    votes = load_brain_votes(data_dir)

    print(f"Journal entries: {len(entries)}")
    print(f"Swing closes (labeled): {len(swing_closes)}")
    print(f"Swing opens (accepted): {len(swing_opens)}")
    print(f"Brain votes records: {len(votes)}")

    if len(swing_closes) < 5:
        print("\n[WARN] Too few swing trades for meaningful analysis.")
        return 1

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 1.1: Brain attribution
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 1.1: BRAIN-LEVEL PnL ATTRIBUTION")
    print("=" * 70)
    brain_attr = phase1_brain_attribution(swing_closes)
    sorted_brains = sorted(brain_attr.items(), key=lambda x: x[1]["pnl"])
    print(f"{'Brain ID':<40s} {'PnL':>8s} {'Trades':>7s} {'WR':>7s} {'Strategies'}")
    print("-" * 70)
    for bid, bs in sorted_brains:
        print(
            f"{bid:<40s} {bs['pnl']:>8.2f} {bs['trades']:>7d} "
            f"{bs['win_rate']:>6.1%}  {', '.join(bs['strategies'])}"
        )

    # Identify swing-specific brains (exclude OU, Barrier, Meta brains)
    swing_brains_set: set[str] = set()
    for bid in brain_attr:
        if any(kw in bid.lower() for kw in ("swing", "trend", "rev_")):
            swing_brains_set.add(bid)
    print(f"\nIdentified {len(swing_brains_set)} swing-specific brains: {sorted(swing_brains_set)}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 1.2: Exit label attribution
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 1.2: EXIT LABEL PnL ATTRIBUTION")
    print("=" * 70)
    exit_attr = phase1_exit_attribution(swing_closes)
    sorted_exits = sorted(exit_attr.items(), key=lambda x: x[1]["pnl"])
    print(f"{'Exit Label':<45s} {'PnL':>8s} {'Count':>6s} {'Avg PnL':>8s}")
    print("-" * 70)
    for label, ls in sorted_exits:
        print(f"{label:<45s} {ls['pnl']:>8.2f} {ls['count']:>6d} {ls['avg_pnl']:>8.4f}")

    # Categorize: TP hits vs SL hits vs managed exits
    tp_pnl = sum(v["pnl"] for k, v in exit_attr.items() if "tp_hit" in k.lower())
    sl_pnl = sum(v["pnl"] for k, v in exit_attr.items() if "sl_hit" in k.lower())
    managed_pnl = sum(
        v["pnl"] for k, v in exit_attr.items()
        if "tp_hit" not in k.lower() and "sl_hit" not in k.lower()
        and k not in ("win", "loss", "breakeven")
    )
    print(f"\n  TP hits: {tp_pnl:+.2f}  |  SL hits: {sl_pnl:+.2f}  |  Managed exits: {managed_pnl:+.2f}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 1.3: Bootstrap significance
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 1.3: BOOTSTRAP SIGNIFICANCE TEST (10,000 iterations)")
    print("=" * 70)
    boot = phase1_bootstrap(swing_closes)
    print(f"  N trades:           {boot.get('n_trades')}")
    print(f"  Actual PnL:         {boot.get('actual_pnl')}")
    print(f"  Random mean:        {boot.get('random_mean')} ± {boot.get('random_std')}")
    print(f"  Random 5%-95%:      [{boot.get('random_p5')}, {boot.get('random_p95')}]")
    print(f"  P(two-sided):       {boot.get('p_value_two_sided')}")
    print(f"  Significant @5%:    {boot.get('significant_at_5pct')}")
    print(f"  Verdict:            {boot.get('interpretation')}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 2.1: Time analysis
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 2.1: TIME-OF-DAY / DAY-OF-WEEK ANALYSIS")
    print("=" * 70)
    time_result = phase2_time_analysis(swing_closes)

    print("\n  By UTC Hour (best → worst):")
    print(f"  {'Hour':<6s} {'PnL':>8s} {'Trades':>7s} {'Avg PnL':>8s}")
    for h in time_result["by_hour_utc"]:
        print(f"  {h['hour']:02d}:00  {h['pnl']:>8.2f} {h['count']:>7d} {h['avg_pnl']:>8.4f}")

    print("\n  By Weekday (best → worst):")
    print(f"  {'Day':<6s} {'PnL':>8s} {'Trades':>7s} {'Avg PnL':>8s}")
    for d in time_result["by_weekday"]:
        print(f"  {d['day']:<6s} {d['pnl']:>8.2f} {d['count']:>7d} {d['avg_pnl']:>8.4f}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 2.2: Per-strategy breakdown
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 2.2: PER-STRATEGY BREAKDOWN")
    print("=" * 70)
    strat_break = phase2_strategy_breakdown(swing_closes)
    print(f"{'Strategy':<20s} {'PnL':>8s} {'Trades':>7s} {'WR':>7s} {'Avg PnL':>8s}")
    print("-" * 70)
    for sname, ss in sorted(strat_break.items(), key=lambda x: x[1]["pnl"]):
        print(
            f"{sname:<20s} {ss['pnl']:>8.2f} {ss['trades']:>7d} "
            f"{ss['win_rate']:>6.1%} {ss['avg_pnl']:>8.4f}"
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 2.3: Signal quality
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 2.3: SWING BRAIN SIGNAL QUALITY (from brain_votes)")
    print("=" * 70)
    sig_quality = phase2_signal_quality(votes, swing_brains_set)
    if "error" in sig_quality:
        print(f"  {sig_quality['error']}")
    elif not sig_quality:
        print("  No swing brain signals found in brain_votes")
    else:
        print(f"  {'Brain ID':<40s} {'Votes':>6s} {'Long%':>7s} {'Short%':>7s} {'Neut%':>7s} {'Conf':>7s}")
        print("-" * 70)
        for bid, sq in sorted(sig_quality.items()):
            print(
                f"  {bid:<40s} {sq['total_votes']:>6d} "
                f"{sq['long_pct']:>6.1f}% {sq['short_pct']:>6.1f}% "
                f"{sq['neutral_pct']:>6.1f}% "
                f"{sq['confidence_mean']:.3f}±{sq['confidence_std']:.3f}"
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total_swing_pnl = sum(float(e.get("pnl", 0) or 0) for e in swing_closes)
    swing_wins = sum(1 for e in swing_closes if float(e.get("pnl", 0) or 0) > 0)
    print(f"  Total swing PnL: {total_swing_pnl:+.2f}")
    print(f"  Win rate: {swing_wins}/{len(swing_closes)} ({swing_wins/max(len(swing_closes),1)*100:.1f}%)")
    print(f"  Avg PnL per trade: {total_swing_pnl/max(len(swing_closes),1):.4f}")
    if boot.get("significant_at_5pct"):
        print(f"  Statistical significance: CONFIRMED (p={boot.get('p_value_two_sided'):.4f})")
    else:
        print(f"  Statistical significance: NOT CONFIRMED (p={boot.get('p_value_two_sided'):.4f})")
    print(f"\n[DONE] All statistics above are the sole source of truth.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
