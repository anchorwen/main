"""StrategyDecision — shared dataclass extracted from strategy_line.py.

Strangler Fig #18: extracted to break circular imports.
StrategyDecision is imported by 7 production files — meta_filter_routing and
trend_isolation_gates both need it, which previously forced lazy imports
inside strategy_line.evaluate().

This module imports ZERO symbols from core.execution.* — only stdlib + __future__.
By living in a leaf dependency, it eliminates the circular import chain:
  strategy_line → meta_filter_routing → StrategyDecision → strategy_line (was cycle)
  strategy_line → trend_isolation_gates → StrategyDecision → strategy_line (was cycle)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ── Cached git commit hash (lazy, once per process lifetime) ──────────────

_GIT_HASH_CACHE: str | None = None


def _get_cached_git_hash() -> str:
    """Return the current HEAD commit hash, cached for process lifetime."""
    global _GIT_HASH_CACHE
    if _GIT_HASH_CACHE is not None:
        return _GIT_HASH_CACHE
    try:
        import subprocess as _sp

        result = _sp.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            _GIT_HASH_CACHE = result.stdout.strip()
        else:
            _GIT_HASH_CACHE = "unknown"
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG — subprocess best-effort metadata
        _GIT_HASH_CACHE = "unknown"
    return _GIT_HASH_CACHE


# ── StrategyDecision dataclass ────────────────────────────────────────────


@dataclass
class StrategyDecision:
    """Output of one strategy line evaluation for one cycle."""

    strategy_name: str
    magic: int
    should_trade: bool
    direction: str  # "long", "short", or "neutral"
    confidence: float
    volume: float
    sl: float
    tp: float
    hard_sl: float
    brain_ids: list[str] = field(default_factory=list)
    brain_votes: list[dict[str, Any]] = field(default_factory=list)
    supporting_count: int = 0
    total_count: int = 0
    regime_mode: str = "full"  # "full" | "reduced" | "shadow"
    venue: str = "live"  # "live" | "shadow"
    reason: str = ""
    entry_z_score: float = 0.0
    entry_half_life: float = 0.0
    entry_context: dict[str, Any] = field(default_factory=dict)
    p_win: float = 0.5
    p_win_source: str = "unknown"
    p_win_degraded: bool = False
    kelly_mult: float = 1.0
    cold_explore: bool = False
    gate_diag: dict[str, Any] = field(default_factory=dict)
    decision_hash: str = ""
    evaluated_at: str = ""
    code_version: str = ""

    def __post_init__(self) -> None:
        """Auto-populate audit fields if not explicitly set."""
        if not self.evaluated_at:
            self.evaluated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if not self.code_version:
            self.code_version = _get_cached_git_hash()
