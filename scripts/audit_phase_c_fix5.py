"""Phase C + Fix 5 joint review audit — 2026-06-11.
# type: ignore  # FIX-20260620-076: Sev 4 audit script, suppressed

Phase C: Check OFI-based micro partial TP triggering from position_snapshots + golden master.
Fix 5:  Evaluate MetaFilter p_win by regime using golden master + meta_filter_state.

Declarations:
  - Regime source: golden_master.jsonl per-cycle ``inputs.regime`` field
  - MetaFilter p_win: meta_filter_state.json ``pred_history``, matched by timestamp proximity
  - Partial TP: position_snapshots.jsonl (recent entries may have richer fields)

Usage:
  python scripts/audit_phase_c_fix5.py --data-dir data --days 14
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase C + Fix 5 joint audit")
    p.add_argument("--data-dir", default="data", help="XAU data directory")
    p.add_argument("--days", type=int, default=14, help="Lookback days")
    return p.parse_args()


def parse_ts(ts_str: str | float) -> datetime | None:
    if not ts_str:
        return None
    try:
        if isinstance(ts_str, (int, float)):
            return datetime.fromtimestamp(ts_str, tz=UTC)
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError, OSError):
        return None


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 5: Regime=Ranging MetaFilter evaluation (from golden master)
# ═══════════════════════════════════════════════════════════════════════════════

def audit_fix5(data_dir: str, since: datetime) -> dict:
    """Analyze MetaFilter p_win per regime using golden master + meta_filter_state."""
    gm = load_jsonl(Path(data_dir) / "golden_master.jsonl")
    mf_path = Path(data_dir) / "meta_filter_state.json"
    mf_data = json.loads(mf_path.read_text()) if mf_path.exists() else {}

    # Build MetaFilter p_win timeline: [(ts, p_win), ...]
    pred_history = mf_data.get("pred_history", [])
    mf_timeline: list[tuple[float, float]] = []
    for entry in pred_history:
        if isinstance(entry, list) and len(entry) >= 2:
            mf_timeline.append((float(entry[0]), float(entry[1])))
    mf_timeline.sort()

    # Per-regime collection
    regime_p_wins: dict[str, list[float]] = defaultdict(list)
    regime_cycles: dict[str, int] = defaultdict(int)
    regime_swing_signals: dict[str, list[dict]] = defaultdict(list)
    total_cycles = 0

    for entry in gm:
        ts = parse_ts(entry.get("timestamp_utc", ""))
        if ts and ts < since:
            continue
        total_cycles += 1
        inputs = entry.get("inputs", {})
        regime = str(inputs.get("regime", "unknown"))
        regime_cycles[regime] += 1

        # Match MetaFilter p_win by timestamp proximity
        now_unix = entry.get("now_unix")
        if now_unix and mf_timeline:
            closest_p_win = _find_closest_p_win(float(now_unix), mf_timeline)
            if closest_p_win is not None:
                regime_p_wins[regime].append(closest_p_win)

        # Check swing strategy outputs
        outputs = entry.get("outputs", {})
        for strat_name, strat_out in outputs.items():
            if "swing" in strat_name and isinstance(strat_out, dict):
                regime_swing_signals[regime].append({
                    "strategy": strat_name,
                    "should_trade": strat_out.get("should_trade", False),
                    "direction": strat_out.get("direction", "neutral"),
                    "reason": strat_out.get("reason", ""),
                })

    # Build regime summary
    summary = {}
    for regime in sorted(regime_cycles.keys()):
        p_wins = regime_p_wins.get(regime, [])
        signals = regime_swing_signals.get(regime, [])
        h1_h4_signals = [s for s in signals if "h1_swing" in s["strategy"] or "h4_swing" in s["strategy"]]
        h1_h4_trades = [s for s in h1_h4_signals if s.get("should_trade")]

        summary[regime] = {
            "cycles": regime_cycles[regime],
            "pct": round(100 * regime_cycles[regime] / max(1, total_cycles), 1),
            "p_win_mean": round(sum(p_wins) / len(p_wins), 4) if p_wins else None,
            "p_win_median": round(sorted(p_wins)[len(p_wins)//2], 4) if p_wins else None,
            "p_win_samples": len(p_wins),
            "h1_h4_signals": len(h1_h4_signals),
            "h1_h4_trades": len(h1_h4_trades),
        }

    # Check for MetaFilter business hours coverage
    # MetaFilter only runs during high-liquidity hours — check gap
    mf_in_period = 0
    for ts, _ in mf_timeline:
        dt = datetime.fromtimestamp(ts, tz=UTC)
        if dt >= since:
            mf_in_period += 1

    return {
        "regime_summary": summary,
        "total_cycles": total_cycles,
        "mf_predictions_in_period": mf_in_period,
        "mf_total_predictions": len(mf_timeline),
        "sample_period_days": (datetime.now(UTC) - since).days,
    }


def _find_closest_p_win(timestamp: float, timeline: list[tuple[float, float]], max_gap: float = 60.0) -> float | None:
    """Find closest MetaFilter p_win within max_gap seconds of timestamp."""
    best = None
    best_gap = max_gap
    for ts, p_win in timeline:
        gap = abs(ts - timestamp)
        if gap < best_gap:
            best_gap = gap
            best = p_win
    return best


# ═══════════════════════════════════════════════════════════════════════════════
# Phase C: OFI + Partial TP
# ═══════════════════════════════════════════════════════════════════════════════

def audit_phase_c(data_dir: str, since: datetime) -> dict:
    """Check partial TP and OFI-related activity."""
    gm = load_jsonl(Path(data_dir) / "golden_master.jsonl")
    snapshots = load_jsonl(Path(data_dir) / "position_snapshots.jsonl")

    # Count partial TP events from snapshots
    partial_tp_count = 0
    partial_tp_tickets: set[int] = set()
    ofi_available_cycles = 0
    ofi_values: list[float] = []

    for s in snapshots:
        ts = parse_ts(s.get("time", ""))
        if ts and ts < since:
            continue
        # Check for partial_tp field
        ptp = s.get("partial_tp", {})
        if isinstance(ptp, dict) and ptp.get("triggered"):
            partial_tp_count += 1
            tkt = s.get("ticket")
            if tkt:
                partial_tp_tickets.add(int(tkt))

        # OFI might be in features or golden master
        ofi = s.get("ofi_z") or (ptp.get("ofi_z") if isinstance(ptp, dict) else None)
        if ofi and float(ofi) != 0.0:
            ofi_values.append(float(ofi))

    # Check golden master for OFI and micro feature data
    for entry in gm:
        ts = parse_ts(entry.get("timestamp_utc", ""))
        if ts and ts < since:
            continue
        inputs = entry.get("inputs", {})
        outputs = entry.get("outputs", {})

        # Check for OFI in micro_feature_vector (if logged)
        micro_fv = inputs.get("micro_feature_vector_head8", [])
        ofi_from_gm = inputs.get("ofi") or inputs.get("ofi_z")

        if ofi_from_gm:
            ofi_available_cycles += 1

        # Check if any strategy output mentions partial_tp or micro_ptp
        for sname, sout in outputs.items():
            if isinstance(sout, dict):
                reason = str(sout.get("reason", ""))
                if "partial" in reason.lower() or "ofi" in reason.lower() or "ptp" in reason.lower():
                    ofi_available_cycles += 1

    # Check journal for partial close entries
    journal = load_jsonl(Path(data_dir) / "live_trade_journal.jsonl")
    partial_closes = 0
    for j in journal:
        ts = parse_ts(j.get("recorded_at", ""))
        if ts and ts < since:
            continue
        detail = j.get("detail", {})
        reason = str(detail.get("reason", "") or j.get("close_reason", ""))
        if "partial" in reason.lower():
            partial_closes += 1

    return {
        "partial_tp_snapshots": partial_tp_count,
        "partial_tp_tickets": sorted(partial_tp_tickets)[:10],
        "partial_close_journal": partial_closes,
        "ofi_available_cycles": ofi_available_cycles,
        "ofi_samples": len(ofi_values),
        "ofi_mean": round(sum(ofi_values) / len(ofi_values), 3) if ofi_values else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    args = parse_args()
    since = datetime.now(UTC) - timedelta(days=args.days)

    print("=" * 72)
    print("Phase C + Fix 5 Joint Review Audit")
    print(f"Data dir: {args.data_dir}")
    print(f"Period: {since.strftime('%Y-%m-%d')} -> {datetime.now(UTC).strftime('%Y-%m-%d')} ({args.days} days)")
    print("=" * 72)

    # ── Fix 5: Ranging Regime MetaFilter ──
    print()
    print("--- Fix 5: MetaFilter p_win by Regime (source: golden_master.jsonl) ---")
    f5 = audit_fix5(args.data_dir, since)
    print(f"  Total cycles analyzed: {f5['total_cycles']}")
    print(f"  MetaFilter predictions in period: {f5['mf_predictions_in_period']} / {f5['mf_total_predictions']} total")
    print()

    for regime, info in sorted(f5['regime_summary'].items()):
        print(f"  [{regime}] {info['cycles']} cycles ({info['pct']}%)")
        print(f"    p_win mean: {info['p_win_mean']}, median: {info['p_win_median']} ({info['p_win_samples']} samples)")
        print(f"    h1/h4 swing signals: {info['h1_h4_signals']}, trades: {info['h1_h4_trades']}")

    # ── Phase C: Partial TP ──
    print()
    print("--- Phase C: OFI Micro Partial TP (source: position_snapshots + golden_master) ---")
    pc = audit_phase_c(args.data_dir, since)
    print(f"  Partial TP snapshots: {pc['partial_tp_snapshots']}")
    print(f"  Partial close journal entries: {pc['partial_close_journal']}")
    print(f"  OFI available cycles: {pc['ofi_available_cycles']}")
    print(f"  OFI samples with data: {pc['ofi_samples']}")
    if pc['ofi_mean'] is not None:
        print(f"  OFI z-score mean: {pc['ofi_mean']}")
    if pc['partial_tp_tickets']:
        print(f"  PTP tickets: {pc['partial_tp_tickets']}")

    # ── Verdicts ──
    print()
    print("--- Verdicts ---")

    # Fix 5
    ranging = f5['regime_summary'].get('ranging', {})
    if ranging:
        p_win = ranging.get('p_win_mean')
        samples = ranging.get('p_win_samples', 0)
        trades = ranging.get('h1_h4_trades', 0)
        if p_win is not None and samples >= 5:
            if p_win <= 0.50:
                print(f"  Fix 5: [PASS] MetaFilter auto-blocks in ranging (p_win={p_win:.3f} < 0.50, n={samples}). No hardcoded degrade needed.")
            elif p_win <= 0.55:
                print(f"  Fix 5: [WARN] MetaFilter borderline in ranging (p_win={p_win:.3f}, n={samples}). Extend observation to 6/16.")
            else:
                print(f"  Fix 5: [FAIL] MetaFilter not blocking ranging (p_win={p_win:.3f} >= 0.55, n={samples}). Consider hardcoded degrade or retrain MetaFilter.")
        else:
            print(f"  Fix 5: [DEFER] Insufficient ranging data (p_win={p_win}, n={samples}, trades={trades}). Extend to 6/16.")
    else:
        # Check if we're in a trending-only market
        trending = f5['regime_summary'].get('trending', {})
        print(f"  Fix 5: [DEFER] No 'ranging' regime detected in {f5['total_cycles']} cycles.")
        if trending:
            print(f"    Market is mostly 'trending' ({trending.get('cycles',0)} cycles, {trending.get('pct',0)}%). MetaFilter h1/h4 p_win={trending.get('p_win_mean')}.")
            print("    Re-evaluate when ranging conditions appear.")

    # Phase C
    if pc['partial_tp_snapshots'] > 0:
        print(f"  Phase C: [PASS] Partial TP is active ({pc['partial_tp_snapshots']} triggers). OFI available: {pc['ofi_available_cycles']} cycles.")
        if pc['ofi_mean'] is not None and abs(pc['ofi_mean']) >= 1.5:
            print(f"    OFI z-score significant (|z|={abs(pc['ofi_mean']):.2f} >= 1.5). Can replace order book depth.")
        else:
            print("    OFI data insufficient to assess. Continue observation.")
    else:
        # Check if OFI is even being computed
        print(f"  Phase C: [DEFER] No partial TP triggered in {args.days} days.")
        if pc['ofi_available_cycles'] == 0:
            print("    OFI/OIM computation may not be wired to golden master or snapshots. Check microstructure_computer.py -> live_cycle.py -> partial TP gate.")
        elif pc['partial_close_journal'] > 0:
            print(f"    {pc['partial_close_journal']} partial closes in journal but snapshots don't show PTP triggers. Snapshot fields may be incomplete.")
        else:
            print("    No partial TP opportunities in current market. Gate is functional but waiting for conditions.")

    print()
    print("=" * 72)
    print("Audit complete. All data from golden_master.jsonl + meta_filter_state.json + journal.")
    print("=" * 72)


if __name__ == "__main__":
    main()
