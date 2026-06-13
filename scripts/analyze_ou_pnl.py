#!/usr/bin/env python
"""OU Statistical Arbitrage Loss Audit — 审计最大亏损源。

Iron Law #11: 此脚本的 stdout 是唯一合法证据源。

Usage:
  python scripts/analyze_ou_pnl.py --data-dir data
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


def load_journal(data_dir: str) -> list[dict[str, Any]]:
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


def filter_ou_closes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e for e in entries
        if e.get("action") == "close"
        and any(kw in str(e.get("strategy", "")).lower() for kw in ("statarb", "stat_arb"))
        and e.get("label") is not None
        and not str(e.get("label", "")).startswith("auto_orphan")
        and e.get("pnl") is not None
    ]


def filter_ou_opens(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e for e in entries
        if e.get("action") == "open"
        and any(kw in str(e.get("strategy", "")).lower() for kw in ("statarb", "stat_arb"))
        and e.get("ack_status") == "accepted"
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: Per-strategy PnL + brain attribution
# ═══════════════════════════════════════════════════════════════════════════════


def phase1_strategy_attribution(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Full per-strategy breakdown: opens, closes, PnL, brain attribution."""
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"opens": 0, "closes": 0, "pnl": 0.0, "wins": 0, "losses": 0, "brain_pnl": defaultdict(float)}
    )

    for e in entries:
        strategy = str(e.get("strategy", "unknown"))
        if "statarb" not in strategy.lower():
            continue
        action = e.get("action", "")
        pnl = float(e.get("pnl", 0) or 0)
        brain_ids = e.get("brain_ids") or []
        if isinstance(brain_ids, str):
            brain_ids = [brain_ids]

        ss = stats[strategy]
        if action == "open" and e.get("ack_status") == "accepted":
            ss["opens"] += 1
        elif action == "close" and e.get("label") and not str(e.get("label", "")).startswith("auto_orphan"):
            ss["closes"] += 1
            ss["pnl"] += pnl
            if pnl > 0.001:
                ss["wins"] += 1
            elif pnl < -0.001:
                ss["losses"] += 1
            for bid in brain_ids:
                ss["brain_pnl"][bid] += pnl

    # Convert defaultdict to regular dict
    for ss in stats.values():
        ss["brain_pnl"] = dict(ss["brain_pnl"])
        n = max(ss["closes"], 1)
        ss["win_rate"] = round(ss["wins"] / n, 4)
        ss["avg_pnl"] = round(ss["pnl"] / n, 4)

    return dict(stats)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Exit label analysis
# ═══════════════════════════════════════════════════════════════════════════════


def phase2_exit_analysis(closes: list[dict[str, Any]]) -> dict[str, Any]:
    """Exit label distribution + PnL."""
    label_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"pnl": 0.0, "count": 0}
    )
    for e in closes:
        label = str(e.get("label", "unknown"))
        pnl = float(e.get("pnl", 0) or 0)
        label_stats[label]["pnl"] += pnl
        label_stats[label]["count"] += 1

    for ls in label_stats.values():
        ls["avg_pnl"] = round(ls["pnl"] / max(ls["count"], 1), 4)

    # Categorize
    tp_hit_pnl = sum(v["pnl"] for k, v in label_stats.items() if "tp_hit" in k.lower())
    sl_hit_pnl = sum(v["pnl"] for k, v in label_stats.items() if "sl_hit" in k.lower())
    ou_revert_pnl = sum(v["pnl"] for k, v in label_stats.items() if "ou_revert" in k.lower() or "ou_reversion" in k.lower())
    managed_pnl = sum(
        v["pnl"] for k, v in label_stats.items()
        if not any(x in k.lower() for x in ("tp_hit", "sl_hit", "ou_revert", "ou_reversion", "win", "loss", "breakeven"))
    )

    return {
        "by_label": dict(sorted(label_stats.items(), key=lambda x: x[1]["pnl"])),
        "tp_hit_pnl": round(tp_hit_pnl, 2),
        "sl_hit_pnl": round(sl_hit_pnl, 2),
        "ou_revert_pnl": round(ou_revert_pnl, 2),
        "managed_pnl": round(managed_pnl, 2),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3: Entry context analysis (z-score at entry)
# ═══════════════════════════════════════════════════════════════════════════════


def phase3_entry_context(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze OU entry signals: z-score, half-life, confidence, p_win."""
    z_scores: list[float] = []
    half_lives: list[float] = []
    confidences: list[float] = []
    p_wins: list[float] = []
    win_z: list[float] = []
    loss_z: list[float] = []

    for e in entries:
        if e.get("action") != "close":
            continue
        ctx = e.get("entry_context", {}) or {}
        z = ctx.get("z_score")
        hl = ctx.get("half_life")
        conf = e.get("confidence")
        pw = e.get("p_win")
        pnl = float(e.get("pnl", 0) or 0)

        if z is not None:
            z_scores.append(float(z))
            if pnl > 0:
                win_z.append(float(z))
            elif pnl < 0:
                loss_z.append(float(z))
        if hl is not None:
            half_lives.append(float(hl))
        if conf is not None:
            confidences.append(float(conf))
        if pw is not None:
            p_wins.append(float(pw))

    return {
        "n_with_zscore": len(z_scores),
        "z_score_mean": round(float(np.mean(z_scores)), 3) if z_scores else None,
        "z_score_std": round(float(np.std(z_scores)), 3) if z_scores else None,
        "win_z_mean": round(float(np.mean(win_z)), 3) if win_z else None,
        "loss_z_mean": round(float(np.mean(loss_z)), 3) if loss_z else None,
        "z_discrimination": round(abs(float(np.mean(win_z) - np.mean(loss_z))), 3) if win_z and loss_z else None,
        "half_life_mean": round(float(np.mean(half_lives)), 1) if half_lives else None,
        "confidence_mean": round(float(np.mean(confidences)), 3) if confidences else None,
        "p_win_mean": round(float(np.mean(p_wins)), 3) if p_wins else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4: Time decay analysis
# ═══════════════════════════════════════════════════════════════════════════════


def phase4_time_decay(closes: list[dict[str, Any]]) -> dict[str, Any]:
    """Track PnL over time — is OU getting worse?"""
    daily_pnl: dict[str, dict[str, Any]] = defaultdict(lambda: {"pnl": 0.0, "count": 0, "cum_pnl": 0.0})

    sorted_closes = sorted(closes, key=lambda e: str(e.get("recorded_at", "")))

    cum = 0.0
    for e in sorted_closes:
        ts = str(e.get("recorded_at", ""))[:10]  # YYYY-MM-DD
        if not ts or ts == "None":
            continue
        pnl = float(e.get("pnl", 0) or 0)
        daily_pnl[ts]["pnl"] += pnl
        daily_pnl[ts]["count"] += 1
        cum += pnl
        daily_pnl[ts]["cum_pnl"] = round(cum, 2)

    # Identify worst streak
    streak_loss = 0.0
    max_streak_loss = 0.0
    streak_count = 0
    max_streak_count = 0
    for date_key in sorted(daily_pnl.keys()):
        dp = daily_pnl[date_key]["pnl"]
        if dp < 0:
            streak_loss += dp
            streak_count += 1
        else:
            streak_loss = 0.0
            streak_count = 0
        if streak_loss < max_streak_loss or (streak_loss == max_streak_loss and streak_count > max_streak_count):
            max_streak_loss = streak_loss
            max_streak_count = streak_count

    return {
        "trading_days": len(daily_pnl),
        "daily_pnl": dict(sorted(daily_pnl.items())),
        "cumulative_pnl": round(cum, 2),
        "max_losing_streak_pnl": round(max_streak_loss, 2),
        "max_losing_streak_days": max_streak_count,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 5: Bootstrap significance
# ═══════════════════════════════════════════════════════════════════════════════


def phase5_significance(closes: list[dict[str, Any]], iterations: int = 10000) -> dict[str, Any]:
    pnls = np.array([abs(float(e.get("pnl", 0) or 0)) for e in closes])
    actual_total = sum(float(e.get("pnl", 0) or 0) for e in closes)

    if len(pnls) < 10:
        return {"error": "too few trades"}

    rng = np.random.RandomState(42)
    random_signs = rng.choice([-1, 1], size=(iterations, len(pnls)))
    random_paths = np.sum(random_signs * pnls, axis=1)

    p_value = 2.0 * min(
        float(np.mean(random_paths >= actual_total)),
        float(np.mean(random_paths <= actual_total)),
    )

    return {
        "n_trades": len(pnls),
        "actual_pnl": round(actual_total, 4),
        "random_mean": round(float(np.mean(random_paths)), 4),
        "random_p5": round(float(np.percentile(random_paths, 5)), 4),
        "random_p95": round(float(np.percentile(random_paths, 95)), 4),
        "p_value": round(p_value, 4),
        "is_significant_loss": p_value < 0.05,
        "verdict": (
            "Statistically significant LOSS — OU Alpha has decayed or was never real"
            if p_value < 0.05 and actual_total < 0
            else "NOT statistically significant"
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="analyze_ou_pnl")
    p.add_argument("--data-dir", default="data", help="Base data directory")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir

    print(f"[analyze_ou_pnl] Data dir: {data_dir}")
    print(f"[analyze_ou_pnl] Iron Law #11: All statistics below are from script stdout only.\n")

    entries = load_journal(data_dir)
    ou_closes = filter_ou_closes(entries)
    ou_opens = filter_ou_opens(entries)

    print(f"Journal entries: {len(entries)}")
    print(f"OU closes (labeled): {len(ou_closes)}")
    print(f"OU opens (accepted): {len(ou_opens)}")

    if len(ou_closes) < 5:
        print("\n[WARN] Too few OU trades for analysis.")
        return 1

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 1: Strategy + Brain Attribution
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 1: STRATEGY + BRAIN PnL ATTRIBUTION")
    print("=" * 70)
    strat = phase1_strategy_attribution(entries)
    for sname, ss in sorted(strat.items(), key=lambda x: x[1]["pnl"]):
        print(f"\n  Strategy: {sname}")
        print(f"    Opens: {ss['opens']}  Closes: {ss['closes']}")
        print(f"    PnL: {ss['pnl']:+.2f}  WR: {ss['win_rate']:.1%}  Avg: {ss['avg_pnl']:+.4f}")
        if ss["brain_pnl"]:
            print(f"    Brain PnL:")
            for bid, bp in sorted(ss["brain_pnl"].items(), key=lambda x: x[1]):
                print(f"      {bid:<35s} {bp:>+8.2f}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 2: Exit Label Analysis
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 2: EXIT LABEL ANALYSIS")
    print("=" * 70)
    exits = phase2_exit_analysis(ou_closes)
    print(f"  TP hits:   {exits['tp_hit_pnl']:+.2f}")
    print(f"  SL hits:   {exits['sl_hit_pnl']:+.2f}")
    print(f"  OU revert: {exits['ou_revert_pnl']:+.2f}")
    print(f"  Managed:   {exits['managed_pnl']:+.2f}")
    print(f"\n  Top 10 labels by PnL:")
    for label, ls in list(exits["by_label"].items())[:10]:
        print(f"    {label:<40s} {ls['pnl']:>+8.2f} ({ls['count']} trades)")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 3: Entry Signal Quality
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 3: ENTRY SIGNAL QUALITY (z-score, confidence)")
    print("=" * 70)
    sig = phase3_entry_context(ou_closes)
    print(f"  Trades with z-score data: {sig['n_with_zscore']}")
    print(f"  Z-score (all):          mean={sig['z_score_mean']} ± {sig['z_score_std']}")
    print(f"  Z-score (wins):         mean={sig['win_z_mean']}")
    print(f"  Z-score (losses):       mean={sig['loss_z_mean']}")
    print(f"  Win-Loss z-separation:  {sig['z_discrimination']}")
    print(f"  Half-life (mean):       {sig['half_life_mean']} bars")
    print(f"  Confidence (mean):      {sig['confidence_mean']}")
    print(f"  P(win) at entry:        {sig['p_win_mean']}")
    if sig['z_discrimination'] is not None and sig['z_discrimination'] < 0.2:
        print(f"  ⚠️  Z-score has almost NO discriminatory power (separation < 0.2)")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 4: Time Decay
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 4: PnL TIME DECAY")
    print("=" * 70)
    decay = phase4_time_decay(ou_closes)
    print(f"  Trading days: {decay['trading_days']}")
    print(f"  Cumulative PnL: {decay['cumulative_pnl']:+.2f}")
    print(f"  Max losing streak: {decay['max_losing_streak_pnl']:+.2f} over {decay['max_losing_streak_days']} days")
    print(f"\n  Daily PnL:")
    for date_key, dp in decay["daily_pnl"].items():
        bar = "█" * max(1, int(abs(dp["pnl"]) * 5))
        sign = "+" if dp["pnl"] >= 0 else ""
        print(f"    {date_key}  {sign}{dp['pnl']:>7.2f}  ({dp['count']:>2d} trades)  {bar}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Phase 5: Statistical Significance
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("PHASE 5: STATISTICAL SIGNIFICANCE (10,000 iterations)")
    print("=" * 70)
    sig_test = phase5_significance(ou_closes)
    print(f"  N trades:           {sig_test.get('n_trades')}")
    print(f"  Actual PnL:         {sig_test.get('actual_pnl')}")
    print(f"  Random 95% range:   [{sig_test.get('random_p5')}, {sig_test.get('random_p95')}]")
    print(f"  P(two-sided):       {sig_test.get('p_value')}")
    print(f"  Significant loss:   {sig_test.get('is_significant_loss')}")
    print(f"  Verdict:            {sig_test.get('verdict')}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    total_ou_pnl = sum(float(e.get("pnl", 0) or 0) for e in ou_closes)
    ou_wins = sum(1 for e in ou_closes if float(e.get("pnl", 0) or 0) > 0)
    print(f"  Total OU PnL: {total_ou_pnl:+.2f}")
    print(f"  Win rate: {ou_wins}/{len(ou_closes)} ({ou_wins/max(len(ou_closes),1)*100:.1f}%)")
    print(f"\n[DONE] All statistics above are the sole source of truth.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
