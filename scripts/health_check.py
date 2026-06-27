#!/usr/bin/env python3
"""System Health Check — Iron Law #11 compliant unified diagnostics.

This is the ONLY authorised entry point for live trading system status queries.
Every number printed to stdout is traceable to its source file and dedup logic.
No inference. No interpretation. No second-guessing.

Usage:
    python scripts/health_check.py --data-dir data_btc
    python scripts/health_check.py --data-dir data_btc --json

Architecture:
    - Trade stats:     ticket-level dedup (same logic as analyze_live_journal.py)
    - State freshness: Plan B catalog TTL (same TTL values as core/state/catalog.py)
    - Governance:      governance_state.json + intent log cross-reference
    - Observability:   intent log scan (scaler, NaN, inference errors)
    - Process status:  file-liveness check (bridge heartbeat, intent cycles)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

UTC = _dt.UTC

# ── Plan B Catalog TTL (from core/state/catalog.py — DO NOT DRIFT) ──
CATALOG_TTL: dict[str, int] = {
    "governance_state.json": 14400,
    "state/daily_ops_state.json": 14400,
    "state/execution_state.json": 1800,
    "state/data_health_state.json": 14400,
    "state/alert_cooling.json": 7200,
    "reports/leaderboard.json": 14400,
    "reports/alpha_allocation.json": 14400,
    "reports/training_readiness.json": 86400,
    "reports/retraining_signal_prev.json": 86400,
    "reports/leaderboard_prev.json": 172800,
    "reports/mt5_bridge_health.json": 900,
    "alpha_registry.json": 14400,
    "alpha_performance.json": 14400,
    "alpha_feed_state.json": 14400,
    "calibrator_feed_state.json": 14400,
    "brain_pnl_ledger.json": 14400,
}

# ── Helpers ────────────────────────────────────────────────────────────────


def _ts() -> str:
    """UTC timestamp for report header."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    """UTC date string for filtering."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _read_json(path: Path) -> dict | None:
    """Read a JSON file, return None on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file, skip blank/broken lines."""
    records: list[dict] = []
    if not path.exists():
        return records
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return records


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Process Liveness
# ═══════════════════════════════════════════════════════════════════════════════


def _check_bridge(data_dir: Path) -> tuple[str, str]:
    """Check MT5 bridge health from heartbeat file."""
    health = _read_json(data_dir / "reports" / "mt5_bridge_health.json")
    if not health:
        return "MISSING", "mt5_bridge_health.json not found"
    hb = health.get("last_heartbeat_utc", "")
    connected = health.get("mt5_connected", False)
    pending = health.get("outbox_pending", None)
    if not connected:
        return "DOWN", "mt5_connected=false"
    # Age check
    try:
        hb_dt = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
        age_s = (datetime.now(UTC) - hb_dt).total_seconds()
        if age_s > 120:
            return "STALE", f"heartbeat {age_s:.0f}s old"
    except (ValueError, TypeError):
        pass
    extra = f"pending={pending}" if pending else ""
    return "OK", extra


def _check_intent(data_dir: Path) -> tuple[str, str]:
    """Check intent loop liveness from latest log."""
    logs = sorted((data_dir / "logs").glob("intent_*.log"))
    if not logs:
        return "MISSING", "no intent log"
    # Read last few lines for latest cycle_end
    latest = logs[-1]
    try:
        with open(latest, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return "ERROR", f"cannot read {latest.name}"
    # Find last cycle_end or cycle_start
    last_time = None
    for line in reversed(lines):
        if "cycle_end" in line or "cycle_start" in line:
            try:
                evt = json.loads(line.strip())
                ts = evt.get("time", "")
                last_time = ts
            except json.JSONDecodeError:
                pass
            break
    if not last_time:
        return "NO_CYCLES", "no cycle events found"
    try:
        evt_dt = datetime.fromisoformat(str(last_time).replace("Z", "+00:00"))
        age_s = (datetime.now(UTC) - evt_dt).total_seconds()
        if age_s > 300:
            return "STALE", f"last cycle {age_s:.0f}s ago"
    except (ValueError, TypeError):
        pass
    return "OK", f"last cycle @ {str(last_time)[:19]}"


def _check_launcher(data_dir: Path) -> tuple[str, str]:
    """Check launcher liveness from latest log."""
    logs = sorted((data_dir / "logs").glob("live_launcher_*.log"))
    if not logs:
        return "MISSING", "no launcher log"
    latest = logs[-1]
    try:
        mtime = os.path.getmtime(latest)
        age_s = time_now() - mtime
        if age_s > 600:
            return "STALE", f"log {age_s:.0f}s old"
    except OSError:
        return "ERROR", f"cannot stat {latest.name}"
    return "OK", ""


def time_now() -> float:
    return datetime.now(UTC).timestamp()


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: State Freshness (Plan B catalog)
# ═══════════════════════════════════════════════════════════════════════════════


def _check_freshness(data_dir: Path) -> dict[str, Any]:
    """Check all catalog artifacts against TTL."""
    now = time_now()
    results: dict[str, Any] = {"ok": 0, "stale": 0, "missing": 0, "empty": 0, "items": []}

    for rel_path, ttl_s in sorted(CATALOG_TTL.items()):
        fp = data_dir / rel_path
        if not fp.exists():
            results["missing"] += 1
            results["items"].append({"path": rel_path, "status": "MISSING"})
            continue
        size = fp.stat().st_size
        if size == 0:
            results["empty"] += 1
            results["items"].append({"path": rel_path, "status": "EMPTY"})
            continue
        mtime = os.path.getmtime(fp)
        age_m = (now - mtime) / 60
        if ttl_s > 0 and age_m > ttl_s / 60:
            results["stale"] += 1
            results["items"].append(
                {"path": rel_path, "status": "STALE", "age_m": int(age_m), "ttl_m": ttl_s // 60}
            )
        else:
            results["ok"] += 1
            results["items"].append({"path": rel_path, "status": "OK", "age_m": int(age_m)})

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: Governance vs Execution Sync
# ═══════════════════════════════════════════════════════════════════════════════


def _check_governance(data_dir: Path) -> dict[str, Any]:
    """Read governance state and cross-reference with executing brains."""
    gov = _read_json(data_dir / "governance_state.json")
    if not gov:
        return {"error": "governance_state.json not readable"}

    bs = gov.get("brain_states", {})
    status_dist: dict[str, int] = Counter()
    live_ids: list[str] = []
    probation_ids: list[str] = []
    frozen_ids: list[str] = []
    retired_ids: list[str] = []

    for bid, b in bs.items():
        st = b.get("status", "?") if isinstance(b, dict) else str(b)
        status_dist[st] = status_dist.get(st, 0) + 1
        if st == "live":
            live_ids.append(bid)
        elif st == "probation":
            probation_ids.append(bid)
        elif st == "frozen":
            frozen_ids.append(bid)
        elif st == "retired":
            retired_ids.append(bid)

    # ── Execution sync: find executing brains from intent log ──
    executing_ids: list[str] = []
    executing_penalized: list[str] = []
    sync_status = "UNKNOWN"

    logs = sorted((data_dir / "logs").glob("intent_*.log"))
    if logs:
        latest = logs[-1]
        # Find the most recent live_intent_loop_start event
        try:
            with open(latest, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if "live_intent_loop_start" in line:
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        gf = evt.get("governance_filter", {})
                        executing_ids = gf.get("kept", [])
                        executing_penalized = [
                            p.get("brain_id", "") for p in gf.get("penalized", [])
                        ]
                        break
        except OSError:
            pass

    # Sync check: every live brain should appear in executing_ids
    if not executing_ids:
        sync_status = "NO_EXECUTING_DATA"
    else:
        live_in_exec = [bid for bid in live_ids if bid in executing_ids]
        missing_from_exec = [bid for bid in live_ids if bid not in executing_ids]
        if missing_from_exec:
            sync_status = (
                f"MISMATCH: {len(missing_from_exec)} live brains NOT executing: {missing_from_exec}"
            )
        else:
            sync_status = "VALID"

    return {
        "total_brains": len(bs),
        "status_distribution": dict(status_dist),
        "live": live_ids,
        "probation": probation_ids,
        "frozen": frozen_ids,
        "retired": retired_ids,
        "executing": executing_ids,
        "executing_penalized": executing_penalized,
        "sync_status": sync_status,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4+5: Trade Stats (ticket-level dedup)
# ═══════════════════════════════════════════════════════════════════════════════


def _check_trades(data_dir: Path) -> dict[str, Any]:
    """Today's trades with ticket-level dedup (same logic as analyze_live_journal.py).

    Dedup rule: group by position_ticket, last close with non-null PnL = realized outcome.
    """
    journal_path = data_dir / "live_trade_journal.jsonl"
    records = _read_jsonl(journal_path)

    today_str = _today()

    # ── Group by position_ticket ──
    tickets = defaultdict(list)
    orphan_events = []
    for r in records:
        pt = r.get("position_ticket")
        if pt:
            tickets[pt].append(r)
        else:
            orphan_events.append(r)

    # ── Dedup: realized trades ──
    all_realized: list[dict] = []
    today_realized: list[dict] = []
    today_open_unclosed: list[dict] = []

    for ticket, events in tickets.items():
        close_events = [
            e for e in events if e.get("action") == "close" and e.get("pnl") is not None
        ]
        if not close_events:
            # Open but never closed
            open_evt = next((e for e in events if e.get("action") == "open"), None)
            if open_evt and today_str in str(open_evt.get("recorded_at", "")):
                today_open_unclosed.append(open_evt)
            continue

        final_close = close_events[-1]
        open_evt = next((e for e in events if e.get("action") == "open"), None)

        realized = {
            "ticket": ticket,
            "open_time": str(open_evt.get("recorded_at", ""))[:19] if open_evt else "?",
            "close_time": str(final_close.get("recorded_at", ""))[:19],
            "side": final_close.get("side", "?"),
            "pnl": final_close.get("pnl", 0.0),
            "label": final_close.get("label", "?"),
            "entry_price": (
                open_evt.get("detail", {}).get("request", {}).get("price") if open_evt else None
            ),
            "entry_sl": open_evt.get("sl") if open_evt else None,
            "entry_tp": open_evt.get("tp") if open_evt else None,
        }
        all_realized.append(realized)
        if today_str in str(final_close.get("recorded_at", "")):
            today_realized.append(realized)

    # ── Today stats ──
    today_opens_raw = [
        r
        for r in records
        if r.get("action") == "open" and today_str in str(r.get("recorded_at", ""))
    ]
    today_closes_raw = [
        r
        for r in records
        if r.get("action") == "close" and today_str in str(r.get("recorded_at", ""))
    ]

    today_wins = [r for r in today_realized if r["pnl"] > 0]
    today_losses = [r for r in today_realized if r["pnl"] < 0]
    today_be = [r for r in today_realized if r["pnl"] == 0]
    today_pnl = sum(r["pnl"] for r in today_realized)

    return {
        "journal_entries": len(records),
        "unique_tickets": len(tickets),
        "orphan_events": len(orphan_events),
        "all_time_settled": len(all_realized),
        "today": {
            "date": today_str,
            "raw_opens": len(today_opens_raw),
            "raw_closes": len(today_closes_raw),
            "settled": len(today_realized),
            "open_unclosed": len(today_open_unclosed),
            "wins": len(today_wins),
            "losses": len(today_losses),
            "breakevens": len(today_be),
            "total_pnl": round(today_pnl, 2),
            "details": today_realized,
            "unclosed": [
                {
                    "ticket": r.get("position_ticket"),
                    "side": r.get("side"),
                    "entry": r.get("detail", {}).get("request", {}).get("price"),
                    "sl": r.get("sl"),
                    "tp": r.get("tp"),
                }
                for r in today_open_unclosed
            ],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6: Observability (scaler, NaN, inference errors)
# ═══════════════════════════════════════════════════════════════════════════════


def _check_observability(data_dir: Path) -> dict[str, Any]:
    """Scan intent log for scaler vitality, NaN, and inference errors."""
    logs = sorted((data_dir / "logs").glob("intent_*.log"))
    if not logs:
        return {"error": "no intent log"}

    latest = logs[-1]

    scaler_loaded = False
    conformal_warm = False
    nan_errors = 0
    inference_errors: dict[str, int] = Counter()
    calibrator_loaded = False
    last_nan_time = None

    try:
        with open(latest, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_name = evt.get("event", "")

                if event_name == "meta_pipeline_wired":
                    if evt.get("micro_scaler_loaded"):
                        scaler_loaded = True
                    if evt.get("conformal_warm"):
                        conformal_warm = True
                    if evt.get("calibrator_loaded"):
                        calibrator_loaded = True

                if "NaN detected" in str(evt.get("error", "")) or "NaN" in str(evt):
                    nan_errors += 1
                    last_nan_time = evt.get("time", last_nan_time)

                if event_name == "brain_inference_error":
                    bid = evt.get("brain_id", "?")
                    err = evt.get("error", "?")
                    inference_errors[f"{bid}: {err}"] += 1
    except OSError:
        return {"error": f"cannot read {latest.name}"}

    return {
        "scaler_loaded": scaler_loaded,
        "conformal_warm": conformal_warm,
        "calibrator_loaded": calibrator_loaded,
        "nan_errors": nan_errors,
        "last_nan_time": last_nan_time,
        "inference_errors": dict(inference_errors),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Section 7: Alerts (24h)
# ═══════════════════════════════════════════════════════════════════════════════


def _check_alerts(data_dir: Path) -> dict[str, Any]:
    """Scan intent log for ERROR/CRITICAL events in the latest session."""
    logs = sorted((data_dir / "logs").glob("intent_*.log"))
    if not logs:
        return {"error": "no intent log"}

    latest = logs[-1]
    critical: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []

    try:
        with open(latest, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                text_lower = line.lower()
                if "critical" in text_lower and ("error" in text_lower or "event" in text_lower):
                    critical.append(line[:200])
                elif '"event"' in line and "error" in text_lower:
                    # Try to extract event name
                    try:
                        evt = json.loads(line)
                        evt_name = evt.get("event", "")
                        if "error" in evt_name.lower() or "degraded" in evt_name.lower():
                            errors.append(f"{evt_name}: {str(evt)[:150]}")
                    except json.JSONDecodeError:
                        if "ERROR" in line:
                            errors.append(line[:200])
                elif "warning" in text_lower and '"event"' in line:
                    try:
                        evt = json.loads(line)
                        evt_name = evt.get("event", "")
                        if "warning" in evt_name.lower():
                            warnings.append(f"{evt_name}: {str(evt)[:150]}")
                    except json.JSONDecodeError:
                        pass
    except OSError:
        return {"error": f"cannot read {latest.name}"}

    return {
        "critical_count": len(critical),
        "warning_count": len(warnings),
        "error_count": len(errors),
        "critical_samples": critical[-3:] if critical else [],
        "warning_samples": warnings[-3:] if warnings else [],
        "error_samples": errors[-3:] if errors else [],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Output Formatter
# ═══════════════════════════════════════════════════════════════════════════════


def _format_age(m: int) -> str:
    """Human-readable age."""
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 24:
        return f"{h}h"
    return f"{h // 24}d"


def print_report(
    proc: dict, fresh: dict, gov: dict, trades: dict, obs: dict, alerts: dict, data_dir: str
) -> None:
    """Print the unified health report to stdout."""
    sep = "=" * 75
    sym = "BTC" if "btc" in data_dir.lower() else "XAU"

    print(sep)
    print(f"  SYSTEM HEALTH REPORT: {data_dir} ({sym}) | UTC: {_ts()}")
    print(sep)

    # ── [1] PROCESSORS ──
    bridge_status, bridge_note = proc.get("bridge", ("?", ""))
    intent_status, intent_note = proc.get("intent", ("?", ""))
    launcher_status, launcher_note = proc.get("launcher", ("?", ""))

    b_flag = "[OK]" if bridge_status == "OK" else f"[{bridge_status}]"
    i_flag = "[OK]" if intent_status == "OK" else f"[{intent_status}]"
    l_flag = "[OK]" if launcher_status == "OK" else f"[{launcher_status}]"

    print(f"  [1] PROCESSORS      : Bridge {b_flag} | Intent {i_flag} | Launcher {l_flag}")
    if bridge_note:
        print(f"                        Bridge: {bridge_note}")
    if intent_note:
        print(f"                        Intent: {intent_note}")
    if launcher_note:
        print(f"                        Launcher: {launcher_note}")

    # ── [2] STATE FRESHNESS ──
    f_ok = fresh.get("ok", 0)
    f_stale = fresh.get("stale", 0)
    f_missing = fresh.get("missing", 0)
    f_empty = fresh.get("empty", 0)
    total_artifacts = f_ok + f_stale + f_missing + f_empty

    freshness_parts = [f"{f_ok}/{total_artifacts} OK"]
    if f_stale:
        freshness_parts.append(f"{f_stale} STALE")
    if f_missing:
        freshness_parts.append(f"{f_missing} MISSING")
    if f_empty:
        freshness_parts.append(f"{f_empty} EMPTY")
    print(f"  [2] STATE FRESHNESS : {', '.join(freshness_parts)}")
    for item in fresh.get("items", []):
        if item["status"] != "OK":
            age_str = _format_age(item.get("age_m", 0))
            ttl_str = _format_age(item.get("ttl_m", 0)) if "ttl_m" in item else ""
            ttl_info = f" (TTL={ttl_str})" if ttl_str else ""
            print(
                f"                        {item['status']}: {item['path']} ({age_str} old{ttl_info})"
            )

    # ── [3] GOVERNANCE ──
    if gov.get("error"):
        print(f"  [3] GOVERNANCE      : ERROR — {gov['error']}")
    else:
        live_list = gov.get("live", [])
        prob_list = gov.get("probation", [])
        frozen_list = gov.get("frozen", [])
        retired_list = gov.get("retired", [])
        sync = gov.get("sync_status", "?")

        gov_parts = [
            f"LIVE {live_list}",
            f"PROBATION {prob_list}",
        ]
        if frozen_list:
            gov_parts.append(f"FROZEN {frozen_list}")
        if retired_list:
            gov_parts.append(f"RETIRED {retired_list}")
        gov_parts.append(f"(Sync: {sync})")
        print(f"  [3] GOVERNANCE      : {' | '.join(gov_parts)}")

    # ── [4] TODAY'S TRADES ──
    td = trades.get("today", {})
    settled = td.get("settled", 0)
    unclosed = td.get("open_unclosed", 0)
    raw_opens = td.get("raw_opens", 0)

    trade_parts = [f"{settled} Settled"]
    if unclosed:
        trade_parts.append(f"{unclosed} Open (unclosed)")
    trade_parts.append(f"({raw_opens} raw open events)")
    print(f"  [4] TODAY'S TRADES  : {', '.join(trade_parts)}")
    print(
        f"                        Date: {td.get('date', '?')} UTC | Dedup: position_ticket groupby"
    )

    details = td.get("details", [])
    if details:
        w = td.get("wins", 0)
        l = td.get("losses", 0)
        be = td.get("breakevens", 0)
        print(f"                        W={w} L={l} BE={be}")
        print(
            f"                        {'Ticket':>12s}  {'Close':<19s}  {'Side':>5s}  {'PnL':>8s}  Label"
        )
        for r in sorted(details, key=lambda x: x["close_time"]):
            print(
                f"                        {r['ticket']:>12}  {r['close_time']:<19s}  "
                f"{r['side']:>5s}  {r['pnl']:>+8.2f}  {r['label'][:45]}"
            )

    unclosed_detail = td.get("unclosed", [])
    if unclosed_detail:
        print("\n                        ── Open positions (unclosed) ──")
        for u in unclosed_detail:
            print(
                f"                        ticket={u['ticket']}  side={u['side']}  "
                f"entry={u['entry']}  sl={u['sl']}  tp={u['tp']}"
            )

    # ── [5] TODAY'S PNL ──
    pnl = td.get("total_pnl", 0)
    all_settled = trades.get("all_time_settled", 0)
    print(
        f"  [5] TODAY'S PNL     : ${pnl:+.2f} (journal close PnL, {all_settled} all-time settled)"
    )
    print(
        "                        [NOTE] PnL source: live_trade_journal.jsonl — MT5 deal verification TBD"
    )

    # ── [6] OBSERVABILITY ──
    scaler = "Loaded" if obs.get("scaler_loaded") else "NOT LOADED"
    conformal = "Warm" if obs.get("conformal_warm") else "Cold"
    calib = "Loaded" if obs.get("calibrator_loaded") else "Not Loaded"
    nan_count = obs.get("nan_errors", 0)
    nan_flag = f"{nan_count} NaN errors" if nan_count else "No NaN detected"
    inf_errs = obs.get("inference_errors", {})
    inf_flag = f"{len(inf_errs)} types" if inf_errs else "None"

    print(
        f"  [6] OBSERVABILITY   : Scaler [{scaler}] | Conformal [{conformal}] | Calibrator [{calib}]"
    )
    print(f"                        NaN: {nan_flag} | Inference Errors: {inf_flag}")
    if inf_errs:
        for err_str, count in sorted(inf_errs.items(), key=lambda x: -x[1])[:3]:
            print(f"                        [{count}x] {err_str[:100]}")

    # ── [7] ALERTS ──
    crit = alerts.get("critical_count", 0)
    warn = alerts.get("warning_count", 0)
    err = alerts.get("error_count", 0)
    print(f"  [7] ALERTS (session): {crit} CRITICAL | {warn} WARN | {err} ERROR")
    for s in alerts.get("error_samples", []):
        print(f"                        {s[:140]}")
    for s in alerts.get("critical_samples", []):
        print(f"                        {s[:140]}")

    print(sep)
    all_ok = (
        bridge_status == "OK"
        and intent_status == "OK"
        and f_stale == 0
        and gov.get("sync_status") == "VALID"
        and nan_count == 0
    )
    verdict = "[OK] ALL SYSTEMS NOMINAL" if all_ok else "[NEEDS REVIEW] — check flagged items above"
    print(f"  VERDICT: {verdict}")
    print(sep)


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> int:
    # ── Windows GBK workaround (FIX-20260611-022) ──
    import io as _io

    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="System Health Check — Iron Law #11 compliant unified diagnostics"
    )
    parser.add_argument("--data-dir", default="data_btc", help="Symbol data directory")
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        script_dir = Path(__file__).resolve().parent
        repo_root = script_dir.parent
        data_dir = repo_root / data_dir

    if not data_dir.exists():
        print(f"ERROR: data directory not found: {data_dir}")
        return 1

    # ── Collect all checks ──
    proc = {
        "bridge": _check_bridge(data_dir),
        "intent": _check_intent(data_dir),
        "launcher": _check_launcher(data_dir),
    }
    fresh = _check_freshness(data_dir)
    gov = _check_governance(data_dir)
    trades = _check_trades(data_dir)
    obs = _check_observability(data_dir)
    alerts = _check_alerts(data_dir)

    if args.json:
        result = {
            "report_time": _ts(),
            "data_dir": str(data_dir),
            "processors": {k: {"status": v[0], "detail": v[1]} for k, v in proc.items()},
            "state_freshness": fresh,
            "governance": gov,
            "trades": trades,
            "observability": obs,
            "alerts": alerts,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print_report(proc, fresh, gov, trades, obs, alerts, args.data_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
