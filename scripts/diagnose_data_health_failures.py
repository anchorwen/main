"""Diagnose all FAIL-level data_health items per Iron Law #11.

Usage: python scripts/diagnose_data_health_failures.py
All statistics below are the sole source of truth.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path


def diagnose_entry_context_vector(data_dir: str) -> dict:
    """Check live_trade_journal for entries missing entry_context.vector.

    DQAF-20260619-004: synced with FIX-003 dual-format detection logic.
    New format: ctx.entry_features.vector, old format: ctx.vector fallback.
    """
    jl_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not jl_path.exists():
        return {"error": "journal not found"}

    total_opens = 0
    missing_ctx = 0
    missing_vector = 0
    empty_vector = 0
    missing_samples: list[dict] = []
    by_strategy: Counter[str] = Counter()
    by_month: Counter[str] = Counter()

    for line in jl_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") != "open":
            continue
        total_opens += 1
        ctx = e.get("entry_context")
        if ctx is None or not isinstance(ctx, dict):
            missing_ctx += 1
            strategy = e.get("strategy", "unknown")
            by_strategy[strategy] += 1
            ts = e.get("recorded_at", "")
            month = ts[:7] if ts else "unknown"
            by_month[month] += 1
            if len(missing_samples) < 5:
                missing_samples.append({
                    "message_id": e.get("message_id", "")[-20:],
                    "strategy": strategy,
                    "recorded_at": ts,
                    "has_entry_context": False,
                    "entry_context_keys": str(type(ctx)),
                    "gap_type": "missing_ctx",
                })
            continue
        # Dual-format: FIX-003 — check entry_features.vector first, then ctx.vector
        entry_features = ctx.get("entry_features")
        if isinstance(entry_features, dict) and entry_features.get("vector"):
            continue  # vector found in new format
        vector = ctx.get("vector")
        if vector is not None:
            if isinstance(vector, list | tuple) and len(vector) == 0:
                empty_vector += 1
            continue  # vector found in old format (or empty — counted)
        # Neither format has a non-empty vector
        missing_vector += 1
        strategy = e.get("strategy", "unknown")
        by_strategy[strategy] += 1
        ts = e.get("recorded_at", "")
        month = ts[:7] if ts else "unknown"
        by_month[month] += 1
        if len(missing_samples) < 5:
            missing_samples.append({
                "message_id": e.get("message_id", "")[-20:],
                "strategy": strategy,
                "recorded_at": ts,
                "has_entry_context": True,
                "entry_context_keys": list(ctx.keys()) if isinstance(ctx, dict) else str(type(ctx)),
                "has_entry_features": isinstance(entry_features, dict),
                "has_vector_key": "vector" in ctx if isinstance(ctx, dict) else False,
                "gap_type": "missing_vector",
            })

    total_missing = missing_ctx + missing_vector + empty_vector
    completeness = 1.0 - (total_missing / max(total_opens, 1))
    return {
        "total_opens": total_opens,
        "missing_ctx": missing_ctx,
        "missing_vector": missing_vector,
        "empty_vector": empty_vector,
        "total_missing": total_missing,
        "completeness": round(completeness, 4),
        "by_strategy": dict(by_strategy.most_common()),
        "by_month": dict(sorted(by_month.items())),
        "samples": missing_samples,
    }


def diagnose_brain_output_health(data_dir: str) -> dict:
    """Check brain_pnl_ledger (ledger_events.jsonl) for brain output silence."""
    ledger_path = Path(data_dir) / "ledger_events.jsonl"
    if not ledger_path.exists():
        return {"error": "ledger not found"}

    # Get recent entries (last 3 hours)
    import time as _time
    from datetime import UTC, datetime

    cutoff = (datetime.now(UTC).timestamp() - 3 * 3600)  # 3h window
    brain_activity: Counter[str] = Counter()
    total_recent = 0
    latest_ts = ""

    for line in ledger_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_str = e.get("timestamp", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        total_recent += 1
        bid = e.get("brain_id", "unknown")
        brain_activity[bid] += 1
        latest_ts = ts_str

    active_brains = len(brain_activity)
    return {
        "window_hours": 3,
        "total_recent_events": total_recent,
        "active_brain_count": active_brains,
        "brain_activity": dict(brain_activity.most_common(10)),
        "latest_timestamp": latest_ts,
        "is_silent": total_recent == 0 or active_brains == 0,
    }


def diagnose_journal_vs_pnl_ledger(data_dir: str) -> dict:
    """Cross-check: compare close counts in trade journal vs PnL ledger settlements."""
    jl_path = Path(data_dir) / "live_trade_journal.jsonl"
    ledger_path = Path(data_dir) / "ledger_events.jsonl"

    journal_closes = 0
    ledger_settled = 0

    # Count journal closes
    if jl_path.exists():
        for line in jl_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") == "close" and e.get("ack_status") == "closed":
                journal_closes += 1

    # Count ledger settlements with non-zero exit_price
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event_type") == "SignalSettled" and e.get("exit_price") is not None:
                ledger_settled += 1

    total = max(journal_closes, ledger_settled, 1)
    delta_pct = round(abs(journal_closes - ledger_settled) / total, 4)
    return {"journal_closes": journal_closes, "ledger_settled": ledger_settled, "delta_pct": delta_pct}


def diagnose_brain_registry_governance(data_dir: str) -> dict:
    """Check alignment between brain_registry and governance_state."""
    registry_path = Path(data_dir) / "brain_performance.json"
    governance_path = Path(data_dir) / "governance_state.json"

    result = {"registry_brains": 0, "governance_brains": 0, "status_mismatches": []}

    if registry_path.exists():
        reg = json.loads(registry_path.read_text(encoding="utf-8"))
        if isinstance(reg, dict):
            result["registry_brains"] = len(reg)
            reg_brains = set(reg.keys())
        else:
            reg_brains = set()
    else:
        reg_brains = set()

    if governance_path.exists():
        gov = json.loads(governance_path.read_text(encoding="utf-8"))
        if isinstance(gov, dict):
            # Governance state uses "brain_states" as the top-level key
            gov_brains_raw = gov.get("brain_states", gov.get("brains", {}))
            if isinstance(gov_brains_raw, dict):
                result["governance_brains"] = len(gov_brains_raw)
                gov_brains = set(gov_brains_raw.keys())
            else:
                gov_brains = set()
        else:
            gov_brains = set()
    else:
        gov_brains = set()

    # Mismatches
    only_registry = reg_brains - gov_brains
    only_gov = gov_brains - reg_brains
    result["only_in_registry"] = sorted(only_registry)[:10]
    result["only_in_governance"] = sorted(only_gov)[:10]
    result["alignment_pct"] = round(
        len(reg_brains & gov_brains) / max(len(reg_brains | gov_brains), 1), 4
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    for label, d in [("BTC", "data_btc"), ("XAU", "data")]:
        print(f"{'='*60}")
        print(f"  {label} ({d})")
        print(f"{'='*60}")

        print("\n--- 1. entry_context VECTOR_MISSING ---")
        r1 = diagnose_entry_context_vector(d)
        print(f"  Total opens: {r1.get('total_opens', '?')}")
        print(f"  missing_ctx: {r1.get('missing_ctx', '?')}")
        print(f"  missing_vector: {r1.get('missing_vector', '?')}")
        print(f"  empty_vector: {r1.get('empty_vector', '?')}")
        print(f"  total_missing: {r1.get('total_missing', '?')}")
        print(f"  completeness: {r1.get('completeness', '?')}")
        print(f"  By strategy: {r1.get('by_strategy', {})}")
        print(f"  By month: {r1.get('by_month', {})}")
        for s in r1.get("samples", [])[:3]:
            print(f"  Sample: {s}")

        print("\n--- 2. journal_vs_pnl_ledger ---")
        r2 = diagnose_journal_vs_pnl_ledger(d)
        for k, v in r2.items():
            print(f"  {k}: {v}")

        print("\n--- 3. brain_registry_governance_alignment ---")
        r3 = diagnose_brain_registry_governance(d)
        for k, v in r3.items():
            if isinstance(v, list):
                print(f"  {k}: [{len(v)} items] {v[:5]}")
            else:
                print(f"  {k}: {v}")

        print("\n--- 4. brain_output_health ---")
        r4 = diagnose_brain_output_health(d)
        for k, v in r4.items():
            print(f"  {k}: {v}")

    print(f"\n{'='*60}")
    print("[DONE] All statistics above are the sole source of truth.")
