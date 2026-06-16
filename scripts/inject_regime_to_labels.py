#!/usr/bin/env python
"""R4 Step 4b: Retroactive regime injection into existing labels.

Matches each label to the closest regime snapshot by timestamp
(within 10 minutes).  Produces labels_with_regime.jsonl with
regime context attached to each trade outcome.

Also outputs a preliminary regime-conditioned win rate analysis.

Usage:
    python scripts/inject_regime_to_labels.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

DATA_SETS = {
    "XAU": {
        "labels": ROOT / "data" / "reports" / "live_labels.jsonl",
        "snapshots": ROOT / "data" / "regime_snapshots.jsonl",
        "output": ROOT / "data" / "reports" / "labels_with_regime.jsonl",
    },
    "BTC": {
        "labels": ROOT / "data_btc" / "reports" / "live_labels.jsonl",
        "snapshots": ROOT / "data_btc" / "regime_snapshots.jsonl",
        "output": ROOT / "data_btc" / "reports" / "labels_with_regime.jsonl",
    },
}


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse various ISO timestamp formats, stripping tzinfo for comparison."""
    try:
        s = str(ts_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def _inject(sym: str, cfg: dict) -> dict:
    labels_path = cfg["labels"]
    snaps_path = cfg["snapshots"]
    out_path = cfg["output"]

    if not labels_path.exists():
        print(f"[SKIP] {sym}: labels not found")
        return {}
    if not snaps_path.exists():
        print(f"[SKIP] {sym}: snapshots not found (run build_regime_snapshots.py first)")
        return {}

    with open(labels_path, encoding="utf-8") as f:
        labels = [json.loads(l) for l in f if l.strip()]
    with open(snaps_path, encoding="utf-8") as f:
        snaps = [json.loads(l) for l in f if l.strip()]

    # Build sorted snapshots for binary search
    snap_times = []
    for s in snaps:
        ts = _parse_ts(s.get("timestamp_utc", ""))
        if ts:
            snap_times.append((ts, s))
    snap_times.sort(key=lambda x: x[0])

    if snap_times:
        print(f"  Snapshots: {snap_times[0][0].isoformat()[:19]} ~ {snap_times[-1][0].isoformat()[:19]}")

    # Find label date range
    label_dates = []
    for label in labels:
        for ts_field in ("close_recorded_at", "open_recorded_at"):
            val = label.get(ts_field, "")
            if val:
                ts = _parse_ts(str(val))
                if ts:
                    label_dates.append(ts)
                    break
    if label_dates:
        print(f"  Labels: {min(label_dates).isoformat()[:19]} ~ {max(label_dates).isoformat()[:19]}")

    MAX_DELTA = timedelta(minutes=10)

    matched = 0
    unmatched = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Regime-conditioned stats
    regime_stats = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl_sum": 0.0})

    with open(out_path, "w", encoding="utf-8") as f:
        for label in labels:
            # ── FIX-20260616-096: MUST use open_recorded_at (entry-time snapshot).
            #   Using close_recorded_at injects FUTURE regime data into the label —
            #   the model would learn from information it cannot know at decision time.
            #   This is classic Look-Ahead Bias — Severity 1.
            label_ts = None
            for ts_field in ("open_recorded_at", "close_recorded_at", "timestamp", "event_time", "recorded_at"):
                val = label.get(ts_field, "")
                if val:
                    label_ts = _parse_ts(str(val))
                    if label_ts:
                        break

            if label_ts is None:
                label["_regime"] = {"error": "no_timestamp"}
                f.write(json.dumps(label, ensure_ascii=False) + "\n")
                unmatched += 1
                continue

            # Binary search for nearest snapshot
            lo, hi = 0, len(snap_times) - 1
            best_idx = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if snap_times[mid][0] <= label_ts:
                    best_idx = mid
                    lo = mid + 1
                else:
                    hi = mid - 1

            # Check forward/backward for closest
            candidates = []
            for offset in range(-2, 3):
                idx = best_idx + offset
                if 0 <= idx < len(snap_times):
                    delta = abs(snap_times[idx][0] - label_ts)
                    if delta <= MAX_DELTA:
                        candidates.append((delta, snap_times[idx][1]))

            if not candidates:
                label["_regime"] = {"error": "no_snapshot_match"}
                f.write(json.dumps(label, ensure_ascii=False) + "\n")
                unmatched += 1
                continue

            # Use closest
            candidates.sort(key=lambda x: x[0])
            snap = candidates[0][1]

            regime_ctx = {
                "trend_direction": snap.get("trend_direction", "neutral"),
                "trend_strength": snap.get("trend_strength", 0.0),
                "detected_regime": snap.get("detected_regime", "unknown"),
                "macro_regime": snap.get("macro_regime", "mixed"),
                "hurst": snap.get("hurst", 0.5),
                "current_atr": snap.get("current_atr", 0.0),
                "atr_percentile": snap.get("atr_percentile", 0.5),
                "snapshot_ts": snap.get("timestamp_utc", ""),
            }
            label["_regime"] = regime_ctx
            f.write(json.dumps(label, ensure_ascii=False) + "\n")
            matched += 1

            # Accumulate stats
            trend = regime_ctx["trend_direction"]
            label_name = label.get("label", "unknown")
            pnl = label.get("pnl", 0) or 0
            regime_stats[trend]["count"] += 1
            regime_stats[trend]["pnl_sum"] += pnl
            if label_name == "win" or (isinstance(pnl, (int, float)) and pnl > 0):
                regime_stats[trend]["wins"] += 1
            elif label_name == "loss" or (isinstance(pnl, (int, float)) and pnl < 0):
                regime_stats[trend]["losses"] += 1

    # Print analysis
    print(f"\n── {sym} ──")
    print(f"  Labels: {len(labels)}, Matched: {matched}, Unmatched: {unmatched}")
    print(f"  Output: {out_path}")
    print("\n  Regime-Conditioned Win Rate:")
    print(f"  {'Trend':<12s} {'Trades':>7s} {'Wins':>6s} {'Losses':>6s} {'WR':>7s} {'PnL_sum':>9s}")
    print(f"  {'-'*12} {'-'*7} {'-'*6} {'-'*6} {'-'*7} {'-'*9}")
    for trend in ["long", "short", "neutral"]:
        s = regime_stats.get(trend, {})
        ct = s.get("count", 0)
        w = s.get("wins", 0)
        l_ = s.get("losses", 0)
        wr = w / (w + l_) * 100 if (w + l_) > 0 else 0
        pnl = s.get("pnl_sum", 0.0)
        print(f"  {trend:<12s} {ct:>7d} {w:>6d} {l_:>6d} {wr:>6.1f}% {pnl:>+9.2f}")

    return dict(regime_stats)


def main() -> int:
    print("=" * 60)
    print("  R4 Step 4b: Regime Label Injection")
    print("=" * 60)

    for sym, cfg in DATA_SETS.items():
        _inject(sym, cfg)

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
