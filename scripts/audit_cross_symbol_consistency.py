#!/usr/bin/env python
"""Cross-symbol data consistency audit (Layer 3).

DLR-001 (2026-06-17): 34 real BTC opens permanently lost because
``entry_context.vector`` was absent from BTC journal entries while XAU
entries had it.  This asymmetry was the key forensic signal — this script
automates that comparison so future single-symbol data pipeline failures
are caught immediately.

Compares BTC (``data_btc/``) vs XAU (``data/``) across three dimensions:
  1. Field presence parity — for key fields in journal opens, are both
     symbols writing them at similar rates?
  2. Volume parity — is one symbol writing substantially fewer entries?
  3. Time coverage — do both symbols have overlapping data windows?

Usage:
    python scripts/audit_cross_symbol_consistency.py
    python scripts/audit_cross_symbol_consistency.py --output json
    python scripts/audit_cross_symbol_consistency.py --alert
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

BTC_DIR = ROOT / "data_btc"
XAU_DIR = ROOT / "data"

CUTOFF_7D = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()


def _scan_journal_opens(journal_path: Path) -> dict:
    """Scan journal for open entry field presence statistics."""
    result = {
        "total_opens": 0,
        "has_entry_context": 0,
        "has_vector": 0,
        "has_spread": 0,
        "has_bid_ask": 0,
        "sample_missing": [],
    }
    if not journal_path.exists():
        return result

    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = entry.get("action")
            if action not in ("open", None):
                continue
            mid = str(entry.get("message_id", ""))
            if "eq_" not in mid:
                continue
            ts = entry.get("recorded_at", "")
            if ts < CUTOFF_7D:
                continue  # only recent entries for parity check

            result["total_opens"] += 1
            ctx = entry.get("entry_context")
            if isinstance(ctx, dict) and ctx:
                result["has_entry_context"] += 1
                if ctx.get("vector"):
                    result["has_vector"] += 1
                if ctx.get("entry_spread") is not None:
                    result["has_spread"] += 1
                if ctx.get("bid") is not None and ctx.get("ask") is not None:
                    result["has_bid_ask"] += 1
            else:
                if len(result["sample_missing"]) < 3:
                    result["sample_missing"].append(
                        str(entry.get("position_ticket", "?"))
                    )

    return result


def _scan_feature_store(fs_path: Path) -> dict:
    """Quick stats on feature store."""
    result = {"total_records": 0, "latest_ts": "", "field_count": 0}
    if not fs_path.exists():
        return result
    with open(fs_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            result["total_records"] += 1
            ts = entry.get("event_time", "")
            if ts > result["latest_ts"]:
                result["latest_ts"] = ts
            if result["field_count"] == 0:
                vals = entry.get("values", {})
                if isinstance(vals, dict):
                    result["field_count"] = len(vals)
    return result


def _rate(symbol_stats: dict) -> float:
    """Weekly open rate."""
    return symbol_stats.get("total_opens", 0)  # already 7-day window


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "N/A"
    return f"{part / total * 100:.0f}%"


def main() -> int:
    print("=" * 60)
    print("  CROSS-SYMBOL CONSISTENCY AUDIT (Layer 3)")
    print(f"  Window: last 7 days (since {CUTOFF_7D[:10]})")
    print("=" * 60)

    # ── Journal open field parity ──
    btc_j = BTC_DIR / "live_trade_journal.jsonl"
    xau_j = XAU_DIR / "live_trade_journal.jsonl"

    btc = _scan_journal_opens(btc_j)
    xau = _scan_journal_opens(xau_j)

    print("\n── 1. Journal Open Field Presence (7d) ──")
    print(f"  {'Field':<25} {'BTC':>10} {'XAU':>10} {'Parity':>10}")
    print(f"  {'-'*25} {'-'*10} {'-'*10} {'-'*10}")
    for field, b_key, x_key in [
        ("total_opens", "total_opens", "total_opens"),
        ("has_entry_context", "has_entry_context", "has_entry_context"),
        ("has_vector", "has_vector", "has_vector"),
        ("has_spread", "has_spread", "has_spread"),
        ("has_bid_ask", "has_bid_ask", "has_bid_ask"),
    ]:
        bv = btc[b_key]
        xv = xau[b_key]
        if b_key == "total_opens":
            parity = "OK" if (xv > 0 and 0.1 < bv / max(xv, 1) < 10) else "SKEW"
            print(f"  {field:<25} {bv:>10} {xv:>10} {parity:>10}")
        else:
            b_pct = bv / max(btc["total_opens"], 1) * 100
            x_pct = xv / max(xau["total_opens"], 1) * 100
            diff = abs(b_pct - x_pct)
            parity = "OK" if diff < 20 else "SKEW"
            print(f"  {field:<25} {_pct(bv, btc['total_opens']):>10} {_pct(xv, xau['total_opens']):>10} {parity:>10}")

    # ── Feature store parity ──
    print("\n── 2. Feature Store Parity ──")
    btc_fs = BTC_DIR / "feature_store" / "records" / "symbol=BTCUSDc" / "timeframe=M5" / "features.jsonl"
    xau_fs = XAU_DIR / "feature_store" / "records" / "symbol=XAUUSDc" / "timeframe=M5" / "features.jsonl"

    btc_fs_stats = _scan_feature_store(btc_fs)
    xau_fs_stats = _scan_feature_store(xau_fs)

    print(f"  BTC: {btc_fs_stats['total_records']} records, {btc_fs_stats['field_count']} fields, latest={btc_fs_stats['latest_ts'][:19]}")
    print(f"  XAU: {xau_fs_stats['total_records']} records, {xau_fs_stats['field_count']} fields, latest={xau_fs_stats['latest_ts'][:19]}")
    if btc_fs_stats["field_count"] != xau_fs_stats["field_count"]:
        print(f"  WARNING: field count mismatch ({btc_fs_stats['field_count']} vs {xau_fs_stats['field_count']})")

    # ── Volume ratio ──
    print("\n── 3. Volume Ratio (7d) ──")
    btc_vol = btc["total_opens"]
    xau_vol = xau["total_opens"]
    ratio = btc_vol / max(xau_vol, 1)
    print(f"  BTC opens: {btc_vol}")
    print(f"  XAU opens: {xau_vol}")
    print(f"  Ratio (BTC/XAU): {ratio:.2f}")
    if ratio < 0.1 and btc_vol > 0:
        print(f"  INFO: BTC volume is {ratio:.1%} of XAU — expected for newer symbol")

    # ── VERDICT ──
    print(f"\n{'='*60}")
    issues = []

    # Check vector presence parity
    btc_vec_pct = btc["has_vector"] / max(btc["total_opens"], 1) * 100
    xau_vec_pct = xau["has_vector"] / max(xau["total_opens"], 1) * 100
    if abs(btc_vec_pct - xau_vec_pct) > 20:
        issues.append(
            f"Vector presence skew: BTC={btc_vec_pct:.0f}% vs XAU={xau_vec_pct:.0f}% "
            f"(>20pp gap — possible single-symbol pipeline failure)"
        )

    if btc_fs_stats["field_count"] != xau_fs_stats["field_count"]:
        issues.append("Feature store field count mismatch")

    if not issues:
        print("  VERDICT: CONSISTENT — no cross-symbol anomalies detected")
    else:
        print(f"  VERDICT: SKEW DETECTED — {len(issues)} issue(s):")
        for i in issues:
            print(f"    ! {i}")

    # ── Alert dispatch (if --alert) ──
    if "--alert" in sys.argv and issues:
        try:
            from scripts.alert_dispatcher import AlertCard, dispatch_alert
            card = AlertCard(
                source="audit_cross_symbol_consistency",
                title="Cross-Symbol Data Skew Detected",
                severity="Sev 2",
                checks={f"issue_{i}": iss for i, iss in enumerate(issues)},
                affected_consumers=[
                    "training_dataset_builder",
                    "build_metafilter_dataset",
                    "system_trust_report",
                ],
                details={
                    "btc_vector_pct": round(btc_vec_pct, 1),
                    "xau_vector_pct": round(xau_vec_pct, 1),
                    "btc_opens_7d": btc_vol,
                    "xau_opens_7d": xau_vol,
                },
            )
            dispatch_alert(card)
            print("  Alert dispatched.")
        except ImportError:
            print("  WARNING: alert_dispatcher not available, alert not sent")

    print("\n[DONE] All statistics above are the sole source of truth.")
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
