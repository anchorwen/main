"""TypedClock — Three incompatible time types for UGR v3.1.

UGR v3.1 §修正3: _raw hardening + type incompatibility.
Eliminates the "wrong clock for arithmetic" class of bugs at the type level.

Type taxonomy::

    MonotonicInstant  — cooldowns, intervals, timeouts (time.monotonic())
    WallInstant       — log timestamps ONLY (datetime.now(UTC))
    Duration          — only from MonotonicInstant subtraction

Incompatibilities enforced by mypy (missing methods → type error):

    ✓  MonotonicInstant - MonotonicInstant → Duration
    ✓  MonotonicInstant + Duration → MonotonicInstant
    ✓  Duration * scalar → Duration
    ✗  WallInstant - WallInstant → AttributeError (NO __sub__)
    ✗  WallInstant + Duration → AttributeError (NO __add__)
    ✗  MonotonicInstant - WallInstant → TypeError (different types)

Dual-layer _raw hardening:

    Layer 1 (runtime): @dataclass(frozen=True, slots=True) →
        FrozenInstanceError on any setattr attempt.  Normal code
        cannot mutate _raw after construction.

    Layer 2 (CI AST): scripts/verify_capresult_ast.py detects
        object.__setattr__(instance, '_raw', ...) on these types
        outside this file → CI ERROR.  Whitelist: this file only.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ═══════════════════════════════════════════════════════════════════════════
# MonotonicInstant — for cooldowns, intervals, deadlines
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True, order=True)
class MonotonicInstant:
    """A point on the monotonic clock (time.monotonic()).

    Supports: subtraction (→ Duration), addition with Duration, comparison.
    Immutable via @dataclass(frozen=True).  _raw protected by dual-layer hardening.
    """

    _raw: float = field(repr=False, compare=True)

    # ── Arithmetic ────────────────────────────────────────────────────

    def __sub__(self, other: object) -> Duration:
        """MonotonicInstant - MonotonicInstant → Duration."""
        if isinstance(other, MonotonicInstant):
            return Duration(self._raw - other._raw)
        return NotImplemented

    def __add__(self, other: object) -> MonotonicInstant:
        """MonotonicInstant + Duration → MonotonicInstant."""
        if isinstance(other, Duration):
            return MonotonicInstant(self._raw + other._raw)
        return NotImplemented

    def __radd__(self, other: object) -> MonotonicInstant:
        """Duration + MonotonicInstant → MonotonicInstant."""
        if isinstance(other, Duration):
            return MonotonicInstant(other._raw + self._raw)
        return NotImplemented

    # ── Comparison helpers ─────────────────────────────────────────────

    def elapsed(self) -> Duration:
        """Duration since this instant. Clock.monotonic() - self."""
        return Clock.monotonic() - self

    def remaining(self, deadline: MonotonicInstant) -> Duration:
        """Duration until deadline. deadline - self."""
        return deadline - self


# ═══════════════════════════════════════════════════════════════════════════
# WallInstant — for log timestamps ONLY, no arithmetic
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True, order=True)
class WallInstant:
    """A wall-clock timestamp (datetime.now(UTC)).

    DELIBERATELY has NO arithmetic methods — no __sub__, no __add__.
    Use for log timestamps, human-readable times, and comparison ONLY.

    To measure elapsed time: use MonotonicInstant, not WallInstant.
    """

    _raw: float = field(repr=False, compare=True)  # UTC timestamp (time.time())

    # ── Formatting (the ONLY operations allowed on wall-clock times) ───

    def isoformat(self) -> str:
        """ISO-8601 formatted UTC timestamp."""
        return self.to_datetime().isoformat()

    def to_datetime(self) -> datetime:
        """Convert to a timezone-aware UTC datetime."""
        return datetime.fromtimestamp(self._raw, tz=UTC)

    def __repr__(self) -> str:
        return f"WallInstant({self.isoformat()})"


# ═══════════════════════════════════════════════════════════════════════════
# Duration — only from MonotonicInstant subtraction
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True, order=True)
class Duration:
    """A time interval (seconds) from MonotonicInstant subtraction.

    Can be: multiplied by scalar, divided by scalar, added to other Duration,
    added to MonotonicInstant.

    Cannot be added to WallInstant (WallInstant has no __add__).
    """

    _raw: float = field(repr=False, compare=True)

    # ── Arithmetic ────────────────────────────────────────────────────

    def __add__(self, other: object) -> Duration:
        """Duration + Duration → Duration."""
        if isinstance(other, Duration):
            return Duration(self._raw + other._raw)
        # Duration + MonotonicInstant is handled by MonotonicInstant.__radd__
        if isinstance(other, MonotonicInstant):
            return NotImplemented  # Let MonotonicInstant.__radd__ handle it
        return NotImplemented

    def __sub__(self, other: Duration) -> Duration:
        """Duration - Duration → Duration."""
        if isinstance(other, Duration):
            return Duration(self._raw - other._raw)
        return NotImplemented

    def __mul__(self, scalar: float) -> Duration:
        """Duration * scalar → Duration."""
        return Duration(self._raw * scalar)

    def __rmul__(self, scalar: float) -> Duration:
        """scalar * Duration → Duration."""
        return Duration(self._raw * scalar)

    def __truediv__(self, scalar: float) -> Duration:
        """Duration / scalar → Duration."""
        return Duration(self._raw / scalar)

    def __neg__(self) -> Duration:
        """-Duration → negative Duration."""
        return Duration(-self._raw)

    # ── Conversion ────────────────────────────────────────────────────

    def total_seconds(self) -> float:
        """Total duration in seconds (float)."""
        return self._raw

    def __repr__(self) -> str:
        return f"Duration({self._raw:.3f}s)"


# ═══════════════════════════════════════════════════════════════════════════
# Clock — unified access point
# ═══════════════════════════════════════════════════════════════════════════


class Clock:
    """System clock — the sole entry point for obtaining time values.

    Usage::

        now = Clock.monotonic()     # MonotonicInstant (for intervals)
        ts  = Clock.wall()          # WallInstant (for log timestamps)

        deadline = now + Duration(5.0)
        elapsed = now.elapsed()
    """

    @staticmethod
    def monotonic() -> MonotonicInstant:
        """Current monotonic time. Use for cooldowns, intervals, deadlines."""
        return MonotonicInstant(_time.monotonic())

    @staticmethod
    def wall() -> WallInstant:
        """Current wall-clock time (UTC). Use for log timestamps ONLY."""
        return WallInstant(_time.time())

    @staticmethod
    def now() -> datetime:
        """Current wall-clock datetime (UTC). Convenience for legacy code."""
        return datetime.now(tz=UTC)
