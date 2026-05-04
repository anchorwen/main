"""Example strategy plugins used for tests and local development."""

from datetime import UTC, datetime
from typing import Any

from core.contracts.ids import new_candidate_id
from core.strategies.contracts import RequiredFeature, Signal, StrategyHealth, StrategyMetadata
from core.strategies.schema_versions import SCHEMA_SIGNAL


class ThresholdAlphaAgent:
    """Simple threshold strategy that emits buy/sell/hold signals from one feature."""

    def __init__(
        self,
        strategy_id: str,
        feature_name: str,
        buy_threshold: float,
        sell_threshold: float,
        symbol: str = "XAUUSD",
    ):
        self._metadata = StrategyMetadata(
            strategy_id=strategy_id,
            name="Threshold Alpha Agent",
            version="0.1.0",
            description="Reference AlphaAgent implementation for threshold signals",
            tags=("reference", "threshold"),
        )
        self._feature_name = feature_name
        self._buy_threshold = buy_threshold
        self._sell_threshold = sell_threshold
        self._symbol = symbol
        self._warmed = False

    def metadata(self) -> StrategyMetadata:
        return self._metadata

    def required_features(self) -> list[RequiredFeature]:
        return [RequiredFeature(name=self._feature_name, timeframe="M1", lookback=1)]

    def warmup(self, context: dict[str, Any]) -> None:
        self._warmed = True

    def generate_signal(self, feature_snapshot: Any, context: dict[str, Any]) -> Signal:
        value = self._feature_value(feature_snapshot)
        if value >= self._buy_threshold:
            side = "buy"
        elif value <= self._sell_threshold:
            side = "sell"
        else:
            side = "hold"
        strength = min(
            1.0, abs(value) / max(abs(self._buy_threshold), abs(self._sell_threshold), 1.0)
        )
        return Signal(
            schema_version=SCHEMA_SIGNAL,
            signal_id=new_candidate_id().replace("candidate_", "sig_", 1),
            strategy_id=self._metadata.strategy_id,
            symbol=getattr(feature_snapshot, "symbol", self._symbol),
            side=side,
            strength=strength,
            confidence=0.5 if side == "hold" else 0.75,
            generated_at=datetime.now(UTC).replace(tzinfo=None),
            reason=f"{self._feature_name}={value}",
            features_used={self._feature_name: value},
            risk_hints={"warmed": self._warmed},
        )

    def explain(self, signal: Signal) -> dict[str, Any]:
        return {
            "strategy_id": self._metadata.strategy_id,
            "signal_id": signal.signal_id,
            "reason": signal.reason,
            "thresholds": {"buy": self._buy_threshold, "sell": self._sell_threshold},
        }

    def health(self) -> StrategyHealth:
        return StrategyHealth(
            status="healthy" if self._warmed else "degraded",
            message="warmed" if self._warmed else "not warmed",
        )

    def _feature_value(self, feature_snapshot: Any) -> float:
        if isinstance(feature_snapshot, dict):
            return float(feature_snapshot.get(self._feature_name, 0.0))
        return float(getattr(feature_snapshot, self._feature_name, 0.0))
