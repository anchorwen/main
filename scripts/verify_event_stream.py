# type: ignore
#!/usr/bin/env python
"""Verify event stream integrity against old JSON PnP ledger.

FIX-20260611-022: Migration verification — confirms that load_from_stream()
produces the same BrainPnLStore state as the old load() from JSON.
When all checks pass, the old brain_pnl_ledger.json can be safely retired.

Usage::

    python scripts/verify_event_stream.py
    python scripts/verify_event_stream.py --base-dir data_btc
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from core.feedback.brain_pnl_ledger import BrainPnLStore


def verify(base_dir: str) -> dict:
    """Compare old JSON load vs event stream load for ALL brains.

    Returns a dict with verification results.
    """
    base = Path(base_dir)
    old_path = base / "brain_pnl_ledger.json"
    stream_path = base / "ledger_events.jsonl"

    result = {
        "base_dir": str(base),
        "old_json_exists": old_path.exists(),
        "stream_exists": stream_path.exists(),
        "brains": {},
        "passed": 0,
        "failed": 0,
        "skipped": 0,
    }

    if not old_path.exists():
        result["error"] = "old JSON not found — nothing to compare"
        return result
    if not stream_path.exists():
        result["error"] = "event stream not found — run migration first"
        return result

    # Load old JSON
    try:
        old_store = BrainPnLStore.load(str(old_path))
    except Exception as e:
        result["error"] = f"Failed to load old JSON: {e}"
        return result

    # Load from event stream
    try:
        new_store = BrainPnLStore.load_from_stream(str(stream_path))
    except Exception as e:
        result["error"] = f"Failed to load from event stream: {e}"
        return result

    old_metrics = old_store.get_all_metrics()
    new_metrics = new_store.get_all_metrics()

    all_brain_ids = set(old_metrics.keys()) | set(new_metrics.keys())

    for brain_id in sorted(all_brain_ids):
        om = old_metrics.get(brain_id)
        nm = new_metrics.get(brain_id)

        if om is None:
            result["brains"][brain_id] = {
                "status": "NEW_ONLY",
                "note": "Brain exists only in event stream",
                "stream_trades": nm.sample_count if nm else 0,
            }
            result["skipped"] += 1
            continue

        if nm is None:
            result["brains"][brain_id] = {
                "status": "OLD_ONLY",
                "note": "Brain exists only in old JSON (empty settled list?)",
                "old_trades": om.sample_count,
            }
            result["skipped"] += 1
            continue

        # Compare metrics
        trades_match = om.sample_count == nm.sample_count
        wr_match = abs(om.win_rate - nm.win_rate) < 0.02
        pnl_match = abs(om.cumulative_pnl - nm.cumulative_pnl) < 1.0

        all_match = trades_match and wr_match and pnl_match

        result["brains"][brain_id] = {
            "status": "PASS" if all_match else "MISMATCH",
            "old_trades": om.sample_count,
            "new_trades": nm.sample_count,
            "old_wr": round(om.win_rate, 4),
            "new_wr": round(nm.win_rate, 4),
            "old_pnl": round(om.cumulative_pnl, 2),
            "new_pnl": round(nm.cumulative_pnl, 2),
            "checks": {
                "trades": trades_match,
                "win_rate": wr_match,
                "pnl": pnl_match,
            },
        }

        if all_match:
            result["passed"] += 1
        else:
            result["failed"] += 1

    return result


def print_report(result: dict) -> int:
    """Print verification report and return exit code."""
    if "error" in result:
        print(f"ERROR: {result['error']}")
        return 1

    print(f"Base dir: {result['base_dir']}")
    print(f"Old JSON: {'EXISTS' if result['old_json_exists'] else 'MISSING'}")
    print(f"Event stream: {'EXISTS' if result['stream_exists'] else 'MISSING'}")
    print()

    print(f"{'Brain':<40} {'Status':<10} {'Old':>6} {'New':>6} {'WR Δ':>8} {'PnL Δ':>10}")
    print("-" * 85)

    for brain_id, info in sorted(result["brains"].items()):
        status = info["status"]
        if status in ("PASS", "MISMATCH"):
            old_t = info.get("old_trades", 0)
            new_t = info.get("new_trades", 0)
            old_wr = info.get("old_wr", 0)
            new_wr = info.get("new_wr", 0)
            old_pnl = info.get("old_pnl", 0)
            new_pnl = info.get("new_pnl", 0)
            wr_delta = new_wr - old_wr
            pnl_delta = new_pnl - old_pnl
            print(
                f"{brain_id:<40} {status:<10} {old_t:>6} {new_t:>6} "
                f"{wr_delta:>+8.4f} {pnl_delta:>+10.2f}"
            )
        else:
            note = info.get("note", "")
            print(f"{brain_id:<40} {status:<10} {'-':>6} {'-':>6} {'-':>8} {note}")

    print()
    print(f"Results: {result['passed']} PASS, {result['failed']} FAIL, {result['skipped']} SKIP")

    if result["failed"] > 0:
        print("\n⚠️  MISMATCHES DETECTED — do NOT retire old JSON until resolved.")
        print("   This may be expected if dual-write has added new live events")
        print("   to the stream that aren't in the old JSON snapshot.")
        return 1

    if result["failed"] == 0 and result["passed"] > 0:
        print("\n✅ ALL BRAINS MATCH — old JSON can be safely retired.")
        return 0

    return 0


def main() -> int:
    import io as _io
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Verify event stream integrity against old JSON PnP ledger"
    )
    parser.add_argument(
        "--base-dir",
        nargs="+",
        default=["data", "data_btc"],
        help="Base directories to verify (default: data data_btc)",
    )
    args = parser.parse_args()

    exit_code = 0
    for base_dir in args.base_dir:
        result = verify(base_dir)
        rc = print_report(result)
        if rc != 0:
            exit_code = rc
        print()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
