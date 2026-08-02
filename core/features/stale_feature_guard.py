"""Stale feature inference guard — hard-fail on stale feature vectors.

DQAF-20260801-011 / FIX-20260801-013 (Institutional Execution Mandate):
The XAU feature pipeline froze at 2026-08-01T00:41:25Z while the market
remained open for another 20 hours.  The downstream ``FeatureService``
silently served the last-known vector during that window — any brain
reaching inference would have scored signals on frozen features.

This module is the inference-boundary defense ordered by the Investment
Committee: before a feature vector is used for live inference, its
timestamp must be validated.  If staleness exceeds 2-3 bars of the
current timeframe, inference is rejected with :class:`StaleFeatureException`
instead of silently degrading.

The exception is deliberately a plain ``Exception`` subclass — it is NOT
caught by the codebase's broad ``except (RuntimeError, ValueError,
KeyError, TypeError, OSError)`` clauses, so it propagates to the explicit
caller handler that converts it into management-only mode.
"""

from __future__ import annotations

# Bar periods (seconds) for institutional timeframes.
_BAR_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


class StaleFeatureException(Exception):
    """Raised when a feature vector is too old for live inference.

    Attributes:
        symbol: Asset symbol the stale features belong to.
        age_seconds: Actual age of the feature timestamp at rejection
            (None when no usable timestamp exists).
        max_age_seconds: The configured staleness ceiling (default 3 bars).
        feature_timestamp: Unix timestamp of the stale feature vector.
        source: Where the feature vector came from (cache / last-known).
    """

    def __init__(
        self,
        message: str,
        *,
        symbol: str,
        age_seconds: float | None,
        max_age_seconds: float,
        feature_timestamp: float | None,
        source: str,
    ) -> None:
        super().__init__(message)
        self.symbol = symbol
        self.age_seconds = age_seconds
        self.max_age_seconds = max_age_seconds
        self.feature_timestamp = feature_timestamp
        self.source = source


def bar_period_seconds(timeframe: str) -> int:
    """Return the bar period in seconds for a timeframe (default M5)."""
    return _BAR_SECONDS.get(timeframe, 300)


def feature_max_age_seconds(timeframe: str, bars: int = 3) -> int:
    """Return the inference staleness ceiling: ``bars`` × bar period.

    Defaults to 3 bars — the Investment Committee's "2-3 bars" mandate.
    """
    return max(1, bars) * bar_period_seconds(timeframe)


def validate_feature_timestamp(
    feature_timestamp: float | None,
    *,
    max_age_seconds: float,
    symbol: str,
    source: str,
) -> None:
    """Raise :class:`StaleFeatureException` when the timestamp is stale.

    - A missing or non-positive timestamp is treated as stale — we cannot
      prove freshness, so inference must not proceed.
    - A future timestamp is rejected as clock skew / misconfigured producer.
    - An age beyond ``max_age_seconds`` is the hard reject boundary.
    """
    import time

    if feature_timestamp is None or feature_timestamp <= 0:
        raise StaleFeatureException(
            f"StaleFeatureException: no usable feature timestamp for {symbol} "
            f"(source={source}) — cannot guarantee freshness for inference",
            symbol=symbol,
            age_seconds=None,
            max_age_seconds=max_age_seconds,
            feature_timestamp=feature_timestamp,
            source=source,
        )
    age = time.time() - feature_timestamp
    if age < 0:
        raise StaleFeatureException(
            f"StaleFeatureException: feature timestamp in the future for {symbol} "
            f"(age={age:.1f}s) — clock skew or misconfigured producer",
            symbol=symbol,
            age_seconds=age,
            max_age_seconds=max_age_seconds,
            feature_timestamp=feature_timestamp,
            source=source,
        )
    if age > max_age_seconds:
        raise StaleFeatureException(
            f"StaleFeatureException: feature vector {age:.0f}s old for {symbol} "
            f"(max={max_age_seconds:.0f}s = {max_age_seconds / max(bar_period_seconds('M5'), 1):.0f} M5 bars) "
            f"— refusing inference on stale features",
            symbol=symbol,
            age_seconds=age,
            max_age_seconds=max_age_seconds,
            feature_timestamp=feature_timestamp,
            source=source,
        )
