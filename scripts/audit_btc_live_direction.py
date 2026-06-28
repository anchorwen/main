"""
audit_btc_live_direction.py — Iron Law #11 compliant audit script.

Statistics: BTC live brains, direction distribution (buy/sell),
recent golden_master signal bias, and governance status.

Output: stdout — the sole source of truth.
"""

import json
import sys
from collections import defaultdict, Counter
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

DATA_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data_btc")

LIVE_BRAINS = {"BTC_Swing_V4", "BTC_Swing_V12_H1_15"}

BRANCH_MAP = {
    "btc_swing": "BTC_Swing_V4",
    "btc_swing_h1": "BTC_Swing_V12_H1_15",
}


def load_jsonl(path: Path):
    if not path.exists():
        print(f"WARNING: {path} not found")
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def audit_governance(data_dir: Path):
    """1. Governance state: live models and their stats."""
    print("=" * 72)
    print("1. GOVERNANCE STATE (governance_state.json)")
    print("=" * 72)
    gf = data_dir / "governance_state.json"
    if not gf.exists():
        print("MISSING: governance_state.json")
        return
    gs = json.loads(open(gf).read())
    states = gs.get("brain_states", {})
    for bid, st in sorted(states.items()):
        status = st.get("status", "?")
        pm = st.get("performance_metrics", {})
        wr = pm.get("win_rate", 0)
        pf = pm.get("profit_factor", 0)
        trades = pm.get("total_trades", 0)
        pnl_r = pm.get("pnl_r", 0)
        src = pm.get("_data_source", "?")
        print(f"  {bid}:")
        print(f"    status={status}")
        print(f"    win_rate={wr:.4f} ({wr*100:.2f}%)")
        print(f"    profit_factor={pf:.2f}")
        print(f"    total_trades={trades}")
        print(f"    pnl_r={pnl_r}")
        print(f"    data_source={src}")
    print()


def audit_journal(data_dir: Path):
    """2. Live trade journal: direction distribution per brain."""
    print("=" * 72)
    print("2. LIVE TRADE JOURNAL ANALYSIS (live_trade_journal.jsonl)")
    print("=" * 72)

    entries = load_jsonl(data_dir / "live_trade_journal.jsonl")
    if not entries:
        return

    # Per-brain: direction counts (all actions)
    total_by_brain_side: Counter[tuple[str, str]] = Counter()
    open_by_brain_side: Counter[tuple[str, str]] = Counter()
    close_by_brain_side: Counter[tuple[str, str]] = Counter()
    open_positions: dict[int, Any] = {}  # ticket -> info

    for e in entries:
        brain_ids = [b for b in (e.get("brain_ids") or []) if b in LIVE_BRAINS]
        if not brain_ids:
            continue
        action = e.get("action", "?")
        side = e.get("side", "?")
        ticket = e.get("position_ticket")

        for bid in brain_ids:
            total_by_brain_side[(bid, side)] += 1
            if action == "open":
                open_by_brain_side[(bid, side)] += 1
                open_positions[ticket] = e
            elif action in ("close", "modify_close"):
                close_by_brain_side[(bid, side)] += 1

    print("--- Direction distribution (ALL actions) ---")
    for bid in sorted(LIVE_BRAINS):
        long_c = total_by_brain_side.get((bid, "long"), 0)
        short_c = total_by_brain_side.get((bid, "short"), 0)
        total = long_c + short_c
        pct_long = long_c / total * 100 if total else 0
        print(
            f"  {bid}: long={long_c} ({pct_long:.1f}%), short={short_c} ({100-pct_long:.1f}%), total={total}"
        )

    print()
    print("--- Open positions ---")
    for bid in sorted(LIVE_BRAINS):
        long_c = open_by_brain_side.get((bid, "long"), 0)
        short_c = open_by_brain_side.get((bid, "short"), 0)
        print(f"  {bid}: long={long_c}, short={short_c}")

    print()
    print("--- Close events ---")
    for bid in sorted(LIVE_BRAINS):
        long_c = close_by_brain_side.get((bid, "long"), 0)
        short_c = close_by_brain_side.get((bid, "short"), 0)
        print(f"  {bid}: long={long_c}, short={short_c}")

    # Time-based direction trend
    print()
    print("--- Direction trend by week ---")
    weeks: defaultdict[tuple[str, str], Counter[str]] = defaultdict(lambda: Counter())
    for e in entries:
        brain_ids = [b for b in (e.get("brain_ids") or []) if b in LIVE_BRAINS]
        if not brain_ids:
            continue
        side = e.get("side", "?")
        recorded_at = e.get("recorded_at", "")
        if recorded_at:
            week_key = recorded_at[:10]  # YYYY-MM-DD
            for bid in brain_ids:
                weeks[(bid, week_key[:7])][side] += 1  # by month

    for (bid, month), sides in sorted(weeks.items()):
        long_c = sides.get("long", 0)
        short_c = sides.get("short", 0)
        total = long_c + short_c
        pct_long = long_c / total * 100 if total else 0
        print(f"  {bid} | {month}: long={long_c}, short={short_c} → {pct_long:.0f}% long")
    print()


def audit_golden_master(data_dir: Path):
    """3. golden_master: recent direction signals."""
    print("=" * 72)
    print("3. GOLDEN MASTER SIGNAL ANALYSIS (golden_master.jsonl)")
    print("=" * 72)
    entries = load_jsonl(data_dir / "golden_master.jsonl")
    if not entries:
        return

    # Last 100 cycles: direction per strategy
    dir_by_strat: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries[-100:]:
        outputs = e.get("outputs", {})
        cycle = e.get("cycle", "?")
        for strat_key, strat_val in outputs.items():
            direction = strat_val.get("direction", "?")
            should_trade = strat_val.get("should_trade", False)
            conf = strat_val.get("confidence", 0)
            dir_by_strat[strat_key].append(
                {
                    "cycle": cycle,
                    "direction": direction,
                    "should_trade": should_trade,
                    "confidence": conf,
                }
            )

    for strat_key, records in sorted(dir_by_strat.items()):
        brain_id = BRANCH_MAP.get(strat_key, strat_key)
        total = len(records)
        long_c = sum(1 for r in records if r["direction"] == "long")
        short_c = sum(1 for r in records if r["direction"] == "short")
        trade_yes = sum(1 for r in records if r["should_trade"])
        avg_conf = sum(r["confidence"] for r in records) / total if total else 0

        print(f"  {strat_key} → {brain_id} (last {total} cycles):")
        print(
            f"    direction: long={long_c} ({long_c/total*100:.1f}%), short={short_c} ({short_c/total*100:.1f}%)"
        )
        print(f"    should_trade=True: {trade_yes}/{total} ({trade_yes/total*100:.1f}%)")
        print(f"    avg_confidence: {avg_conf:.3f}")

    # Last 10 entries full detail
    print()
    print("--- Last 10 golden_master entries ---")
    for e in entries[-10:]:
        ts = e.get("timestamp_utc", "?")
        outputs = e.get("outputs", {})
        print(f"  [{ts}]")
        for sk, sv in outputs.items():
            print(
                f"    {sk}: dir={sv.get('direction','?')}, trade={sv.get('should_trade',False)}, conf={sv.get('confidence',0):.3f}"
            )
    print()


def audit_brain_configs(data_dir: Path):
    """4. Brain model configs: look for config/bias metadata."""
    print("=" * 72)
    print("4. BRAIN CONFIGURATION FILES")
    print("=" * 72)

    # Check brains/ directory
    brains_dir = data_dir / "brains"
    if brains_dir.exists():
        for f in sorted(brains_dir.glob("*.json")):
            cfg = json.loads(open(f).read())
            bid = cfg.get("brain_id", "?")
            status = cfg.get("status", "?")
            direction_bias = cfg.get("direction_bias", "?")
            if isinstance(cfg, dict):
                # Print relevant fields
                relevant = {
                    k: v
                    for k, v in cfg.items()
                    if k
                    in (
                        "brain_id",
                        "status",
                        "direction_bias",
                        "direction",
                        "trend_direction",
                        "model_type",
                        "timeframe",
                    )
                }
                print(f"  {f.name}: {json.dumps(relevant)}")

    # Check models/ directory
    models_dir = data_dir / "models" / "brains"
    if models_dir.exists():
        for f in sorted(models_dir.glob("BTC_Swing_V*")):
            if f.is_file():
                print(f"  models/brains/{f.name}")
            elif f.is_dir():
                for sub in sorted(f.glob("*.json")):
                    print(f"  models/brains/{f.name}/{sub.name}")

    # Check brain_performance.json for direction_bias
    perf_file = data_dir / "brain_performance.json"
    if perf_file.exists():
        print()
        print("--- brain_performance.json direction_bias ---")
        bp = json.loads(open(perf_file).read())
        for bid in sorted(LIVE_BRAINS):
            info = bp.get(bid, {})
            if isinstance(info, dict):
                db = info.get("direction_bias", "N/A")
                wr = info.get("win_rate", "N/A")
                pf = info.get("profit_factor", "N/A")
                print(f"  {bid}: direction_bias={db}, win_rate={wr}, pf={pf}")
    print()


def audit_mt5_positions(data_dir: Path):
    """5. Current MT5 positions (if snapshots available)."""
    print("=" * 72)
    print("5. POSITION SNAPSHOTS (most recent)")
    print("=" * 72)
    entries = load_jsonl(data_dir / "position_snapshots.jsonl")
    if not entries:
        print("  No snapshots found.")
        return

    # Show most recent snapshot
    latest = entries[-1]
    print(f"  Latest snapshot (ticket={latest.get('ticket')}, time={latest.get('time')}):")
    for k, v in latest.items():
        print(f"    {k}: {v}")

    # Also show all unique tickets in last 50 snapshots
    recent = entries[-50:]
    tickets = set()
    for e in recent:
        t = e.get("ticket")
        if t not in tickets:
            tickets.add(t)
    print(f"  Unique tickets in last 50 snapshots: {len(tickets)}")
    print()


def main():
    data_dir = DATA_DIR
    print(f"BTC Live Brain Direction Audit — {datetime.now(timezone.utc).isoformat()}")
    print(f"Data directory: {data_dir.resolve()}")
    print(f"Live brains tracked: {LIVE_BRAINS}")
    print()

    audit_governance(data_dir)
    audit_journal(data_dir)
    audit_golden_master(data_dir)
    audit_brain_configs(data_dir)
    audit_mt5_positions(data_dir)

    print("=" * 72)
    print("CONCLUSION")
    print("=" * 72)


if __name__ == "__main__":
    main()
