"""Timeframe auto-scaling — pure function extracted from live_cycle.py.

Strangler Fig #21 (FIX-20260619-021): Extracted apply_timeframe_scaling()
as a pure function with zero I/O, zero state dependencies, and deterministic
output.  YAML authors write physically intuitive values (e.g. H1 bar counts)
and this module converts them to M5-bar cycle units.
"""

from __future__ import annotations

# ── Timeframe → M5-bar multiplier ──────────────────────────────────────────
# Maps human-readable timeframe labels to M5-bar multipliers.
# For sqrt(t)-based ATR scaling, we use sqrt(multiplier) because variance grows
# linearly with time (random walk), so stddev grows with sqrt(time).
TIMEFRAME_TO_M5: dict[str, int] = {
    "M5": 1,
    "M15": 3,
    "M30": 6,
    "H1": 12,
    "H4": 48,
    "D1": 288,
}


def apply_timeframe_scaling(strategy_configs: dict) -> dict:
    """Auto-scale human-readable exit parameters to M5-bar cycles.

    Transforms the strategy_configs dict in-place so that every consumer
    downstream (strategy evaluation, position management) receives values
    already expressed in M5-bar units.  YAML authors write the physically
    intuitive number (e.g. ``hesitation_cycles: 3`` on an H1 strategy means
    "3 x H1 bars"), and this function multiplies by the timeframe ratio.

    Returns the same dict (mutated) for call-site convenience.
    """
    for _name, scfg in strategy_configs.items():
        if not isinstance(scfg, dict):
            continue
        tf = str(scfg.get("timeframe", "M5"))
        mult = TIMEFRAME_TO_M5.get(tf, 1)

        exit_cfg = scfg.get("exit")
        if isinstance(exit_cfg, dict):
            # Scale hesitation_cycles
            raw_hesitation = exit_cfg.get("hesitation_cycles")
            if raw_hesitation is not None:
                exit_cfg["hesitation_cycles"] = int(raw_hesitation) * mult
            # Scale time_exit_cycles
            raw_time = exit_cfg.get("time_exit_cycles")
            if raw_time is not None:
                exit_cfg["time_exit_cycles"] = int(raw_time) * mult
            # Scale max_hold_cycles if present
            raw_max_hold = exit_cfg.get("max_hold_cycles")
            if raw_max_hold is not None:
                exit_cfg["max_hold_cycles"] = int(raw_max_hold) * mult

        # Stash the multiplier so downstream (SL/TP, Meta Exit) can use it
        scfg["_tf_mult"] = mult

    return strategy_configs
