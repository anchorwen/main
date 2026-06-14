"""Conformal calibrator state contract — Pydantic validator for runtime state.

Column 2 (Institutional Data SLA): The calibrator state is validated on every
load and save.  Business logic invariants are checked alongside structural
validation — this is the highest form of contract testing.

Usage:
    from core.contracts.calibrator_contract import CalibratorState

    state = CalibratorState(**json.loads(state_path.read_text()))
    state.validate_business_invariants(warmup_samples=50)
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field, model_validator


class CalibratorHistoryEntry(BaseModel):
    """A single (p_win, label, timestamp) entry in the calibrator history."""

    p_win: float = Field(..., ge=0.0, le=1.0)
    label: int = Field(..., ge=-1, le=1)  # -1=loss, 0=breakeven, 1=win
    timestamp: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def p_win_must_be_finite(self) -> CalibratorHistoryEntry:
        if math.isnan(self.p_win) or math.isinf(self.p_win):
            raise ValueError(f"p_win cannot be NaN or Inf, got: {self.p_win}")
        return self


class CalibratorState(BaseModel):
    """Conformal calibrator state — validated on every save/load cycle.

    This contract enforces both structural integrity (types, ranges) AND
    business logic invariants (e.g., if you have enough history, you MUST
    have computation progress — cold-start is not a permanent state).
    """

    history: list[CalibratorHistoryEntry] = Field(default_factory=list)
    clamp_hits_upper: int = Field(default=0, ge=0)
    clamp_hits_lower: int = Field(default=0, ge=0)
    total_computations: int = Field(default=0, ge=0)
    cold_started: bool = False

    # ── Business Logic Invariant ──
    # If the calibrator has enough samples to be warm AND is not marked as
    # cold-started, BUT total_computations is still 0, we have the exact
    # bug that DQAF-20260614-002 fixed: cold_start_from_journal() set
    # _cold_started=True unconditionally, preventing compute_threshold()
    # from ever being called.

    def validate_business_invariants(self, warmup_samples: int = 50) -> list[str]:
        """Check business logic invariants. Returns list of violation messages."""
        violations: list[str] = []

        history_count = len(self.history)

        # Invariant 1: If warm and not cold-started, there MUST be computation progress
        if history_count >= warmup_samples and not self.cold_started and self.total_computations == 0:
            violations.append(
                f"BUSINESS_LOGIC_VIOLATION: {history_count} samples (>= warmup {warmup_samples}), "
                f"cold_started=False, but total_computations=0. "
                f"The calibrator has data but has never computed a threshold. "
                f"This is the DQAF-20260614-002 cold-start bug pattern — "
                f"cold_start_from_journal() set cold_started=True unconditionally."
            )

        # Invariant 2: cold_started should be False when history exceeds warmup
        if history_count >= warmup_samples and self.cold_started:
            violations.append(
                f"BUSINESS_LOGIC_VIOLATION: {history_count} samples (>= warmup {warmup_samples}), "
                f"but cold_started=True.  The calibrator should transition to warm state."
            )

        # Invariant 3: total_computations should never exceed history size
        # (each computation uses the current history, but you can compute
        # multiple times per sample — this is a soft check)
        if self.total_computations > history_count * 2:
            violations.append(
                f"BUSINESS_LOGIC_WARNING: total_computations ({self.total_computations}) "
                f"greatly exceeds history size ({history_count}).  Possible counter overflow."
            )

        # Invariant 4: NaN check on all p_win values
        nan_count = sum(1 for h in self.history if math.isnan(h.p_win) or math.isinf(h.p_win))
        if nan_count > 0:
            violations.append(
                f"DATA_QUALITY_VIOLATION: {nan_count}/{history_count} history entries "
                f"have NaN or Inf p_win values."
            )

        # Invariant 5: Label distribution sanity check
        if history_count >= 20:
            wins = sum(1 for h in self.history if h.label == 1)
            losses = sum(1 for h in self.history if h.label == -1)
            if wins + losses == 0:
                violations.append(
                    "DATA_QUALITY_VIOLATION: No win/loss labels in calibrator history. "
                    "All entries are breakeven (label=0) — calibrator has no signal to learn from."
                )

        return violations

    def is_operational(self, warmup_samples: int = 50) -> bool:
        """Return True if the calibrator is in a healthy operational state."""
        return (
            len(self.history) >= warmup_samples
            and not self.cold_started
            and self.total_computations > 0
        )

    model_config = {"extra": "allow"}  # Forward-compatible
