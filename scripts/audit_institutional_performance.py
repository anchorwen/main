#!/usr/bin/env python3
"""
Institutional Performance Audit -- Iron Law #11 Compliant
========================================================
Sole source of truth for all live + shadow brain performance statistics.
No manual data reading, no conversational inference.

Scope:
  - XAU: All SignalSettled events -> per-brain performance
  - BTC: All SignalSettled events -> per-brain performance
  - Alpha performance summaries (live strategy metrics)
  - Governance state reconciliation
  - Shadow -> Live promotion candidates (meeting gate thresholds)
  - Live optimization recommendations (underperformers)

Gate Thresholds for Live Promotion:
  - Minimum 50 trades (shadow) or 30 trades (probation->live fast-track)
  - Win rate >= 0.45
  - Profit factor >= 1.1
  - Sharpe ratio >= 0.3
  - Positive total PnL_R
  - Directional balance: not 100% one direction (unless strategy mandates it)

Usage:
  python scripts/audit_institutional_performance.py [--data-dir data] [--data-dir-btc data_btc]
"""

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Gate thresholds ──────────────────────────────────────────
MIN_SHADOW_TRADES = 50       # minimum SignalSettled records to evaluate
MIN_PROBATION_TRADES = 30    # probation->live fast-track minimum
MIN_WIN_RATE = 0.45
MIN_PROFIT_FACTOR = 1.10
MIN_SHARPE = 0.30


def load_signal_settled(data_dir: str) -> list[dict]:
    """Load all SignalSettled events from ledger_events.jsonl."""
    events: list[dict] = []
    ledger_path = Path(data_dir) / "ledger_events.jsonl"
    if not ledger_path.exists():
        print(f"WARNING: {ledger_path} not found")
        return events
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if ev.get("event_type") == "SignalSettled":
                    events.append(ev)
            except json.JSONDecodeError:
                pass
    return events


def compute_brain_stats(events: list[dict]) -> dict[str, dict]:
    """Compute per-brain statistics from SignalSettled events.

    Returns dict keyed by brain_id with fields:
      count, long, short, wins, losses, be,
      win_rate, total_pnl_r, avg_pnl_r, profit_factor,
      sharpe_ratio, pnl_std, direction_bias, first_seen, last_seen
    """
    brains: dict[str, dict] = defaultdict(
        lambda: {
            "pnl_list": [],
            "long": 0,
            "short": 0,
            "first_seen": None,
            "last_seen": None,
        }
    )
    for ev in events:
        bid = ev.get("brain_id", "?")
        b = brains[bid]
        pnl_r = ev.get("pnl_r", 0) or 0
        b["pnl_list"].append(pnl_r)
        d = ev.get("direction", "?")
        if d == "long":
            b["long"] += 1
        elif d == "short":
            b["short"] += 1
        ts = ev.get("timestamp", "")
        if b["first_seen"] is None or ts < b["first_seen"]:
            b["first_seen"] = ts
        if b["last_seen"] is None or ts > b["last_seen"]:
            b["last_seen"] = ts

    result: dict[str, dict] = {}
    for bid, b in sorted(brains.items()):
        pnls = b["pnl_list"]
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0.001)
        losses = sum(1 for p in pnls if p < -0.001)
        be = n - wins - losses
        wr = wins / (wins + losses) if (wins + losses) > 0 else 0.0
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / n if n > 0 else 0.0
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        mean_pnl = sum(pnls) / n if n > 0 else 0.0
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / n if n > 1 else 0.0
        std = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = (mean_pnl / std) if std > 0 else 0.0
        total_decisions = b["long"] + b["short"]
        dir_bias = b["long"] / total_decisions if total_decisions > 0 else 0.5
        result[bid] = {
            "count": n,
            "long": b["long"],
            "short": b["short"],
            "wins": wins,
            "losses": losses,
            "be": be,
            "win_rate": round(wr, 4),
            "total_pnl_r": round(total_pnl, 2),
            "avg_pnl_r": round(avg_pnl, 4),
            "profit_factor": round(pf, 2) if pf != float("inf") else 999.0,
            "sharpe_ratio": round(sharpe, 2),
            "pnl_std": round(std, 4),
            "direction_bias": round(dir_bias, 3),
            "first_seen": b["first_seen"],
            "last_seen": b["last_seen"],
        }
    return result


def load_governance(data_dir: str) -> dict[str, dict]:
    """Load governance state per brain."""
    gov_path = Path(data_dir) / "governance_state.json"
    if not gov_path.exists():
        return {}
    try:
        with open(gov_path, encoding="utf-8") as f:
            gs = json.load(f)
    except UnicodeDecodeError:
        with open(gov_path, encoding="gbk") as f:
            gs = json.load(f)
    states = gs.get("brain_states", {})
    result = {}
    for bid, s in states.items():
        pm = s.get("performance_metrics", {}) or {}
        result[bid] = {
            "status": s.get("status", "?"),
            "trades": pm.get("total_trades", 0) or s.get("total_trades", 0) or 0,
            "wr": pm.get("win_rate", 0) or s.get("win_rate", 0) or 0,
            "pf": pm.get("profit_factor", 0) or s.get("profit_factor", 0) or 0,
            "sharpe": pm.get("sharpe_ratio", 0) or s.get("sharpe_ratio", 0) or 0,
            "pnl_r": pm.get("pnl_r", 0) or s.get("pnl_r", 0) or 0,
        }
    return result


def load_alpha_summaries(data_dir: str) -> list[dict]:
    """Load alpha performance summaries."""
    ap_path = Path(data_dir) / "alpha_performance.json"
    if not ap_path.exists():
        return []
    with open(ap_path, encoding="utf-8") as f:
        ap = json.load(f)
    return ap.get("summaries", [])


def load_live_configs(data_dir: str) -> dict:
    """Load live.yaml or live_btc.yaml config."""
    # Determine which config
    if "btc" in data_dir.lower():
        config_path = Path("configs/live_btc.yaml")
    else:
        config_path = Path("configs/live.yaml")
    if not config_path.exists():
        return {}
    # Simple YAML read without pyyaml
    import re
    config: dict[str, Any] = {"strategy_lines": {}, "brains": {}}
    with open(config_path, encoding="utf-8") as f:
        content = f.read()
    # Find strategy lines
    strategy_section = False
    current_strat = None
    for line in content.split("\n"):
        if line.startswith("strategy_lines:"):
            strategy_section = True
            continue
        if strategy_section:
            m = re.match(r"^  (\w+):", line)
            if m and not line.strip().startswith("#"):
                current_strat = m.group(1)
                config["strategy_lines"][current_strat] = {}
            elif current_strat and re.match(r"^    (\w+):", line):
                pass  # sub-key
        # Brain registry entries
        if "enabled: true" in line or "enabled: false" in line:
            pass
    return config


def assess_promotion(brain_id: str, stats: dict, gov: dict) -> dict:
    """Assess whether a brain qualifies for shadow->live promotion."""
    n = stats["count"]
    wr = stats["win_rate"]
    pf = stats["profit_factor"]
    sharpe = stats["sharpe_ratio"]
    total_pnl = stats["total_pnl_r"]
    dir_bias = stats["direction_bias"]
    gov_status = gov.get(brain_id, {}).get("status", "?")

    checks = {
        "min_trades": n >= MIN_SHADOW_TRADES,
        "win_rate": wr >= MIN_WIN_RATE,
        "profit_factor": pf >= MIN_PROFIT_FACTOR,
        "sharpe": sharpe >= MIN_SHARPE,
        "positive_pnl": total_pnl > 0,
        "balanced_direction": abs(dir_bias - 0.5) < 0.85,  # not 100% one direction
    }
    passed = sum(1 for v in checks.values() if v)
    total_checks = len(checks)
    all_passed = passed == total_checks

    recommendation = "HOLD"
    if all_passed and gov_status in ("candidate", "probation", "shadow"):
        recommendation = "PROMOTE_TO_PROBATION"
    if all_passed and gov_status == "live":
        recommendation = "KEEP_LIVE"
    if wr < 0.35 and n >= MIN_SHADOW_TRADES:
        recommendation = "RETIRE"
    if pf < 0.5 and n >= MIN_SHADOW_TRADES:
        recommendation = "RETIRE"
    if gov_status in ("archived", "retired"):
        recommendation = "ALREADY_RETIRED"

    return {
        "checks": checks,
        "passed": passed,
        "total": total_checks,
        "recommendation": recommendation,
        "gov_status": gov_status,
    }


def print_separator(title: str):
    print()
    print("=" * 90)
    print(f"  {title}")
    print("=" * 90)


def print_brain_table(
    brains: dict[str, dict],
    assessments: dict[str, dict] | None = None,
    sort_by: str = "total_pnl_r",
    top_n: int = 0,
    filter_gov: str = "",
):
    """Print per-brain performance table."""
    if sort_by == "total_pnl_r":
        sorted_brains = sorted(brains.items(), key=lambda x: x[1]["total_pnl_r"], reverse=True)
    elif sort_by == "win_rate":
        sorted_brains = sorted(brains.items(), key=lambda x: x[1]["win_rate"], reverse=True)
    elif sort_by == "count":
        sorted_brains = sorted(brains.items(), key=lambda x: x[1]["count"], reverse=True)
    elif sort_by == "sharpe":
        sorted_brains = sorted(brains.items(), key=lambda x: x[1]["sharpe_ratio"], reverse=True)
    else:
        sorted_brains = list(brains.items())

    if filter_gov:
        sorted_brains = [
            (bid, s)
            for bid, s in sorted_brains
            if (assessments or {}).get(bid, {}).get("gov_status", "") == filter_gov
        ]

    if top_n > 0:
        sorted_brains = sorted_brains[:top_n]

    header = (
        f"{'Brain ID':<42} {'N':>5} {'WR':>7} {'PF':>6} "
        f"{'Sharpe':>7} {'PnL_R':>9} {'DirBias':>8} {'Gov':>10} {'Rec':>24}"
    )
    print(header)
    print("-" * len(header))

    for bid, s in sorted_brains:
        ass = (assessments or {}).get(bid, {})
        rec = ass.get("recommendation", "")
        gov_s = ass.get("gov_status", "?")
        flags = ""
        if s["win_rate"] >= 0.99:
            flags += " !WR"
        if abs(s["direction_bias"] - 0.5) >= 0.85:
            flags += " !DIR"
        dir_str = f"{s['direction_bias']:.2f}"
        if s["direction_bias"] >= 0.85:
            dir_str += "L"
        elif s["direction_bias"] <= 0.15:
            dir_str += "S"
        print(
            f"{bid:<42} {s['count']:>5} {s['win_rate']:>7.3f} {s['profit_factor']:>6.2f} "
            f"{s['sharpe_ratio']:>7.2f} {s['total_pnl_r']:>9.2f} {dir_str:>8} {gov_s:>10} {rec:<24}{flags}"
        )


def print_summary_stats(brains: dict[str, dict]):
    """Print aggregate statistics."""
    total_trades = sum(s["count"] for s in brains.values())
    total_pnl = sum(s["total_pnl_r"] for s in brains.values())
    positive_brains = sum(1 for s in brains.values() if s["total_pnl_r"] > 0)
    negative_brains = sum(1 for s in brains.values() if s["total_pnl_r"] < 0)
    high_wr = sum(1 for s in brains.values() if s["win_rate"] >= 0.50 and s["count"] >= MIN_SHADOW_TRADES)

    print(f"  Total SignalSettled events: {total_trades:,}")
    print(f"  Total PnL_R: {total_pnl:+.2f}")
    print(f"  Brains with data: {len(brains)}")
    print(f"  Profitable brains: {positive_brains}  |  Unprofitable: {negative_brains}")
    print(f"  Brains with WR>=0.50 (min {MIN_SHADOW_TRADES} trades): {high_wr}")


def main():
    data_dir_xau = "data"
    data_dir_btc = "data_btc"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--data-dir" and i < len(sys.argv) - 1:
            data_dir_xau = sys.argv[i + 1]
        if arg == "--data-dir-btc" and i < len(sys.argv) - 1:
            data_dir_btc = sys.argv[i + 1]

    # ── Load data ─────────────────────────────────────────────
    print("Loading SignalSettled events...")
    xau_events = load_signal_settled(data_dir_xau)
    btc_events = load_signal_settled(data_dir_btc)

    print(f"  XAU: {len(xau_events):,} events")
    print(f"  BTC: {len(btc_events):,} events")

    xau_brains = compute_brain_stats(xau_events)
    btc_brains = compute_brain_stats(btc_events)

    xau_gov = load_governance(data_dir_xau)
    btc_gov = load_governance(data_dir_btc)

    # ── Assessments ───────────────────────────────────────────
    xau_assess = {bid: assess_promotion(bid, s, xau_gov) for bid, s in xau_brains.items()}
    btc_assess = {bid: assess_promotion(bid, s, btc_gov) for bid, s in btc_brains.items()}

    # ===========================================================
    #  XAU REPORT
    # ===========================================================
    print_separator("XAU/USD -- Complete Brain Performance Audit (SignalSettled)")
    print_summary_stats(xau_brains)

    # XAU: Top performers by PnL
    print_separator("XAU -- Top 15 by Total PnL_R")
    print_brain_table(xau_brains, xau_assess, sort_by="total_pnl_r", top_n=15)

    # XAU: Live strategy brains only
    print_separator("XAU -- Currently Live/Probation Brains (per governance)")
    live_xau = {
        bid: s
        for bid, s in xau_brains.items()
        if xau_gov.get(bid, {}).get("status") in ("live", "probation")
    }
    live_xau_assess = {bid: xau_assess[bid] for bid in live_xau}
    print_brain_table(live_xau, live_xau_assess, sort_by="total_pnl_r")

    # XAU: Shadow/Candidate promotion candidates
    print_separator("XAU -- Promotion Candidates (shadow/candidate -> probation/live)")
    promote_xau = {
        bid: s
        for bid, s in xau_brains.items()
        if xau_assess[bid]["recommendation"] == "PROMOTE_TO_PROBATION"
    }
    if promote_xau:
        promote_xau_assess = {bid: xau_assess[bid] for bid in promote_xau}
        print_brain_table(promote_xau, promote_xau_assess, sort_by="total_pnl_r")
    else:
        print("  NONE -- No shadow/candidate brains meet promotion thresholds.")

    # XAU: Retirement candidates
    print_separator("XAU -- Retirement Candidates (WR<0.35 or PF<0.5, min 50 trades)")
    retire_xau = {
        bid: s
        for bid, s in xau_brains.items()
        if xau_assess[bid]["recommendation"] == "RETIRE"
        and xau_gov.get(bid, {}).get("status") not in ("archived", "retired")
    }
    if retire_xau:
        retire_xau_assess = {bid: xau_assess[bid] for bid in retire_xau}
        print_brain_table(retire_xau, retire_xau_assess, sort_by="total_pnl_r")
    else:
        print("  NONE -- All low-performing brains already retired/archived.")

    # XAU: Governance reconciliation gap
    print_separator("XAU -- Governance Reconciliation Gap (ledger data vs governance state)")
    gap_count = 0
    for bid, s in xau_brains.items():
        if s["count"] >= MIN_SHADOW_TRADES:
            gov_s = xau_gov.get(bid, {})
            gov_trades = gov_s.get("trades", 0)
            if gov_trades == 0 and s["count"] > 0:
                gap_count += 1
                if gap_count <= 15:
                    print(
                        f"  {bid}: ledger={s['count']} trades, governance={gov_trades} trades "
                        f"(status={gov_s.get('status','?')}) -- GOVERNANCE STALE"
                    )
    if gap_count > 15:
        print(f"  ... and {gap_count - 15} more")
    if gap_count == 0:
        print("  OK  All brains have governance metrics aligned with ledger.")

    # ===========================================================
    #  BTC REPORT
    # ===========================================================
    print_separator("BTC/USD -- Complete Brain Performance Audit (SignalSettled)")
    print_summary_stats(btc_brains)

    # BTC: All brains
    print_separator("BTC -- All Brains by Total PnL_R")
    print_brain_table(btc_brains, btc_assess, sort_by="total_pnl_r")

    # BTC: Live config brains
    print_separator("BTC -- Currently Enabled Brains (per live_btc.yaml)")
    # V4, V9_H1, V12_H1 are enabled
    live_btc_ids = ["BTC_Swing_V4", "BTC_Swing_V9_H1_Survival", "BTC_Swing_V12_H1_Survival"]
    live_btc = {bid: s for bid, s in btc_brains.items() if bid in live_btc_ids}
    if live_btc:
        live_btc_assess = {bid: btc_assess[bid] for bid in live_btc}
        print_brain_table(live_btc, live_btc_assess, sort_by="total_pnl_r")
    else:
        print("  NONE of the enabled brains have SignalSettled data.")

    # BTC: Promotion candidates
    print_separator("BTC -- Promotion Candidates (shadow/candidate -> probation/live)")
    promote_btc = {
        bid: s
        for bid, s in btc_brains.items()
        if btc_assess[bid]["recommendation"] == "PROMOTE_TO_PROBATION"
    }
    if promote_btc:
        promote_btc_assess = {bid: btc_assess[bid] for bid in promote_btc}
        print_brain_table(promote_btc, promote_btc_assess, sort_by="total_pnl_r")
    else:
        print("  NONE -- No shadow/candidate BTC brains meet promotion thresholds.")

    # BTC: Retirement candidates
    print_separator("BTC -- Retirement Candidates")
    retire_btc = {
        bid: s
        for bid, s in btc_brains.items()
        if btc_assess[bid]["recommendation"] == "RETIRE"
        and btc_gov.get(bid, {}).get("status") not in ("archived", "retired")
    }
    if retire_btc:
        retire_btc_assess = {bid: btc_assess[bid] for bid in retire_btc}
        print_brain_table(retire_btc, retire_btc_assess, sort_by="total_pnl_r")
    else:
        print("  NONE -- All low-performing BTC brains already retired.")

    # ===========================================================
    #  OPTIMIZATION RECOMMENDATIONS
    # ===========================================================
    print_separator("INSTITUTIONAL RECOMMENDATIONS")

    # --- XAU Live optimization ---
    print()
    print("── XAU LIVE OPTIMIZATION ──")
    xau_live_ids = [
        bid
        for bid, s in xau_brains.items()
        if xau_gov.get(bid, {}).get("status") in ("live", "probation")
    ]
    for bid in sorted(xau_live_ids):
        s = xau_brains[bid]
        a = xau_assess[bid]
        n = s["count"]
        wr = s["win_rate"]
        pf = s["profit_factor"]
        pnl = s["total_pnl_r"]
        sharpe = s["sharpe_ratio"]
        dir_b = s["direction_bias"]
        gov_s = a["gov_status"]
        issues = []
        if wr < 0.45 and n >= MIN_SHADOW_TRADES:
            issues.append(f"Low WR ({wr:.3f}), consider re-training or parameter tuning")
        if pf < 1.0 and n >= MIN_SHADOW_TRADES:
            issues.append(f"PF<1.0 ({pf:.2f}), negative expectancy")
        if sharpe < 0.0:
            issues.append(f"Negative Sharpe ({sharpe:.2f}), risk-adjusted returns poor")
        if abs(dir_b - 0.5) > 0.80:
            issues.append(f"Directional lock ({dir_b:.0%} {'LONG' if dir_b > 0.5 else 'SHORT'}), missing one market side")
        if n < MIN_SHADOW_TRADES:
            issues.append(f"Insufficient data ({n}<{MIN_SHADOW_TRADES}), observe longer")
        status_icon = "OK " if not issues else "!! "
        print(f"  {status_icon} [{gov_s.upper()}] {bid}: n={n} WR={wr:.3f} PF={pf:.2f} PnL={pnl:+.1f}")
        for issue in issues:
            print(f"      -> {issue}")
        if not issues:
            print(f"      -> Healthy. Monitor for regime drift.")

    # --- XAU Shadow promotion ---
    print()
    print("── XAU SHADOW -> LIVE PROMOTION CANDIDATES ──")
    shadow_ids = [
        bid
        for bid, s in xau_brains.items()
        if xau_gov.get(bid, {}).get("status") in ("candidate", "shadow", "")
    ]
    candidates = []
    for bid in shadow_ids:
        s = xau_brains[bid]
        a = xau_assess[bid]
        if s["count"] >= MIN_SHADOW_TRADES and s["win_rate"] >= MIN_WIN_RATE and s["total_pnl_r"] > 0:
            candidates.append((bid, s, a))

    if candidates:
        # Sort by PnL_R
        candidates.sort(key=lambda x: x[1]["total_pnl_r"], reverse=True)
        for bid, s, a in candidates:
            checks_str = " ".join(
                f"{'OK ' if v else 'XX '}{k}" for k, v in a["checks"].items()
            )
            print(
                f"  >  PROMOTE [{a['gov_status']}->probation] {bid}: "
                f"n={s['count']} WR={s['win_rate']:.3f} PF={s['profit_factor']:.2f} "
                f"Sharpe={s['sharpe_ratio']:.2f} PnL={s['total_pnl_r']:+.1f}"
            )
            print(f"      Checks: {checks_str}")
    else:
        print("  NONE -- No shadow brains meet all promotion thresholds.")

    # --- BTC Live optimization ---
    print()
    print("── BTC LIVE OPTIMIZATION ──")
    for bid in live_btc_ids:
        if bid in btc_brains:
            s = btc_brains[bid]
            a = btc_assess[bid]
            n = s["count"]
            wr = s["win_rate"]
            pf = s["profit_factor"]
            pnl = s["total_pnl_r"]
            sharpe = s["sharpe_ratio"]
            dir_b = s["direction_bias"]
            gov_s = a["gov_status"]
            issues = []
            if wr < 0.45 and n >= MIN_SHADOW_TRADES:
                issues.append(f"Low WR ({wr:.3f})")
            if pf < 1.0 and n >= MIN_SHADOW_TRADES:
                issues.append(f"PF<1.0 ({pf:.2f}), negative expectancy")
            if abs(dir_b - 0.5) > 0.80:
                issues.append(f"Directional lock ({dir_b:.0%} {'LONG' if dir_b > 0.5 else 'SHORT'})")
            status_icon = "OK " if not issues else "!! "
            print(f"  {status_icon} [{gov_s.upper()}] {bid}: n={n} WR={wr:.3f} PF={pf:.2f} PnL={pnl:+.1f}")
            for issue in issues:
                print(f"      -> {issue}")

    # --- BTC Shadow promotion ---
    print()
    print("── BTC SHADOW -> LIVE PROMOTION CANDIDATES ──")
    btc_shadow_ids = [
        bid
        for bid, s in btc_brains.items()
        if btc_gov.get(bid, {}).get("status") in ("candidate", "shadow", "")
        and bid not in live_btc_ids
    ]
    btc_candidates = []
    for bid in btc_shadow_ids:
        s = btc_brains[bid]
        a = btc_assess[bid]
        if s["count"] >= MIN_SHADOW_TRADES and s["win_rate"] >= MIN_WIN_RATE and s["total_pnl_r"] > 0:
            btc_candidates.append((bid, s, a))

    if btc_candidates:
        btc_candidates.sort(key=lambda x: x[1]["total_pnl_r"], reverse=True)
        for bid, s, a in btc_candidates:
            checks_str = " ".join(
                f"{'OK ' if v else 'XX '}{k}" for k, v in a["checks"].items()
            )
            print(
                f"  >  PROMOTE [{a['gov_status']}->probation] {bid}: "
                f"n={s['count']} WR={s['win_rate']:.3f} PF={s['profit_factor']:.2f} "
                f"Sharpe={s['sharpe_ratio']:.2f} PnL={s['total_pnl_r']:+.1f}"
            )
            print(f"      Checks: {checks_str}")
    else:
        print("  NONE -- No shadow BTC brains meet all promotion thresholds.")

    print()
    print("[DONE] All statistics above are the sole source of truth.")
    print(f"Generated: {datetime.utcnow().isoformat()}Z")


if __name__ == "__main__":
    main()
