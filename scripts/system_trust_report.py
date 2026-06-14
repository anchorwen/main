#!/usr/bin/env python
"""System Trust Report — deterministic, reproducible health check.

Iron Law #11 compliant: every statistic in this report comes from this
script's stdout.  No verbal reasoning, no sampling hallucination, no
agent inference.

Usage:
    python scripts/system_trust_report.py

Design rules (STR_SPEC_20260613):
    - Zero arguments — auto-detects data/ and data_btc/
    - Time window: journal recorded_at, not script wall-clock
    - Frozen contamination: weight>0=FAIL, weight=0=WARN
    - Cross-asset detection: prefix-based (BTC_/XAU_) + directory scan
    - VERDICT: CRITICAL(>=1 FAIL) / NEEDS REVIEW(>=1 WARN) / OK

Sections:
    1. Data Pipeline Integrity
    2. Brain Portfolio Health
    3. Frozen Brain Participation
    4. Trade Quality
    5. Runtime Status
    6. Config-Governance Alignment
    → VERDICT
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ── stdout encoding fix for Windows ──
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.now(UTC).replace(tzinfo=None)
NOW_ISO = NOW.isoformat()
CUTOFF_24H = (NOW - timedelta(hours=24)).isoformat()

# ── Data directory auto-detection ──
DATA_DIRS = {}
for label, candidate in [("XAU", "data"), ("BTC", "data_btc")]:
    p = ROOT / candidate
    if p.is_dir():
        DATA_DIRS[label] = p

# ── File freshness limits (minutes) ──
FRESH_LIMITS: dict[str, float] = {
    "execution_state.json": 30,
    "bar_sync_state.json": 30,
    "golden_master.jsonl": 120,
    "feature_store": 30,
    "leaderboard.json": 120,
    "daily_ops_state.json": 1440,
    "governance_state.json": 120,
    "data_health_state.json": 30,
    "live_labels.jsonl": 120,
    "live_trade_journal.jsonl": 30,
    "position_snapshots.jsonl": 30,
    "alert_audit.jsonl": 60,
    "exit_watchdog_alerts.jsonl": 120,
    "mt5_bridge_health.json": 5,
    "calibrator_feed_state.json": 120,
}

# ── Required data files per symbol ──
DATA_FILES = [
    "state/execution_state.json",
    "state/bar_sync_state.json",
    "state/daily_ops_state.json",
    "state/data_health_state.json",
    "golden_master.jsonl",
    "ledger_events.jsonl",
    "reports/leaderboard.json",
    "reports/live_labels.jsonl",
    "live_trade_journal.jsonl",
    "position_snapshots.jsonl",
    "logs/alert_audit.jsonl",
    "reports/exit_watchdog_alerts.jsonl",
    "reports/mt5_bridge_health.json",
    "calibrator_feed_state.json",
    "governance_state.json",
    "brain_pnl_ledger.json",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _utc_iso() -> str:
    return NOW_ISO


def _age_minutes(file_path: Path) -> float:
    try:
        return (NOW.timestamp() - file_path.stat().st_mtime) / 60
    except OSError:
        return 9999.0


def _read_json(path: Path) -> Any:
    """Read JSON with utf-8 encoding (Windows-safe)."""
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    """Read JSONL file, skip empty lines, utf-8 encoding."""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _load_all_data() -> dict:
    """Load all data files for all symbols into memory."""
    data: dict[str, dict[str, Any]] = {}
    for sym, base in DATA_DIRS.items():
        sym_data: dict[str, Any] = {}
        for rel in DATA_FILES:
            path = base / rel
            label = rel.replace("/", "_").replace(".jsonl", "").replace(".json", "")
            label = label.replace("state_", "").replace("reports_", "").replace("logs_", "")
            if path.exists():
                try:
                    if path.suffix == ".jsonl":
                        sym_data[label] = _read_jsonl(path)
                    else:
                        sym_data[label] = _read_json(path)
                except (json.JSONDecodeError, OSError) as exc:
                    sym_data[label] = {"_error": str(exc)}
            else:
                sym_data[label] = {"_missing": True}
        data[sym] = sym_data
    return data


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Data Pipeline Integrity
# ═══════════════════════════════════════════════════════════════════════════════

def section_1_pipeline(data: dict) -> dict:
    """Check file existence and freshness for all symbols."""
    results: dict[str, list[dict]] = {}
    for sym in DATA_DIRS:
        base = DATA_DIRS[sym]
        checks = []
        for rel in DATA_FILES:
            path = base / rel
            exists = path.exists()
            age = _age_minutes(path) if exists else None
            limit = None
            for key, lim in FRESH_LIMITS.items():
                if key in rel:
                    limit = lim
                    break
            stale = (age is not None and limit is not None and age > limit)
            checks.append({
                "file": rel,
                "exists": exists,
                "age_min": round(age, 1) if age else None,
                "limit_min": limit,
                "stale": stale,
            })
        results[sym] = checks

    # Feature store freshness (per-symbol)
    sym_feature_map = {"XAU": "symbol=XAUUSDc/timeframe=M5", "BTC": "symbol=BTCUSDc/timeframe=M5"}
    for sym in DATA_DIRS:
        base = DATA_DIRS[sym]
        tf_dir_name = sym_feature_map.get(sym, "")
        if tf_dir_name:
            fs_path = base / "feature_store" / "records" / tf_dir_name / "features.jsonl"
            fs_age = _age_minutes(fs_path)
            limit = FRESH_LIMITS["feature_store"]
            results[sym].append({
                "file": f"feature_store/{tf_dir_name}",
                "exists": fs_path.exists(),
                "age_min": round(fs_age, 1),
                "limit_min": limit,
                "stale": fs_age > limit,
            })
    return results


def _print_section_1(results: dict) -> list[str]:
    flags: list[str] = []
    print("── 1. DATA PIPELINE INTEGRITY ──")
    for sym in DATA_DIRS:
        checks = results.get(sym, [])
        missing = [c for c in checks if not c["exists"]]
        stale = [c for c in checks if c.get("stale")]
        print(f"  {sym}: {sum(1 for c in checks if c['exists'])}/{len(checks)} files exist"
              f" | missing={len(missing)} stale={len(stale)}")
        for s in stale[:5]:
            print(f"    STALE: {s['file']} — {s['age_min']:.0f}min (limit={s['limit_min']}min)")
        for m in missing:
            print(f"    MISSING: {m['file']}")
            flags.append(f"WARN|{sym}|missing_file:{m['file']}")
        if stale:
            flags.append(f"WARN|{sym}|{len(stale)}_stale_files")
        if not missing and not stale:
            flags.append(f"OK|{sym}|all_files_fresh")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: Brain Portfolio Health
# ═══════════════════════════════════════════════════════════════════════════════

def section_2_brain_portfolio(data: dict) -> dict:
    """Parse brain states from governance for all symbols."""
    portfolios: dict[str, list[dict]] = {}
    for sym in DATA_DIRS:
        gov = data.get(sym, {}).get("governance_state", {})
        if isinstance(gov, dict) and "_missing" not in gov:
            brain_states = gov.get("brain_states", {})
        else:
            brain_states = {}
        brains = []
        for bid, bs in brain_states.items():
            pm = bs.get("performance_metrics", {})
            brains.append({
                "brain_id": bid,
                "status": bs.get("status", "?"),
                "vote_weight": bs.get("vote_weight"),
                "pf": pm.get("profit_factor", 0),
                "wr": pm.get("win_rate", 0),
                "pnl_r": pm.get("pnl_r", 0),
                "trades": pm.get("total_trades", 0),
                "sharpe": pm.get("sharpe_ratio", 0),
            })
        brains.sort(key=lambda b: b["pnl_r"], reverse=True)
        portfolios[sym] = brains
    return portfolios


def _print_section_2(portfolios: dict, data: dict) -> list[str]:
    flags: list[str] = []
    print("── 2. BRAIN PORTFOLIO HEALTH ──")
    for sym in DATA_DIRS:
        brains = portfolios.get(sym, [])
        print(f"\n  {sym} ({len(brains)} brains):")
        print(f"    {'Brain ID':<40s} {'Status':<12s} {'PF':>6s} {'WR':>6s} {'PnL-R':>8s} {'Trades':>7s} {'VoteWt':>6s}")
        print(f"    {'-'*40} {'-'*12} {'-'*6} {'-'*6} {'-'*8} {'-'*7} {'-'*6}")
        for b in brains:
            vw = f"{b['vote_weight']:.1f}" if b['vote_weight'] is not None else "?"
            print(f"    {b['brain_id']:<40s} {b['status']:<12s} "
                  f"{b['pf']:>6.2f} {b['wr']:>6.3f} {b['pnl_r']:>8.1f} "
                  f"{b['trades']:>7d} {vw:>6s}")

        # Count live journal participation per brain
        journal = data.get(sym, {}).get("live_trade_journal", [])
        if isinstance(journal, dict):
            journal = []
        live_journal_trades: dict[str, int] = {}
        for entry in journal:
            if isinstance(entry, dict):
                for bid in (entry.get("brain_ids") or []):
                    live_journal_trades[bid] = live_journal_trades.get(bid, 0) + 1

        # Flags
        for b in brains:
            lj_trades = live_journal_trades.get(b["brain_id"], 0)
            if b["status"] == "live" and b["pf"] < 1.0 and b["trades"] > 0:
                print(f"    ⚠ LIVE_BRAIN_NEGATIVE_EV: {b['brain_id']} PF={b['pf']:.2f} trades={b['trades']}")
                flags.append(f"WARN|{sym}|live_negative_ev:{b['brain_id']}")
            if b["status"] == "live" and b["trades"] == 0 and lj_trades == 0:
                print(f"    ⚠ LIVE_BRAIN_NO_TRADES: {b['brain_id']} (0 training + 0 live trades)")
                flags.append(f"WARN|{sym}|live_no_trades:{b['brain_id']}")
            elif b["status"] == "live" and b["trades"] == 0 and lj_trades > 0:
                print(f"    ℹ LIVE_BRAIN_TRADING: {b['brain_id']} (0 training trades, {lj_trades} live journal entries)")
            if b["pf"] > 1.0 and b["status"] in ("candidate", "probation"):
                print(f"    ℹ PROFITABLE_NOT_PROMOTED: {b['brain_id']} PF={b['pf']:.2f} status={b['status']}")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: Frozen Brain Participation (critical gap)
# ═══════════════════════════════════════════════════════════════════════════════

def section_3_frozen_participation(data: dict, portfolios: dict) -> dict:
    """Scan recent 24h journal for frozen brain_ids in trade decisions.

    Time window: journal recorded_at timestamp (not script wall-clock).
    Weight distinction: vote_weight > 0 → FAIL, vote_weight = 0 → WARN.
    """
    results: dict[str, dict] = {}
    for sym in DATA_DIRS:
        frozen_brains = {}
        for b in portfolios.get(sym, []):
            if b["status"] == "frozen":
                frozen_brains[b["brain_id"]] = b["vote_weight"] or 0

        journal = data.get(sym, {}).get("live_trade_journal", [])
        if isinstance(journal, dict):
            journal = []

        total_closes_24h = 0
        closes_with_frozen = 0
        frozen_with_weight = 0
        frozen_weight_zero = 0
        frozen_ids_seen: set[str] = set()
        sample_entries: list[dict] = []

        for entry in journal:
            if not isinstance(entry, dict):
                continue
            recorded = entry.get("recorded_at", "")
            if recorded < CUTOFF_24H:
                continue
            if entry.get("action") != "close":
                continue
            total_closes_24h += 1
            brain_ids = entry.get("brain_ids") or []
            frozen_in = [bid for bid in brain_ids if bid in frozen_brains]
            if frozen_in:
                closes_with_frozen += 1
                frozen_ids_seen.update(frozen_in)
                for bid in frozen_in:
                    vw = frozen_brains.get(bid, 0)
                    if vw and vw > 0:
                        frozen_with_weight += 1
                    else:
                        frozen_weight_zero += 1
                if len(sample_entries) < 5:
                    sample_entries.append({
                        "time": recorded[:19],
                        "frozen_brains": frozen_in,
                        "total_brains": len(brain_ids),
                        "label": entry.get("label", "?"),
                    })

        contamination_pct = (closes_with_frozen / total_closes_24h * 100) if total_closes_24h > 0 else 0

        results[sym] = {
            "frozen_brains": frozen_brains,
            "total_closes_24h": total_closes_24h,
            "closes_with_frozen": closes_with_frozen,
            "contamination_pct": round(contamination_pct, 1),
            "frozen_ids_seen": frozen_ids_seen,
            "frozen_with_weight": frozen_with_weight,
            "frozen_weight_zero": frozen_weight_zero,
            "samples": sample_entries,
        }
    return results


def _print_section_3(results: dict) -> list[str]:
    flags: list[str] = []
    print("── 3. FROZEN BRAIN PARTICIPATION (last 24h) ──")
    for sym in DATA_DIRS:
        r = results.get(sym, {})
        frozen_brains = r.get("frozen_brains", {})
        print(f"\n  {sym}:")
        print(f"    Frozen brains in governance: {list(frozen_brains.keys()) if frozen_brains else '(none)'}")
        print(f"    Closes in 24h: {r['total_closes_24h']}")
        print(f"    Closes with frozen brain_ids: {r['closes_with_frozen']} ({r['contamination_pct']}%)")
        print(f"    Frozen with weight>0: {r['frozen_with_weight']}")
        print(f"    Frozen with weight=0: {r['frozen_weight_zero']}")
        for s in r.get("samples", [])[:3]:
            print(f"    SAMPLE: {s['time']} label={s['label']} "
                  f"frozen={s['frozen_brains']} of {s['total_brains']} brains")

        if r["contamination_pct"] > 0:
            if r["frozen_with_weight"] > 0:
                print(f"    ❌ FAIL: Frozen brains with vote_weight>0 in {sym} closes!")
                flags.append(f"FAIL|{sym}|frozen_contamination_weighted:{r['frozen_with_weight']}")
            else:
                print(f"    ⚠ WARN: Frozen brains in {sym} closes (all weight=0, audit residual)")
                flags.append(f"WARN|{sym}|frozen_contamination_zero_weight:{r['frozen_weight_zero']}")
        else:
            print("    ✅ No frozen brain contamination")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: Trade Quality
# ═══════════════════════════════════════════════════════════════════════════════

def section_4_trade_quality(data: dict) -> dict:
    """Compute trade quality metrics from journal.

    Dedup by position_ticket, take last close with non-null PnL.
    Win rate excl breakeven (PnL>0=win, PnL<0=loss, PnL==0=breakeven).
    """
    quality: dict[str, dict] = {}
    for sym in DATA_DIRS:
        journal = data.get(sym, {}).get("live_trade_journal", [])
        if isinstance(journal, dict):
            journal = []

        # Dedup by position_ticket
        ticket_closes: dict[str, dict] = {}
        for entry in journal:
            if not isinstance(entry, dict):
                continue
            ticket = entry.get("position_ticket")
            if not ticket:
                continue
            if entry.get("action") != "close":
                continue
            pnl = entry.get("pnl")
            if pnl is not None:
                ticket_closes[str(ticket)] = entry

        closes = list(ticket_closes.values())
        pnls = [c.get("pnl", 0) or 0 for c in closes]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        breakevens = [p for p in pnls if p == 0]

        total_pnl = sum(pnls)
        profit_factor = (abs(sum(wins)) / abs(sum(losses))) if sum(losses) != 0 else (float("inf") if sum(wins) > 0 else 0)
        wr = (len(wins) / (len(wins) + len(losses)) * 100) if (len(wins) + len(losses)) > 0 else 0

        # PnL by label
        by_label: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        for c in closes:
            label = c.get("label", "unknown")
            pnl = c.get("pnl", 0) or 0
            by_label[label]["count"] += 1
            by_label[label]["pnl"] += pnl
            if pnl > 0:
                by_label[label]["wins"] += 1
            elif pnl < 0:
                by_label[label]["losses"] += 1

        # Direction distribution
        sides: dict[str, int] = defaultdict(int)
        for entry in journal:
            if isinstance(entry, dict) and entry.get("action") in ("open", None) and entry.get("side"):
                sides[entry["side"]] += 1

        quality[sym] = {
            "total_trades": len(closes),
            "wins": len(wins),
            "losses": len(losses),
            "breakevens": len(breakevens),
            "win_rate_pct": round(wr, 1),
            "total_pnl_usd": round(total_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
            "max_win": round(max(wins), 2) if wins else 0,
            "max_loss": round(min(losses), 2) if losses else 0,
            "by_label": {k: dict(v) for k, v in sorted(by_label.items(), key=lambda x: x[1]["pnl"])},
            "sides": dict(sides),
        }
    return quality


def _print_section_4(quality: dict) -> list[str]:
    flags: list[str] = []
    print("── 4. TRADE QUALITY ──")
    for sym in DATA_DIRS:
        q = quality.get(sym, {})
        if not q or q["total_trades"] == 0:
            print(f"  {sym}: No trades")
            continue
        print(f"\n  {sym}: {q['total_trades']} trades | WR={q['win_rate_pct']:.1f}% | "
              f"PF={q['profit_factor']:.2f} | PnL=${q['total_pnl_usd']:+.2f}")
        print(f"    avg_win=${q['avg_win']:.2f} avg_loss=${q['avg_loss']:.2f} "
              f"max_win=${q['max_win']:.2f} max_loss=${q['max_loss']:.2f}")
        print(f"    Sides: {q.get('sides', {})}")
        print("    PnL by label:")
        for label, stats in q.get("by_label", {}).items():
            lbl = label or "(none)"
            print(f"      {lbl:<45s} count={stats['count']:>3d}  PnL=${stats['pnl']:>+10.2f}  "
                  f"W={stats['wins']:>3d} L={stats['losses']:>3d}")

        if q["win_rate_pct"] < 35 and q["total_trades"] >= 20:
            flags.append(f"WARN|{sym}|low_win_rate:{q['win_rate_pct']:.1f}%")
        if q["profit_factor"] < 1.0 and q["total_trades"] >= 20:
            flags.append(f"WARN|{sym}|negative_profit_factor:{q['profit_factor']:.2f}")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: Runtime Status
# ═══════════════════════════════════════════════════════════════════════════════

def section_5_runtime(data: dict) -> dict:
    """Check circuit breaker, budget, bridge health."""
    runtime: dict[str, dict] = {}
    for sym in DATA_DIRS:
        sym_data = data.get(sym, {})

        es = sym_data.get("execution_state", {})
        if isinstance(es, dict) and "_missing" not in es:
            cb_tripped = es.get("circuit_breaker_tripped", False)
            trip_reason = es.get("circuit_breaker_trip_reason", "")
            budgets = es.get("budgets", {})
        else:
            cb_tripped = False
            trip_reason = ""
            budgets = {}

        bridge = sym_data.get("mt5_bridge_health", {})
        if isinstance(bridge, dict) and "_missing" not in bridge:
            bridge_connected = bridge.get("mt5_connected", False)
            bridge_hb_age_s = (NOW - datetime.fromisoformat(
                bridge.get("last_heartbeat_utc", "2000-01-01T00:00:00").replace("Z", "+00:00")
            ).replace(tzinfo=None)).total_seconds()
            bridge_pid = bridge.get("pid", "?")
        else:
            bridge_connected = False
            bridge_hb_age_s = 99999
            bridge_pid = "?"

        dh = sym_data.get("data_health_state", {})
        if isinstance(dh, dict) and "_missing" not in dh:
            dh_overall = dh.get("overall_status", "UNKNOWN")
        else:
            dh_overall = "UNKNOWN"

        runtime[sym] = {
            "circuit_breaker_tripped": cb_tripped,
            "trip_reason": trip_reason,
            "budgets": budgets,
            "bridge_connected": bridge_connected,
            "bridge_hb_age_s": round(bridge_hb_age_s, 0),
            "bridge_pid": bridge_pid,
            "data_health_overall": dh_overall,
        }
    return runtime


def _print_section_5(runtime: dict) -> list[str]:
    flags: list[str] = []
    print("── 5. RUNTIME STATUS ──")
    for sym in DATA_DIRS:
        r = runtime.get(sym, {})
        print(f"\n  {sym}:")
        print(f"    Circuit breaker: {'TRIPPED' if r['circuit_breaker_tripped'] else 'NOT TRIPPED'}"
              f"{' — ' + r['trip_reason'] if r['trip_reason'] else ''}")
        print(f"    Bridge: connected={r['bridge_connected']} heartbeat={r['bridge_hb_age_s']:.0f}s ago PID={r['bridge_pid']}")
        print(f"    Data health overall: {r['data_health_overall']}")

        for budget_name, budget in r.get("budgets", {}).items():
            if isinstance(budget, dict):
                print(f"    Budget [{budget_name}]: "
                      f"daily_pnl={budget.get('daily_pnl_pct', 0):+.2f}% "
                      f"consL={budget.get('consecutive_losses', 0)} "
                      f"trades={budget.get('total_trades_today', 0)} "
                      f"paused={'YES' if budget.get('paused') else 'no'}")

        if r["circuit_breaker_tripped"]:
            flags.append(f"FAIL|{sym}|circuit_breaker_tripped:{r['trip_reason']}")
        if not r["bridge_connected"]:
            flags.append(f"FAIL|{sym}|bridge_disconnected")
        if r["bridge_hb_age_s"] > 120:
            flags.append(f"WARN|{sym}|bridge_heartbeat_stale:{r['bridge_hb_age_s']:.0f}s")
        for budget_name, budget in r.get("budgets", {}).items():
            if isinstance(budget, dict):
                if budget.get("consecutive_losses", 0) >= 5:
                    flags.append(f"WARN|{sym}|consecutive_losses:{budget.get('consecutive_losses')}")
                if budget.get("daily_pnl_pct", 0) < -3:
                    flags.append(f"WARN|{sym}|daily_pnl:{budget.get('daily_pnl_pct'):.1f}%")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6: Config-Governance Alignment
# ═══════════════════════════════════════════════════════════════════════════════

def section_6_config_alignment(portfolios: dict) -> dict:
    """Detect cross-asset brain contamination and config-gov mismatches.

    Cross-asset detection:
        1. brain_id with 'BTC_' prefix appearing in XAU config → contamination
        2. brain_id with 'XAU_' prefix appearing in BTC config → contamination
        3. No prefix → scan configs/brains/ vs configs/brains_btc/
    """
    alignment: dict[str, dict] = {"XAU": {}, "BTC": {}}

    # Collect brain_ids from live config YAML registry_entries
    config_brains: dict[str, set[str]] = {"XAU": set(), "BTC": set()}
    config_enabled: dict[str, dict[str, bool]] = {"XAU": {}, "BTC": {}}
    for sym, yaml_path in [("XAU", "configs/live.yaml"), ("BTC", "configs/live_btc.yaml")]:
        try:
            import yaml  # noqa: F401 — optional dependency
            with open(ROOT / yaml_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            entries = cfg.get("brains", {}).get("registry_entries", [])
            for entry in entries:
                if isinstance(entry, dict):
                    path = entry.get("path", "")
                    bid = path.replace("\\", "/").split("/")[-1].replace(".json", "")
                    if bid and "normalization" not in bid and "meta_stage" not in bid:
                        config_brains[sym].add(bid)
                        config_enabled[sym][bid] = entry.get("enabled", False)
        except Exception:
            # Fallback: scan configs/brains*/ directory
            cfg_dir = ROOT / "configs" / ("brains" if sym == "XAU" else "brains_btc")
            if cfg_dir.is_dir():
                for f in cfg_dir.glob("*.json"):
                    if "normalization" in f.name or "meta_stage" in f.name:
                        continue
                    config_brains[sym].add(f.stem)
                    config_enabled[sym][f.stem] = True

    # Collect governance brain_ids
    gov_brains: dict[str, set[str]] = {}
    for sym in DATA_DIRS:
        gov_brains[sym] = {b["brain_id"] for b in portfolios.get(sym, [])}

    # Cross-asset detection
    cross_asset: list[dict] = []
    for sym, cfg_ids in config_brains.items():
        other_sym = "BTC" if sym == "XAU" else "XAU"
        for bid in cfg_ids:
            # Prefix-based detection
            if bid.startswith("BTC_") and sym == "XAU":
                cross_asset.append({"brain_id": bid, "config_symbol": sym, "prefix_match": "BTC_ in XAU"})
            elif bid.startswith("XAU_") and sym == "BTC":
                cross_asset.append({"brain_id": bid, "config_symbol": sym, "prefix_match": "XAU_ in BTC"})
            # Directory-based: check if same brain_id exists in other symbol's gov
            elif bid in gov_brains.get(other_sym, set()):
                cross_asset.append({"brain_id": bid, "config_symbol": sym,
                                   "present_in_gov": other_sym})

    # Config-gov mismatches
    mismatches: dict[str, list[dict]] = {}
    for sym in DATA_DIRS:
        issues = []
        gov_set = gov_brains.get(sym, set())
        cfg_set = config_brains.get(sym, set())
        in_gov_not_cfg = sorted(gov_set - cfg_set)
        in_cfg_not_gov = sorted(cfg_set - gov_set)
        for bid in in_gov_not_cfg:
            b = next((b for b in portfolios.get(sym, []) if b["brain_id"] == bid), None)
            if b and b["status"] not in ("archived", "shadow"):
                issues.append({"type": "in_gov_not_config", "brain_id": bid, "status": b["status"]})
        for bid in in_cfg_not_gov:
            enabled = config_enabled.get(sym, {}).get(bid, False)
            issues.append({"type": "in_config_not_gov", "brain_id": bid, "config_enabled": enabled})
        # Additional: config enabled but gov frozen
        for bid in gov_set & config_brains.get(sym, set()):
            b = next((b for b in portfolios.get(sym, []) if b["brain_id"] == bid), None)
            enabled = config_enabled.get(sym, {}).get(bid, False)
            if b and b["status"] == "frozen" and enabled:
                issues.append({"type": "config_enabled_but_gov_frozen", "brain_id": bid})
        mismatches[sym] = issues

    alignment["cross_asset"] = cross_asset
    alignment["mismatches"] = mismatches
    alignment["config_brains"] = {s: sorted(c) for s, c in config_brains.items()}
    alignment["gov_brains"] = {s: sorted(g) for s, g in gov_brains.items()}
    return alignment


def _print_section_6(alignment: dict) -> list[str]:
    flags: list[str] = []
    print("── 6. CONFIG-GOVERNANCE ALIGNMENT ──")

    cross_asset = alignment.get("cross_asset", [])
    if cross_asset:
        print(f"\n  ❌ CROSS-ASSET CONTAMINATION ({len(cross_asset)}):")
        for ca in cross_asset:
            print(f"    {ca['brain_id']} in {ca['config_symbol']} config"
                  f"{' — ' + ca.get('prefix_match', '') if ca.get('prefix_match') else ''}"
                  f"{' — present in ' + ca.get('present_in_gov', '') + ' governance' if ca.get('present_in_gov') else ''}")
        flags.append(f"FAIL|cross_asset|{len(cross_asset)}_contaminated_brains")
    else:
        print("\n  ✅ No cross-asset brain contamination detected")


    mismatches = alignment.get("mismatches", {})
    for sym in DATA_DIRS:
        issues = mismatches.get(sym, [])
        if issues:
            print(f"\n  ⚠ {sym} config-gov mismatches ({len(issues)}):")
            for issue in issues:
                extra = ""
                if issue.get("status"):
                    extra += f" status={issue['status']}"
                if "config_enabled" in issue:
                    extra += f" config_enabled={issue['config_enabled']}"
                print(f"    {issue['type']}: {issue['brain_id']}{extra}")
            flags.append(f"WARN|{sym}|config_gov_mismatch:{len(issues)}")
        else:
            print(f"\n  {sym}: ✅ Config-governance aligned")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# VERDICT Aggregation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_verdict(all_flags: list[str]) -> dict:
    """Aggregate flags into VERDICT.

    Rules (STR_SPEC_20260613):
        - Any FAIL → CRITICAL
        - Zero FAIL, any WARN → NEEDS REVIEW
        - Zero FAIL, zero WARN → OK
    """
    fails = [f for f in all_flags if f.startswith("FAIL|")]
    warns = [f for f in all_flags if f.startswith("WARN|")]
    oks = [f for f in all_flags if f.startswith("OK|")]

    verdict = "CRITICAL" if fails else ("NEEDS REVIEW" if warns else "OK")
    return {
        "verdict": verdict,
        "fails": fails,
        "warns": warns,
        "oks": oks,
        "total_flags": len(all_flags),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6b: Candidate Signal Diversity (FIX-20260613-078)
# ═══════════════════════════════════════════════════════════════════════════════

def _check_signal_diversity(data: dict, portfolios: dict) -> list[str]:
    """Detect candidate/probation brains with >90% directional agreement.

    Uses ledger_events (SignalSettled) for per-brain direction distribution.
    Near-identical signals from two brains add no diversity to Parliament
    and amplify single-direction risk.
    """
    flags: list[str] = []
    print("\n── 6b. CANDIDATE SIGNAL DIVERSITY ──")
    from collections import Counter as _Counter
    for sym in DATA_DIRS:
        events = data.get(sym, {}).get("ledger_events", [])
        if isinstance(events, dict):
            events = []
        brain_dirs: dict[str, list[str]] = {}
        for e in events:
            if isinstance(e, dict) and e.get("event_type") == "SignalSettled":
                bid = e.get("brain_id", "")
                d = e.get("direction", "")
                if bid and d:
                    brain_dirs.setdefault(bid, []).append(d)
        candidates = [b for b in portfolios.get(sym, [])
                      if b["status"] in ("candidate", "probation") and b["trades"] >= 20]
        found = 0
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                a, b = candidates[i], candidates[j]
                a_dirs = brain_dirs.get(a["brain_id"], [])
                b_dirs = brain_dirs.get(b["brain_id"], [])
                if len(a_dirs) < 20 or len(b_dirs) < 20:
                    continue
                a_top = _Counter(a_dirs).most_common(1)[0]
                b_top = _Counter(b_dirs).most_common(1)[0]
                if a_top[0] == b_top[0] and a_top[0] in ("long", "short"):
                    agree_a = a_top[1] / len(a_dirs) * 100
                    agree_b = b_top[1] / len(b_dirs) * 100
                    if agree_a > 90 and agree_b > 90:
                        print(f"  ⚠ {sym}: {a['brain_id']} & {b['brain_id']} "
                              f"both {min(agree_a, agree_b):.0f}%+ {a_top[0]} — near-identical, low diversity")
                        flags.append(f"WARN|{sym}|candidate_signal_cloning:{a['brain_id']}+{b['brain_id']}")
                        found += 1
        if found == 0:
            print(f"  {sym}: ✅ No candidate signal cloning detected")
    return flags


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    print("=" * 80)
    print(f"  SYSTEM TRUST REPORT — {NOW_ISO[:19]}")
    print("  Iron Law #11 Compliant — All statistics from script stdout")
    print("=" * 80)

    # Load all data
    data = _load_all_data()

    # Run all sections
    all_flags: list[str] = []

    # Section 1: Pipeline
    s1 = section_1_pipeline(data)
    all_flags.extend(_print_section_1(s1))

    # Section 2: Brain Portfolio
    portfolios = section_2_brain_portfolio(data)
    all_flags.extend(_print_section_2(portfolios, data))

    # Section 3: Frozen Participation
    s3 = section_3_frozen_participation(data, portfolios)
    all_flags.extend(_print_section_3(s3))

    # Section 4: Trade Quality
    s4 = section_4_trade_quality(data)
    all_flags.extend(_print_section_4(s4))

    # Section 5: Runtime
    s5 = section_5_runtime(data)
    all_flags.extend(_print_section_5(s5))

    # Section 6: Config Alignment
    s6 = section_6_config_alignment(portfolios)
    all_flags.extend(_print_section_6(s6))

    # Section 6b: Candidate signal diversity (FIX-20260613-078)
    all_flags.extend(_check_signal_diversity(data, portfolios))

    # VERDICT
    verdict = compute_verdict(all_flags)
    print(f"\n{'=' * 80}")
    print(f"  VERDICT: {verdict['verdict']}")
    print(f"  FAILs: {len(verdict['fails'])} | WARNs: {len(verdict['warns'])} | OKs: {len(verdict['oks'])}")
    for f in verdict["fails"]:
        print(f"    ❌ {f}")
    for w in verdict["warns"]:
        print(f"    ⚠ {w}")
    print(f"{'=' * 80}")

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 1 if verdict["verdict"] == "CRITICAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
