"""Real-time institutional audit -- Iron Law #11 compliant.
Usage: python scripts/live_audit_realtime.py [hours] [btc|xau|both]
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_BTC = PROJECT_ROOT / "data_btc"
DATA_XAU = PROJECT_ROOT / "data"


def load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def parse_ts(val):
    if not val:
        return None
    try:
        s = val.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def audit_symbol(label, data_dir, hours):
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    result: dict[str, Any] = {}

    # -- 1. Golden Master (latest cycle) --
    gm = load_jsonl(data_dir / "golden_master.jsonl")
    if gm:
        last_gm = gm[-1]
        gm_ts = parse_ts(last_gm.get("timestamp_utc") or last_gm.get("time"))
        inputs = last_gm.get("inputs", {})
        result["golden_master"] = {
            "total_entries": len(gm),
            "last_cycle_utc": gm_ts.isoformat() if gm_ts else "N/A",
            "mid_price": inputs.get("mid_price"),
            "spread": inputs.get("spread"),
            "atr": inputs.get("current_atr"),
            "regime": inputs.get("regime"),
            "trend_direction": inputs.get("trend_direction"),
            "risk_budget_usd": inputs.get("risk_budget_usd"),
            "health_volume_mult": inputs.get("health_volume_mult"),
            "session_volume_mult": inputs.get("session_volume_mult"),
        }

    # -- 2. Live Trade Journal --
    journal = load_jsonl(data_dir / "live_trade_journal.jsonl")
    recent_journal = []
    open_positions = {}
    strategy_pnl: defaultdict[str, float] = defaultdict(float)

    for entry in journal:
        ts = parse_ts(entry.get("recorded_at"))
        if not ts:
            continue
        if ts >= cutoff:
            recent_journal.append(entry)
        # Track opens
        ack = entry.get("ack_status", "")
        action = entry.get("action", "")
        if action in ("order_send", "open_long", "open_short") and ack == "accepted":
            pos_ticket = entry.get("position_ticket") or entry.get("detail", {}).get("order")
            if pos_ticket:
                open_positions[pos_ticket] = {
                    "strategy": entry.get("strategy", "?"),
                    "side": entry.get("side", "?"),
                    "entry_price": entry.get("detail", {}).get("request", {}).get("price"),
                    "volume": entry.get("volume"),
                    "opened_at": entry.get("recorded_at"),
                    "magic": entry.get("magic"),
                    "p_win": entry.get("p_win"),
                }
        # Track closes - accumulate PnL
        if entry.get("pnl") is not None:
            sname = entry.get("strategy", "?")
            strategy_pnl[sname] += float(entry["pnl"])

    # Remove closed positions from open set
    for entry in journal:
        pnl = entry.get("pnl")
        if pnl is not None:
            pos_ticket = entry.get("position_ticket")
            if pos_ticket and pos_ticket in open_positions:
                del open_positions[pos_ticket]

    result["trade_journal"] = {
        "total_entries": len(journal),
        "recent_entries_24h": len(recent_journal),
        "open_positions": len(open_positions),
    }
    if open_positions:
        result["open_positions_detail"] = [
            {"ticket": k, **v} for k, v in list(open_positions.items())[:20]
        ]

    # -- 3. Live Labels --
    labels = load_jsonl(data_dir / "reports" / "live_labels.jsonl")
    recent_labels = []
    for lb in labels:
        ts = parse_ts(lb.get("close_recorded_at") or lb.get("open_recorded_at"))
        if ts and ts >= cutoff:
            recent_labels.append(lb)

    label_counts: defaultdict[str, int] = defaultdict(int)
    label_pnl: defaultdict[str, float] = defaultdict(float)
    for lb in labels:
        lb_type = lb.get("label", "?")
        label_counts[lb_type] += 1
        if lb.get("pnl") is not None:
            label_pnl[lb_type] += float(lb["pnl"])

    result["live_labels"] = {
        "total_entries": len(labels),
        "recent_24h": len(recent_labels),
        "label_distribution": dict(label_counts),
        "label_pnl": {k: round(v, 2) for k, v in sorted(label_pnl.items(), key=lambda x: -x[1])},
    }

    # -- 4. Governance State --
    gov = data_dir / "governance_state.json"
    if gov.exists():
        with open(gov, encoding="utf-8") as f:
            gs = json.load(f)
        states = gs.get("brain_states", {})
        status_counts: defaultdict[str, int] = defaultdict(int)
        live_brains = []
        for bid, bd in states.items():
            st = bd.get("status", "?")
            status_counts[st] += 1
            if st == "live":
                live_brains.append(
                    {
                        "id": bid,
                        "vote_weight": bd.get("vote_weight"),
                        "trades": bd.get("total_trades"),
                        "pnl_r": bd.get("total_pnl_r"),
                        "winrate": bd.get("rolling_winrate"),
                    }
                )
        result["governance"] = {
            "status_distribution": dict(status_counts),
            "live_brains": live_brains,
            "total_brains": len(states),
        }

    # -- 5. Exit Watchdog Alerts --
    wd = load_jsonl(data_dir / "reports" / "exit_watchdog_alerts.jsonl")
    recent_wd = []
    for w in wd:
        w_ts = parse_ts(w.get("time", ""))
        if w_ts and w_ts >= cutoff:
            recent_wd.append(w)
    result["exit_watchdog"] = {
        "total_alerts": len(wd),
        "recent_24h": len(recent_wd),
    }
    if recent_wd:
        result["watchdog_recent_detail"] = [
            {
                "time": str(w.get("time", ""))[:19],
                "strategy": str(w.get("strategy", "?")),
                "event": str(w.get("event", "?")),
                "pnl": w.get("pnl"),
            }
            for w in recent_wd[-10:]
        ]

    # -- 6. PnL summary by strategy --
    result["pnl_summary"] = {
        k: round(v, 2) for k, v in sorted(strategy_pnl.items(), key=lambda x: -x[1])[:20]
    }

    # -- 7. Kelly Diag (MetaFilter routing check) --
    kelly_from_gm = [g for g in gm if g.get("event") == "kelly_diag"]
    recent_kelly = []
    for k in kelly_from_gm:
        k_ts = parse_ts(k.get("time", ""))
        if k_ts and k_ts >= cutoff:
            recent_kelly.append(k)

    mf_rejects = []
    for j in journal:
        reason = str(j.get("reason", ""))
        comment = str(j.get("comment", ""))
        if "meta_filter_rejected" in reason or "meta_filter_rejected" in comment:
            mf_rejects.append(j)

    result["kelly_diagnostics"] = {
        "gm_total_kelly": len(kelly_from_gm),
        "recent_kelly_count": len(recent_kelly),
        "journal_meta_filter_rejects": len(mf_rejects),
    }

    if recent_kelly:
        statarb_rejects = [k for k in recent_kelly if "statarb" in str(k.get("stage", ""))]
        swing_rejects = [
            k for k in statarb_rejects if "swing" in str(k.get("strategy", "")).lower()
        ]
        result["kelly_detail"] = {
            "statarb_rejects": len(statarb_rejects),
            "swing_in_statarb": len(swing_rejects),
            "swing_rejects_list": [
                {
                    "time": str(k.get("time", ""))[:19],
                    "strategy": str(k.get("strategy", "?")),
                    "stage": str(k.get("stage", "?")),
                    "p_win": k.get("result_p_win"),
                    "passed": k.get("passed"),
                    "reason": str(k.get("reason", "")),
                }
                for k in swing_rejects
            ],
            "recent_events": [
                {
                    "time": str(k.get("time", ""))[:19],
                    "strategy": str(k.get("strategy", "?")),
                    "stage": str(k.get("stage", "?")),
                    "p_win": k.get("result_p_win"),
                    "passed": k.get("passed"),
                }
                for k in recent_kelly[-20:]
            ],
        }

    return result


def main():
    hours = 24
    if len(sys.argv) > 1:
        with contextlib.suppress(ValueError):
            hours = int(sys.argv[1])

    symbol = "both"
    if len(sys.argv) > 2:
        symbol = sys.argv[2].lower()

    print("=" * 80)
    print("  LIVE SYSTEM AUDIT -- Iron Law #11 Compliance")
    print(f"  Generated: {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}")
    print(f"  Window: last {hours}h")
    print("=" * 80)

    for sym_label, data_dir in [("BTC", DATA_BTC), ("XAU", DATA_XAU)]:
        if symbol not in ("both", sym_label.lower()):
            continue

        print(f"\n{'=' * 80}")
        print(f"  {sym_label} ({data_dir})")
        print(f"{'=' * 80}")

        try:
            audit = audit_symbol(sym_label, data_dir, hours)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
            print(f"  ERROR during audit: {e}")
            import traceback

            traceback.print_exc()
            continue

        # Golden Master
        gm = audit.get("golden_master", {})
        if gm:
            print("\n  [GOLDEN MASTER] Last cycle:")
            print(f"    Entries: {gm['total_entries']} | Time: {gm['last_cycle_utc']}")
            print(f"    Mid: {gm['mid_price']} | Spread: {gm['spread']} | ATR: {gm['atr']}")
            print(f"    Regime: {gm['regime']} | Trend: {gm['trend_direction']}")
            print(
                f"    Risk Budget: ${gm['risk_budget_usd']} | Vol Mult: {gm['health_volume_mult']}/{gm['session_volume_mult']}"
            )

        # Governance
        gov = audit.get("governance", {})
        if gov:
            print("\n  [GOVERNANCE]:")
            print(f"    Status: {gov['status_distribution']}")
            print(f"    Total: {gov['total_brains']} | Live: {len(gov['live_brains'])}")
            if gov["live_brains"]:
                for b in gov["live_brains"]:
                    print(
                        f"      LIVE: {b['id']:35s} weight={b['vote_weight']} trades={b['trades']} PnL={b['pnl_r']}R WR={b['winrate']}"
                    )

        # Trade Journal
        tj = audit.get("trade_journal", {})
        print("\n  [TRADE JOURNAL]:")
        print(
            f"    Total entries: {tj['total_entries']} | Recent {hours}h: {tj['recent_entries_24h']}"
        )
        print(f"    Open positions: {tj['open_positions']}")
        if tj.get("open_positions_detail"):
            for op in tj["open_positions_detail"]:
                print(
                    f"      OPEN: ticket={op['ticket']} | {op['strategy']:30s} | {op['side']:5s} | entry={op['entry_price']} vol={op['volume']} | p_win={op['p_win']} | opened={op['opened_at']}"
                )

        # PnL Summary
        pnl = audit.get("pnl_summary", {})
        if pnl:
            print("\n  [PnL BY STRATEGY]:")
            for sname, s_pnl in pnl.items():
                print(f"      {sname:35s} {s_pnl:>+10.2f}R")

        # Live Labels
        ll = audit.get("live_labels", {})
        if ll:
            print("\n  [LIVE LABELS]:")
            print(f"    Total: {ll['total_entries']} | Recent {hours}h: {ll['recent_24h']}")
            print("    Distribution:")
            for lb_type, cnt in sorted(ll["label_distribution"].items(), key=lambda x: -x[1]):
                lp = ll["label_pnl"].get(lb_type, 0)
                print(f"      {lb_type:20s} {cnt:>4d} trades | PnL: {lp:>+10.2f}R")

        # Exit Watchdog
        wd = audit.get("exit_watchdog", {})
        print("\n  [EXIT WATCHDOG]:")
        print(f"    Total alerts: {wd['total_alerts']} | Recent {hours}h: {wd['recent_24h']}")
        if wd.get("watchdog_recent_detail"):
            for w in wd["watchdog_recent_detail"]:
                print(
                    f"      {w['time']} | {w['strategy']:30s} | {w['event']:25s} | PnL={w['pnl']}"
                )

        # Kelly Diagnostics (MetaFilter check)
        kd = audit.get("kelly_diagnostics", {})
        print("\n  [KELLY / METAFILTER DIAGNOSTICS]:")
        print(
            f"    GM kelly events: {kd['gm_total_kelly']} | Recent {hours}h: {kd['recent_kelly_count']}"
        )
        print(f"    Journal MF rejects: {kd['journal_meta_filter_rejects']}")

        if kd.get("kelly_detail"):
            detail = kd["kelly_detail"]
            print(f"    StatArb MetaFilter rejects: {detail['statarb_rejects']}")
            print(
                f"    [!] SWING MetaFilter rejects (should be 0 after DQAF-065): {detail['swing_in_statarb']}"
            )
            if detail["swing_in_statarb"] > 0:
                print("    [FAIL] CATEGORY ERROR NOT RESOLVED! Swing MetaFilter rejects found:")
                for k in detail["swing_rejects_list"]:
                    print(
                        f"      {k['time']} | {k['strategy']:30s} | {k['stage']} | p_win={k['p_win']} | {k['reason']}"
                    )
            else:
                print("    [OK] No swing MetaFilter rejects -- DQAF-065 confirmed working")
            if detail.get("recent_events"):
                print("    Recent kelly events:")
                for k in detail["recent_events"]:
                    print(
                        f"      {k['time']} | {k['strategy']:30s} | {k['stage']:35s} | p_win={k['p_win']} | passed={k['passed']}"
                    )

    print(f"\n{'=' * 80}")
    print("  AUDIT COMPLETE -- the above is the sole source of truth")
    print("=" * 80)


if __name__ == "__main__":
    main()
