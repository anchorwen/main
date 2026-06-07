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
import os
import time
from pathlib import Path
from typing import Any


def _utc_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(tzinfo=None).isoformat()


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
    intraday_dd_active: bool = False,
) -> None:
    """Snapshot all execution guard state to disk (atomic write via tmp+replace)."""
    p = Path(save_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    payload: dict[str, Any] = {
        "version": 2,  # bumped — new fields
        "saved_at_utc": _utc_iso(),
        "budgets": {},
        "cooldown_registry": {},
        "family_entry_tracker": {},
        "sl_streak_blocks": sl_streak_blocks or {},
        "sl_streak_global_block": sl_streak_global_block,
        "consecutive_degraded": consecutive_degraded,
        "circuit_breaker_tripped": circuit_breaker_tripped,
        "intraday_dd_active": intraday_dd_active,
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

    # ── Atomic write ──
    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
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
                try:
                    budget.load_state(budget_snapshot)
                except Exception:  # noqa: BLE001
                    pass

    # ── Restore cooldown registry ──
    cd_data = data.get("cooldown_registry", {})
    if cd_data and state._cooldown_registry is not None:
        try:
            state._cooldown_registry.load_state(cd_data)
        except Exception:  # noqa: BLE001
            pass

    # ── Restore family entry tracker ──
    fe_data = data.get("family_entry_tracker", {})
    if fe_data and state._family_entry_tracker is not None:
        try:
            state._family_entry_tracker.load_state(fe_data)
        except Exception:  # noqa: BLE001
            pass

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
        state._consecutive_degraded_cycles,
        data.get("consecutive_degraded", 0),
    )
    if data.get("circuit_breaker_tripped", False):
        state._circuit_breaker_tripped = True

    # Intraday DD kill — prevents restart from clearing drawdown block
    if data.get("intraday_dd_active", False):
        state.block_new_entries = True
