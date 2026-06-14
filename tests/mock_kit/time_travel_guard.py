"""Time-Travel Guard — structural defense against look-ahead bias.

In quantitative systems, the most insidious bugs are NOT crashes but silent
future-data leaks: a feature computed at time T accidentally reads price data
from time T+1 via off-by-one indexing, ``df.shift(-1)``, or ASOF join mismatch.

This module provides two tools:

1. **TimeTravelGuard** (explicit API) — record every timestamp accessed and
   assert that no access peeked past the evaluation time.  Use this when you
   control the data access code.

2. **TimeTravelProxy** (transparent wrapper) — wraps a ``pd.DataFrame`` and
   intercepts ``.loc[]``, ``.iloc[]``, ``.at[]``, ``.iat[]``, and column
   access to silently record every timestamp touched.  Use this when you
   want to audit EXISTING code without modifying it.

Typical usage (explicit)::

    from tests.mock_kit.time_travel_guard import TimeTravelGuard
    import pandas as pd

    df = pd.DataFrame(...)  # DatetimeIndex
    guard = TimeTravelGuard(df)

    # Instrumented access: guard.slice_to(T) records the max timestamp
    history = guard.slice_to(pd.Timestamp("2026-01-15 10:00"))
    feature = compute_feature(history)

    # Assert no future leakage
    guard.assert_no_lookahead(pd.Timestamp("2026-01-15 10:00"))

Typical usage (transparent proxy for auditing existing code)::

    from tests.mock_kit.time_travel_guard import TimeTravelProxy

    proxy = TimeTravelProxy(df)
    result = existing_feature_function(proxy)  # proxy acts like a DataFrame
    proxy.assert_no_lookahead(evaluation_time)

Design notes:
    - A "lookahead violation" is any access to data with timestamp > evaluation_time.
    - The guard records (timestamp, context, stack frame) for diagnostics.
    - Zero-tolerance: even 1 microsecond into the future is a violation.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TimeTravelAccess:
    """A single recorded data access."""

    timestamp: pd.Timestamp
    context: str
    stack_summary: str  # truncated traceback for diagnostics


@dataclass
class TimeTravelViolation(Exception):
    """Raised when look-ahead bias is detected."""

    evaluation_time: pd.Timestamp
    violations: list[TimeTravelAccess]
    message: str = ""

    def __post_init__(self) -> None:
        if not self.message:
            self.message = self._build_message()

    def _build_message(self) -> str:
        header = (
            f"LOOKAHEAD BIAS DETECTED: {len(self.violations)} access(es) "
            f"past evaluation time {self.evaluation_time.isoformat()}\n"
        )
        details = []
        for i, v in enumerate(self.violations[:5]):
            delta = v.timestamp - self.evaluation_time
            details.append(
                f"  [{i+1}] ts={v.timestamp.isoformat()} "
                f"(+{delta.total_seconds():.1f}s ahead) "
                f"context={v.context or '<none>'}\n"
                f"      {v.stack_summary}"
            )
        if len(self.violations) > 5:
            details.append(f"  ... and {len(self.violations) - 5} more")
        return header + "\n".join(details)


# ---------------------------------------------------------------------------
# TimeTravelGuard (explicit API)
# ---------------------------------------------------------------------------
class TimeTravelGuard:
    """Explicit time-travel auditor for new or instrumented code.

    Wrap a DataFrame and use ``guard.slice_to(T)`` instead of ``df.loc[:T]``.
    Every access is recorded.  ``assert_no_lookahead()`` validates all accesses
    are ≤ the declared evaluation time.
    """

    def __init__(self, df: pd.DataFrame) -> None:
        if not isinstance(df.index, pd.DatetimeIndex):
            raise TypeError(
                f"TimeTravelGuard requires a DataFrame with DatetimeIndex, "
                f"got {type(df.index).__name__}"
            )
        self._df: pd.DataFrame = df
        self._accesses: list[TimeTravelAccess] = []
        self._scope_stack: list[str] = []

    # -- Scope management --------------------------------------------------

    @contextmanager
    def scope(self, name: str) -> Any:
        """Context manager that labels all accesses within with a scope name.

        Usage::

            with guard.scope("feature_computation"):
                data = guard.slice_to(T)
                ...
        """
        self._scope_stack.append(name)
        try:
            yield
        finally:
            self._scope_stack.pop()

    @property
    def current_scope(self) -> str:
        return self._scope_stack[-1] if self._scope_stack else ""

    # -- Data access -------------------------------------------------------

    def slice_to(self, ts: pd.Timestamp, *, context: str = "") -> pd.DataFrame:
        """Return ``df.loc[:ts]`` and record the access.

        This is the primary instrumented access method.  All data reads
        that should be bounded by the evaluation time must go through here.
        """
        return self._record_and_return(ts, context, lambda: self._df.loc[:ts])

    def slice_between(
        self, start: pd.Timestamp, end: pd.Timestamp, *, context: str = ""
    ) -> pd.DataFrame:
        """Return ``df.loc[start:end]`` and record the access."""
        return self._record_and_return(
            end, context, lambda: self._df.loc[start:end]
        )

    def at_timestamp(self, ts: pd.Timestamp, *, context: str = "") -> pd.Series:
        """Return ``df.loc[ts]`` and record the access."""
        return self._record_and_return(ts, context, lambda: self._df.loc[ts])

    def _record_and_return(
        self,
        ts: pd.Timestamp,
        context: str,
        fetcher: Callable[[], Any],
    ) -> Any:
        """Record the access and return the fetched data."""
        # Capture stack for diagnostics — skip internal guard frames
        stack = traceback.extract_stack(limit=10)
        caller_frame = stack[0]
        for frame in reversed(stack):
            if "time_travel_guard" not in frame.filename:
                caller_frame = frame
                break
        stack_summary = (
            f"{caller_frame.filename}:{caller_frame.lineno} "
            f"in {caller_frame.name}"
        )

        full_context = f"{self.current_scope}/{context}" if self.current_scope and context else (
            self.current_scope or context or "unnamed"
        )

        self._accesses.append(
            TimeTravelAccess(
                timestamp=ts,
                context=full_context,
                stack_summary=stack_summary,
            )
        )
        return fetcher()

    # -- Column access (for Series-level operations) -----------------------

    def column(self, name: str) -> pd.Series:
        """Return a column as a plain Series (no time-travel tracking).

        Use this to get a column for computation — the time-travel protection
        comes from the *index* bounds, not individual column reads.
        """
        return self._df[name]

    @property
    def columns(self) -> pd.Index:
        return self._df.columns

    @property
    def index(self) -> pd.DatetimeIndex:
        return self._df.index

    @property
    def df(self) -> pd.DataFrame:
        """Access the underlying DataFrame directly (UNGUARDED).

        Use only for writes or operations that don't read future data.
        """
        return self._df

    # -- Assertions --------------------------------------------------------

    def assert_no_lookahead(self, evaluation_time: pd.Timestamp) -> None:
        """Raise TimeTravelViolation if any recorded access peeked past
        *evaluation_time*.

        Args:
            evaluation_time: The declared "now" — all data accesses must be
                at or before this timestamp.

        Raises:
            TimeTravelViolation: One or more accesses peeked into the future.
        """
        violations = [
            a for a in self._accesses if a.timestamp > evaluation_time
        ]
        if violations:
            raise TimeTravelViolation(
                evaluation_time=evaluation_time,
                violations=violations,
            )

    def assert_clean(self, evaluation_time: pd.Timestamp) -> bool:
        """Return True if no lookahead; False otherwise (no exception)."""
        try:
            self.assert_no_lookahead(evaluation_time)
            return True
        except TimeTravelViolation:
            return False

    @property
    def max_timestamp_accessed(self) -> pd.Timestamp | None:
        """The latest timestamp accessed, or None if no accesses."""
        if not self._accesses:
            return None
        return max(a.timestamp for a in self._accesses)

    @property
    def accesses(self) -> list[TimeTravelAccess]:
        """Read-only view of recorded accesses (for test inspection)."""
        return list(self._accesses)

    @property
    def access_count(self) -> int:
        return len(self._accesses)


# ---------------------------------------------------------------------------
# TimeTravelProxy (transparent DataFrame wrapper)
# ---------------------------------------------------------------------------
class TimeTravelProxy:
    """Transparent DataFrame proxy that intercepts index-based access.

    Wraps a ``pd.DataFrame`` so that existing code can use it as if it were
    a normal DataFrame, while silently recording every timestamp touched.

    Intercepted operations:
        - ``proxy.loc[...]`` — records max timestamp in the slice
        - ``proxy.iloc[...]`` — records max timestamp (index-mapped)
        - ``proxy.at[...]`` — records the accessed timestamp
        - ``proxy.iat[...]`` — records (index-mapped)
        - ``proxy['column']`` — delegates to underlying df (no timestamp tracking)

    Non-intercepted operations are forwarded to the underlying DataFrame via
    ``__getattr__``.

    Usage::

        proxy = TimeTravelProxy(df)
        # Existing code operates on proxy as if it were a DataFrame:
        close_prices = proxy['close']
        window = proxy.loc[:'2026-01-15']
        some_result = legacy_feature_function(proxy)
        # Then assert:
        proxy.assert_no_lookahead(pd.Timestamp('2026-01-15 10:00'))
    """

    def __init__(self, df: pd.DataFrame) -> None:
        if not isinstance(df.index, pd.DatetimeIndex):
            raise TypeError(
                f"TimeTravelProxy requires a DataFrame with DatetimeIndex, "
                f"got {type(df.index).__name__}"
            )
        self._df: pd.DataFrame = df
        self._accesses: list[TimeTravelAccess] = []
        self._scope_stack: list[str] = []

    # -- Scope management --------------------------------------------------

    @contextmanager
    def scope(self, name: str) -> Any:
        self._scope_stack.append(name)
        try:
            yield
        finally:
            self._scope_stack.pop()

    @property
    def current_scope(self) -> str:
        return self._scope_stack[-1] if self._scope_stack else ""

    # -- Intercepted indexers ----------------------------------------------

    @property
    def loc(self) -> _LocInterceptor:
        return _LocInterceptor(self)

    @property
    def iloc(self) -> _ILocInterceptor:
        return _ILocInterceptor(self)

    @property
    def at(self) -> _AtInterceptor:
        return _AtInterceptor(self)

    @property
    def iat(self) -> _IAtInterceptor:
        return _IAtInterceptor(self)

    # -- Column access -----------------------------------------------------

    def __getitem__(self, key: str | list[str]) -> Any:
        """Column access — delegates to underlying df.

        Column access alone does not carry timestamp information.
        Timestamp tracking happens via .loc / .iloc / .at / .iat.
        """
        return self._df[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._df[key] = value

    # -- Attribute forwarding ----------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward unknown attributes to the underlying DataFrame."""
        # Avoid infinite recursion during init
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._df, name)
        if callable(attr):
            return attr  # Return bound method, will operate on _df
        return attr

    # -- DataFrame protocol compat -----------------------------------------

    @property
    def columns(self) -> pd.Index:
        return self._df.columns

    @property
    def index(self) -> pd.DatetimeIndex:
        return self._df.index

    @property
    def shape(self) -> tuple[int, int]:
        return self._df.shape

    @property
    def dtypes(self) -> pd.Series:
        return self._df.dtypes

    @property
    def values(self) -> Any:
        return self._df.values

    def __len__(self) -> int:
        return len(self._df)

    def __repr__(self) -> str:
        return f"TimeTravelProxy({self._df.shape})"

    # -- Access recording --------------------------------------------------

    def _record(self, ts: pd.Timestamp) -> None:
        stack = traceback.extract_stack(limit=12)
        # Find the first frame outside this module (closest to the actual call site)
        caller_frame = stack[0]
        for frame in reversed(stack):
            if "time_travel_guard" not in frame.filename:
                caller_frame = frame
                break
        stack_summary = (
            f"{caller_frame.filename}:{caller_frame.lineno} "
            f"in {caller_frame.name}"
        )

        self._accesses.append(
            TimeTravelAccess(
                timestamp=ts,
                context=self.current_scope,
                stack_summary=stack_summary,
            )
        )

    # -- Assertions --------------------------------------------------------

    def assert_no_lookahead(self, evaluation_time: pd.Timestamp) -> None:
        violations = [
            a for a in self._accesses if a.timestamp > evaluation_time
        ]
        if violations:
            raise TimeTravelViolation(
                evaluation_time=evaluation_time,
                violations=violations,
            )

    def assert_clean(self, evaluation_time: pd.Timestamp) -> bool:
        try:
            self.assert_no_lookahead(evaluation_time)
            return True
        except TimeTravelViolation:
            return False

    @property
    def max_timestamp_accessed(self) -> pd.Timestamp | None:
        if not self._accesses:
            return None
        return max(a.timestamp for a in self._accesses)

    @property
    def accesses(self) -> list[TimeTravelAccess]:
        """Read-only view of recorded accesses (for test inspection)."""
        return list(self._accesses)

    @property
    def access_count(self) -> int:
        return len(self._accesses)


# ---------------------------------------------------------------------------
# Interceptor helpers
# ---------------------------------------------------------------------------
class _LocInterceptor:
    """Intercepts ``proxy.loc[...]`` and records max timestamp accessed."""

    def __init__(self, guard: TimeTravelProxy) -> None:
        self._guard = guard
        self._df = guard._df

    def __getitem__(self, key: Any) -> Any:
        result = self._df.loc[key]
        self._record_max_ts(result)
        return result

    def _record_max_ts(self, result: Any) -> None:
        """Extract max timestamp from result and record it."""
        ts = _extract_max_timestamp(result)
        if ts is not None:
            self._guard._record(ts)


class _ILocInterceptor:
    """Intercepts ``proxy.iloc[...]`` and records max timestamp accessed."""

    def __init__(self, guard: TimeTravelProxy) -> None:
        self._guard = guard
        self._df = guard._df

    def __getitem__(self, key: Any) -> Any:
        result = self._df.iloc[key]
        self._record_max_ts(result)
        return result

    def _record_max_ts(self, result: Any) -> None:
        ts = _extract_max_timestamp(result)
        if ts is not None:
            self._guard._record(ts)


class _AtInterceptor:
    """Intercepts ``proxy.at[...]`` and records the accessed timestamp."""

    def __init__(self, guard: TimeTravelProxy) -> None:
        self._guard = guard
        self._df = guard._df

    def __getitem__(self, key: Any) -> Any:
        result = self._df.at[key]
        # key is (index_label, column_name) — extract the timestamp
        if isinstance(key, tuple) and len(key) >= 1:
            ts = _to_timestamp(key[0])
            if ts is not None:
                self._guard._record(ts)
        return result

    def __setitem__(self, key: Any, value: Any) -> None:
        self._df.at[key] = value


class _IAtInterceptor:
    """Intercepts ``proxy.iat[...]`` and records the accessed timestamp."""

    def __init__(self, guard: TimeTravelProxy) -> None:
        self._guard = guard
        self._df = guard._df

    def __getitem__(self, key: Any) -> Any:
        result = self._df.iat[key]
        if isinstance(key, tuple) and len(key) >= 1:
            row_idx = key[0]
            if 0 <= row_idx < len(self._df.index):
                ts = _to_timestamp(self._df.index[row_idx])
                if ts is not None:
                    self._guard._record(ts)
        return result

    def __setitem__(self, key: Any, value: Any) -> None:
        self._df.iat[key] = value


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_max_timestamp(result: Any) -> pd.Timestamp | None:
    """Extract the maximum timestamp from a DataFrame, Series, or scalar."""
    if isinstance(result, pd.DataFrame):
        if isinstance(result.index, pd.DatetimeIndex) and len(result.index) > 0:
            return result.index.max()
    elif isinstance(result, pd.Series):
        if isinstance(result.index, pd.DatetimeIndex) and len(result.index) > 0:
            return result.index.max()
    elif isinstance(result, pd.Timestamp):
        return result
    return None


def _to_timestamp(val: Any) -> pd.Timestamp | None:
    """Convert a value to pd.Timestamp if possible."""
    if isinstance(val, pd.Timestamp):
        return val
    try:
        return pd.Timestamp(val)
    except (ValueError, TypeError):
        return None
