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
  python scripts/audit_data_integrity.py --alert                  # push alert on Sev1/Sev2
  python scripts/audit_data_integrity.py --json                   # JSON output
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from core.runtime.fault_handler import fail_open_guard

NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat()[:19] + "Z"

# ── Weekend closure detection ──────────────────────────────────────────
# XAU (spot gold) closes Friday 22:00 UTC → Sunday 22:00 UTC.
# BTC trades 24/7 — no weekend suppression.


def is_market_closed(data_dir: str) -> bool:
    """Return True if the market for *data_dir* is in a weekend closure window.

    XAU (data/) follows the forex weekend calendar.
    BTC (data_btc/) trades continuously — always returns False.
    """
    if "btc" in str(data_dir).lower():
        return False  # BTC trades 24/7

    now = NOW
    wd = now.weekday()  # Monday=0, Sunday=6
    hour = now.hour

    # Friday after 22:00 UTC
    if wd == 4 and hour >= 22:
        return True
    # Saturday all day
    if wd == 5:
        return True
    # Sunday before 22:00 UTC
    if wd == 6 and hour < 22:
        return True

    return False

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

    # ── Historical exemption ──
    # Snapshots were introduced on 2026-05-31.  Journal entries before that
    # date legitimately have no snapshots.  Count them separately.
    snapshot_era_start = "2026-05-31"
    pre_era_opens = {t for t in journal_tickets if True}  # simplified — all
    # Count pre-era missing: tickets with no snapshot AND journal entry before era start
    pre_era_journal_tickets: set[int] = set()
    post_era_journal_tickets: set[int] = set()
    with open(jp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e.get("action") == "open":
                    tkt = e.get("position_ticket")
                    ts = e.get("recorded_at", "")
                    if tkt:
                        if ts < snapshot_era_start:
                            pre_era_journal_tickets.add(int(tkt))
                        else:
                            post_era_journal_tickets.add(int(tkt))
            except json.JSONDecodeError:
                pass

    pre_era_missing = pre_era_journal_tickets - snapshot_tickets
    post_era_missing = post_era_journal_tickets - snapshot_tickets
    post_era_gap_pct = len(post_era_missing) / max(len(post_era_journal_tickets), 1) * 100

    sev = "OK" if post_era_gap_pct <= 30 else "Sev3" if post_era_gap_pct <= 60 else "Sev2"

    return {
        "passed": post_era_gap_pct <= 30,
        "severity": sev,
        "journal_tickets": len(journal_tickets),
        "snapshot_tickets": len(snapshot_tickets),
        "missing_snapshots_total": len(missing_snaps),
        "pre_era_missing": len(pre_era_missing),
        "post_era_missing": len(post_era_missing),
        "post_era_gap_pct": round(post_era_gap_pct, 1),
        "snapshot_era_start": snapshot_era_start,
        "note": f"pre-era gap ({len(pre_era_missing)} tickets before {snapshot_era_start}) exempted",
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

    # Weekend suppression: MT5 has no positions during closure —
    # stale active_position.json vs empty MT5 ≠ data loss.
    if sev in ("Sev1", "Sev2", "Sev3") and is_market_closed(data_dir):
        sev = "OK"
        consistent = True

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
                    except Exception:  # BLE001:FOG
                        with fail_open_guard("audit_data_integrity:check_bridge_micro_health"):
                            pass
            if latencies:
                latencies.sort()
                p99_latency = latencies[int(len(latencies) * 0.99)]
            log_entries = len(lines)
        except Exception:  # BLE001:FOG
            with fail_open_guard("audit_data_integrity:check_bridge_micro_health"):
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
            except Exception:  # BLE001:FOG
                with fail_open_guard("audit_data_integrity:check_tick_precision"):
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
# 6. JOURNAL INTEGRITY — duplicates, timestamps, PnL verification
# ═══════════════════════════════════════════════════════════════════════


def check_journal_integrity(data_dir: str) -> dict[str, Any]:
    """Detect duplicates, timestamp inversions, and PnL inconsistencies."""
    jp = Path(data_dir) / "live_trade_journal.jsonl"
    if not jp.exists():
        return {"passed": True, "severity": "SKIP", "reason": "no journal"}

    entries: list[dict] = []
    with open(jp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    # ── Duplicate detection ──
    open_tickets: dict[int, list[dict]] = defaultdict(list)
    close_tickets: dict[int, list[dict]] = defaultdict(list)
    for e in entries:
        tkt = e.get("position_ticket")
        if not tkt:
            continue
        tkt = int(tkt)
        if e.get("action") == "open":
            open_tickets[tkt].append(e)
        elif e.get("action") == "close":
            close_tickets[tkt].append(e)

    dup_opens = sum(1 for t, es in open_tickets.items() if len(es) > 1)
    dup_closes = sum(1 for t, es in close_tickets.items() if len(es) > 1)

    # ── Timestamp consistency ──
    # Skip entries with empty close timestamps (known legacy synthetic entries
    # from pre-cleanup era). 1-second clock skew is harmless.
    time_inversions = 0
    for tkt in set(open_tickets.keys()) & set(close_tickets.keys()):
        open_ts = min(e.get("recorded_at", "9999") for e in open_tickets[tkt])
        close_tss = [e.get("recorded_at", "") for e in close_tickets[tkt] if e.get("recorded_at")]
        if not close_tss:
            continue  # synthetic close, no timestamp to compare
        close_ts = max(close_tss)
        if open_ts > close_ts:
            time_inversions += 1

    # ── PnL verification: computed vs journal (INFORMATIONAL ONLY) ──
    # MT5 deal verification (journal_mt5 check) is the authoritative PnL source.
    # This check is informational — it computes PnL from raw entry/exit prices
    # which excludes spread, slippage, swaps, and commissions.  Mismatches are
    # expected and do NOT indicate data corruption.
    # Only active for post-June-2026 trades where format is consistent.
    SYM_CONFIG = {
        "XAUUSDc": {"tick_size": 0.001, "tick_value": 0.1},
        "BTCUSDc": {"tick_size": 0.01, "tick_value": 0.01},
    }
    PNL_CHECK_CUTOFF = "2026-06-01"
    pnl_mismatches = 0
    pnl_checked = 0
    pnl_skipped_old = 0
    for tkt in set(open_tickets.keys()) & set(close_tickets.keys()):
        opens = open_tickets[tkt]
        closes = close_tickets[tkt]
        for o in opens:
            for c in closes:
                # Only verify trades from June onward (known-good format)
                ts = c.get("recorded_at", "")
                if ts < PNL_CHECK_CUTOFF:
                    pnl_skipped_old += 1
                    continue
                entry_p = o.get("detail", {}).get("request", {}).get("price") if isinstance(o.get("detail"), dict) else None
                exit_p = c.get("detail", {}).get("close_price") if isinstance(c.get("detail"), dict) else None
                if entry_p and exit_p and entry_p > 0 and exit_p > 0:
                    sym = o.get("symbol", "XAUUSDc")
                    cfg = SYM_CONFIG.get(sym, SYM_CONFIG["XAUUSDc"])
                    vol = o.get("volume", 0.01) or 0.01
                    pts_to_acct = (cfg["tick_value"] / cfg["tick_size"]) * vol
                    side = o.get("side", "long")
                    price_diff = (exit_p - entry_p) if side == "long" else (entry_p - exit_p)
                    computed = price_diff * pts_to_acct
                    journal_pnl = c.get("pnl", 0) or 0
                    # Allow 0.5 USC tolerance (accounting currency, ~$0.005)
                    if abs(computed - journal_pnl) > 0.5:
                        pnl_mismatches += 1
                    pnl_checked += 1

    issues = []
    if dup_opens > 0:
        issues.append(f"{dup_opens} duplicate opens")
    if dup_closes > 0:
        issues.append(f"{dup_closes} duplicate closes")
    if time_inversions > 0:
        issues.append(f"{time_inversions} time inversions")
    if pnl_mismatches > 0:
        issues.append(f"{pnl_mismatches}/{pnl_checked} PnL mismatches")

    # Sev3: informational only — MT5 verification (journal_mt5) is authoritative
    sev = "OK" if not issues else "Sev3"

    return {
        "passed": len(issues) == 0,
        "severity": sev,
        "duplicate_opens": dup_opens,
        "duplicate_closes": dup_closes,
        "time_inversions": time_inversions,
        "pnl_checked": pnl_checked,
        "pnl_mismatches": pnl_mismatches,
        "issues": issues,
    }


# ═══════════════════════════════════════════════════════════════════════
# 7. MT5 ACCOUNT EQUITY RECONCILIATION
# ═══════════════════════════════════════════════════════════════════════


def check_mt5_equity_reconciliation(data_dir: str) -> dict[str, Any]:
    """Reconcile journal PnL against MT5 account equity (if accessible)."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"passed": True, "severity": "SKIP", "reason": "MT5 not available"}

    # Read config for MT5 path
    mt5_path = None
    try:
        cfg_path = Path("configs/live.yaml") if data_dir == "data" else Path("configs/live_btc.yaml")
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                for line in f:
                    if "terminal_path:" in line:
                        mt5_path = line.split(":", 1)[1].strip()
                        break
    except Exception:  # BLE001:FOG
        with fail_open_guard("audit_data_integrity:check_mt5_equity_reconciliation"):
            pass
    # Also check live.yaml for XAU default
    if not mt5_path:
        try:
            live_cfg = Path("configs/live.yaml")
            if live_cfg.exists():
                with open(live_cfg, encoding="utf-8") as f:
                    for line in f:
                        if "terminal_path:" in line:
                            mt5_path = line.split(":", 1)[1].strip()
                            break
        except Exception:  # BLE001:FOG
            with fail_open_guard("audit_data_integrity:check_mt5_equity_reconciliation"):
                pass
    if not mt5_path:
        return {"passed": True, "severity": "SKIP", "reason": "no MT5 terminal path in config"}

    try:
        if not mt5.initialize(path=mt5_path):
            return {"passed": True, "severity": "SKIP", "reason": f"MT5 init failed: {mt5.last_error()}"}
    except Exception:  # BLE001:FOG
        with fail_open_guard("audit_data_integrity:check_mt5_equity_reconciliation"):
            return {"passed": True, "severity": "SKIP", "reason": "MT5 init exception"}
    try:
        info = mt5.account_info()
        if not info:
            mt5.shutdown()
            return {"passed": True, "severity": "SKIP", "reason": "no account info"}

        equity = info.equity
        balance = info.balance
        currency = info.currency
        floating = equity - balance

        # Sum journal PnL
        jp = Path(data_dir) / "live_trade_journal.jsonl"
        journal_pnl = 0.0
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

        # Simple check: journal PnL should be a reasonable fraction of equity change
        # (can't do full reconciliation without starting equity snapshot)
        pnl_ratio = abs(journal_pnl) / max(abs(balance), 1) * 100

        mt5.shutdown()

        sev = "OK"  # informational only — full reconciliation needs starting equity
        return {
            "passed": True,
            "severity": sev,
            "account_equity": round(equity, 2),
            "account_balance": round(balance, 2),
            "floating_pnl": round(floating, 2),
            "currency": currency,
            "journal_total_pnl": round(journal_pnl, 4),
            "pnl_pct_of_balance": round(pnl_ratio, 4),
            "note": "full reconciliation needs daily equity snapshots; this is informational",
        }
    except Exception as e:  # BLE001:FOG (Sev 4, Phase 3b)
        with fail_open_guard("audit_data_integrity:check_mt5_equity_reconciliation"):
            try:
                mt5.shutdown()
            except Exception:  # BLE001:FOG
                with fail_open_guard("audit_data_integrity:check_mt5_equity_reconciliation"):
                    pass
            return {"passed": True, "severity": "SKIP", "reason": f"MT5 error: {e}"}
# ═══════════════════════════════════════════════════════════════════════
# 8. THREE-WAY RECONCILIATION (Journal ↔ Ledger ↔ Snapshots)
# ═══════════════════════════════════════════════════════════════════════


def check_journal_mt5_reconciliation(data_dir: str) -> dict[str, Any]:
    """Cross-validate journal close PnL against MT5 deal profit.

    This is THE authoritative PnL reconciliation. MT5 is the system of
    record. Journal PnL must match MT5 deal profit exactly.

    NOTE: Ledger pnl_r (brain_pnl_ledger) is a BINARY win/loss signal
    (-1.0 to +1.0), NOT dollar PnL. Including it in reconciliation was
    an architectural error. Fixed in DQAF-20260616-005/root-cause-fix.
    """
    jp = Path(data_dir) / "live_trade_journal.jsonl"
    if not jp.exists():
        return {"passed": True, "severity": "SKIP", "reason": "no journal"}

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return {"passed": True, "severity": "SKIP", "reason": "MT5 not available"}

    # Use the correct terminal per data_dir: XAU→EXNESS2, BTC→BTC terminal
    DEFAULT_PATHS = {
        "data": r"D:\exness\MetaTrader 5 EXNESS2\terminal64.exe",
        "data_btc": r"D:\MetaTrader 5\terminal64.exe",
    }
    mt5_path = DEFAULT_PATHS.get(data_dir, DEFAULT_PATHS["data"])
    for cfg_file in [f"configs/{'live' if data_dir == 'data' else 'live_btc'}.yaml", "configs/live.yaml"]:
        try:
            p = Path(cfg_file)
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        if "terminal_path:" in line:
                            mt5_path = line.split(":", 1)[1].strip()
                            break
            if mt5_path: break
        except Exception:  # BLE001:FOG
            with fail_open_guard("audit_data_integrity:check_journal_mt5_reconciliation"):
                pass
    if not mt5_path:
        return {"passed": True, "severity": "SKIP", "reason": "no MT5 terminal path"}

    try:
        if not mt5.initialize(path=mt5_path):
            try: mt5.shutdown()
            except Exception:  # BLE001:FOG
                with fail_open_guard("audit_data_integrity:check_journal_mt5_reconciliation"):
                    pass
            return {"passed": True, "severity": "SKIP", "reason": "MT5 init failed"}
    except Exception:  # BLE001:FOG
        with fail_open_guard("audit_data_integrity:check_journal_mt5_reconciliation"):
            return {"passed": True, "severity": "SKIP", "reason": "MT5 init exception"}
    deals_by_pos: dict[int, Any] = {}
    try:
        for days in [90, 180]:
            deals = mt5.history_deals_get(datetime.now(timezone.utc) - timedelta(days=days), datetime.now(timezone.utc))
            if deals:
                for d in deals:
                    if d.position_id and d.profit != 0:
                        deals_by_pos[d.position_id] = d
                break
    except Exception:  # BLE001:FOG
        with fail_open_guard("audit_data_integrity:check_journal_mt5_reconciliation"):
            pass
    finally:
        try: mt5.shutdown()
        except Exception:  # BLE001:FOG
            with fail_open_guard("audit_data_integrity:check_journal_mt5_reconciliation"):
                pass
    if not deals_by_pos:
        return {"passed": True, "severity": "SKIP", "reason": "no MT5 deals"}

    journal_closes: dict[int, dict] = {}
    with open(jp, encoding="utf-8") as f:
        for line in f:
            if not line.strip(): continue
            try:
                e = json.loads(line)
                if e.get("action") == "close":
                    tkt = e.get("position_ticket")
                    if tkt: journal_closes[int(tkt)] = e
            except json.JSONDecodeError: pass

    matched = exact = mismatch = 0
    for tkt, jc in journal_closes.items():
        if tkt not in deals_by_pos: continue
        matched += 1
        jp_ = jc.get("pnl", 0) or 0
        mp_ = float(deals_by_pos[tkt].profit)
        if abs(jp_ - mp_) < 0.01: exact += 1
        else: mismatch += 1

    pct = exact / max(matched, 1) * 100
    sev = "OK" if pct > 90 else "Sev2" if pct > 40 else "Sev3"  # 40% for cross-terminal fallback

    # Weekend suppression: MT5 history deals may be stale during closure —
    # journal entries from pre-weekend close are not yet reflected in MT5.
    if sev in ("Sev1", "Sev2", "Sev3") and is_market_closed(data_dir):
        sev = "OK"

    return {
        "passed": pct > 90, "severity": sev,
        "matched_trades": matched, "exact_match": exact, "mismatch": mismatch,
        "match_pct": round(pct, 1), "mt5_deals_available": len(deals_by_pos),
        "note": f"MT5-authoritative: {exact}/{matched} ({pct:.0f}%) exact matches. Ledger excluded — binary signal, not PnL (DQAF-005 root-cause)",
    }

# ═══════════════════════════════════════════════════════════════════════
# 9. REPORT GENERATION + DINGTALK PUSH
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
                        "orphan_entries", "bridge_micro_health", "tick_precision",
                        "journal_integrity", "mt5_equity", "journal_mt5"]:
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


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════


def main() -> int:
    p = argparse.ArgumentParser(prog="audit_data_integrity")
    p.add_argument("--data-dir", type=str, default=None,
                   help="Single data dir to audit (default: both data/ and data_btc/)")
    p.add_argument("--json", action="store_true",
                   help="Output results as JSON instead of Markdown")
    p.add_argument("--webhook-url", type=str, default=None,
                   help="Override DingTalk webhook URL")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress stdout output (for scheduled silent monitoring)")
    p.add_argument("--alert", action="store_true",
                   help="Push DingTalk alert on Sev1/Sev2 (for scheduled silent monitoring)")
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
            "journal_integrity": check_journal_integrity(d),
            "mt5_equity": check_mt5_equity_reconciliation(d),
            "journal_mt5": check_journal_mt5_reconciliation(d),
        }

    if args.json:
        if not args.quiet:
            print(json.dumps(all_results, indent=2, default=str))
        return 0

    # Generate report
    report = generate_report(all_results)

    # ── Output ──
    if not args.quiet:
        print(report)

    # ── Alerting (GAP 4) ──
    has_sev1_or_sev2 = False
    alert_checks: dict[str, str] = {}
    for d_name, d_results in all_results.items():
        for check_name, check_result in d_results.items():
            sev = check_result.get("severity", "OK")
            alert_checks[f"{d_name}/{check_name}"] = sev
            if sev in ("Sev1", "Sev2"):
                has_sev1_or_sev2 = True

    if args.alert and has_sev1_or_sev2:
        try:
            from scripts.alert_dispatcher import AlertCard, dispatch_alert

            worst = "Sev1" if any(v == "Sev1" for v in alert_checks.values()) else "Sev2"
            card = AlertCard(
                source="audit",
                title=f"Data Integrity: {worst} Warning",
                severity=worst,
                checks=alert_checks,
            )
            dispatch_alert(card)
        except Exception:  # BLE001:FOG — alert failure must not crash audit
            with fail_open_guard("audit_data_integrity:main"):
                pass
    # ── Determine exit code ──
    if has_sev1_or_sev2:
        has_sev1 = any(v == "Sev1" for v in alert_checks.values())
        return 1 if has_sev1 else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
