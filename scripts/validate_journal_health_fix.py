"""Validate DQAF-20260619-002: simulate fixed check_journal_completeness logic.

Iron Law #11 compliant — all statistics from script stdout.
Usage: python scripts/validate_journal_health_fix.py
"""

from __future__ import annotations

import json
from pathlib import Path


def simulate_fixed_check(base_dir: str) -> dict:
    jl_path = Path(base_dir) / "live_trade_journal.jsonl"
    closes: list[dict] = []
    modify_count = 0
    tickets_seen: dict[tuple, str] = {}
    dupes = 0

    for line in jl_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = entry.get("action", "")
        if action == "modify_sltp":
            modify_count += 1
            continue
        if action != "close":
            continue
        closes.append(entry)
        ticket = entry.get("position_ticket")
        if not ticket:
            continue
        ack = entry.get("ack_status", "")
        detail = entry.get("detail", {}) if isinstance(entry.get("detail"), dict) else {}
        reason = detail.get("reason", "")
        if isinstance(reason, str) and reason.startswith("auto_orphan_"):
            continue
        key = (ticket, ack)
        if key in tickets_seen:
            dupes += 1
        else:
            tickets_seen[key] = entry.get("message_id", "")

    total = len(closes)
    cp_eligible = 0
    cp_found = 0
    for e in closes:
        detail = e.get("detail", {}) if isinstance(e.get("detail"), dict) else {}
        reason = detail.get("reason", "")
        ack = e.get("ack_status", "")
        if isinstance(reason, str) and reason.startswith("auto_orphan_"):
            continue
        if ack == "rejected":
            continue
        cp_eligible += 1
        cp = detail.get("close_price")
        if cp and cp > 0:
            cp_found += 1
            continue
        req = detail.get("request", {}) if isinstance(detail.get("request"), dict) else {}
        cp_req = req.get("close_price") or req.get("price")
        if cp_req and cp_req > 0:
            cp_found += 1

    cp_rate = cp_found / max(cp_eligible, 1)
    trail_rate = modify_count / max(total, 1)

    return {
        "total_closes": total,
        "cp_eligible": cp_eligible,
        "cp_found": cp_found,
        "cp_rate": round(cp_rate, 4),
        "modify_count": modify_count,
        "trail_rate": round(trail_rate, 4),
        "dupes": dupes,
        "verdict": "PASS"
        if (cp_rate >= 0.50 and dupes <= 10 and trail_rate >= 0.10)
        else "FAIL",
    }


if __name__ == "__main__":
    for label, d in [("BTC", "data_btc"), ("XAU", "data")]:
        r = simulate_fixed_check(d)
        print(f"=== {label} (FIXED check) ===")
        for k, v in r.items():
            print(f"  {k}: {v}")
        print()
    print("[DONE] All statistics above are the sole source of truth.")
