"""Immutable trading contracts — the single source of truth for all inter-module
data in the live hot path.

All four boundaries use frozen :class:`dataclass` instances with ``slots=True``.
No bare dicts cross module boundaries.  Every module declares what it produces
and what it consumes.

Boundary map::

    Brain Adapters ──→ Parliament ──→ Strategy Lines ──→ Guards ──→ Dispatch
       (BrainSignal)   (ConsensusResult)  (StrategyDecision)

Failure contract: :class:`DegradedResult` replaces every ``except Exception: pass``
so downstream modules can decide whether to degrade, skip, or circuit-break.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── Direction type shared across all contracts ──
Direction = Literal["long", "short", "neutral"]
TradeDirection = Literal["long", "short"]  # never neutral at dispatch


# ═══════════════════════════════════════════════════════════════════════════════
# 1.1  Data Contracts — module outputs
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class BrainSignal:
    """Standardised signal from a single brain adapter to the parliament.

    Replaces the 14-field :class:`BrainDecisionProposal` whose ``prediction``
    dict carried direction/confidence/probabilities with no schema enforcement.
    The old ``up_probability`` / ``down_probability`` comparison was the root
    cause of FIX-20260522-013 (sign-flip bug) — this contract eliminates that
    entire class of error by carrying only the *decided* direction.
    """

    brain_id: str
    direction: Direction
    confidence: float  # [0.0, 1.0]
    raw_score: float  # original model output (BPS, z-score, logit, …)

    # Config-level binary permission gate (FIX-20260625-139).
    # 0.0 = muted (shadow/retired) — brain cannot vote regardless of PnL.
    # >0.0 = voting rights proportional to weight.
    # Read from brain JSON config; carried through pipeline to _compute_weighted()
    # fail-fast gate in contract_groups.py.
    vote_weight: float = 1.0

    # Diagnostics — carried through the pipeline but not used for voting.
    fallback: bool = False
    runtime_ms: float = 0.0

    # Per-adapter diagnostics (z_score, theta, half_life, feature_count, etc.).
    # Preserves the information that was formerly in extensions.raw_outputs
    # so that shadow_recorder can write complete brain_votes entries.
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    """Group-level consensus from parliament to a strategy line.

    Replaces the 10-field :class:`GroupSignal`.  Five of those fields were
    dropped between ``_compute_consensus()`` and ``evaluate()`` in the current
    code — this contract makes the surviving subset explicit.
    """

    direction: Direction
    confidence: float  # [0.0, 1.0]
    supporting_brains: list[str] = field(default_factory=list)
    dissenting_brains: list[str] = field(default_factory=list)
    brain_ids: list[str] = field(default_factory=list)  # all brains in the group

    # Counts — carried for diagnostics / governance logging.
    supporting_count: int = 0
    total_count: int = 0


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """Final trade decision from strategy line to the execution pipeline.

    Replaces the 20-field :class:`StrategyDecision` dataclass in
    ``core/execution/strategy_line.py``.  Many of those fields
    (``entry_context``, ``entry_z_score``, ``p_win``, ``kelly_mult``,
    ``brain_votes``) were dropped between layers — this contract captures only
    what the execution pipeline actually reads.
    """

    strategy_name: str
    direction: TradeDirection
    confidence: float
    volume: float
    sl: float
    tp: float

    # Required by execution pipeline.
    magic: int = 0
    hard_sl: float = 0.0
    brain_ids: list[str] = field(default_factory=list)
    reason: str = "approved"

    # Diagnostics — carried for journal / audit trail, never gating.
    p_win: float = 0.0
    kelly_mult: float = 1.0

    # Gate diagnostics — per-cycle gate audit (which gate blocked and why).
    # Populated by strategy_line.evaluate() when should_trade=False.
    # Dict is mutable even though the dataclass is frozen (the reference is
    # frozen, not the contents).
    gate_diag: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# 1.2  Failure Contract — replaces every ``except Exception: pass``
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DegradedResult:
    """Standardised degradation signal emitted when a module cannot produce
    its normal output.

    Every ``except Exception: pass`` in the hot path must be replaced with
    an instance of this class.  Downstream modules check ``isinstance(x,
    DegradedResult)`` before consuming, and decide whether to:

    - Use a fallback (e.g. last-known-good value),
    - Skip the cycle's Alpha phase (management only),
    - Increment the circuit-breaker counter (3 consecutive → suspend).
    """

    module: str
    reason: str
    error_detail: str = ""
    fallback_data: dict[str, Any] | None = None
