"""
forensic_feature_analysis.py — Phase 1.1: 特征分布取证

分析 golden_master.jsonl inputs 中可能驱动方向偏置的关键字段:
1. trend_direction 分布 (按月)
2. macro_regime 分布
3. detected_regime 分布
4. feature_vector_head8 统计特征
5. 各 regime 下的 direction 产出率
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data_btc")


def load_jsonl(path: Path):
    if not path.exists():
        print(f"ERROR: {path} not found")
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    gm_path = DATA_DIR / "golden_master.jsonl"
    entries = load_jsonl(gm_path)
    if not entries:
        return

    print("=" * 72)
    print("1. trend_direction 分布 (按月)")
    print("=" * 72)
    trend_by_month: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for e in entries:
        ts = e.get("timestamp_utc", "")
        if not ts:
            continue
        month = ts[:7]
        td = e.get("inputs", {}).get("trend_direction", "?")
        trend_by_month[month][td] += 1

    for month in sorted(trend_by_month):
        counts = trend_by_month[month]
        total = sum(counts.values())
        print(f"  {month} (n={total}):")
        for td in sorted(counts):
            c = counts[td]
            print(f"    {td}: {c} ({c/total*100:.1f}%)")

    print()
    print("=" * 72)
    print("2. macro_regime 分布 (按月)")
    print("=" * 72)
    macro_by_month: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for e in entries:
        ts = e.get("timestamp_utc", "")
        if not ts:
            continue
        month = ts[:7]
        mr = e.get("inputs", {}).get("macro_regime", "?")
        macro_by_month[month][mr] += 1

    for month in sorted(macro_by_month):
        counts = macro_by_month[month]
        total = sum(counts.values())
        print(f"  {month} (n={total}):")
        for mr in sorted(counts):
            c = counts[mr]
            print(f"    {mr}: {c} ({c/total*100:.1f}%)")

    print()
    print("=" * 72)
    print("3. trend_direction × direction 交叉表 (btc_swing)")
    print("=" * 72)
    cross: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for e in entries:
        td = e.get("inputs", {}).get("trend_direction", "?")
        direction = e.get("outputs", {}).get("btc_swing", {}).get("direction", "?")
        cross[td][direction] += 1

    for td in sorted(cross):
        counts = cross[td]
        total = sum(counts.values())
        print(f"  trend_direction={td} (n={total}):")
        for d in sorted(counts):
            c = counts[d]
            print(f"    btc_swing direction={d}: {c} ({c/total*100:.1f}%)")

    print()
    print("=" * 72)
    print("4. trend_direction × direction 交叉表 (btc_swing_h1)")
    print("=" * 72)
    cross_h1: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for e in entries:
        td = e.get("inputs", {}).get("trend_direction", "?")
        outputs = e.get("outputs", {})
        if "btc_swing_h1" in outputs:
            direction = outputs["btc_swing_h1"].get("direction", "?")
            cross_h1[td][direction] += 1

    for td in sorted(cross_h1):
        counts = cross_h1[td]
        total = sum(counts.values())
        print(f"  trend_direction={td} (n={total}):")
        for d in sorted(counts):
            c = counts[d]
            print(f"    btc_swing_h1 direction={d}: {c} ({c/total*100:.1f}%)")

    print()
    print("=" * 72)
    print("5. detected_regime 分布 × direction")
    print("=" * 72)
    regime_cross: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for e in entries:
        dr = e.get("inputs", {}).get("detected_regime", "?")
        direction = e.get("outputs", {}).get("btc_swing", {}).get("direction", "?")
        regime_cross[dr][direction] += 1

    for dr in sorted(regime_cross):
        counts = regime_cross[dr]
        total = sum(counts.values())
        print(f"  detected_regime={dr} (n={total}):")
        for d in sorted(counts):
            c = counts[d]
            print(f"    btc_swing={d}: {c} ({c/total*100:.1f}%)")

    print()
    print("=" * 72)
    print("6. feature_vector_head8 统计 (btc_swing direction 分组)")
    print("=" * 72)
    # Head8: [ret_1, trend_dir_encoded, atr, vol_ratio, rsi, macd, bb_pos, obv_ratio]
    head8_names = [
        "ret_1",
        "trend_dir_enc",
        "atr",
        "vol_ratio",
        "rsi",
        "macd",
        "bb_pos",
        "obv_ratio",
    ]
    long_vals: defaultdict[int, list[float]] = defaultdict(list)
    short_vals: defaultdict[int, list[float]] = defaultdict(list)
    neutral_vals: defaultdict[int, list[float]] = defaultdict(list)

    for e in entries:
        fv = e.get("inputs", {}).get("feature_vector_head8", [])
        if not fv or len(fv) < 8:
            continue
        direction = e.get("outputs", {}).get("btc_swing", {}).get("direction", "?")
        target = None
        if direction == "long":
            target = long_vals
        elif direction == "short":
            target = short_vals
        else:
            target = neutral_vals
        for i in range(8):
            target[i].append(fv[i])

    for label, vals in [("long", long_vals), ("short", short_vals), ("neutral", neutral_vals)]:
        if not vals[0]:
            print(f"  {label}: no samples")
            continue
        print(f"  {label} (n={len(vals[0])}):")
        for i, name in enumerate(head8_names):
            vs = vals[i]
            avg = sum(vs) / len(vs)
            mn, mx = min(vs), max(vs)
            print(f"    {name}: avg={avg:.4f}, min={mn:.4f}, max={mx:.4f}")

    print()
    print("=" * 72)
    print("7. 关键字段在 should_trade=True 时的 direction 分布")
    print("=" * 72)
    trade_long = 0
    trade_short = 0
    no_trade_long = 0
    no_trade_short = 0

    for e in entries:
        for sk in ["btc_swing", "btc_swing_h1"]:
            sv = e.get("outputs", {}).get(sk, {})
            direction = sv.get("direction", "?")
            should_trade = sv.get("should_trade", False)
            if should_trade:
                if direction == "long":
                    trade_long += 1
                elif direction == "short":
                    trade_short += 1
            else:
                if direction == "long":
                    no_trade_long += 1
                elif direction == "short":
                    no_trade_short += 1

    print(f"  should_trade=True  → long: {trade_long}, short: {trade_short}")
    print(f"  should_trade=False → long: {no_trade_long}, short: {no_trade_short}")
    if trade_long + trade_short > 0:
        print(f"  ** 实际开仓中 long 占比: {trade_long/(trade_long+trade_short)*100:.1f}% **")
    print()

    print("=" * 72)
    print("8. V4 方向偏转时间点精确定位 (2026-06 每日)")
    print("=" * 72)
    daily_dir: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for e in entries:
        ts = e.get("timestamp_utc", "")
        if not ts or not ts.startswith("2026-06"):
            continue
        day = ts[:10]
        direction = e.get("outputs", {}).get("btc_swing", {}).get("direction", "?")
        daily_dir[day][direction] += 1

    for day in sorted(daily_dir):
        counts = daily_dir[day]
        total = sum(counts.values())
        long_c = counts.get("long", 0)
        short_c = counts.get("short", 0)
        print(
            f"  {day}: long={long_c}, short={short_c}, other={total-long_c-short_c}  (total={total})"
        )

    print()

    print("=" * 72)
    print("9. V4 direction 偏转日 (6/11) 前后对比")
    print("=" * 72)
    before_611 = {"long": 0, "short": 0}
    after_611 = {"long": 0, "short": 0}
    for e in entries:
        ts = e.get("timestamp_utc", "")
        direction = e.get("outputs", {}).get("btc_swing", {}).get("direction", "?")
        if not ts:
            continue
        if ts < "2026-06-11":
            if direction in before_611:
                before_611[direction] += 1
        elif ts >= "2026-06-12":
            if direction in after_611:
                after_611[direction] += 1

    b_total = sum(before_611.values())
    a_total = sum(after_611.values())
    print(f"  Before 6/11 (n={b_total}): long={before_611['long']}, short={before_611['short']}")
    print(f"  After  6/12 (n={a_total}): long={after_611['long']}, short={after_611['short']}")
    if b_total > 0:
        print(f"  Before short%: {before_611['short']/b_total*100:.1f}%")
    if a_total > 0:
        print(f"  After  short%: {after_611['short']/a_total*100:.1f}%")
    print()

    # Check if there's any data on 6/11 itself
    on_611 = {"long": 0, "short": 0}
    for e in entries:
        ts = e.get("timestamp_utc", "")
        if ts and ts.startswith("2026-06-11"):
            direction = e.get("outputs", {}).get("btc_swing", {}).get("direction", "?")
            if direction in on_611:
                on_611[direction] += 1
    print(f"  On 6/11 itself: long={on_611['long']}, short={on_611['short']}")

    print()
    print("=" * 72)
    print("10. btc_swing_h1 首次出现日期 + direction 全历史")
    print("=" * 72)
    h1_first = None
    h1_daily: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for e in entries:
        ts = e.get("timestamp_utc", "")
        outputs = e.get("outputs", {})
        if "btc_swing_h1" not in outputs:
            continue
        if h1_first is None:
            h1_first = ts
        if ts:
            day = ts[:10]
            direction = outputs["btc_swing_h1"].get("direction", "?")
            h1_daily[day][direction] += 1

    print(f"  btc_swing_h1 first appeared: {h1_first}")
    for day in sorted(h1_daily):
        counts = h1_daily[day]
        total = sum(counts.values())
        parts = [f"{d}={c}" for d, c in sorted(counts.items())]
        print(f"  {day}: {', '.join(parts)} (n={total})")


if __name__ == "__main__":
    main()
