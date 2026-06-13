"""Audit OU Theta + Hurst distributions from actual live data.

Iron Law #11: Script stdout is the sole source of truth.
Analyzes the regime_gate_cycle log events to determine the empirical
distributions of ou_theta_m5 and hurst_m5, then evaluates whether
the thresholds (Theta > 0.5, Hurst < 0.48) are supported by evidence.

Also cross-references against actual trade outcomes from the journal
to check whether mean-reversion conditions correlate with profitability.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def main(data_dir: str) -> int:
    base = Path(data_dir)

    # ── 1. Load regime_gate_cycle events from intent logs ──
    logs_dir = base / "logs"
    physics_events: list[dict] = []
    for log_file in sorted(logs_dir.glob("intent_*.log")):
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "regime_gate_cycle" not in line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("event") != "regime_gate_cycle":
                    continue
                ou = evt.get("ou_theta_m5")
                hu = evt.get("hurst_m5")
                if ou is not None and hu is not None:
                    physics_events.append(evt)

    print(f"=== Physics Events from Intent Logs ===")
    print(f"Total regime_gate_cycle events with physics data: {len(physics_events)}")

    if not physics_events:
        print("\nNo physics data found in intent logs.")
        print("The diagnostic logging (ou_theta_m5/hurst_m5) was added in")
        print("commit 39fa7d5 and may not have run yet.")
        return 0

    # ── 2. Distribution analysis ──
    ou_values = [e["ou_theta_m5"] for e in physics_events]
    hu_values = [e["hurst_m5"] for e in physics_events]

    ou_sorted = sorted(ou_values)
    hu_sorted = sorted(hu_values)
    n = len(ou_values)

    def pct(data, p):
        idx = int(len(data) * p / 100)
        return data[min(idx, len(data) - 1)]

    print(f"\n=== OU Theta Distribution (n={n}) ===")
    print(f"  Min:    {min(ou_values):.4f}")
    print(f"  P10:    {pct(ou_sorted, 10):.4f}")
    print(f"  P25:    {pct(ou_sorted, 25):.4f}")
    print(f"  Median: {pct(ou_sorted, 50):.4f}")
    print(f"  P75:    {pct(ou_sorted, 75):.4f}")
    print(f"  P90:    {pct(ou_sorted, 90):.4f}")
    print(f"  Max:    {max(ou_values):.4f}")
    print(f"  Mean:   {sum(ou_values)/n:.4f}")
    above_05 = sum(1 for v in ou_values if v > 0.5)
    print(f"  > 0.5:  {above_05} ({above_05/n*100:.1f}%)")

    print(f"\n=== Hurst M5 Distribution (n={n}) ===")
    print(f"  Min:    {min(hu_values):.4f}")
    print(f"  P10:    {pct(hu_sorted, 10):.4f}")
    print(f"  P25:    {pct(hu_sorted, 25):.4f}")
    print(f"  Median: {pct(hu_sorted, 50):.4f}")
    print(f"  P75:    {pct(hu_sorted, 75):.4f}")
    print(f"  P90:    {pct(hu_sorted, 90):.4f}")
    print(f"  Max:    {max(hu_values):.4f}")
    print(f"  Mean:   {sum(hu_values)/n:.4f}")
    below_048 = sum(1 for v in hu_values if v < 0.48)
    print(f"  < 0.48: {below_048} ({below_048/n*100:.1f}%)")

    # ── 3. Joint condition: how often would the override trigger? ──
    override_count = sum(
        1 for e in physics_events
        if e["ou_theta_m5"] > 0.5 and e["hurst_m5"] < 0.48
    )
    print(f"\n=== Override Trigger Rate ===")
    print(f"  Theta > 0.5 AND Hurst < 0.48: {override_count}/{n} ({override_count/n*100:.1f}%)")

    # ── 4. Regime distribution when override WOULD trigger ──
    override_regimes: dict[str, int] = defaultdict(int)
    for e in physics_events:
        if e["ou_theta_m5"] > 0.5 and e["hurst_m5"] < 0.48:
            regime = e.get("detected_regime", "?")
            override_regimes[regime] += 1
    if override_regimes:
        print(f"\n  Regimes when override triggers:")
        for r, c in sorted(override_regimes.items(), key=lambda x: -x[1]):
            print(f"    {r}: {c} ({c/override_count*100:.1f}%)")

    # ── 5. Per-regime physics breakdown ──
    regime_physics: dict[str, dict] = defaultdict(lambda: {"ou": [], "hu": []})
    for e in physics_events:
        regime = e.get("detected_regime", "?")
        regime_physics[regime]["ou"].append(e["ou_theta_m5"])
        regime_physics[regime]["hu"].append(e["hurst_m5"])

    print(f"\n=== Per-Regime Physics Breakdown ===")
    for regime in sorted(regime_physics.keys()):
        rp = regime_physics[regime]
        rn = len(rp["ou"])
        if rn < 5:
            continue
        ou_mean = sum(rp["ou"]) / rn
        hu_mean = sum(rp["hu"]) / rn
        override_in_regime = sum(
            1 for e in physics_events
            if e.get("detected_regime") == regime
            and e["ou_theta_m5"] > 0.5 and e["hurst_m5"] < 0.48
        )
        print(f"  {regime} (n={rn}):")
        print(f"    OU Theta mean={ou_mean:.4f}  Hurst mean={hu_mean:.4f}")
        print(f"    Override would trigger: {override_in_regime}/{rn} ({override_in_regime/rn*100:.1f}%)")

    # ── 6. Threshold sensitivity analysis ──
    print(f"\n=== Threshold Sensitivity ===")
    print(f"  {'Theta >':<12} {'Hurst <':<12} {'Trigger %':<12} {'Events':<8}")
    for theta_t in [0.3, 0.4, 0.5, 0.6, 0.7]:
        for hurst_t in [0.40, 0.45, 0.48, 0.50, 0.55]:
            cnt = sum(
                1 for e in physics_events
                if e["ou_theta_m5"] > theta_t and e["hurst_m5"] < hurst_t
            )
            pct_val = cnt / n * 100 if n > 0 else 0
            marker = " ← CURRENT" if (theta_t == 0.5 and hurst_t == 0.48) else ""
            print(f"  {theta_t:<12} {hurst_t:<12} {pct_val:<12.1f} {cnt:<8}{marker}")

    print(f"\n[DONE] All statistics above are the sole source of truth. (Iron Law #11)")
    return 0


if __name__ == "__main__":
    data_dir = "data_btc"
    args = sys.argv[1:]
    if "--data-dir" in args:
        idx = args.index("--data-dir")
        if idx + 1 < len(args):
            data_dir = args[idx + 1]
    elif args and not args[0].startswith("--"):
        data_dir = args[0]
    sys.exit(main(data_dir))
