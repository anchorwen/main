#!/usr/bin/env python
"""One-shot system health overview — all new infrastructure in one page.

Usage::

    python scripts/system_health.py
    python scripts/system_health.py --base-dir data_btc
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def _utc_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _age_minutes(ts: str | None) -> float:
    if not ts:
        return -1
    try:
        dt = datetime.fromisoformat(str(ts)[:19])
        return (datetime.now(UTC).replace(tzinfo=None) - dt.replace(tzinfo=None)).total_seconds() / 60
    except Exception:
        return -1


def check_symbol(base_dir: str, label: str) -> dict:
    """Run all health checks for one symbol."""
    base = Path(base_dir)
    result: dict = {"label": label, "base_dir": str(base)}

    # ── 1. Event Stream ──
    stream_path = base / "ledger_events.jsonl"
    if stream_path.exists():
        lines = 0
        sources: dict[str, int] = {}
        last_ts = None
        with open(stream_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                try:
                    evt = json.loads(line)
                    src = evt.get("source", "unknown")
                    sources[src] = sources.get(src, 0) + 1
                    ts = evt.get("timestamp", "")
                    if ts:
                        last_ts = ts
                except Exception:
                    pass
        result["stream"] = {
            "exists": True,
            "total_events": lines,
            "sources": sources,
            "last_event_age_min": round(_age_minutes(last_ts), 1) if last_ts else None,
        }
    else:
        result["stream"] = {"exists": False}

    # ── 2. Projection (live-only governance view) ──
    try:
        from core.data.projections import project_governance_state

        proj = project_governance_state(stream_path, source_filter={"live", "migration"})
        brains = {k: v for k, v in proj.items() if not k.startswith("_")}
        active = [(bid, m) for bid, m in brains.items() if m.get("total_trades", 0) > 0]
        active.sort(key=lambda x: x[1].get("pnl_r", 0), reverse=True)
        result["projection"] = {
            "total_brains": len(brains),
            "active_brains": len(active),
            "top3": [
                {"id": bid, "trades": m["total_trades"], "wr": m["win_rate"], "pnl": m["pnl_r"]}
                for bid, m in active[:3]
            ],
            "bottom3": [
                {"id": bid, "trades": m["total_trades"], "wr": m["win_rate"], "pnl": m["pnl_r"]}
                for bid, m in active[-3:]
            ],
        }
    except Exception as e:
        result["projection"] = {"error": str(e)}

    # ── 3. Degradation status ──
    dh_path = base / "state" / "data_health_state.json"
    if dh_path.exists():
        try:
            dh = json.loads(dh_path.read_text(encoding="utf-8"))
            sources = dh.get("sources", {})
            fails = [k for k, v in sources.items() if v.get("status") == "fail"]
            warns = [k for k, v in sources.items() if v.get("status") == "warn"]
            overall = dh.get("overall_status", "unknown")

            from core.observability.degradation import evaluate_staleness

            stale_level = evaluate_staleness(sources)
            result["degradation"] = {
                "overall": overall,
                "staleness_level": stale_level.name if stale_level else "NORMAL",
                "fails": fails[:5],
                "warns": warns[:5],
            }
        except Exception as e:
            result["degradation"] = {"error": str(e)}

    # ── 4. Governance status ──
    gov_path = base / "governance_state.json"
    if gov_path.exists():
        try:
            gov = json.loads(gov_path.read_text(encoding="utf-8"))
            states = gov.get("brain_states", {})
            status_counts: dict[str, int] = {}
            for bid, bs in states.items():
                st = bs.get("status", "unknown")
                status_counts[st] = status_counts.get(st, 0) + 1
            live_brains = [bid for bid, bs in states.items() if bs.get("status") == "live"]
            result["governance"] = {
                "total_brains": len(states),
                "status_counts": status_counts,
                "live_brains": live_brains,
            }
        except Exception as e:
            result["governance"] = {"error": str(e)}

    # ── 5. Data freshness ──
    key_files = {
        "execution_state": base / "state" / "execution_state.json",
        "bar_sync_state": base / "bar_sync_state.json",
        "golden_master": base / "golden_master.jsonl",
    }
    freshness = {}
    for name, path in key_files.items():
        if path.exists():
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            age = (datetime.now(UTC) - mtime).total_seconds() / 60
            freshness[name] = round(age, 1)
        else:
            freshness[name] = None
    result["freshness"] = freshness

    return result


def print_report(results: list[dict]) -> None:
    """Print a one-page health summary."""
    print("=" * 70)
    print(f"  SYSTEM HEALTH — {_utc_iso()[:19]}")
    print("=" * 70)

    for r in results:
        label = r["label"]
        print(f"\n── {label} ({r['base_dir']}) ──")

        # Stream
        s = r.get("stream", {})
        if s.get("exists"):
            srcs = s.get("sources", {})
            src_str = ", ".join(f"{k}={v}" for k, v in sorted(srcs.items()))
            age = s.get("last_event_age_min")
            age_str = f"{age:.0f}min ago" if age is not None else "?"
            print(f"  Event Stream: {s['total_events']} events ({src_str}), last {age_str}")
        else:
            print("  Event Stream: NOT FOUND")

        # Projection
        p = r.get("projection", {})
        if "error" not in p:
            print(f"  Projection: {p['active_brains']}/{p['total_brains']} brains active")
            if p.get("top3"):
                top = p["top3"][0]
                print(f"    Top: {top['id']} — {top['trades']}t, {top['wr']:.1%} WR, {top['pnl']:+.1f}R")
            if p.get("bottom3"):
                bot = p["bottom3"][-1]
                print(f"    Bottom: {bot['id']} — {bot['trades']}t, {bot['wr']:.1%} WR, {bot['pnl']:+.1f}R")

        # Degradation
        d = r.get("degradation", {})
        if d:
            level = d.get("staleness_level", "?")
            icon = {"NORMAL": "[NORMAL]", "YELLOW": "[YELLOW]", "ORANGE": "[ORANGE]", "RED": "[RED]"}.get(level, "[?]")
            print(f"  Degradation: {icon} staleness={level} (data_health={d.get('overall','?')})")
            if level == "NORMAL" and d.get("overall") == "CRITICAL":
                print(f"    Note: Data stale/quality issues exist but key sources are fresh — trading allowed")
            if d.get("fails"):
                print(f"    Fails: {', '.join(d['fails'][:3])}")
            if d.get("warns"):
                print(f"    Warns: {', '.join(d['warns'][:3])}")

        # Governance
        g = r.get("governance", {})
        if g:
            sc = g.get("status_counts", {})
            sc_str = ", ".join(f"{k}={v}" for k, v in sorted(sc.items()))
            live = g.get("live_brains", [])
            print(f"  Governance: {g['total_brains']} brains ({sc_str})")
            if live:
                print(f"    Live: {', '.join(live)}")
            else:
                print("    Live: NONE — manual whitelist active")

        # Freshness
        f = r.get("freshness", {})
        stale = [(k, v) for k, v in f.items() if v is not None and v > 15]
        fresh_items = ", ".join(f"{k}={v:.0f}min" for k, v in f.items() if v is not None)
        print(f"  Freshness: {fresh_items}")
        if stale:
            print(f"    STALE: {', '.join(f'{k}={v:.0f}min' for k, v in stale)}")

    print(f"\n{'=' * 70}")
    print("  END OF REPORT")
    print(f"{'=' * 70}")


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(description="System health overview")
    parser.add_argument("--base-dir", nargs="+", default=["data", "data_btc"],
                        help="Base directories (default: data data_btc)")
    parser.add_argument("--label", nargs="+", default=["XAU", "BTC"],
                        help="Labels (default: XAU BTC)")
    args = parser.parse_args()

    results = []
    for base_dir, label in zip(args.base_dir, args.label):
        results.append(check_symbol(base_dir, label))

    print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
