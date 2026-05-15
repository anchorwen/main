"""Centralized magic-number → strategy-name mapping.

Single source of truth — referenced by live_cycle, live_intent_loop,
backtest_runner, and any code that needs to resolve a MT5 magic number
to its owning strategy line.

Synced with configs/live.yaml strategy_lines.*.magic (2026-05-12).
"""

from __future__ import annotations

MAGIC_TO_STRATEGY: dict[int, str] = {
    90001: "barrier_12bar",
    90002: "micro_3bar",
    90003: "statarb_dynamic",
    90101: "micro_m15",
    90103: "statarb_m15",
    90201: "micro_h1",
    # Swing strategies (TF-specific barrier contracts, D1 features)
    90301: "daily_swing",
    90310: "m15_swing",
    90320: "m30_swing",
    90330: "h1_swing",
    90340: "h4_swing",
}

# Reverse lookup
STRATEGY_TO_MAGIC: dict[str, int] = {v: k for k, v in MAGIC_TO_STRATEGY.items()}
