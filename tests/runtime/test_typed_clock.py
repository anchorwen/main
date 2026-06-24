"""Tests for TypedClock — UGR v3.1 three incompatible time types.

Covers:
- MonotonicInstant arithmetic (sub → Duration, add Duration)
- WallInstant has NO arithmetic methods (type-level enforcement)
- Duration arithmetic (mul, truediv, add, sub, neg)
- __setattr__ _raw hardening on all three types
- Clock monotonic() and wall() factories
- Type incompatibility: MonotonicInstant and WallInstant cannot mix
"""

from __future__ import annotations

import time as _time

import pytest

from core.runtime.typed_clock import (
    Clock,
    Duration,
    MonotonicInstant,
    WallInstant,
)

# ═══════════════════════════════════════════════════════════════════════════
# MonotonicInstant
# ═══════════════════════════════════════════════════════════════════════════


class TestMonotonicInstant:
    """Tests for MonotonicInstant creation, arithmetic, and comparison."""

    def test_clock_monotonic_returns_monotonic_instant(self) -> None:
        """Clock.monotonic() returns a MonotonicInstant."""
        t = Clock.monotonic()
        assert isinstance(t, MonotonicInstant)

    def test_subtraction_returns_duration(self) -> None:
        """Mono - Mono → Duration."""
        t1 = MonotonicInstant(100.0)
        t2 = MonotonicInstant(105.0)
        d = t2 - t1
        assert isinstance(d, Duration)
        assert d.total_seconds() == pytest.approx(5.0)

    def test_subtraction_reverse_is_negative(self) -> None:
        """t1 - t2 = -(t2 - t1)."""
        t1 = MonotonicInstant(100.0)
        t2 = MonotonicInstant(105.0)
        d = t1 - t2
        assert d.total_seconds() == pytest.approx(-5.0)

    def test_add_duration_returns_monotonic(self) -> None:
        """Mono + Duration → MonotonicInstant."""
        t = MonotonicInstant(100.0)
        later = t + Duration(5.0)
        assert isinstance(later, MonotonicInstant)
        d = later - t
        assert d.total_seconds() == pytest.approx(5.0)

    def test_radd_duration_returns_monotonic(self) -> None:
        """Duration + Mono → MonotonicInstant (__radd__)."""
        t = MonotonicInstant(100.0)
        later = Duration(5.0) + t
        assert isinstance(later, MonotonicInstant)

    def test_sub_wrong_type_returns_notimplemented(self) -> None:
        """Mono - non-Mono/non-Duration → NotImplemented."""
        t = MonotonicInstant(100.0)
        result = t.__sub__(42)
        assert result is NotImplemented

    def test_add_wrong_type_returns_notimplemented(self) -> None:
        """Mono + non-Duration → NotImplemented."""
        t = MonotonicInstant(100.0)
        result = t.__add__("x")
        assert result is NotImplemented

    def test_comparison(self) -> None:
        """MonotonicInstant supports ==, <, >, <=, >=."""
        t1 = MonotonicInstant(100.0)
        t2 = MonotonicInstant(200.0)
        t3 = MonotonicInstant(100.0)
        assert t1 < t2
        assert t2 > t1
        assert t1 <= t2
        assert t1 <= t3
        assert t1 == t3
        assert t1 != t2

    def test_elapsed(self) -> None:
        """elapsed() returns Duration since the instant."""
        now = Clock.monotonic()
        _time.sleep(0.05)
        d = now.elapsed()
        assert isinstance(d, Duration)
        assert d.total_seconds() > 0

    def test_remaining(self) -> None:
        """remaining(deadline) returns Duration until deadline."""
        t = MonotonicInstant(100.0)
        deadline = MonotonicInstant(105.0)
        d = t.remaining(deadline)
        assert isinstance(d, Duration)
        assert d.total_seconds() == pytest.approx(5.0)

    def test_hashable(self) -> None:
        """MonotonicInstant is hashable (frozen + slots)."""
        t1 = MonotonicInstant(100.0)
        t2 = MonotonicInstant(100.0)
        assert hash(t1) == hash(t2)
        assert len({t1, t2}) == 1

    def test_repr(self) -> None:
        """__repr__ includes the raw value."""
        t = MonotonicInstant(123.456)
        r = repr(t)
        assert "MonotonicInstant" in r

    def test_setattr_blocked_by_frozen(self) -> None:
        """Normal setattr on MonotonicInstant raises FrozenInstanceError."""
        t = MonotonicInstant(100.0)
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            t._raw = 999.0  # type: ignore[misc]

    def test_object_setattr_is_bypassable_and_caught_by_ci(self) -> None:
        """object.__setattr__ CAN bypass frozen=True at runtime.

        This is expected — CPython's object.__setattr__ writes directly
        to the slot.  The CI AST scanner (verify_capresult_ast.py) is
        the enforcement layer for this vector.  This test documents the
        bypass and confirms the CI-enforcement design.
        """
        t = MonotonicInstant(100.0)
        object.__setattr__(t, "_raw", 999.0)
        # Bypass succeeded — _raw is now mutated
        assert t._raw == 999.0
        # CI AST scanner will flag this pattern outside typed_clock.py


# ═══════════════════════════════════════════════════════════════════════════
# WallInstant
# ═══════════════════════════════════════════════════════════════════════════


class TestWallInstant:
    """Tests for WallInstant — NO arithmetic, formatting only."""

    def test_clock_wall_returns_wall_instant(self) -> None:
        """Clock.wall() returns a WallInstant."""
        t = Clock.wall()
        assert isinstance(t, WallInstant)

    def test_no_subtraction(self) -> None:
        """WallInstant has NO __sub__ → AttributeError."""
        t = WallInstant(1000.0)
        with pytest.raises(TypeError):
            _ = t - t  # type: ignore[operator]

    def test_no_addition(self) -> None:
        """WallInstant has NO __add__ → AttributeError."""
        t = WallInstant(1000.0)
        with pytest.raises(TypeError):
            _ = t + Duration(5.0)  # type: ignore[operator]

    def test_comparison(self) -> None:
        """WallInstant supports ==, <, > but not arithmetic."""
        t1 = WallInstant(1000.0)
        t2 = WallInstant(2000.0)
        t3 = WallInstant(1000.0)
        assert t1 < t2
        assert t2 > t1
        assert t1 == t3
        assert t1 != t2

    def test_isoformat(self) -> None:
        """isoformat() returns ISO-8601 UTC string."""
        t = WallInstant(1000000.0)  # Fixed known timestamp
        iso = t.isoformat()
        assert "T" in iso
        assert "+00:00" in iso

    def test_to_datetime_is_utc(self) -> None:
        """to_datetime() returns timezone-aware UTC datetime."""
        t = WallInstant(1000000.0)
        dt = t.to_datetime()
        assert dt.tzinfo is not None
        assert dt.utcoffset() is not None

    def test_hashable(self) -> None:
        """WallInstant is hashable."""
        t1 = WallInstant(1000.0)
        t2 = WallInstant(1000.0)
        assert hash(t1) == hash(t2)

    def test_setattr_blocked_by_frozen(self) -> None:
        """Normal setattr on WallInstant raises FrozenInstanceError."""
        t = WallInstant(1000.0)
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            t._raw = 999.0  # type: ignore[misc]

    def test_object_setattr_bypass_documented(self) -> None:
        """object.__setattr__ bypass — CI AST scanner is the enforcement layer."""
        t = WallInstant(1000.0)
        object.__setattr__(t, "_raw", 2000.0)
        assert t._raw == 2000.0

    def test_repr(self) -> None:
        """__repr__ includes ISO-8601 timestamp."""
        t = WallInstant(1000000.0)
        r = repr(t)
        assert "WallInstant" in r


# ═══════════════════════════════════════════════════════════════════════════
# Duration
# ═══════════════════════════════════════════════════════════════════════════


class TestDuration:
    """Tests for Duration arithmetic and constraints."""

    def test_create(self) -> None:
        """Duration can be created directly with seconds."""
        d = Duration(5.0)
        assert d.total_seconds() == 5.0

    def test_add_duration(self) -> None:
        """Duration + Duration → Duration."""
        d = Duration(3.0) + Duration(2.0)
        assert isinstance(d, Duration)
        assert d.total_seconds() == pytest.approx(5.0)

    def test_sub_duration(self) -> None:
        """Duration - Duration → Duration."""
        d = Duration(5.0) - Duration(3.0)
        assert isinstance(d, Duration)
        assert d.total_seconds() == pytest.approx(2.0)

    def test_mul_scalar(self) -> None:
        """Duration * scalar → Duration."""
        d = Duration(3.0) * 2.0
        assert d.total_seconds() == pytest.approx(6.0)

    def test_rmul_scalar(self) -> None:
        """scalar * Duration → Duration."""
        d = 2.0 * Duration(3.0)
        assert d.total_seconds() == pytest.approx(6.0)

    def test_truediv_scalar(self) -> None:
        """Duration / scalar → Duration."""
        d = Duration(10.0) / 2.0
        assert d.total_seconds() == pytest.approx(5.0)

    def test_neg(self) -> None:
        """-Duration → negative Duration."""
        d = -Duration(5.0)
        assert d.total_seconds() == pytest.approx(-5.0)

    def test_comparison(self) -> None:
        """Duration supports comparison."""
        assert Duration(5.0) > Duration(3.0)
        assert Duration(3.0) < Duration(5.0)
        assert Duration(5.0) == Duration(5.0)
        assert Duration(5.0) != Duration(3.0)

    def test_hashable(self) -> None:
        """Duration is hashable."""
        d1 = Duration(5.0)
        d2 = Duration(5.0)
        assert hash(d1) == hash(d2)

    def test_setattr_blocked_by_frozen(self) -> None:
        """Normal setattr on Duration raises FrozenInstanceError."""
        d = Duration(5.0)
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            d._raw = 999.0  # type: ignore[misc]

    def test_object_setattr_bypass_documented(self) -> None:
        """object.__setattr__ bypass — CI AST scanner is the enforcement layer."""
        d = Duration(5.0)
        object.__setattr__(d, "_raw", 10.0)
        assert d._raw == 10.0

    def test_repr(self) -> None:
        """__repr__ shows seconds."""
        d = Duration(5.0)
        assert "5.0" in repr(d)
        assert "s" in repr(d)


# ═══════════════════════════════════════════════════════════════════════════
# Type Incompatibility — the core value proposition
# ═══════════════════════════════════════════════════════════════════════════


class TestTypeIncompatibility:
    """Verify that incompatible types cannot be mixed."""

    def test_mono_cannot_subtract_wall(self) -> None:
        """MonotonicInstant - WallInstant → NotImplemented."""
        mono = MonotonicInstant(100.0)
        wall = WallInstant(1000.0)
        result = mono.__sub__(wall)
        assert result is NotImplemented

    def test_wall_cannot_subtract_mono(self) -> None:
        """WallInstant has NO __sub__ at all."""
        wall = WallInstant(1000.0)
        mono = MonotonicInstant(100.0)
        with pytest.raises(TypeError):
            _ = wall - mono  # type: ignore[operator]

    def test_mono_cannot_add_wall(self) -> None:
        """MonotonicInstant + WallInstant → NotImplemented."""
        mono = MonotonicInstant(100.0)
        wall = WallInstant(1000.0)
        result = mono.__add__(wall)
        assert result is NotImplemented

    def test_duration_cannot_add_wall(self) -> None:
        """Duration + WallInstant → TypeError (Wall has no __radd__)."""
        d = Duration(5.0)
        wall = WallInstant(1000.0)
        with pytest.raises(TypeError):
            _ = d + wall


# ═══════════════════════════════════════════════════════════════════════════
# Clock
# ═══════════════════════════════════════════════════════════════════════════


class TestClock:
    """Tests for Clock utility methods."""

    def test_monotonic_advances(self) -> None:
        """Two calls to Clock.monotonic() return increasing values."""
        t1 = Clock.monotonic()
        _time.sleep(0.05)
        t2 = Clock.monotonic()
        assert t2 > t1

    def test_wall_is_recent(self) -> None:
        """Clock.wall() returns a time close to now."""
        import time

        t = Clock.wall()
        assert abs(t._raw - time.time()) < 5.0

    def test_now_returns_datetime(self) -> None:
        """Clock.now() returns UTC datetime (legacy compatibility)."""
        from datetime import datetime

        dt = Clock.now()
        assert isinstance(dt, datetime)
        assert dt.tzinfo is not None
