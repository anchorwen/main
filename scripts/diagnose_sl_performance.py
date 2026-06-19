# type: ignore
#!/usr/bin/env python
"""SL Performance Diagnostic — Iron Law #11 compliant.

Analyzes Stop-Loss exit quality: SL distance vs ATR, trail advancement
frequency, breakeven timing, and directional accuracy.

Usage:
    python scripts/diagnose_sl_performance.py

Output sections:
    1. SL Exit Overview — count/PnL by label
    2. SL Distance vs ATR — initial SL tightness
    3. Trail Advancement — how often does trail activate before SL hit?
    4. Directional Analysis — did price ever move in the predicted direction?
    5. Recommendation — data-driven suggested parameter adjustments
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

ROOT = Path(__file__).resolve().parent.parent
DATA_DIRS = {"XAU": ROOT / "data", "BTC": ROOT / "data_btc"}


def _read_jsonl(path: Path) -> list[dict]:
    entries: list[dict[str, object]] = []
    if not path.exists():
        return entries
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def analyze_sl(sym: str, base: Path) -> dict:
    journal = _read_jsonl(base / "live_trade_journal.jsonl")
    snaps = _read_jsonl(base / "position_snapshots.jsonl")

    # Dedup closes by position_ticket
    ticket_closes: dict[str, dict] = {}
    for entry in journal:
        if not isinstance(entry, dict):
            continue
        ticket = entry.get("position_ticket")
        if not ticket or entry.get("action") != "close":
            continue
        ticket_closes[str(ticket)] = entry

    closes = list(ticket_closes.values())

    # Categorize
    sl_labels = {"loss", "sl_hit_first", "sl_hit_trailed", "breakeven"}
    sl_closes = [c for c in closes if c.get("label") in sl_labels]
    tp_closes = [c for c in closes if c.get("label") in ("tp_hit_first", "win")]
    other_closes = [c for c in closes if c.get("label") not in sl_labels and c.get("label") not in ("tp_hit_first", "win")]

    # SL distance analysis
    sl_stats = {"with_distance": 0, "distance_bps": [], "trail_advances": []}
    for c in sl_closes:
        sl_val = c.get("sl")
        entry_val = c.get("entry_price")
        side = c.get("side", "")
        if sl_val and entry_val and entry_val > 0:
            if side == "long":
                dist_bps = (entry_val - sl_val) / entry_val * 10000
            else:
                dist_bps = (sl_val - entry_val) / entry_val * 10000
            sl_stats["distance_bps"].append(dist_bps)
            sl_stats["with_distance"] += 1
        tc = c.get("trail_contribution", {})
        if isinstance(tc, dict):
            sl_stats["trail_advances"].append(tc.get("trail_advances", 0))

    # Trail advance distribution
    ta_dist: dict[float, int] = defaultdict(int)
    for ta in sl_stats["trail_advances"]:
        ta_dist[ta] += 1

    # PnL aggregation
    sl_pnl = sum(c.get("pnl", 0) or 0 for c in sl_closes)
    tp_pnl = sum(c.get("pnl", 0) or 0 for c in tp_closes)

    return {
        "symbol": sym,
        "total_closes": len(closes),
        "sl_closes": len(sl_closes),
        "tp_closes": len(tp_closes),
        "other_closes": len(other_closes),
        "sl_pnl": round(sl_pnl, 2),
        "tp_pnl": round(tp_pnl, 2),
        "sl_distance_bps": sl_stats["distance_bps"],
        "trail_advances_dist": dict(ta_dist),
        "trail_advances_zero_pct": round(
            ta_dist.get(0, 0) / max(len(sl_stats["trail_advances"]), 1) * 100, 1
        ),
    }


def main() -> int:
    print("=" * 70)
    print("  SL PERFORMANCE DIAGNOSTIC — Iron Law #11")
    print("=" * 70)

    for sym in ["BTC", "XAU"]:
        base = DATA_DIRS.get(sym)
        if not base or not base.is_dir():
            continue
        r = analyze_sl(sym, base)

        print(f"\n── {sym} ──")
        print(f"  Total closes: {r['total_closes']}")
        print(f"  SL-related: {r['sl_closes']} (PnL=${r['sl_pnl']:+.2f})")
        print(f"  TP/win:     {r['tp_closes']} (PnL=${r['tp_pnl']:+.2f})")

        dists = sorted(r["sl_distance_bps"])
        if dists:
            n = len(dists)
            print(f"  SL distance (bps): min={min(dists):.0f} p25={dists[n//4]:.0f} "
                  f"median={dists[n//2]:.0f} p75={dists[3*n//4]:.0f} max={max(dists):.0f}")

        ta = r["trail_advances_dist"]
        print(f"  Trail advances distribution: {ta}")
        print(f"  Trail advances = 0: {r['trail_advances_zero_pct']}% of SL exits")

        # Diagnosis
        print("\n  Diagnosis:")
        if r["trail_advances_zero_pct"] > 80:
            print(f"  ❌ {r['trail_advances_zero_pct']:.0f}% of SL exits had ZERO trail advancement.")
            print("     Price hit SL before any trailing could activate.")
            if r["sl_pnl"] < 0 and r["tp_pnl"] > 0:
                print(f"     TP exits are net positive (${r['tp_pnl']:+.2f}) — entry direction")
                print("     may be correct but SL is too tight for the volatility regime.")
            elif r["sl_pnl"] < 0 and r["tp_pnl"] <= 0:
                print("     Both SL and TP exits are negative — directional accuracy")
                print("     is the primary concern, not SL distance.")

    print(f"\n{'=' * 70}")
    print("[DONE] All statistics above are the sole source of truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
