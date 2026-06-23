"""Execution state persistence — survives process restart.

FIX-20260603-072: Global Execution State Hydration.

On restart, three in-memory guard components are wiped clean:
  - CooldownRegistry  (per-strategy absolute refractory period)
  - FamilyEntryTracker (cross-strategy entry spacing)
  - StrategyBudget     (daily PnL, SL cooldown, consecutive-loss pause)

This module snapshots them to disk on every save cycle and restores
them during startup bootstrap so that a restart does not clear the
defense-in-depth gate state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import fail_open_guard
from core.runtime.time_utils import _utc_iso  # consolidated


def save_execution_state(
    save_path: str | Path,
    strategies: dict[str, Any],
    cooldown_registry: Any,
    family_entry_tracker: Any,
    *,
    # ── FIX-20260605-120: additional guard state ──
    sl_streak_blocks: dict[str, float] | None = None,
    sl_streak_global_block: float = 0.0,
    consecutive_degraded: int = 0,
    circuit_breaker_tripped: bool = False,
    circuit_breaker_tripped_at: float = 0.0,
    intraday_dd_active: bool = False,
    # ── DQAF-20260608-003: full counter persistence ──
    consecutive_stale_cycles: int = 0,
    consecutive_stale_features: int = 0,
    circuit_breaker_trip_reason: str = "",
    # ── DQAF-20260615-004: known_open_tickets persistence ──
    known_open_tickets: dict[str, Any] | None = None,
) -> None:
    """Snapshot all execution guard state to disk (atomic write via tmp+replace)."""
    p = Path(save_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    payload: dict[str, Any] = {
        "version": 3,  # DQAF-20260615-004: + known_open_tickets
        "schema_version": "execution_state.v3",
        "saved_at_utc": _utc_iso(),
        "budgets": {},
        "known_open_tickets": {},
        "cooldown_registry": {},
        "family_entry_tracker": {},
        "sl_streak_blocks": sl_streak_blocks or {},
        "sl_streak_global_block": sl_streak_global_block,
        "consecutive_degraded": consecutive_degraded,
        "circuit_breaker_tripped": circuit_breaker_tripped,
        "circuit_breaker_tripped_at": circuit_breaker_tripped_at,
        "intraday_dd_active": intraday_dd_active,
        # ── DQAF-20260608-003: full counter persistence ──
        "consecutive_stale_cycles": consecutive_stale_cycles,
        "consecutive_stale_features": consecutive_stale_features,
        "circuit_breaker_trip_reason": circuit_breaker_trip_reason,
    }
    # ── DQAF-20260615-004: Persist known_open_tickets ──
    if known_open_tickets:
        payload["known_open_tickets"] = {
            str(t): {k: v for k, v in data.items()} for t, data in known_open_tickets.items()
        }

    # ── Strategy budgets ──
    for sname, strategy in strategies.items():
        budget = getattr(strategy, "budget", None)
        if budget is not None and hasattr(budget, "get_state"):
            payload["budgets"][sname] = budget.get_state()

    # ── Cooldown registry ──
    if cooldown_registry is not None and hasattr(cooldown_registry, "get_state"):
        payload["cooldown_registry"] = cooldown_registry.get_state()

    # ── Family entry tracker ──
    if family_entry_tracker is not None and hasattr(family_entry_tracker, "get_state"):
        payload["family_entry_tracker"] = family_entry_tracker.get_state()

    # ── DQAF-20260616-002/P1.2: State machine invariant checks ────────────
    # Auto-heal detectable inconsistencies before persisting.  These are
    # tagged "auto_heal" for easy grep-based weekly reconciliation
    # (count the drifts that occurred without crashing the system).
    _heals: list[str] = []
    if not circuit_breaker_tripped and circuit_breaker_tripped_at > 0:
        _heals.append(
            f"auto_heal:circuit_breaker_tripped_at_reset:"
            f"breaker_not_tripped_but_timestamp={circuit_breaker_tripped_at:.0f}"
        )
        circuit_breaker_tripped_at = 0.0
        payload["circuit_breaker_tripped_at"] = 0.0
    if circuit_breaker_tripped and not circuit_breaker_trip_reason:
        _heals.append("auto_heal:circuit_breaker_trip_reason_defaulted")
        circuit_breaker_trip_reason = "unknown_trip"
        payload["circuit_breaker_trip_reason"] = "unknown_trip"
    if _heals:
        print(
            json.dumps(
                {
                    "event": "state_invariant_auto_heal",
                    "time": _utc_iso(),
                    "heal_count": len(_heals),
                    "heals": _heals,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    # ── Atomic write (DQAF-046 Plan B) ──
    # StateWriter resolves the canonical path from data_dir + catalog path_template.
    # This is correct when save_path lives under data/ or data_btc/ (production).
    # For non-canonical paths (e.g. pytest tmpdir), write directly to save_path
    # so callers can control the exact file location.
    _in_data_tree = any(parent.name in ("data", "data_btc") for parent in p.parents)
    if _in_data_tree:
        try:
            from core.state.catalog import lookup
            from core.state.writer import StateWriter

            writer = StateWriter.from_state_path(save_path)
            writer.write_artifact(lookup("EXECUTION_STATE"), writer._symbol, payload)
        except OSError:
            pass  # Disk write failure is non-fatal
    else:
        # Non-canonical path — direct atomic write (preserves test compatibility)
        try:
            _tmp = p.with_suffix(p.suffix + ".tmp")
            _tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            _tmp.replace(p)
        except OSError:
            pass  # Disk write failure is non-fatal


def load_execution_state(save_path: str | Path) -> dict[str, Any] | None:
    """Load a previously persisted execution state snapshot.

    Returns None if the file does not exist, is unreadable, or is older
    than 24 hours (stale — market regime has shifted).
    """
    p = Path(save_path)
    if not p.exists():
        return None

    # Reject state older than 24 hours
    try:
        age_h = (time.time() - p.stat().st_mtime) / 3600
        if age_h > 24.0:
            try:  # noqa: SIM105
                p.unlink()
            except OSError:
                pass
            return None
    except OSError:
        return None

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(data, dict) or "version" not in data:
        return None

    return data


def restore_execution_state(
    state: Any,
    strategies: dict[str, Any],
    data_dir: str = "data",
) -> None:
    """Restore execution guard state from disk into live objects.

    Called once after strategy building but before the first evaluation
    cycle.  Silently returns if no persisted state exists (first run).

    Budget hydration is guarded by ``last_trade_day`` — if the persisted
    state is from a previous calendar day, daily counters are NOT restored
    (they will reset on the next trade record).
    """
    _path = Path(data_dir) / "state" / "execution_state.json"
    data = load_execution_state(_path)
    if data is None:
        return

    # ── Restore budgets ──
    budgets_data: dict[str, Any] = data.get("budgets", {})
    if budgets_data:
        for sname, budget_snapshot in budgets_data.items():
            strategy = strategies.get(sname)
            if strategy is None:
                continue
            budget = getattr(strategy, "budget", None)
            if budget is not None and hasattr(budget, "load_state"):
                with fail_open_guard("ExecutionState:BudgetLoad"):
                    budget.load_state(budget_snapshot)

    # ── Restore cooldown registry ──
    cd_data = data.get("cooldown_registry", {})
    if cd_data and state._cooldown_registry is not None:
        with fail_open_guard("ExecutionState:CooldownLoad"):
            state._cooldown_registry.load_state(cd_data)

    # ── Restore family entry tracker ──
    fe_data = data.get("family_entry_tracker", {})
    if fe_data and state._family_entry_tracker is not None:
        with fail_open_guard("ExecutionState:FamilyEntryLoad"):
            state._family_entry_tracker.load_state(fe_data)

    # ── FIX-20260605-120: restore additional guard state ──
    # SL streak cooldown timers — prevents restart from clearing SL blocks
    _sb = data.get("sl_streak_blocks", {})
    if _sb:
        state.sl_streak_blocked_until.update(_sb)
    _sg = data.get("sl_streak_global_block", 0.0)
    if _sg > 0:
        state.sl_streak_blocked_all_until = max(state.sl_streak_blocked_all_until, _sg)

    # Circuit breaker state — prevents restart from clearing degraded-cycle counter
    state._consecutive_degraded_cycles = max(
        getattr(state, "_consecutive_degraded_cycles", 0),
        data.get("consecutive_degraded", 0),
    )
    if data.get("circuit_breaker_tripped", False):
        state._circuit_breaker_tripped = True
        state._circuit_breaker_tripped_at = max(
            getattr(state, "_circuit_breaker_tripped_at", 0.0),
            data.get("circuit_breaker_tripped_at", 0.0),
        )
        # ── DQAF-20260608-003: restore trip reason for diagnostics ──
        if data.get("circuit_breaker_trip_reason", ""):
            state._circuit_breaker_trip_reason = data["circuit_breaker_trip_reason"]

    # ── DQAF-20260608-003: restore stale counters ──
    # These counters were previously NOT persisted, causing "ghost breaker"
    # after restart: breaker=True but the counter that triggered it was lost.
    # Now restored with max() semantics (disk may be stale vs in-memory).
    # Use getattr with default 0 for backward compatibility with tests/mocks.
    state._consecutive_stale_cycles = max(
        getattr(state, "_consecutive_stale_cycles", 0),
        data.get("consecutive_stale_cycles", 0),
    )
    state._consecutive_stale_features = max(
        getattr(state, "_consecutive_stale_features", 0),
        data.get("consecutive_stale_features", 0),
    )

    # Intraday DD kill — prevents restart from clearing drawdown block
    if data.get("intraday_dd_active", False):
        state.block_new_entries = True
