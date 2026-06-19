"""Daily operations scheduler — extracted from live_cycle.py (Strangler Fig).

Runs the daily_ops pipeline within the live trading cycle, including:
- Persisting execution timestamp to prevent same-day re-trigger on restart
- Running scripts.daily_ops.run_daily_ops synchronously
- Resource cleanup (GC + feature store compaction)
- Governance re-evaluation after PnL data refresh
"""

from __future__ import annotations

import gc
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.runtime.live_cycle import LiveCycleConfig, LiveCycleState


from core.runtime.time_utils import _utc_iso  # consolidated


def _save_daily_ops_state(base_dir: str, ts: float) -> None:
    """Persist last daily_ops timestamp to disk."""
    try:
        state_dir = os.path.join(base_dir, "state")
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "daily_ops_state.json")
        with open(state_path, "w") as f:
            json.dump({"last_daily_ops_utc": ts}, f)
    except Exception as _exc:  # BLE001:REVIEWED
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "Failed to save daily_ops_state: %s", _exc, exc_info=True
        )


def run_scheduled_daily_ops(config: LiveCycleConfig, state: LiveCycleState) -> None:
    """Execute daily_ops pipeline synchronously within the current cycle."""
    print(
        json.dumps({"event": "daily_ops_scheduled", "time": _utc_iso()}, ensure_ascii=False),
        flush=True,
    )

    # Persist "decided to execute" BEFORE running to prevent edge reentry.
    # If the process crashes mid-execution, the persisted timestamp ensures
    # the post-restart date-based check skips re-trigger for the same day.
    state._last_daily_ops_utc = datetime.now(UTC).timestamp()
    _save_daily_ops_state(config.base_dir, state._last_daily_ops_utc)

    try:
        from scripts.daily_ops import run_daily_ops

        result = run_daily_ops(
            base_dir=config.base_dir,
            skip_shadow=True,
            skip_recap=True,
            mt5_terminal_path=config.mt5_terminal_path,
        )
        state._tracker_reload_pending = True  # daily_ops wrote enriched tracker to disk

        # Persist the full report to disk (CLI uses --output, API path doesn't)
        _report_path = os.path.join(config.base_dir, "reports", "ops_logs", "p1_daily_run.log")
        try:
            os.makedirs(os.path.dirname(_report_path), exist_ok=True)
            with open(_report_path, "a", encoding="utf-8") as _f:
                _f.write(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n")
        except OSError as _exc:
            print(
                json.dumps(
                    {
                        "event": "daily_ops_report_write_failed",
                        "path": _report_path,
                        "error": str(_exc),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        print(
            json.dumps(
                {
                    "event": "daily_ops_complete",
                    "time": _utc_iso(),
                    "steps": len(result.get("steps", [])),
                    "errors": result.get("errors", 0),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        # Resource cleanup: force GC + compact feature store + prune old labels
        _cleanup_started = time.perf_counter()
        try:
            gc.collect()
            # Compact local feature store to prevent unbounded JSONL growth
            try:
                from core.features.local_feature_store import LocalFeatureStore

                _store = LocalFeatureStore(base_dir=config.base_dir)
                _store.compact(retention_days=7)
            except Exception:  # BLE001:REVIEWED
                pass
            # FIX-20260601-047: prune label files older than 30 days
            try:
                _labels_dir = Path(config.base_dir) / "labels"
                if _labels_dir.exists():
                    _cutoff = time.time() - (30 * 86400)
                    _pruned = 0
                    for _lf in _labels_dir.glob("*.jsonl"):
                        try:
                            if _lf.stat().st_mtime < _cutoff:
                                _lf.unlink()
                                _pruned += 1
                        except OSError:
                            pass
                    if _pruned > 0:
                        print(
                            json.dumps(
                                {"event": "labels_pruned", "count": _pruned, "retention_days": 30},
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
            except Exception:  # BLE001:REVIEWED
                pass
            _cleanup_ms = (time.perf_counter() - _cleanup_started) * 1000.0
            print(
                json.dumps(
                    {"event": "resource_cleanup_complete", "cleanup_ms": round(_cleanup_ms, 1)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as _cleanup_exc:  # BLE001:REVIEWED
            print(
                json.dumps(
                    {"event": "resource_cleanup_failed", "error": str(_cleanup_exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # Re-run governance after daily_ops refreshes PnL data
        try:
            from core.feedback.brain_performance_tracker import BrainPerformanceTracker
            from core.feedback.brain_pnl_ledger import BrainPnLStore
            from core.governance.governance_service import GovernanceService
            from scripts.training.governance_scheduler import run_governance_cycle

            _pnl_path = os.path.join(config.base_dir, "brain_pnl_ledger.json")
            _gov_path = os.path.join(config.base_dir, "governance_state.json")

            _pnl_store = BrainPnLStore.load(_pnl_path) if os.path.exists(_pnl_path) else None
            _governance = (
                GovernanceService.load(_gov_path)
                if os.path.exists(_gov_path)
                else GovernanceService()
            )

            if _pnl_store is not None:
                _tracker = BrainPerformanceTracker(window_size=100)
                _gov_report = run_governance_cycle(
                    _tracker, _governance, dry_run=False, pnl_store=_pnl_store
                )
                _governance.save(_gov_path, lock_timeout=1.0)

                _applied = len(_gov_report.get("actions_applied", []))
                _flagged = len(_gov_report.get("actions_flagged", []))
                if _applied or _flagged:
                    print(
                        json.dumps(
                            {
                                "event": "daily_governance_cycle",
                                "time": _utc_iso(),
                                "actions_applied": _applied,
                                "actions_flagged": _flagged,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            else:
                print(
                    json.dumps(
                        {
                            "event": "daily_governance_skip",
                            "reason": "no_pnl_ledger",
                            "time": _utc_iso(),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        except Exception as _gov_exc:  # BLE001:REVIEWED
            print(
                json.dumps(
                    {"event": "daily_governance_error", "time": _utc_iso(), "error": str(_gov_exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
    except Exception as exc:  # BLE001:REVIEWED
        print(
            json.dumps(
                {"event": "daily_ops_error", "time": _utc_iso(), "error": str(exc)},
                ensure_ascii=False,
            ),
            flush=True,
        )
