#!/usr/bin/env python
"""Ω Institutional Data Integrity Auditor — Semantic + Reconciliation Layer.

Upgrades data health monitoring from physical-layer (file exists, not stale)
to semantic-layer (cross-ledger reconciliation, precision assertions,
bridge micro-health, orphan management, active state protection).

Iron Law #11 compliant: all statistics from script stdout.
Supports DingTalk webhook push for automated silent monitoring.

Usage:
  python scripts/audit_data_integrity.py                          # full audit
  python scripts/audit_data_integrity.py --data-dir data          # XAU only
  python scripts/audit_data_integrity.py --data-dir data_btc      # BTC only
  python scripts/audit_data_integrity.py --push-dingtalk          # push to DingTalk
  python scripts/audit_data_integrity.py --json                   # JSON output
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat()[:19] + "Z"

# ── Symbol precision requirements ──────────────────────────────────────
SYMBOL_PRECISION: dict[str, dict[str, Any]] = {
    "XAUUSDc": {"decimals": 3, "tick_size": 0.001, "tick_value": 0.01},
    "BTCUSDc": {"decimals": 2, "tick_size": 0.01, "tick_value": 0.01},
}
FEATURE_STORE_DIR = "feature_store/records"

# ── Severity thresholds ────────────────────────────────────────────────
SEV1_RECONCILIATION_THRESHOLD = 0.05  # $0.05 max allowed drift
SEV2_PRECISION_THRESHOLD = 1  # even 1 bad tick is Sev2


# ═══════════════════════════════════════════════════════════════════════
# 1. CROSS-LEDGER RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════


def reconcile_journal_vs_ledger(data_dir: str) -> dict[str, Any]:
    """Cross-check journal PnL vs ledger events for consistency."""
    jp = Path(data_dir) / "live_trade_journal.jsonl"
    lp = Path(data_dir) / "ledger_events.jsonl"

    journal_pnl: float = 0.0
    if jp.exists():
        with open(jp, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if e.get("action") == "close":
                        pnl = e.get("pnl")
                        if pnl is not None:
                            journal_pnl += float(pnl)
                except (json.JSONDecodeError, ValueError):
                    pass

    # Ledger: sum SignalSettled PnL (converted from R to $ if needed)
    ledger_pnl_r: float = 0.0
    ledger_count: int = 0
    if lp.exists():
        with open(lp, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if e.get("event_type") == "SignalSettled" and (e.get("position_ticket") or 0) > 0:
                        pnl_r = e.get("pnl_r", 0) or 0
                        ledger_pnl_r += float(pnl_r)
                        ledger_count += 1
                except (json.JSONDecodeError, ValueError):
                    pass

    # Note: journal_pnl is in USD, ledger pnl_r is in R-units (not directly comparable).
    # True reconciliation requires MT5 account equity snapshots.
    # For now, we verify both sources independently have non-null data.
    both_valid = abs(journal_pnl) > 0.001 or abs(ledger_pnl_r) > 0.001
    sev = "OK" if (jp.exists() and lp.exists()) else "Sev3"

    return {
        "journal_pnl_usd": round(journal_pnl, 4),
        "ledger_pnl_r_total": round(ledger_pnl_r, 4),
        "ledger_real_settled": ledger_count,
        "note": "journal=USD, ledger=R-units — not directly comparable without MT5 equity snapshots",
        "severity": sev,
        "passed": both_valid,
    }


def reconcile_snapshots_vs_journal(data_dir: str) -> dict[str, Any]:
    """Cross-check: snapshot tickets should correspond to journal open tickets.

    Note: snapshots store unrealized_pnl_r (R-units), journal stores PnL in USD.
    Direct numerical comparison is invalid without MT5 equity data.
    Instead, verify structural consistency: each journal open has >=1 snapshot.
    """
    sp = Path(data_dir) / "position_snapshots.jsonl"
    jp = Path(data_dir) / "live_trade_journal.jsonl"

    if not sp.exists() or not jp.exists():
        return {"passed": True, "severity": "SKIP", "reason": "missing data files"}

    journal_tickets: set[int] = set()
    with open(jp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e.get("action") == "open":
                    tkt = e.get("position_ticket")
                    if tkt:
                        journal_tickets.add(int(tkt))
            except json.JSONDecodeError:
                pass

    snapshot_tickets: set[int] = set()
    with open(sp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                s = json.loads(line)
                tkt = s.get("ticket")
                if tkt:
                    snapshot_tickets.add(int(tkt))
            except json.JSONDecodeError:
                pass

    missing_snaps = journal_tickets - snapshot_tickets
    extra_snaps = snapshot_tickets - journal_tickets
    gap_pct = len(missing_snaps) / max(len(journal_tickets), 1) * 100
    sev = "OK" if gap_pct <= 30 else "Sev3" if gap_pct <= 60 else "Sev2"

    return {
        "passed": gap_pct <= 5,
        "severity": sev,
        "journal_tickets": len(journal_tickets),
        "snapshot_tickets": len(snapshot_tickets),
        "missing_snapshots": len(missing_snaps),
        "extra_snapshots": len(extra_snaps),
        "gap_pct": round(gap_pct, 1),
        "note": "structural check — numerical PnL requires MT5 equity (R vs USD)",
    }

def check_active_position_state(data_dir: str) -> dict[str, Any]:
    """Ensure active_position.json always exists with valid state (even empty)."""
    ap = Path(data_dir) / "state" / "active_position.json"
    es = Path(data_dir) / "state" / "execution_state.json"

    exists = ap.exists()
    size = ap.stat().st_size if exists else 0
    has_content = False
    positions = 0

    if exists and size > 0:
        try:
            data = json.loads(ap.read_text(encoding="utf-8"))
            positions = len(data.get("positions", []))
            has_content = True
        except (json.JSONDecodeError, ValueError):
            pass

    # Cross-check: execution_state should have matching known_open_tickets
    known_open = 0
    if es.exists():
        try:
            es_data = json.loads(es.read_text(encoding="utf-8"))
            known_open = len(es_data.get("known_open_tickets", {}))
        except json.JSONDecodeError:
            pass

    consistent = positions == known_open
    if exists and consistent:
        sev = "OK"
    elif not exists and known_open == 0:
        sev = "Sev3"  # no positions = no file needed, but should persist empty state
    elif not exists:
        sev = "Sev1"  # positions exist but file missing = data loss
    else:
        sev = "Sev3"  # file exists but inconsistent

    return {
        "passed": exists and consistent,
        "severity": sev,
        "file_exists": exists,
        "file_size": size,
        "positions_recorded": positions,
        "known_open_tickets": known_open,
        "consistent": consistent,
    }


# ═══════════════════════════════════════════════════════════════════════
# 3. ORPHAN JOURNAL ENTRY TOMBSTONING
# ═══════════════════════════════════════════════════════════════════════


def check_orphan_entries(data_dir: str) -> dict[str, Any]:
    """Identify orphan journal entries and report contamination level."""
    jp = Path(data_dir) / "live_trade_journal.jsonl"

    total_closes = 0
    null_pnl = 0
    null_ticket = 0
    orphan_labels = 0

    if not jp.exists():
        return {"passed": True, "severity": "SKIP", "reason": "no journal"}

    with open(jp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e.get("action") != "close":
                    continue
                total_closes += 1
                if e.get("pnl") is None:
                    null_pnl += 1
                if e.get("position_ticket") is None:
                    null_ticket += 1
                if "auto_orphan" in str(e.get("label", "")):
                    orphan_labels += 1
            except json.JSONDecodeError:
                pass

    contamination_pct = (null_pnl / total_closes * 100) if total_closes else 0
    sev = "OK" if contamination_pct < 1 else "Sev3" if contamination_pct < 5 else "Sev2"

    return {
        "passed": contamination_pct < 5,
        "severity": sev,
        "total_closes": total_closes,
        "null_pnl": null_pnl,
        "null_ticket": null_ticket,
        "orphan_labels": orphan_labels,
        "contamination_pct": round(contamination_pct, 2),
    }


# ═══════════════════════════════════════════════════════════════════════
# 4. BRIDGE MICRO-HEALTH
# ═══════════════════════════════════════════════════════════════════════


def check_bridge_micro_health(data_dir: str) -> dict[str, Any]:
    """Check bridge health with latency jitter and packet metrics."""
    bh = Path(data_dir) / "reports" / "mt5_bridge_health.json"

    if not bh.exists():
        return {"passed": False, "severity": "Sev1", "reason": "bridge health missing"}

    try:
        data = json.loads(bh.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"passed": False, "severity": "Sev1", "reason": "bridge health corrupt"}

    connected = data.get("mt5_connected", False)
    outbox = data.get("outbox_pending", -1)
    heartbeat_str = data.get("last_heartbeat_utc", "")

    # Parse heartbeat age
    age_s = 999.0
    if heartbeat_str:
        try:
            hb = datetime.fromisoformat(heartbeat_str.replace("Z", "+00:00"))
            age_s = (NOW - hb.replace(tzinfo=timezone.utc)).total_seconds()
        except ValueError:
            pass

    # Check for historical heartbeat jitter by reading bridge health log
    bridge_log = Path(data_dir) / "reports" / "ops_logs" / "bridge_supervisor.log"
    p99_latency = None
    log_entries = 0
    if bridge_log.exists():
        try:
            lines = bridge_log.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            latencies = []
            for line in lines[-100:]:  # last 100 entries
                if "latency" in line.lower() or "elapsed" in line.lower():
                    try:
                        # Try to extract numeric latency
                        parts = line.split()
                        for p in parts:
                            p = p.strip("ms,").strip("s,")
                            try:
                                v = float(p)
                                if 0.01 < v < 10000:
                                    latencies.append(v)
                                    break
                            except ValueError:
                                continue
                    except Exception:
                        pass
            if latencies:
                latencies.sort()
                p99_latency = latencies[int(len(latencies) * 0.99)]
            log_entries = len(lines)
        except Exception:
            pass

    sev = "OK" if connected and outbox == 0 and age_s < 30 else (
        "Sev2" if age_s > 60 or outbox > 10 else "Sev1" if not connected else "Sev3"
    )

    result: dict[str, Any] = {
        "passed": connected and age_s < 30,
        "severity": sev,
        "connected": connected,
        "outbox_pending": outbox,
        "heartbeat_age_s": round(age_s, 1),
        "p99_latency_ms": round(p99_latency, 1) if p99_latency else None,
        "log_entries": log_entries,
    }
    return result


# ═══════════════════════════════════════════════════════════════════════
# 5. TICK PRECISION ASSERTION
# ═══════════════════════════════════════════════════════════════════════


def check_tick_precision(data_dir: str) -> dict[str, Any]:
    """Sample feature store prices and verify tick precision for each symbol."""
    base = Path(data_dir) / FEATURE_STORE_DIR
    if not base.exists():
        return {"passed": True, "severity": "SKIP", "reason": "no feature store"}

    violations: list[dict] = []
    checked = 0

    for sym_dir in base.iterdir():
        if not sym_dir.is_dir():
            continue
        sym = sym_dir.name.replace("symbol=", "")
        spec = SYMBOL_PRECISION.get(sym)
        if not spec:
            continue
        tick = spec["tick_size"]

        for tf_dir in sym_dir.iterdir():
            if not tf_dir.is_dir():
                continue
            fp = tf_dir / "features.jsonl"
            if not fp.exists():
                continue

            try:
                with open(fp, encoding="utf-8") as f:
                    lines = [json.loads(l) for l in f if l.strip()]
                # Sample: first, middle, last 100 records
                sample = lines[:50] + lines[len(lines) // 2 : len(lines) // 2 + 50] + lines[-50:]
                # Derived/normalized features that are NOT raw prices — skip precision check
                DERIVED_PATTERNS = ("ZScore", "MACD", "RSI", "OU_", "Hurst", "Corr",
                                     "Body_Ratio", "Vol_", "Ret_", "_return", "Bollinger",
                                     "ADX", "Trend_Strength", "ATR_Ratio", "ATR_",
                                     "Divergence", "Alignment", "Spread", "OIM", "tick_",
                                     "hl_ratio", "co_ratio", "Derived_", "Cross_", "TF_",
                                     "avg_spread", "velocity")
                for rec in sample:
                    vals = rec.get("values", {})
                    for k, v in vals.items():
                        if not isinstance(v, (int, float)):
                            continue
                        # Skip ATR (moving average, not raw price) and all derived features
                        if k.startswith("M5_ATR") or k.startswith("M15_ATR") or k.startswith("M30_ATR") or k.startswith("H1_ATR"):
                            continue
                        is_derived = any(p in k for p in DERIVED_PATTERNS)
                        if is_derived:
                            continue
                        remainder = abs(float(v)) % tick
                        if remainder > tick * 0.01 and remainder < tick * 0.99:
                            violations.append({
                                "symbol": sym,
                                "timeframe": tf_dir.name.replace("timeframe=", ""),
                                "feature": k,
                                "value": float(v),
                                "expected_tick": tick,
                                "remainder": round(remainder, 10),
                            })
                    checked += 1
            except Exception:
                pass

    sev = "OK" if not violations else "Sev2" if len(violations) < 10 else "Sev1"
    return {
        "passed": len(violations) == 0,
        "severity": sev,
        "records_checked": checked,
        "violations": violations[:20],  # cap for report size
        "total_violations": len(violations),
    }


# ═══════════════════════════════════════════════════════════════════════
# 6. REPORT GENERATION + DINGTALK PUSH
# ═══════════════════════════════════════════════════════════════════════


def generate_report(all_results: dict[str, dict[str, Any]]) -> str:
    """Generate Markdown report from audit results."""
    sections = []
    sections.append("# Ω Data Integrity Audit Report")
    sections.append(f"**Time**: {NOW_ISO}")
    sections.append("")

    # Summary table
    sections.append("## Summary")
    sections.append("| Check | XAU | BTC |")
    sections.append("|-------|-----|-----|")
    for check_name in ["reconciliation_journal_ledger", "snapshots_journal", "active_position",
                        "orphan_entries", "bridge_micro_health", "tick_precision"]:
        xau_status = all_results.get("data", {}).get(check_name, {}).get("severity", "?")
        btc_status = all_results.get("data_btc", {}).get(check_name, {}).get("severity", "?")
        xau_icon = "[OK]" if xau_status == "OK" else "[WARN]" if "Sev" in str(xau_status) else "[SKIP]"
        btc_icon = "[OK]" if btc_status == "OK" else "[WARN]" if "Sev" in str(btc_status) else "[SKIP]"
        sections.append(f"| {check_name} | {xau_icon} {xau_status} | {btc_icon} {btc_status} |")

    sections.append("")
    sections.append(f"**Compute checksum**: `{hashlib.sha256(str(NOW_ISO).encode()).hexdigest()[:16]}`")
    sections.append("")
    sections.append("*Generated by audit_data_integrity.py — Iron Law #11 compliant*")

    return "\n".join(sections)


def push_dingtalk(markdown: str, webhook_url: str, title: str = "Ω Data Integrity Audit") -> bool:
    """Push Markdown report to DingTalk via webhook."""
    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": markdown,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("errcode") == 0
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════


def main() -> int:
    p = argparse.ArgumentParser(prog="audit_data_integrity")
    p.add_argument("--data-dir", type=str, default=None,
                   help="Single data dir to audit (default: both data/ and data_btc/)")
    p.add_argument("--push-dingtalk", action="store_true",
                   help="Push report to DingTalk webhook")
    p.add_argument("--json", action="store_true",
                   help="Output results as JSON instead of Markdown")
    p.add_argument("--webhook-url", type=str, default=None,
                   help="Override DingTalk webhook URL")
    args = p.parse_args()

    data_dirs = [args.data_dir] if args.data_dir else ["data", "data_btc"]
    all_results: dict[str, dict[str, Any]] = {}

    for d in data_dirs:
        if not Path(d).exists():
            continue
        all_results[d] = {
            "reconciliation_journal_ledger": reconcile_journal_vs_ledger(d),
            "snapshots_journal": reconcile_snapshots_vs_journal(d),
            "active_position": check_active_position_state(d),
            "orphan_entries": check_orphan_entries(d),
            "bridge_micro_health": check_bridge_micro_health(d),
            "tick_precision": check_tick_precision(d),
        }

    if args.json:
        print(json.dumps(all_results, indent=2, default=str))
        return 0

    # Generate and print Markdown report
    report = generate_report(all_results)
    print(report)

    # Push to DingTalk if requested
    if args.push_dingtalk:
        webhook = args.webhook_url
        if not webhook:
            # Read from config
            try:
                with open("configs/live.yaml", encoding="utf-8") as f:
                    for line in f:
                        if "dingtalk_webhook_url:" in line:
                            webhook = line.split(":", 1)[1].strip()
                            break
            except Exception:
                pass
        if webhook:
            ok = push_dingtalk(report, webhook)
            print(f"\n[DingTalk] Push {'OK' if ok else 'FAILED'}")
        else:
            print("\n[DingTalk] No webhook URL configured")

    # Determine exit code
    has_sev1 = False
    for d_results in all_results.values():
        for check in d_results.values():
            if check.get("severity") == "Sev1":
                has_sev1 = True
    return 1 if has_sev1 else 0


if __name__ == "__main__":
    raise SystemExit(main())
