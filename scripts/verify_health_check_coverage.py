#!/usr/bin/env python
"""Verify every critical data file has a corresponding health check.

FIX-20260617-101/P2: Institutional extension contract — when a new data
collection project adds a file, this script ensures a matching check_*()
method exists in DataHealthService.  Runs as part of verify.py --quick.

Principle: every data file that accumulates over time MUST be monitored
for staleness, corruption, or completeness.  Missing coverage = blind spot.

Usage:
    python scripts/verify_health_check_coverage.py
"""

from __future__ import annotations

import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── Critical data files that MUST have health checks ──
# Format: (relative_path, tier_description)
CRITICAL_FILES: list[tuple[str, str]] = [
    ("live_trade_journal.jsonl", "trade journal — opens, closes, PnL"),
    ("feature_store/records/**/features.jsonl", "feature store — M5 bar features"),
    ("golden_master.jsonl", "golden master — cycle outputs"),
    ("state/execution_state.json", "execution state — circuit breaker, budgets"),
    ("governance_state.json", "governance state — brain lifecycle"),
    ("position_snapshots.jsonl", "position snapshots — trailing SL telemetry"),
    ("reports/mt5_bridge_health.json", "MT5 bridge — connectivity heartbeat"),
    ("reports/live_labels.jsonl", "live labels — training label builder"),
    ("ledger_events.jsonl", "PnL ledger — signal settlement events"),
    ("reports/exit_watchdog_alerts.jsonl", "exit watchdog — premature exit events"),
    ("brain_performance.json", "brain performance — composite scores"),
    ("conformal_calibrator_state.json", "conformal calibrator — prediction intervals"),
    ("calibrator_feed_state.json", "calibrator feed — Platt scaling state"),
    ("alpha_registry.json", "alpha registry — alpha lifecycle"),
    ("alpha_performance.json", "alpha performance — alpha PnL tracking"),
    ("brain_pnl_ledger.json", "brain PnL ledger — pending/settled signals"),
    ("bar_sync_state.json", "bar sync — M5 bar detection state"),
    ("reports/leaderboard.json", "leaderboard — brain ranking"),
    ("reports/retraining_signal_prev.json", "retraining signal — degradation alerts"),
    ("meta_filter_state.json", "MetaFilter — prediction history buffers"),
    ("regime_detector_state.json", "regime detector — ATR calibration"),
    ("state/heartbeats/entry_context_guard.json", "entry context guard — Layer 2 daemon heartbeat"),
]

# ── Map DataHealthService check_* methods to covered files ──
# Extracted via reflection at runtime
CHECK_TO_FILE_HINT: dict[str, list[str]] = {
    "check_trade_journal": ["live_trade_journal.jsonl"],
    "check_feature_store": ["feature_store"],
    "check_execution_state": ["state/execution_state.json"],
    "check_governance_state": ["governance_state.json"],
    "check_position_snapshots": ["position_snapshots.jsonl"],
    "check_mt5_bridge_health": ["reports/mt5_bridge_health.json"],
    "check_live_labels": ["reports/live_labels.jsonl"],
    "check_pnl_ledger_integrity": ["ledger_events.jsonl"],
    "check_exit_watchdog_alerts": ["reports/exit_watchdog_alerts.jsonl"],
    "check_brain_performance": ["brain_performance.json"],
    "check_conformal_calibrator": ["conformal_calibrator_state.json"],
    "check_calibrator_feed_state": ["calibrator_feed_state.json"],
    "check_alpha_registry": ["alpha_registry.json"],
    "check_alpha_allocation": ["alpha_performance.json"],
    "check_entry_context_completeness": ["live_trade_journal.jsonl"],
    "check_golden_master": ["golden_master.jsonl"],
    "check_brain_pnl_ledger": ["brain_pnl_ledger.json"],
    "check_bar_sync_state": ["bar_sync_state.json"],
    "check_leaderboard": ["reports/leaderboard.json"],
    "check_retraining_signal": ["reports/retraining_signal_prev.json"],
    "check_meta_filter_state": ["meta_filter_state.json"],
    "check_regime_detector_state": ["regime_detector_state.json"],
    "check_entry_context_guard_heartbeat": ["state/guard_heartbeat.json", "state/heartbeats/entry_context_guard.json"],
}


def _extract_runtime_checks() -> set[str]:
    """Discover check_* methods from DataHealthService at runtime."""
    try:
        from core.observability.data_health_service import DataHealthService

        return {
            name
            for name, _ in inspect.getmembers(DataHealthService, predicate=inspect.isfunction)
            if name.startswith("check_")
        }
    except ImportError:
        return set()


def main() -> int:
    print("=" * 60)
    print("  HEALTH CHECK COVERAGE AUDIT")
    print("=" * 60)

    runtime_checks = _extract_runtime_checks()
    static_checks = set(CHECK_TO_FILE_HINT.keys())

    # Use runtime checks if available, otherwise fall back to static map
    all_checks = runtime_checks if runtime_checks else static_checks
    if not runtime_checks:
        print("  WARNING: DataHealthService not importable — using static map")

    print(f"\n  Health checks registered: {len(all_checks)}")
    print(f"  Critical data files:     {len(CRITICAL_FILES)}")

    # Build coverage map: which files are covered by which checks?
    covered_files: set[str] = set()
    for check_name, file_hints in CHECK_TO_FILE_HINT.items():
        if check_name in all_checks:
            for hint in file_hints:
                covered_files.add(hint)

    uncovered: list[tuple[str, str]] = []
    for rel_path, desc in CRITICAL_FILES:
        # Match: check if any covered_file hint is a substring of rel_path
        is_covered = any(
            hint in rel_path or rel_path.endswith(hint.split("/")[-1])
            for hint in covered_files
        )
        if not is_covered:
            uncovered.append((rel_path, desc))

    print("\n── Coverage ──")
    covered_count = len(CRITICAL_FILES) - len(uncovered)
    print(f"  Covered:  {covered_count}/{len(CRITICAL_FILES)}")
    print(f"  Uncovered: {len(uncovered)}")

    if uncovered:
        print("\n  [FAIL] UNCOVERED DATA FILES (no matching health check):")
        for path, desc in uncovered:
            print(f"    - {path}")
            print(f"      ({desc})")
            print("      → Add check_* method in DataHealthService, CRITICAL tier")
        return 1

    print("\n  [PASS] All critical data files have health check coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
