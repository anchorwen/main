"""Strategy exit config validation — pure function extracted from live_cycle.py.

Strangler Fig #22 (FIX-20260619-022): Extracted validate_strategy_exit_configs()
as a pure function with zero I/O, zero state dependencies, and deterministic
output.  Catches YAML configuration drift before it causes silent runtime
misbehavior.
"""

from __future__ import annotations

# ── Expected exit configuration keys ───────────────────────────────────────
# Any key in a strategy's ``exit:`` block that is NOT in this set triggers
# a configuration warning (the key is silently ignored at runtime).
_EXPECTED_EXIT_KEYS: set[str] = {
    "flip_exit_enabled",
    "flip_threshold",
    "zscore_exit_enabled",
    "time_exit_cycles",
    "min_r_for_hold",
    "confidence_decay_exit",
    "hesitation_cycles",
    "trail_enabled",
    "trail_atr_mult",
    "trail_atr_mult_low",
    "trail_atr_mult_high",
    "trail_activation_atr",
    "breakeven_threshold_atr",
    "max_hold_cycles",
    "ev_trajectory_enabled",
    "kalman_velocity_threshold_bps",
    "grace_period_emergency_r",
}


def validate_strategy_exit_configs(strategy_configs: dict) -> list[str]:
    """Check all strategy ``exit:`` blocks for unknown keys.

    Returns a list of warning strings (empty if clean).  Unknown keys are
    silently ignored at runtime, so this catches configuration drift before
    it causes surprising behaviour.
    """
    warnings: list[str] = []
    for name, scfg in strategy_configs.items():
        exit_cfg = scfg.get("exit", {}) if isinstance(scfg, dict) else {}
        unknown = set(exit_cfg) - _EXPECTED_EXIT_KEYS
        if unknown:
            warnings.append(f"strategy_lines.{name}.exit: unknown keys {sorted(unknown)}")
    return warnings
