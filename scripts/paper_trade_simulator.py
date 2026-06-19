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
sys.path.insert(0, str(PROJECT_ROOT))
DATA_DIR = PROJECT_ROOT / "data"
DECISIONS_DIR = DATA_DIR / "decisions"
FEATURE_FILE = (
    DATA_DIR / "feature_store" / "records" / "symbol=XAUUSDc" / "timeframe=M5" / "features.jsonl"
)
OHLC_FILE = DATA_DIR / "raw" / "xauusdc_m5_1y.csv"
OHLC_RECENT_FILE = DATA_DIR / "raw" / "xauusdc_m5_recent.csv"
PAPER_JOURNAL = DATA_DIR / "paper_trade_journal.jsonl"

# ── Default config (fallback when live.yaml is unavailable) ─────────────
SL_ATR_MULT = 2.0  # stop-loss as multiple of M5_ATR_14
TP_ATR_MULT = 3.5  # take-profit as multiple of M5_ATR_14
MAX_HOLD_BARS = 288  # 24h in M5 bars — close at market if neither SL/TP hit
COOLDOWN_SECONDS = 300  # 5 min between entries (avoid duplicates)
LOT_SIZE = 0.01  # standard mini lot
LOT_MULTIPLIER = 100.0  # XAUUSD: 1 lot = 100 oz, PnL = (exit-entry) * 100 * lots
MIN_BARS_FORWARD = 6  # need at least this many future bars to simulate
SPREAD_COST = 0.30  # fallback fixed spread when SpreadModel is unavailable
MIN_TRAIL_DISTANCE = 0.5  # minimum trail distance in price units


def load_live_config() -> dict[str, Any] | None:
    """Load live.yaml config for strategy-aligned simulation parameters."""
    config_path = PROJECT_ROOT / "configs" / "live.yaml"
    if not config_path.exists():
        return None
    try:
        import yaml

        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:  # BLE001:REVIEWED
        return None


def build_strategy_param_map(
    config: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Build brain_type → strategy_params map from live.yaml strategy_lines.

    Returns a dict keyed by brain_type with sl_atr_mult, tp_atr_mult, trail_enabled, etc.
    """
    param_map: dict[str, dict[str, Any]] = {}
    if config is None:
        return param_map

    strategy_lines = config.get("strategy_lines", {})
    for _line_name, line_cfg in strategy_lines.items():
        if not isinstance(line_cfg, dict):
            continue
        brain_types = line_cfg.get("brain_types", [])
        sl_cfg = line_cfg.get("sl", {}) or {}
        tp_cfg = line_cfg.get("tp", {}) or {}
        exit_cfg = line_cfg.get("exit", {}) or {}
        params = {
            "sl_atr_mult": sl_cfg.get("base_atr_mult", SL_ATR_MULT),
            "tp_atr_mult": tp_cfg.get("base_atr_mult", TP_ATR_MULT),
            "trail_enabled": exit_cfg.get("trail_enabled", False),
            "trail_atr_mult": exit_cfg.get("trail_atr_mult", sl_cfg.get("base_atr_mult", 2.0)),
            "dynamic_sl": sl_cfg.get("dynamic", False),
        }
        for bt in brain_types:
            param_map[str(bt).strip()] = params
    return param_map


def resolve_strategy_params(
    brain_id: str,
    param_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Resolve strategy params for a brain_id by matching against configs/brains/."""
    # Load brain config to get brain_type
    brains_dir = PROJECT_ROOT / "configs" / "brains"
    brain_type = None
    for config_path in sorted(brains_dir.glob("*.json")):
        if config_path.name.endswith(".normalization.json"):
            continue
        try:
            entry = json.loads(config_path.read_text(encoding="utf-8"))
            if entry.get("brain_id") == brain_id:
                brain_type = entry.get("brain_type")
                break
        except (json.JSONDecodeError, OSError):
            continue

    if brain_type and brain_type in param_map:
        return param_map[brain_type]

    # Fallback: prefix match
    for bt, params in param_map.items():
        if bt in brain_id or brain_id.lower().startswith(bt.replace("_", "")):
            return params

    return {
        "sl_atr_mult": SL_ATR_MULT,
        "tp_atr_mult": TP_ATR_MULT,
        "trail_enabled": False,
        "trail_atr_mult": SL_ATR_MULT,
        "dynamic_sl": False,
    }


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
                        "supporting_brains": (
                            rec.get("attribution", {}).get("supporting_brains", [])
                        ),
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


def _estimate_spread(entry_time: datetime, atr: float, mid_price: float) -> tuple[float, float]:
    """Estimate dynamic (spread, slippage) for a bar timestamp + ATR."""
    try:
        from core.simulation.spread_model import SpreadModel

        model = SpreadModel()
        # Convert naive datetime to UTC if needed
        utc_time = entry_time.replace(tzinfo=UTC) if entry_time.tzinfo is None else entry_time
        return model.estimate(now_utc=utc_time, atr=atr, mid_price=mid_price)
    except Exception:  # BLE001:REVIEWED
        return (SPREAD_COST, 0.05)


def simulate_trade(
    decision: dict,
    bars: list[dict],
    start_idx: int,
    features: dict[str, float],
    sl_mult: float = SL_ATR_MULT,
    tp_mult: float = TP_ATR_MULT,
    max_hold_bars: int = MAX_HOLD_BARS,
    trail_enabled: bool = False,
    trail_atr_mult: float | None = None,
    spread_cost: float = SPREAD_COST,
) -> dict[str, Any] | None:
    """Simulate a single trade from decision entry through exit.

    Supports trailing stop (trail_enabled) and uses dynamic spread + slippage
    when SpreadModel is available (falls back to fixed SPREAD_COST).
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

    # ── Dynamic spread + slippage estimation ──
    dynamic_spread, dynamic_slippage = _estimate_spread(entry_time, atr, entry_price)
    total_friction = dynamic_spread + dynamic_slippage

    # Compute SL and TP
    sl_distance = sl_mult * atr
    tp_distance = tp_mult * atr
    if side == "LONG":
        sl = entry_price - sl_distance
        tp = entry_price + tp_distance
    else:
        sl = entry_price + sl_distance
        tp = entry_price - tp_distance

    trail_distance = (trail_atr_mult or sl_mult) * atr

    # Walk forward through bars
    exit_price = None
    exit_time = None
    exit_reason = "timeout"
    highest_favorable = entry_price  # for trailing stop
    current_sl = sl

    end_idx = min(start_idx + max_hold_bars + 1, len(bars))
    for i in range(start_idx + 1, end_idx):
        bar = bars[i]

        # Trailing stop update (on close price, before checking SL/TP)
        if trail_enabled:
            if side == "LONG":
                if bar["close"] > highest_favorable:
                    highest_favorable = bar["close"]
                    trail_sl = highest_favorable - trail_distance
                    if trail_sl > current_sl + MIN_TRAIL_DISTANCE:
                        current_sl = trail_sl
            else:  # SHORT
                if bar["close"] < highest_favorable:
                    highest_favorable = bar["close"]
                    trail_sl = highest_favorable + trail_distance
                    if trail_sl < current_sl - MIN_TRAIL_DISTANCE:
                        current_sl = trail_sl

        # Check SL and TP
        if side == "LONG":
            if bar["low"] <= current_sl:
                exit_price = current_sl
                exit_time = bar["time"]
                exit_reason = "sl_hit"
                break
            if bar["high"] >= tp:
                exit_price = tp
                exit_time = bar["time"]
                exit_reason = "tp_hit"
                break
        else:  # SHORT
            if bar["high"] >= current_sl:
                exit_price = current_sl
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

    # Estimate exit friction (may differ from entry due to session change)
    exit_spread, exit_slippage = (dynamic_spread, dynamic_slippage)
    if exit_time and exit_time != entry_time:
        try:  # noqa: SIM105
            exit_spread, exit_slippage = _estimate_spread(exit_time, atr, exit_price)
        except Exception:  # BLE001:REVIEWED
            pass
    exit_friction = exit_spread + exit_slippage

    # Compute PnL (entry at ask, exit at bid — spread + slippage cost included)
    if side == "LONG":
        effective_entry = entry_price + total_friction / 2  # entry at ask + slippage
        effective_exit = exit_price - exit_friction / 2  # exit at bid - slippage
        pnl = (effective_exit - effective_entry) * LOT_MULTIPLIER * LOT_SIZE
    else:
        effective_entry = entry_price - total_friction / 2  # entry at bid - slippage
        effective_exit = exit_price + exit_friction / 2  # exit at ask + slippage
        pnl = (effective_entry - effective_exit) * LOT_MULTIPLIER * LOT_SIZE
    pnl = round(pnl, 2)

    # Determine label
    if exit_reason == "tp_hit":
        label = "tp_hit_first"
    elif exit_reason == "sl_hit":
        label = "sl_hit_first"
    else:
        label = "breakeven" if abs(pnl) < 0.5 else ("tp_hit_first" if pnl > 0 else "sl_hit_first")

    return {
        "schema_version": "paper_trade_journal.v2",
        "message_id": f"paper_{decision['record_id']}",
        "recorded_at": exit_time.isoformat() if exit_time else entry_time.isoformat(),
        "ack_status": "closed",
        "symbol": "XAUUSDc",
        "side": side.lower(),
        "action": "open",
        "entry_price": round(effective_entry, 5),
        "exit_price": round(effective_exit, 5),
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
            "entry_price": round(effective_entry, 5),
            "exit_price": round(effective_exit, 5),
            "sl": round(sl, 5),
            "tp": round(tp, 5),
            "atr": round(atr, 5),
            "spread_cost": round(dynamic_spread, 5),
            "slippage_cost": round(dynamic_slippage, 5),
            "total_friction": round(total_friction, 5),
            "trail_enabled": trail_enabled,
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
    *,
    per_brain: bool = False,
) -> dict[str, Any]:
    """Run paper trade simulation over all decisions. Returns summary.

    When per_brain=True, also attributes each trade to its supporting
    brains and writes per-brain metrics to paper_trade_by_brain.jsonl.
    """

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
    print("\n[3/5] Collecting OPEN decisions...")
    decisions = collect_decisions(since=since)
    print(f"  {len(decisions)} OPEN decisions found")
    if not decisions:
        return {"status": "ok", "trades": 0, "skipped": 0}

    # 4. Load strategy config for per-brain SL/TP parameters
    print("\n[4/5] Loading strategy configuration...")
    live_config = load_live_config()
    strategy_param_map = build_strategy_param_map(live_config)
    if strategy_param_map:
        print(f"  {len(strategy_param_map)} brain_type → strategy mappings loaded")
    else:
        print("  No strategy config found, using defaults " f"(SL={sl_mult}, TP={tp_mult})")

    # 5. Simulate trades
    print("\n[5/5] Simulating trades...")
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

        # Resolve strategy params for this decision's brain
        supporting = decision.get("supporting_brains", [])
        primary_brain = supporting[0] if supporting else ""
        strat_params = resolve_strategy_params(primary_brain, strategy_param_map)
        trade = simulate_trade(
            decision,
            bars,
            bar_idx,
            features,
            sl_mult=strat_params["sl_atr_mult"],
            tp_mult=strat_params["tp_atr_mult"],
            max_hold_bars=max_hold_bars,
            trail_enabled=strat_params.get("trail_enabled", False),
            trail_atr_mult=strat_params.get("trail_atr_mult"),
        )
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

    # ── Per-brain attribution ──
    per_brain_summary: dict[str, Any] = {}
    if per_brain and trades:
        from collections import defaultdict

        brain_trades: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in trades:
            decision_id = trade.get("decision_record_id", "")
            # Find the original decision to get supporting brains
            supporting = []
            for d in decisions:
                if d.get("record_id") == decision_id:
                    supporting = d.get("supporting_brains", [])
                    break
            for brain_id in supporting:
                brain_trades[brain_id].append(trade)

        per_brain_records = []
        for brain_id, bt in sorted(brain_trades.items()):
            n = len(bt)
            total_pnl = sum(t["pnl"] for t in bt)
            wins = sum(1 for t in bt if t["label"] == "tp_hit_first")
            losses = sum(1 for t in bt if t["label"] == "sl_hit_first")
            tp_exits = sum(1 for t in bt if t["exit_reason"] == "tp_hit")
            sl_exits = sum(1 for t in bt if t["exit_reason"] == "sl_hit")
            timeout_exits = sum(1 for t in bt if t["exit_reason"] == "timeout")
            avg_pnl = total_pnl / n if n > 0 else 0
            win_rate = wins / n if n > 0 else 0
            total_spread = sum(t.get("detail", {}).get("spread_cost", 0) for t in bt)
            total_slippage = sum(t.get("detail", {}).get("slippage_cost", 0) for t in bt)

            # Average hold time
            hold_seconds = []
            for t in bt:
                try:
                    et = datetime.fromisoformat(t["entry_time"])
                    ct = datetime.fromisoformat(t["close_time"])
                    hold_seconds.append(abs((ct - et).total_seconds()))
                except (ValueError, KeyError):
                    pass
            avg_hold_min = (sum(hold_seconds) / len(hold_seconds) / 60) if hold_seconds else 0

            record = {
                "brain_id": brain_id,
                "total_trades": n,
                "win_rate": round(win_rate, 4),
                "total_pnl": round(total_pnl, 2),
                "avg_pnl": round(avg_pnl, 2),
                "wins": wins,
                "losses": losses,
                "tp_exits": tp_exits,
                "sl_exits": sl_exits,
                "timeout_exits": timeout_exits,
                "avg_hold_minutes": round(avg_hold_min, 1),
                "total_spread_cost": round(total_spread, 4),
                "total_slippage_cost": round(total_slippage, 4),
            }
            per_brain_records.append(record)

        # Sort by total PnL descending
        per_brain_records.sort(key=lambda r: r["total_pnl"], reverse=True)
        per_brain_summary = {
            "schema_version": "paper_trade_per_brain.v1",
            "total_brains": len(per_brain_records),
            "brains": per_brain_records,
        }

        # Print per-brain table
        print("\n  Per-Brain Performance:")
        print(
            f"  {'Brain':<40} {'Trades':>7} {'WR':>7} {'Total PnL':>10} {'Avg PnL':>9} {'Spread':>8}"
        )
        print(f"  {'-'*40} {'-'*7} {'-'*7} {'-'*10} {'-'*9} {'-'*8}")
        for r in per_brain_records[:15]:
            print(
                f"  {r['brain_id']:<40} {r['total_trades']:>7} "
                f"{r['win_rate']:>6.1%} {r['total_pnl']:>+10.2f} {r['avg_pnl']:>+9.2f} "
                f"{r['total_spread_cost']:>8.2f}"
            )

        # Write per-brain journal
        if not dry_run:
            per_brain_path = (output_path or PAPER_JOURNAL).parent / "paper_trade_by_brain.jsonl"
            try:
                with open(per_brain_path, "w", encoding="utf-8") as f:
                    for brain_id, bt in sorted(brain_trades.items()):
                        for trade in bt:
                            entry = {**trade, "_attributed_brain": brain_id}
                            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                print(f"\n  Per-brain journal: {per_brain_path}")
            except OSError as e:
                print(f"\n  ERROR writing per-brain journal: {e}")

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

    result: dict[str, Any] = {
        "status": "ok",
        "trades": len(trades),
        "skipped_no_bar": skipped_no_bar,
        "skipped_no_atr": skipped_no_atr,
        "skipped_cooldown": skipped_cooldown,
        "total_pnl": round(sum(t["pnl"] for t in trades), 2) if trades else 0,
        "win_rate": round(wins / len(trades), 4) if trades else 0,
    }
    if per_brain:
        result["per_brain"] = per_brain_summary
    return result


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
    p.add_argument(
        "--per-brain",
        action="store_true",
        help="Attribute each trade to supporting brains and write per-brain journal",
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
        per_brain=args.per_brain,
    )
    if args.dry_run:
        print(f"\n{json.dumps(summary, indent=2, ensure_ascii=False, default=str)}")
    return 0 if summary["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
