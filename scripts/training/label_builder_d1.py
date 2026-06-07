"""D1 Barrier Label Builder — generate single directional labels for daily swing models.

Generates ONE label per entry bar (not dual long/short), eliminating the label-collision
problem where the same feature vector received contradictory labels.

Algorithm: walk forward through D1 OHLC, compute SL/TP barriers for both long and short
at each entry, then determine which of the 4 barriers is hit FIRST.  The first-hit
barrier determines the directional label:

    Long TP  hit first → label =  1 (bullish, PnL = +tp_atr_mult)
    Short SL hit first → label =  1 (bullish, PnL = +sl_atr_mult)
    Short TP hit first → label = -1 (bearish, PnL = +tp_atr_mult)
    Long SL  hit first → label = -1 (bearish, PnL = +sl_atr_mult)
    Timeout (none hit within horizon) → label = 0

Intra-bar collision (both SL and TP of the same side hit on the same D1 bar) is resolved
by consulting H4 data to determine which level was breached first.  Without H4 data, the
bar's OHLC direction (close vs open) serves as a heuristic.

Usage:
    python scripts/training/label_builder_d1.py \\
        --csv data/raw/xauusdc_d1_merged.csv \\
        --contract d1_swing_10d \\
        --output data/labels/d1_swing_10d.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# ── Contract definitions ──


@dataclass(frozen=True)
class D1BarrierContract:
    contract_id: str
    horizon_bars: int
    sl_atr_mult: float
    tp_atr_mult: float
    atr_period: int = 14
    min_atr: float = 0.5

    @property
    def rr_ratio(self) -> float:
        return self.tp_atr_mult / self.sl_atr_mult


D1_CONTRACTS: dict[str, D1BarrierContract] = {
    "d1_swing_5d": D1BarrierContract(
        contract_id="d1_swing_5d",
        horizon_bars=5,
        sl_atr_mult=2.0,
        tp_atr_mult=3.5,
    ),
    "d1_swing_10d": D1BarrierContract(
        contract_id="d1_swing_10d",
        horizon_bars=10,
        sl_atr_mult=2.0,
        tp_atr_mult=4.0,
    ),
    "d1_swing_20d": D1BarrierContract(
        contract_id="d1_swing_20d",
        horizon_bars=20,
        sl_atr_mult=2.5,
        tp_atr_mult=5.0,
    ),
}

# Multi-TF barrier contracts — spectrum from M15 (intraday) to D1 (weekly)
MULTI_TF_CONTRACTS: dict[str, D1BarrierContract] = {
    # ── M15 intraday (~6 hour hold, 24 bars) ──
    "m15_swing_24bar": D1BarrierContract(
        contract_id="m15_swing_24bar",
        horizon_bars=24,
        sl_atr_mult=1.5,
        tp_atr_mult=3.0,
        atr_period=14,
        min_atr=0.3,
    ),
    # ── M30 intraday (~6 hour hold, 12 bars) ──
    "m30_swing_12bar": D1BarrierContract(
        contract_id="m30_swing_12bar",
        horizon_bars=12,
        sl_atr_mult=1.5,
        tp_atr_mult=3.0,
        atr_period=14,
        min_atr=0.3,
    ),
    # ── H1 daily swing (~1 day hold, 24 bars) ──
    "h1_swing_24bar": D1BarrierContract(
        contract_id="h1_swing_24bar",
        horizon_bars=24,
        sl_atr_mult=2.0,
        tp_atr_mult=3.5,
        atr_period=14,
        min_atr=0.4,
    ),
    # ── H4 multi-day swing (~3 day hold, 18 bars) ──
    "h4_swing_18bar": D1BarrierContract(
        contract_id="h4_swing_18bar",
        horizon_bars=18,
        sl_atr_mult=2.0,
        tp_atr_mult=4.0,
        atr_period=14,
        min_atr=0.5,
    ),
}

# Unified contract registry
ALL_CONTRACTS = {**D1_CONTRACTS, **MULTI_TF_CONTRACTS}


# ── Data loading ──


def _load_d1_csv(
    csv_path: Path,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Load D1 OHLC CSV.  Returns (opens, highs, lows, closes, timestamps)."""
    with open(csv_path, encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {csv_path}")

        col_map: dict[str, str] = {}
        for key in reader.fieldnames:
            kl = key.strip().lower()
            if kl in ("time", "datetime", "date", "timestamp"):
                col_map["time"] = key
            elif kl in ("open", "o"):
                col_map["open"] = key
            elif kl in ("high", "h"):
                col_map["high"] = key
            elif kl in ("low", "l"):
                col_map["low"] = key
            elif kl in ("close", "c"):
                col_map["close"] = key

        missing = {"time", "open", "high", "low", "close"} - set(col_map)
        if missing:
            raise ValueError(f"CSV missing columns: {missing}. Found: {list(reader.fieldnames)}")

        rows: list[dict[str, float]] = []
        timestamps: list[str] = []
        for row in reader:
            try:
                timestamps.append(str(row[col_map["time"]]))
                rows.append(
                    {
                        "open": float(row[col_map["open"]]),
                        "high": float(row[col_map["high"]]),
                        "low": float(row[col_map["low"]]),
                        "close": float(row[col_map["close"]]),
                    }
                )
            except (ValueError, KeyError):
                continue

    opens = np.array([r["open"] for r in rows], dtype=np.float64)
    highs = np.array([r["high"] for r in rows], dtype=np.float64)
    lows = np.array([r["low"] for r in rows], dtype=np.float64)
    closes = np.array([r["close"] for r in rows], dtype=np.float64)
    return opens, highs, lows, closes, timestamps


def _load_h4_csv(
    csv_path: Path | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]] | None:
    """Load H4 OHLC CSV for intra-bar collision resolution.  Returns None if unavailable."""
    if csv_path is None or not csv_path.exists():
        return None
    try:
        return _load_d1_csv(csv_path)  # same OHLC format
    except Exception:  # noqa: BLE001
        return None


# ── ATR computation ──


def _compute_atr(h: np.ndarray, low: np.ndarray, c: np.ndarray, period: int = 14) -> float:
    """ATR at the most recent bar, using `period` bars of history."""
    if len(c) < period + 1:
        return 0.0
    prev_c = c[-(period + 1) : -1]
    cur_h = h[-period:]
    cur_l = low[-period:]
    tr = np.maximum(
        cur_h - cur_l,
        np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)),
    )
    return float(np.mean(tr))


# ── Intra-bar collision resolution ──


def _resolve_intra_bar_first(
    d1_bar_high: float,
    d1_bar_low: float,
    d1_bar_open: float,
    d1_bar_close: float,
    level_a: float,
    level_b: float,
    h4_bars_for_this_day: np.ndarray | list[tuple[float, float]] | None = None,
) -> str:
    """Determine which of two barrier levels was hit first within a D1 bar.

    `level_a` and `level_b` are the two barrier prices (e.g. SL and TP).
    Returns "a_first", "b_first", or "both" (simultaneous / cannot disambiguate).

    With H4 data: checks which level was breached first by scanning 6 H4 bars.
    Without H4 data: uses the D1 bar's direction as a heuristic:
        - close > open (up bar) → level above entry was hit first ("a_first" if a > b)
        - close < open (down bar) → level below entry was hit first
    """
    if h4_bars_for_this_day is not None and len(h4_bars_for_this_day) > 0:
        # Scan H4 bars in chronological order to find first breach
        for h4_high, h4_low in h4_bars_for_this_day:
            a_hit = h4_low <= level_a <= h4_high
            b_hit = h4_low <= level_b <= h4_high
            if a_hit and not b_hit:
                return "a_first"
            if b_hit and not a_hit:
                return "b_first"
            if a_hit and b_hit:
                # Both hit within the same H4 bar — check which extreme is closer
                dist_a = min(abs(h4_high - level_a), abs(h4_low - level_a))
                dist_b = min(abs(h4_high - level_b), abs(h4_low - level_b))
                return "a_first" if dist_a < dist_b else "b_first"

    # Fallback: use D1 bar direction
    up_bar = d1_bar_close >= d1_bar_open
    level_a_higher = level_a > level_b

    if up_bar and level_a_higher:
        return "a_first"  # up bar hits higher level first
    if up_bar and not level_a_higher:
        return "b_first"  # up bar hits higher level (b) first
    if not up_bar and level_a_higher:
        return "b_first"  # down bar hits lower level (b) first
    if not up_bar and not level_a_higher:
        return "a_first"  # down bar hits lower level (a) first

    return "both"


# ── Core label building ──


def build_barrier_labels(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    opens: np.ndarray,
    timestamps: list[str],
    contract: D1BarrierContract,
    *,
    h4_highs: np.ndarray | None = None,
    h4_lows: np.ndarray | None = None,
    h4_timestamps: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Generate ONE directional label per entry bar via first-barrier-hit.

    Walk forward through D1 price history.  At each bar, compute barriers for
    both long and short.  Determine which of the 4 barriers is hit FIRST.
    Output a single label: 1 (long), -1 (short), or 0 (neutral/timeout).
    """
    n = len(closes)
    horizon = contract.horizon_bars
    labels: list[dict[str, Any]] = []

    max_entry = n - horizon - 1
    if max_entry <= contract.atr_period:
        return labels

    # Pre-index H4 bars by D1 date for intra-bar collision resolution
    h4_by_date: dict[str, list[tuple[float, float]]] = {}
    if h4_highs is not None and h4_lows is not None and h4_timestamps is not None:
        for i, h4_ts in enumerate(h4_timestamps):
            date_key = _parse_date_for_h4(h4_ts)
            if date_key not in h4_by_date:
                h4_by_date[date_key] = []
            h4_by_date[date_key].append((float(h4_highs[i]), float(h4_lows[i])))

    for entry_idx in range(contract.atr_period, max_entry):
        entry_price = float(closes[entry_idx])
        entry_time = timestamps[entry_idx]

        atr_val = _compute_atr(
            highs[: entry_idx + 1],
            lows[: entry_idx + 1],
            closes[: entry_idx + 1],
            period=contract.atr_period,
        )

        if atr_val < contract.min_atr:
            continue

        sl_distance = contract.sl_atr_mult * atr_val
        tp_distance = contract.tp_atr_mult * atr_val

        # Barrier levels
        long_sl = entry_price - sl_distance
        long_tp = entry_price + tp_distance
        short_sl = entry_price + sl_distance
        short_tp = entry_price - tp_distance

        # Track which barrier was hit first (overall, among all 4)
        first_hit: str | None = None  # "long_tp", "long_sl", "short_tp", "short_sl"
        first_hit_bar: int | None = None
        first_hit_price: float | None = None

        for fwd in range(1, horizon + 1):
            idx = entry_idx + fwd
            if idx >= n:
                break
            bar_high = float(highs[idx])
            bar_low = float(lows[idx])
            bar_open = float(opens[idx])
            bar_close = float(closes[idx])

            # Get H4 bars for this D1 bar's date
            date_key = _parse_date_for_h4(timestamps[idx])
            h4_intraday = h4_by_date.get(date_key)

            # Check which barriers are hit on this D1 bar
            long_tp_hit = bar_high >= long_tp
            long_sl_hit = bar_low <= long_sl
            short_tp_hit = bar_low <= short_tp
            short_sl_hit = bar_high >= short_sl

            # Collect candidates hit on this bar
            candidates: list[tuple[str, float]] = []

            # Long side: TP and SL
            if long_tp_hit and long_sl_hit:
                # Both hit on same bar — resolve via H4 or heuristic
                resolution = _resolve_intra_bar_first(
                    bar_high,
                    bar_low,
                    bar_open,
                    bar_close,
                    long_tp,
                    long_sl,
                    h4_intraday,
                )
                if resolution == "a_first":
                    candidates.append(("long_tp", long_tp))
                else:
                    candidates.append(("long_sl", long_sl))
            elif long_tp_hit:
                candidates.append(("long_tp", long_tp))
            elif long_sl_hit:
                candidates.append(("long_sl", long_sl))

            # Short side: TP and SL
            if short_tp_hit and short_sl_hit:
                resolution = _resolve_intra_bar_first(
                    bar_high,
                    bar_low,
                    bar_open,
                    bar_close,
                    short_sl,
                    short_tp,
                    h4_intraday,
                )
                if resolution == "a_first":
                    candidates.append(("short_sl", short_sl))
                else:
                    candidates.append(("short_tp", short_tp))
            elif short_tp_hit:
                candidates.append(("short_tp", short_tp))
            elif short_sl_hit:
                candidates.append(("short_sl", short_sl))

            # If multiple barriers hit on this bar, the one closest to entry_price
            # was likely hit first (within-bar proximity heuristic)
            if len(candidates) > 1:
                candidates.sort(key=lambda x: abs(x[1] - entry_price))

            if candidates:
                first_hit = candidates[0][0]
                first_hit_bar = fwd
                first_hit_price = candidates[0][1]
                break

        # Default to timeout
        if first_hit is None:
            first_hit = "timeout"

        # Determine label and P&L from first hit
        if first_hit == "long_tp":
            label_int = 1
            pnl_r = contract.tp_atr_mult
        elif first_hit == "short_sl":
            label_int = 1
            pnl_r = contract.sl_atr_mult  # price went up by at least SL distance
        elif first_hit == "short_tp":
            label_int = -1
            pnl_r = contract.tp_atr_mult
        elif first_hit == "long_sl":
            label_int = -1
            pnl_r = contract.sl_atr_mult  # price went down by at least SL distance
        else:  # timeout
            label_int = 0
            pnl_r = 0.0

        labels.append(
            {
                "schema_version": "training_label.v1",
                "contract_id": contract.contract_id,
                "symbol": "XAUUSDc",
                "entry_time": entry_time,
                "entry_idx": entry_idx,
                "entry_price": round(entry_price, 3),
                "side": "long" if label_int == 1 else ("short" if label_int == -1 else "neutral"),
                "sl_price_long": round(long_sl, 3),
                "tp_price_long": round(long_tp, 3),
                "sl_price_short": round(short_sl, 3),
                "tp_price_short": round(short_tp, 3),
                "atr_at_entry": round(atr_val, 2),
                "horizon_bars": horizon,
                "label": str(label_int),
                "label_int": label_int,
                "label_name": first_hit,
                "hit_bar_index": first_hit_bar,
                "hit_price": round(first_hit_price, 3) if first_hit_price is not None else None,
                "pnl_r": round(pnl_r, 4),
                "sl_atr_mult": contract.sl_atr_mult,
                "tp_atr_mult": contract.tp_atr_mult,
            }
        )

    return labels


def _parse_date_for_h4(ts: str) -> str:
    """Normalize timestamp to YYYY-MM-DD for H4→D1 date bucketing."""
    ts_stripped = ts.strip()
    if len(ts_stripped) >= 10:
        if ts_stripped[4] == "-":
            return ts_stripped[:10]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y.%m.%d"):
            try:
                return (
                    __import__("datetime")
                    .datetime.strptime(
                        ts_stripped[:19] if len(ts_stripped) >= 19 else ts_stripped, fmt
                    )
                    .strftime("%Y-%m-%d")
                )
            except ValueError:
                continue
        return ts_stripped[:10]
    return ts_stripped


# ── Statistics ──


def print_label_stats(labels: list[dict[str, Any]], contract_name: str) -> None:
    """Print single-direction label distribution summary."""
    total = len(labels)
    long_count = sum(1 for lbl in labels if lbl["label_int"] == 1)
    short_count = sum(1 for lbl in labels if lbl["label_int"] == -1)
    neutral_count = sum(1 for lbl in labels if lbl["label_int"] == 0)

    # Breakdown by hit type
    hit_counts: dict[str, int] = {}
    for lbl in labels:
        hit_counts[lbl["label_name"]] = hit_counts.get(lbl["label_name"], 0) + 1

    avg_atr = np.mean([lbl["atr_at_entry"] for lbl in labels]) if labels else 0.0

    pnl_values = [lbl["pnl_r"] for lbl in labels]
    print(
        f"[{contract_name}] total={total:5d}  "
        f"long={long_count:4d} ({long_count / max(total, 1) * 100:5.1f}%)  "
        f"short={short_count:4d} ({short_count / max(total, 1) * 100:5.1f}%)  "
        f"neutral={neutral_count:4d} ({neutral_count / max(total, 1) * 100:5.1f}%)  "
        f"avg_atr={avg_atr:.2f}"
    )
    print(f"[{contract_name}] Hit breakdown: {json.dumps(hit_counts)}")
    print(
        f"[{contract_name}] PnL stats: mean={np.mean(pnl_values):.3f}R  "
        f"std={np.std(pnl_values):.3f}R  "
        f"max={np.max(pnl_values):.3f}R  min={np.min(pnl_values):.3f}R"
    )


# ── CLI ──


def main():
    parser = argparse.ArgumentParser(prog="label_builder_d1")
    parser.add_argument(
        "--csv",
        type=Path,
        required=True,
        help="Path to D1 OHLC CSV (e.g. data/raw/xauusdc_d1_merged.csv)",
    )
    parser.add_argument(
        "--contract", type=str, default="d1_swing_5d", help="Contract name (see --list-contracts)"
    )
    parser.add_argument("--output", type=Path, required=True, help="Output JSONL file path")
    parser.add_argument(
        "--h4-csv",
        type=Path,
        default=None,
        help="Path to H4 OHLC CSV for intra-bar collision resolution",
    )
    parser.add_argument(
        "--list-contracts", action="store_true", help="List available contracts and exit"
    )
    args = parser.parse_args()

    if args.list_contracts:
        print("Available barrier contracts:")
        for name, c in ALL_CONTRACTS.items():
            print(
                f"  {name:25s}  horizon={c.horizon_bars:3d} bars  "
                f"SL={c.sl_atr_mult:.1f}×ATR  TP={c.tp_atr_mult:.1f}×ATR  "
                f"RR={c.rr_ratio:.2f}:1"
            )
        return 0

    if args.contract not in ALL_CONTRACTS:
        print(f"Unknown contract '{args.contract}'. Available: {list(ALL_CONTRACTS)}")
        return 2

    contract = ALL_CONTRACTS[args.contract]
    csv_path: Path = args.csv
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return 2

    print(f"[label_builder_d1] Loading {csv_path} ...")
    opens, highs, lows, closes, timestamps = _load_d1_csv(csv_path)
    print(f"[label_builder_d1] Loaded {len(closes)} bars " f"({timestamps[0]} → {timestamps[-1]})")

    # Optional H4 data
    h4_highs = None
    h4_lows = None
    h4_timestamps = None
    if args.h4_csv:
        h4_data = _load_h4_csv(args.h4_csv)
        if h4_data is not None:
            _, h4_highs, h4_lows, _, h4_timestamps = h4_data
            print(
                f"[label_builder_d1] Loaded {len(h4_timestamps)} H4 bars for collision resolution"
            )

    print(
        f"[label_builder_d1] Building labels for contract '{args.contract}' "
        f"(horizon={contract.horizon_bars}, SL={contract.sl_atr_mult}×ATR, "
        f"TP={contract.tp_atr_mult}×ATR) ..."
    )

    labels = build_barrier_labels(
        highs,
        lows,
        closes,
        opens,
        timestamps,
        contract,
        h4_highs=h4_highs,
        h4_lows=h4_lows,
        h4_timestamps=h4_timestamps,
    )
    print_label_stats(labels, args.contract)

    output_path: Path = args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for lbl in labels:
            f.write(json.dumps(lbl, ensure_ascii=False) + "\n")

    print(f"[label_builder_d1] Wrote {len(labels)} labels to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
