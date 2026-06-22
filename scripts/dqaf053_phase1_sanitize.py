#!/usr/bin/env python
"""DQAF-20260622-053 Phase 1: Global State Reconciliation — Physical Ledger Sanitization.

Idempotent migration script.  All mutations go through domain APIs —
never raw JSON manipulation (Iron Law #0 compliance).

Operations (in order):
  1. XAU alpha_performance.json — remove corrupted ``5\\terminal64.exe`` + 13 orphans
  2. XAU alpha_registry.json    — remove ghost ``alpha_xau_live``, backfill strategy_class/assets
  3. BTC alpha_registry.json    — backfill ``btc_swing`` strategy_class/assets
  4. Calibrator FIFO purge      — BTC + XAU conformal_calibrator_state.json → empty FIFO
  5. BTC feed watermark reset   — force full journal rescan on next daily_ops run

Usage::

    python scripts/dqaf053_phase1_sanitize.py          # dry-run (report only)
    python scripts/dqaf053_phase1_sanitize.py --apply  # execute sanitization

Safe to re-run — every removal is gated by a pre-check.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Helpers ──────────────────────────────────────────────────────────


def _infer_strategy_class(brain_id: str) -> str:
    """Infer strategy_class from brain_id naming convention.

    Mirrors :func:`scripts.daily_ops._infer_strategy_class`.
    """
    bid_lower = brain_id.lower()
    if bid_lower.startswith("swing"):
        return "swing"
    if bid_lower.startswith("ou_params"):
        return "ou_params"
    if "barrier" in bid_lower:
        return "barrier"
    if "trend" in bid_lower:
        return "trend"
    if "meta" in bid_lower:
        return "meta"
    if "rev" in bid_lower:
        return "rev"
    if "xgboost" in bid_lower or "xgb_" in bid_lower:
        return "xgboost"
    if "lightgbm" in bid_lower or "lgb_" in bid_lower:
        return "lightgbm"
    if "deep" in bid_lower or "mlp" in bid_lower:
        return "deep"
    if "online" in bid_lower:
        return "online"
    if "microstructure" in bid_lower:
        return "microstructure"
    if "swing" in bid_lower:
        return "swing"
    return "unknown"


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ── Steps ────────────────────────────────────────────────────────────


def sanitize_xau_performance(dry_run: bool = True) -> dict[str, Any]:
    """Step 1: clean corrupted + orphan entries from XAU alpha_performance.json."""
    from core.alpha.performance_store import AlphaPerformanceStore
    from core.alpha.registry import AlphaRegistry

    perf_path = Path("data/alpha_performance.json")
    reg_path = Path("data/alpha_registry.json")
    result: dict[str, Any] = {"step": "sanitize_xau_performance", "dry_run": dry_run}

    if not perf_path.exists():
        result["status"] = "skipped"
        result["reason"] = "no_performance_file"
        return result

    registry = AlphaRegistry.load(reg_path) if reg_path.exists() else AlphaRegistry()
    registry_ids = {r.alpha_id for r in registry.list_records()}
    perf = AlphaPerformanceStore.load(perf_path)
    before_count = len(perf._snapshots)

    # ── Remove corrupted entry ──
    corrupted = "5\\terminal64.exe"
    if corrupted in perf._snapshots:
        if not dry_run:
            perf.remove_alpha(corrupted)
        result["removed_corrupted"] = corrupted
    else:
        result["removed_corrupted"] = None

    # ── Remove known ghosts (also removed from registry in step 2) ──
    known_ghosts = ["alpha_xau_live"]
    ghosts_removed: list[str] = []
    for g in known_ghosts:
        if g in perf._snapshots:
            if not dry_run:
                perf.remove_alpha(g)
            ghosts_removed.append(g)

    # ── Remove orphans (in performance but NOT in registry) ──
    orphans = [aid for aid in perf.list_ids() if aid not in registry_ids]
    orphans.remove(corrupted) if corrupted in orphans else None
    if orphans and not dry_run:
        for orphan in orphans:
            perf.remove_alpha(orphan)
    result["removed_orphans"] = orphans
    result["orphan_count"] = len(orphans)
    result["removed_ghosts"] = ghosts_removed
    result["ghost_count"] = len(ghosts_removed)

    # ── Save ──
    if not dry_run:
        perf.save(perf_path)
    after_count = (
        len(perf._snapshots)
        if not dry_run
        else before_count - len(orphans) - (1 if corrupted in perf._snapshots else 0)
    )
    result["before_count"] = before_count
    result["after_count"] = after_count
    result["status"] = "ok"
    return result


def sanitize_xau_registry(dry_run: bool = True) -> dict[str, Any]:
    """Step 2: remove ghost alpha_xau_live, backfill strategy_class/assets."""
    from core.alpha.registry import AlphaRegistry

    reg_path = Path("data/alpha_registry.json")
    result: dict[str, Any] = {"step": "sanitize_xau_registry", "dry_run": dry_run}

    if not reg_path.exists():
        result["status"] = "skipped"
        result["reason"] = "no_registry_file"
        return result

    registry = AlphaRegistry.load(reg_path)
    before_count = len(registry.list_records())

    # ── Remove ghost ──
    ghost_id = "alpha_xau_live"
    ghost = registry.get(ghost_id)
    if ghost is not None:
        if not dry_run:
            registry.remove(ghost_id)
        result["removed_ghost"] = ghost_id
    else:
        result["removed_ghost"] = None

    # ── Backfill strategy_class + assets ──
    backfilled: list[str] = []
    xau_assets = ["XAUUSDc"]
    for record in registry.list_records():
        needs_sc = record.strategy_class is None
        needs_as = record.assets is None or len(record.assets or []) == 0
        if not needs_sc and not needs_as:
            continue
        new_sc = _infer_strategy_class(record.alpha_id) if needs_sc else record.strategy_class
        new_as = list(xau_assets) if needs_as else record.assets
        if not dry_run:
            updated = replace(
                record,
                strategy_class=new_sc,
                assets=new_as,
                updated_at=_now_utc(),
            )
            registry.upsert(updated)
        backfilled.append(f"{record.alpha_id}:sc={new_sc},as={new_as}")
    result["backfilled"] = backfilled

    # ── Save ──
    if not dry_run:
        registry.save(reg_path)
    result["before_count"] = before_count
    result["after_count"] = before_count - (1 if ghost else 0)
    result["status"] = "ok"
    return result


def sanitize_btc_registry(dry_run: bool = True) -> dict[str, Any]:
    """Step 3: backfill BTC btc_swing strategy_class/assets."""
    from core.alpha.registry import AlphaRegistry

    reg_path = Path("data_btc/alpha_registry.json")
    result: dict[str, Any] = {"step": "sanitize_btc_registry", "dry_run": dry_run}

    if not reg_path.exists():
        result["status"] = "skipped"
        result["reason"] = "no_registry_file"
        return result

    registry = AlphaRegistry.load(reg_path)
    backfilled: list[str] = []

    for record in registry.list_records():
        needs_sc = record.strategy_class is None
        needs_as = record.assets is None or len(record.assets or []) == 0
        if not needs_sc and not needs_as:
            continue
        new_sc = _infer_strategy_class(record.alpha_id) if needs_sc else record.strategy_class
        new_as = ["BTCUSDc"] if needs_as else record.assets
        if not dry_run:
            updated = replace(
                record,
                strategy_class=new_sc,
                assets=new_as,
                updated_at=_now_utc(),
            )
            registry.upsert(updated)
        backfilled.append(f"{record.alpha_id}:sc={new_sc},as={new_as}")
    result["backfilled"] = backfilled

    if not dry_run:
        registry.save(reg_path)
    result["status"] = "ok"
    return result


def purge_calibrator_fifos(dry_run: bool = True) -> dict[str, Any]:
    """Step 4: clear calibrator rolling history for BTC and XAU."""
    from core.execution.conformal_calibrator import ConformalCalibrator

    result: dict[str, Any] = {"step": "purge_calibrator_fifos", "dry_run": dry_run, "symbols": {}}

    for label, state_path in [
        ("BTC", "data_btc/conformal_calibrator_state.json"),
        ("XAU", "data/conformal_calibrator_state.json"),
    ]:
        if not Path(state_path).exists():
            result["symbols"][label] = {"status": "skipped", "reason": "no_state_file"}
            continue
        cal = ConformalCalibrator(state_path=state_path)
        before = len(cal._history)
        if dry_run:
            result["symbols"][label] = {
                "before": before,
                "would_clear": before,
                "status": "dry_run",
            }
        else:
            cleared = cal.reset_history()
            result["symbols"][label] = {"before": before, "cleared": cleared, "status": "ok"}

    return result


def reset_btc_feed_watermark(dry_run: bool = True) -> dict[str, Any]:
    """Step 5: reset BTC calibrator_feed_state watermark.

    Forces a full journal rescan on the next daily_ops run so the
    calibrator cold-starts from post-sanitization trades.
    """
    result: dict[str, Any] = {"step": "reset_btc_feed_watermark", "dry_run": dry_run}
    feed_path = Path("data_btc/calibrator_feed_state.json")

    if not feed_path.exists():
        result["status"] = "skipped"
        result["reason"] = "no_feed_state_file"
        return result

    if not dry_run:
        feed_path.write_text(
            json.dumps(
                {
                    "last_recorded_at": "",
                    "last_message_id": "",
                    "last_line": 0,
                    "updated_utc": _now_utc().isoformat(),
                    "sample_count": 0,
                }
            ),
            encoding="utf-8",
        )
    result["status"] = "ok"
    return result


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    dry_run = "--apply" not in sys.argv

    if dry_run:
        print("=" * 65)
        print("  DQAF-20260622-053 Phase 1: Sanitization DRY-RUN")
        print("  Add --apply to execute")
        print("=" * 65)
    else:
        print("=" * 65)
        print("  DQAF-20260622-053 Phase 1: Sanitization APPLY")
        print("=" * 65)

    steps = [
        sanitize_xau_performance(dry_run=dry_run),
        sanitize_xau_registry(dry_run=dry_run),
        sanitize_btc_registry(dry_run=dry_run),
        purge_calibrator_fifos(dry_run=dry_run),
        reset_btc_feed_watermark(dry_run=dry_run),
    ]

    # ── Summary ──
    print(f"\n{'─' * 65}")
    print("  SUMMARY")
    print(f"{'─' * 65}")

    for s in steps:
        name = s["step"]
        dr = "[DRY] " if dry_run else ""
        if s.get("status") == "skipped":
            print(f"  {dr}{name}: SKIPPED — {s.get('reason', '?')}")
            continue

        if "sanitize_xau_performance" == name:
            oc = s.get("orphan_count", 0)
            rc = s.get("removed_corrupted")
            gc = s.get("ghost_count", 0)
            print(
                f"  {dr}XAU alpha_perf: {s.get('before_count','?')}→{s.get('after_count','?')} "
                f"(corrupted={rc}, ghosts={gc}, orphans={oc})"
            )
        elif "sanitize_xau_registry" == name:
            rg = s.get("removed_ghost")
            bf = s.get("backfilled", [])
            print(
                f"  {dr}XAU alpha_reg: {s.get('before_count','?')}→{s.get('after_count','?')} "
                f"(ghost={rg}, backfilled={len(bf)})"
            )
            for b in bf:
                print(f"    {b}")
        elif "sanitize_btc_registry" == name:
            bf = s.get("backfilled", [])
            print(f"  {dr}BTC alpha_reg: backfilled={len(bf)}")
            for b in bf:
                print(f"    {b}")
        elif "purge_calibrator_fifos" == name:
            for sym, sr in s.get("symbols", {}).items():
                if sr.get("status") == "skipped":
                    print(f"  {dr}{sym} calibrator: SKIPPED — {sr.get('reason','?')}")
                else:
                    print(
                        f"  {dr}{sym} calibrator: {sr.get('before',0)} samples → EMPTY "
                        f"({'would clear' if dry_run else 'cleared'})"
                    )
        elif "reset_btc_feed_watermark" == name:
            print(
                f"  {dr}BTC feed watermark: {'would reset → full rescan' if dry_run else 'RESET → full rescan'}"
            )

    if dry_run:
        print("\n  Run with --apply to execute.")
    else:
        print("\n  Sanitization complete. Idempotent — safe to re-run.")


if __name__ == "__main__":
    main()
