"""Paper trade simulator — generate labeled trade outcomes from shadow decisions.

Reads shadow/live decision records, simulates trade execution using historical
OHLC data, and writes a paper_trade_journal.jsonl compatible with the
online_feedback_hook so the OnlineLearnerAdapter can learn from every decision.

Usage:
  python scripts/paper_trade_simulator.py                    # scan all decisions
  python scripts/paper_trade_simulator.py --since 2026-05-01 # recent only
  python scripts/paper_trade_simulator.py --dry-run           # preview without writing
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DECISIONS_DIR = DATA_DIR / "decisions"
FEATURE_FILE = (
    DATA_DIR / "feature_store" / "records" / "symbol=XAUUSDc" / "timeframe=M5" / "features.jsonl"
)
OHLC_FILE = DATA_DIR / "raw" / "xauusdc_m5_1y.csv"
OHLC_RECENT_FILE = DATA_DIR / "raw" / "xauusdc_m5_recent.csv"
PAPER_JOURNAL = DATA_DIR / "paper_trade_journal.jsonl"

# ── Config ──────────────────────────────────────────────────────────────
SL_ATR_MULT = 2.0  # stop-loss as multiple of M5_ATR_14
TP_ATR_MULT = 3.5  # take-profit as multiple of M5_ATR_14
MAX_HOLD_BARS = 288  # 24h in M5 bars — close at market if neither SL/TP hit
COOLDOWN_SECONDS = 300  # 5 min between entries (avoid duplicates)
LOT_SIZE = 0.01  # standard mini lot
LOT_MULTIPLIER = 100.0  # XAUUSD: 1 lot = 100 oz, PnL = (exit-entry) * 100 * lots
MIN_BARS_FORWARD = 6  # need at least this many future bars to simulate


def load_ohlc(path: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """Load OHLC bars, optionally filtered from a start date."""
    bars: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                dt = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                continue
            if since and dt.replace(tzinfo=UTC) < since:
                continue
            bars.append(
                {
                    "time": dt.replace(tzinfo=None),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                }
            )
    return bars


def load_features(path: Path) -> dict[str, float]:
    """Load features keyed by event_time (ISO) → M5_ATR_14 value."""
    features: dict[str, float] = {}
    if not path.exists():
        return features
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = rec.get("event_time", "")
        atr = rec.get("values", {}).get("M5_ATR_14")
        if et and atr is not None:
            features[et[:19]] = float(atr)
    return features


def collect_decisions(since: datetime | None = None) -> list[dict[str, Any]]:
    """Walk decisions/ and return OPEN actions sorted by event_time."""
    decisions: list[dict[str, Any]] = []
    if not DECISIONS_DIR.exists():
        return decisions

    for date_dir in sorted(DECISIONS_DIR.iterdir()):
        if not date_dir.is_dir():
            continue
        for jsonl_file in sorted(date_dir.glob("*.jsonl")):
            for line in jsonl_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                action = str(rec.get("labels", {}).get("decision_action", "")).upper()
                if action != "OPEN":
                    continue

                et_str = rec.get("event_time", "")
                try:
                    et = datetime.fromisoformat(et_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue

                if since and et.replace(tzinfo=UTC) < since:
                    continue

                decisions.append(
                    {
                        "record_id": rec.get("record_id", ""),
                        "event_time": et,
                        "side": str(rec.get("labels", {}).get("decision_side", "FLAT")).upper(),
                        "venue": rec.get("execution", {}).get("venue", "shadow"),
                        "consensus": rec.get("attribution", {}).get("consensus", {}),
                    }
                )
    decisions.sort(key=lambda d: d["event_time"])
    return decisions


def compute_atr_from_bars(bars: list[dict], idx: int, period: int = 14) -> float | None:
    """Compute ATR directly from OHLC bars (price points, not normalized)."""
    if idx < period:
        return None
    tr_sum = 0.0
    for i in range(idx - period + 1, idx + 1):
        high = bars[i]["high"]
        low = bars[i]["low"]
        prev_close = bars[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_sum += tr
    return tr_sum / period


def round_down_to_m5(dt: datetime) -> str:
    """Round a datetime down to the nearest M5 boundary, return ISO prefix."""
    minute = (dt.minute // 5) * 5
    rounded = dt.replace(minute=minute, second=0, microsecond=0)
    return rounded.isoformat()[:19]


def find_bar(bars: list[dict], target: datetime) -> int | None:
    """Binary search for the bar whose time <= target (most recent). Returns index."""
    lo, hi = 0, len(bars) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if bars[mid]["time"] <= target:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def simulate_trade(
    decision: dict,
    bars: list[dict],
    start_idx: int,
    features: dict[str, float],
    sl_mult: float = SL_ATR_MULT,
    tp_mult: float = TP_ATR_MULT,
    max_hold_bars: int = MAX_HOLD_BARS,
) -> dict[str, Any] | None:
    """Simulate a single trade from decision entry through exit.

    Returns a journal entry dict, or None if the trade can't be simulated.
    """
    side = decision["side"]
    if side not in ("LONG", "SHORT"):
        return None

    entry_bar = bars[start_idx]
    entry_price = entry_bar["close"]
    entry_time = entry_bar["time"]

    # Compute ATR directly from OHLC bars (consistent across all time periods)
    atr = compute_atr_from_bars(bars, start_idx)
    if atr is None or atr <= 0:
        # Fallback: try feature store
        atr_key = round_down_to_m5(entry_time)
        atr = features.get(atr_key)
        if atr is None or atr <= 0:
            for offset in range(1, 6):
                dt_offset = entry_time + timedelta(minutes=offset * 5)
                atr = features.get(dt_offset.isoformat()[:19])
                if atr and atr > 0:
                    break
                dt_offset = entry_time - timedelta(minutes=offset * 5)
                atr = features.get(dt_offset.isoformat()[:19])
                if atr and atr > 0:
                    break
    if atr is None or atr <= 0:
        return None

    # Compute SL and TP
    sl_distance = sl_mult * atr
    tp_distance = tp_mult * atr
    if side == "LONG":
        sl = entry_price - sl_distance
        tp = entry_price + tp_distance
    else:
        sl = entry_price + sl_distance
        tp = entry_price - tp_distance

    # Walk forward through bars
    exit_price = None
    exit_time = None
    exit_reason = "timeout"

    end_idx = min(start_idx + max_hold_bars + 1, len(bars))
    for i in range(start_idx + 1, end_idx):
        bar = bars[i]
        if side == "LONG":
            if bar["low"] <= sl:
                exit_price = sl
                exit_time = bar["time"]
                exit_reason = "sl_hit"
                break
            if bar["high"] >= tp:
                exit_price = tp
                exit_time = bar["time"]
                exit_reason = "tp_hit"
                break
        else:  # SHORT
            if bar["high"] >= sl:
                exit_price = sl
                exit_time = bar["time"]
                exit_reason = "sl_hit"
                break
            if bar["low"] <= tp:
                exit_price = tp
                exit_time = bar["time"]
                exit_reason = "tp_hit"
                break

    if exit_price is None:
        # Timeout — close at market
        last_bar = bars[end_idx - 1]
        exit_price = last_bar["close"]
        exit_time = last_bar["time"]
        exit_reason = "timeout"

    # Compute PnL
    if side == "LONG":
        pnl = (exit_price - entry_price) * LOT_MULTIPLIER * LOT_SIZE
    else:
        pnl = (entry_price - exit_price) * LOT_MULTIPLIER * LOT_SIZE
    pnl = round(pnl, 2)

    # Determine label
    if exit_reason == "tp_hit":
        label = "tp_hit_first"
    elif exit_reason == "sl_hit":
        label = "sl_hit_first"
    else:
        label = "breakeven" if abs(pnl) < 0.5 else ("tp_hit_first" if pnl > 0 else "sl_hit_first")

    return {
        "schema_version": "paper_trade_journal.v1",
        "message_id": f"paper_{decision['record_id']}",
        "recorded_at": exit_time.isoformat() if exit_time else entry_time.isoformat(),
        "ack_status": "closed",
        "symbol": "XAUUSDc",
        "side": side.lower(),
        "action": "open",
        "entry_price": round(entry_price, 5),
        "exit_price": round(exit_price, 5),
        "sl": round(sl, 5),
        "tp": round(tp, 5),
        "pnl": pnl,
        "label": label,
        "exit_reason": exit_reason,
        "volume": LOT_SIZE,
        "venue": "paper",
        "hold_bars": (end_idx - start_idx - 1) if exit_reason == "timeout" else None,
        "entry_time": entry_time.isoformat(),
        "close_time": exit_time.isoformat() if exit_time else entry_time.isoformat(),
        "detail": {
            "label": label,
            "pnl": pnl,
            "entry_price": round(entry_price, 5),
            "exit_price": round(exit_price, 5),
            "sl": round(sl, 5),
            "tp": round(tp, 5),
            "atr": round(atr, 5),
        },
        "decision_record_id": decision["record_id"],
        "consensus": decision.get("consensus", {}),
    }


# ── Main ────────────────────────────────────────────────────────────────


def run_simulator(
    since: datetime | None = None,
    dry_run: bool = False,
    output_path: Path | None = None,
    sl_mult: float = SL_ATR_MULT,
    tp_mult: float = TP_ATR_MULT,
    max_hold_bars: int = MAX_HOLD_BARS,
) -> dict[str, Any]:
    """Run paper trade simulation over all decisions. Returns summary."""

    print("=" * 60)
    print("PAPER TRADE SIMULATOR")
    print("=" * 60)

    # 1. Load OHLC
    print("\n[1/4] Loading OHLC data...")
    bars = load_ohlc(OHLC_FILE, since=since)
    if OHLC_RECENT_FILE.exists():
        recent_bars = load_ohlc(OHLC_RECENT_FILE, since=since)
        # Merge and deduplicate by time
        existing_times = {b["time"] for b in bars}
        for b in recent_bars:
            if b["time"] not in existing_times:
                bars.append(b)
        bars.sort(key=lambda b: b["time"])
        print(f"  {len(bars)} bars loaded (incl. {len(recent_bars)} recent)")
    else:
        print(f"  {len(bars)} bars loaded")
    if not bars:
        return {"status": "error", "error": "no OHLC data"}

    # 2. Load features (ATR)
    print("\n[2/4] Loading features for ATR...")
    features = load_features(FEATURE_FILE)
    print(f"  {len(features)} feature timestamps with ATR")

    # 3. Collect decisions
    print("\n[3/4] Collecting OPEN decisions...")
    decisions = collect_decisions(since=since)
    print(f"  {len(decisions)} OPEN decisions found")
    if not decisions:
        return {"status": "ok", "trades": 0, "skipped": 0}

    # 4. Simulate trades
    print("\n[4/4] Simulating trades...")
    trades: list[dict[str, Any]] = []
    skipped_no_bar = 0
    skipped_no_atr = 0
    skipped_cooldown = 0
    last_entry_time: datetime | None = None

    for i, decision in enumerate(decisions):
        et = decision["event_time"]

        # Cooldown check
        if last_entry_time and (et - last_entry_time).total_seconds() < COOLDOWN_SECONDS:
            skipped_cooldown += 1
            continue

        # Find entry bar
        bar_idx = find_bar(bars, et)
        if bar_idx is None:
            skipped_no_bar += 1
            continue

        # Need enough future bars
        if bar_idx + MIN_BARS_FORWARD >= len(bars):
            skipped_no_bar += 1
            continue

        trade = simulate_trade(decision, bars, bar_idx, features, sl_mult, tp_mult, max_hold_bars)
        if trade is None:
            skipped_no_atr += 1
            continue

        trades.append(trade)
        last_entry_time = et

        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(decisions)} decisions processed, {len(trades)} trades simulated")

    print("\n  Results:")
    print(f"    Trades simulated:  {len(trades)}")
    print(f"    Skipped (no bar):  {skipped_no_bar}")
    print(f"    Skipped (no ATR):  {skipped_no_atr}")
    print(f"    Skipped (cooldown): {skipped_cooldown}")

    # Stats
    if trades:
        wins = sum(1 for t in trades if t["label"] == "tp_hit_first")
        losses = sum(1 for t in trades if t["label"] == "sl_hit_first")
        total_pnl = sum(t["pnl"] for t in trades)
        tp_count = sum(1 for t in trades if t["exit_reason"] == "tp_hit")
        sl_count = sum(1 for t in trades if t["exit_reason"] == "sl_hit")
        timeout_count = sum(1 for t in trades if t["exit_reason"] == "timeout")
        avg_hold = (
            sum(
                abs(
                    (
                        datetime.fromisoformat(t["close_time"])
                        - datetime.fromisoformat(t["entry_time"])
                    ).total_seconds()
                )
                for t in trades
            )
            / len(trades)
            / 3600
        )
        print("\n  Performance:")
        print(f"    Wins:       {wins}")
        print(f"    Losses:     {losses}")
        print(f"    Win rate:   {wins / len(trades) * 100:.1f}%")
        print(f"    Total PnL:  ${total_pnl:,.2f}")
        print(f"    Avg PnL:    ${total_pnl / len(trades):,.2f}")
        print(f"    TP exits:   {tp_count}")
        print(f"    SL exits:   {sl_count}")
        print(f"    Timeouts:   {timeout_count}")
        print(f"    Avg hold:   {avg_hold:.1f}h")

    # Write journal
    out = output_path or PAPER_JOURNAL
    if not dry_run and trades:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                for trade in trades:
                    f.write(json.dumps(trade, ensure_ascii=False, default=str) + "\n")
            print(f"\n  Journal written: {out} ({len(trades)} entries)")
        except OSError as e:
            print(f"\n  ERROR writing journal: {e}")
            return {"status": "error", "error": str(e)}

    if dry_run:
        print("\n  [DRY RUN] No journal written")

    return {
        "status": "ok",
        "trades": len(trades),
        "skipped_no_bar": skipped_no_bar,
        "skipped_no_atr": skipped_no_atr,
        "skipped_cooldown": skipped_cooldown,
        "total_pnl": round(sum(t["pnl"] for t in trades), 2) if trades else 0,
        "win_rate": round(wins / len(trades), 4) if trades else 0,
    }


def main() -> int:
    p = argparse.ArgumentParser(prog="paper_trade_simulator")
    p.add_argument(
        "--since", type=str, default=None, help="Start date YYYY-MM-DD (default: all data)"
    )
    p.add_argument("--dry-run", action="store_true", help="Preview without writing journal")
    p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output path (default: data/paper_trade_journal.jsonl)",
    )
    p.add_argument(
        "--sl-mult",
        type=float,
        default=SL_ATR_MULT,
        help=f"SL ATR multiplier (default: {SL_ATR_MULT})",
    )
    p.add_argument(
        "--tp-mult",
        type=float,
        default=TP_ATR_MULT,
        help=f"TP ATR multiplier (default: {TP_ATR_MULT})",
    )
    p.add_argument(
        "--max-hold-bars",
        type=int,
        default=MAX_HOLD_BARS,
        help=f"Max hold bars (default: {MAX_HOLD_BARS})",
    )
    args = p.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            print(f"Invalid date: {args.since}", file=sys.stderr)
            return 1

    summary = run_simulator(
        since=since,
        dry_run=args.dry_run,
        output_path=Path(args.output) if args.output else None,
        sl_mult=args.sl_mult,
        tp_mult=args.tp_mult,
        max_hold_bars=args.max_hold_bars,
    )
    if args.dry_run:
        print(f"\n{json.dumps(summary, indent=2, ensure_ascii=False, default=str)}")
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
