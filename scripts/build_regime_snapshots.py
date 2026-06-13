#!/usr/bin/env python
"""R4 Step 4a: Build regime snapshots from Golden Master.

Extracts trend_direction, ADX proxy (trend_strength), ATR, Hurst,
and macro_regime from golden_master.jsonl at 5-minute granularity.
Used by inject_regime_to_labels.py for retroactive label enrichment.

Usage:
    python scripts/build_regime_snapshots.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent


def _build(sym: str, gm_path: Path, fs_path: Path | None, out_path: Path) -> int:
    snapshots = []

    # Source 1: Golden Master (has trend_direction, regime, macro)
    if gm_path.exists():
        with open(gm_path, encoding="utf-8") as f:
            gms = [json.loads(l) for l in f if l.strip()]
        for gm in gms:
            inputs = gm.get("inputs", {})
            snapshots.append({
                "timestamp_utc": gm.get("timestamp_utc", ""),
                "cycle": gm.get("cycle", 0),
                "source": "golden_master",
                "trend_direction": inputs.get("trend_direction", "neutral"),
                "trend_strength": inputs.get("trend_strength", 0.0),
                "detected_regime": inputs.get("detected_regime", inputs.get("regime", "unknown")),
                "macro_regime": inputs.get("macro_regime", "mixed"),
                "hurst": inputs.get("hurst", 0.5),
                "current_atr": inputs.get("current_atr", 0.0),
                "mid_price": inputs.get("mid_price", 0.0),
                "spread": inputs.get("spread", 0.0),
                "atr_percentile": inputs.get("atr_percentile", 0.5),
            })
        print(f"  GM: {len(gms)} cycles")

    # Source 2: Feature Store (has Hurst, ATR, RSI, MACD — derive trend from RSI/MACD)
    if fs_path and fs_path.exists():
        with open(fs_path, encoding="utf-8") as f:
            fs_entries = [json.loads(l) for l in f if l.strip()]
        gm_timestamps = {s["timestamp_utc"][:19] for s in snapshots}
        fs_added = 0
        for entry in fs_entries:
            ts = entry.get("event_time", "")
            if ts[:19] in gm_timestamps:
                continue  # already covered by GM
            values = entry.get("values", {})
            # Derive regime from RSI (extreme values → trending, middle → ranging)
            rsi = float(values.get("M5_RSI_14", 50) or 50)
            macd = float(values.get("M5_MACD", 0) or 0)
            atr = float(values.get("M5_ATR_14", 0) or 0)
            hurst = float(values.get("M5_Hurst", 0.5) or 0.5)
            if rsi > 55 and macd > 0:
                trend = "long"
            elif rsi < 45 and macd < 0:
                trend = "short"
            else:
                trend = "neutral"
            snapshots.append({
                "timestamp_utc": ts,
                "source": "feature_store",
                "trend_direction": trend,
                "trend_strength": abs(rsi - 50) / 50.0,
                "detected_regime": "normal",
                "macro_regime": "mixed",
                "hurst": hurst,
                "current_atr": atr,
                "mid_price": 0.0,
                "spread": 0.0,
                "atr_percentile": 0.5,
            })
            fs_added += 1
        print(f"  FS: {fs_added} additional snapshots (total {len(snapshots)})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for snap in snapshots:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    # Summary
    from collections import Counter
    trends = Counter(s["trend_direction"] for s in snapshots)
    regimes = Counter(s["detected_regime"] for s in snapshots)

    print(f"  {sym}: {len(snapshots)} snapshots → {out_path}")
    print(f"    Trend: {dict(trends)}")
    print(f"    Regime: {dict(regimes)}")
    return len(snapshots)


def main() -> int:
    print("=" * 60)
    print("  R4 Step 4a: Regime Snapshot Builder")
    print("=" * 60)

    total = 0
    total += _build(
        "XAU",
        ROOT / "data" / "golden_master.jsonl",
        ROOT / "data" / "feature_store" / "records" / "symbol=XAUUSDc" / "timeframe=M5" / "features.jsonl",
        ROOT / "data" / "regime_snapshots.jsonl",
    )
    total += _build(
        "BTC",
        ROOT / "data_btc" / "golden_master.jsonl",
        ROOT / "data_btc" / "feature_store" / "records" / "symbol=BTCUSDc" / "timeframe=M5" / "features.jsonl",
        ROOT / "data_btc" / "regime_snapshots.jsonl",
    )

    print(f"\n[DONE] {total} total snapshots written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
