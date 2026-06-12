"""Label Contract — the mathematical expression of a trading hypothesis.

A Label Contract defines how training labels are generated from price data.
It is versioned, immutable, and reproducible: given the same price history and
the same contract, you always get the same labels.

Core algorithm: build_barrier_labels() — given OHLC arrays, entry point, and
side, determines which barrier (TP or SL) price hits first within the horizon.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA_VERSION = "label_contract.v1"

# ── ATR calculation (standalone, no MT5 dependency) ──


def _compute_atr(h: np.ndarray, low: np.ndarray, c: np.ndarray, period: int = 14) -> float:
    """Compute Average True Range from numpy arrays (most recent `period` bars)."""
    if len(c) < period + 1:
        return 0.0
    prev_c = c[-(period + 1) : -1]
    cur_h = h[-period:]
    cur_l = low[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)))
    return float(np.mean(tr))


# ── Barrier label builder (the core algorithm) ──


@dataclass
class BarrierResult:
    """Result of a single barrier label computation."""

    label: str  # "tp_hit_first" | "sl_hit_first" | "timeout"
    hit_bar_index: int | None  # Which bar the barrier was hit (0-indexed from entry)
    hit_price: float | None  # Price at which barrier was hit
    entry_price: float
    sl_price: float
    tp_price: float
    atr_at_entry: float
    horizon_bars: int


def _build_barrier_labels_array(
    h: np.ndarray,
    low: np.ndarray,
    c: np.ndarray,
    *,
    entry_idx: int,
    side: str,
    sl_atr_mult: float,
    tp_atr_mult: float,
    horizon_bars: int,
    atr_period: int = 14,
    fallback_atr: float | None = None,
    spread_points: float = 30,
    slippage_points: float = 10,
    tick_value: float = 0.01,
    tick_size: float = 0.01,
    volume: float = 0.01,
) -> BarrierResult:
    """Build barrier labels for a single entry point.

    Starting at entry_idx, walk forward up to horizon_bars (or end of data).
    For each bar, check if the high (for short SL / long TP) or low (for
    long SL / short TP) breaches the barrier. First breach wins.

    Transaction costs are modeled: SL is hit earlier due to slippage,
    TP must exceed the spread to be considered hit.

    Args:
        h: High price array.
        low: Low price array.
        c: Close price array.
        entry_idx: Index in arrays where trade opens.
        side: "long" or "short".
        sl_atr_mult: Stop-loss distance in ATR multiples.
        tp_atr_mult: Take-profit distance in ATR multiples.
        horizon_bars: Max bars to look forward.
        atr_period: ATR lookback period.
        spread_points: Raw MT5 spread in points. Default 30 for XAUUSDc.
        slippage_points: Raw MT5 slippage in points. Default 10 for XAUUSDc.
        tick_value: MT5 SYMBOL_TRADE_TICK_VALUE (monetary value of one tick).
        tick_size: MT5 SYMBOL_TRADE_TICK_SIZE (price step per tick).
        volume: Trade volume in lots.

    Returns:
        BarrierResult with label, hit info, and reference prices.
    """
    entry_price = float(c[entry_idx])
    spread_cost = spread_points * tick_size
    slippage_cost = slippage_points * tick_size

    # Compute ATR at entry (using data up to entry_idx)
    atr_val = _compute_atr(
        h[: entry_idx + 1], low[: entry_idx + 1], c[: entry_idx + 1], period=atr_period
    )
    if atr_val <= 0:
        if fallback_atr is not None and fallback_atr > 0:
            atr_val = fallback_atr
        else:
            # Cannot compute meaningful barriers — reject label construction.
            # 2.31 only works for XAUUSD M5; wrong for EURUSD (~0.001) or H1/M15.
            raise ValueError(
                f"ATR computation failed (atr_val={atr_val:.4f}) and no "
                f"fallback_atr provided.  Hardcoded 2.31 is only valid for "
                f"XAUUSD M5 — provide a symbol/timeframe-appropriate fallback."
            )

    sl_dist = sl_atr_mult * atr_val
    tp_dist = tp_atr_mult * atr_val

    if side == "long":
        sl_price = entry_price - sl_dist
        tp_price = entry_price + tp_dist
        effective_sl = sl_price - slippage_cost  # SL hit earlier with slippage
        effective_tp = tp_price - spread_cost  # TP must exceed spread
    else:
        sl_price = entry_price + sl_dist
        tp_price = entry_price - tp_dist
        effective_sl = sl_price + slippage_cost
        effective_tp = tp_price + spread_cost

    end_idx = min(entry_idx + horizon_bars + 1, len(c))

    for i in range(entry_idx + 1, end_idx):
        bar_high = float(h[i])
        bar_low = float(low[i])

        if side == "long":
            if bar_low <= effective_sl:
                return BarrierResult(
                    label="sl_hit_first",
                    hit_bar_index=i - entry_idx,
                    hit_price=effective_sl,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    atr_at_entry=round(atr_val, 6),
                    horizon_bars=horizon_bars,
                )
            if bar_high >= effective_tp:
                return BarrierResult(
                    label="tp_hit_first",
                    hit_bar_index=i - entry_idx,
                    hit_price=effective_tp,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    atr_at_entry=round(atr_val, 6),
                    horizon_bars=horizon_bars,
                )
        else:  # short
            if bar_high >= effective_sl:
                return BarrierResult(
                    label="sl_hit_first",
                    hit_bar_index=i - entry_idx,
                    hit_price=effective_sl,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    atr_at_entry=round(atr_val, 6),
                    horizon_bars=horizon_bars,
                )
            if bar_low <= effective_tp:
                return BarrierResult(
                    label="tp_hit_first",
                    hit_bar_index=i - entry_idx,
                    hit_price=effective_tp,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    atr_at_entry=round(atr_val, 6),
                    horizon_bars=horizon_bars,
                )

    # Neither barrier hit within horizon
    return BarrierResult(
        label="timeout",
        hit_bar_index=None,
        hit_price=None,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        atr_at_entry=round(atr_val, 6),
        horizon_bars=horizon_bars,
    )


# ── Friction dead-band squashing ──


def apply_friction_deadband(raw_return_bps: np.ndarray, friction_bps: float) -> np.ndarray:
    """Bidirectional friction with dead-band zeroing.

    Subtractive friction creates phantom inverted signals: a +2 bps return
    minus 3 bps friction = -1 bps target → model learns to SHORT on tiny
    uptrends.  This is catastrophic for cent accounts where friction is a
    large fraction of the expected return.

    Dead-band rule:
      - return > +friction  → return - friction  (long signal)
      - return < -friction  → return + friction  (short signal)
      - |return| ≤ friction → 0                  (neutral — stay flat)

    The model learns that sub-friction moves are not tradeable.
    """
    adjusted = np.zeros_like(raw_return_bps, dtype=np.float64)
    long_wins = raw_return_bps > friction_bps
    adjusted[long_wins] = raw_return_bps[long_wins] - friction_bps
    short_wins = raw_return_bps < -friction_bps
    adjusted[short_wins] = raw_return_bps[short_wins] + friction_bps
    return adjusted


# ── Label Contract dataclass ──


@dataclass
class LabelContract:
    """A versioned, immutable specification of how training labels are generated.

    Usage:
        contract = LabelContract.from_file("blueprints/contracts/label-survival-barrier-1.0.0.json")
        result = contract.build_barrier_labels(highs, lows, closes, entry_idx=50, side="long")
    """

    schema_version: str
    contract_id: str
    type: str  # survival_barrier | regression | binary_class
    horizon_bars: int
    label_classes: dict[str, str]

    # Barriers (for survival_barrier type)
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.5
    bar_timeframe: str = "M5"
    atr_period: int = 14
    atr_timeframe: str = "M5"

    # For regression type
    regression_target: str | None = None  # "forward_return" | "log_return"

    # ATR fallback for symbol/timeframe-aware label construction (T4-C1 fix).
    # When None, label construction raises ValueError if ATR cannot be computed.
    fallback_atr: float | None = None

    # Transaction cost modeling (Exness Standard Cent XAUUSDc: ~30 points spread, ~10 points slippage)
    # Cost formula: cost_in_price = points * tick_size (MT5-native, physics-grounded)
    spread_points: float = 30  # raw MT5 points (not pips)
    slippage_points: float = 10  # conservative estimate for normal market conditions
    tick_value: float = 0.01  # SYMBOL_TRADE_TICK_VALUE for XAUUSDc cent account
    tick_size: float = 0.001  # SYMBOL_TRADE_TICK_SIZE: XAUUSDc=0.001 (3-digit), BTC pairs=0.01

    # Metadata
    timeout_label: str = "timeout"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Expected schema_version={SCHEMA_VERSION}, got {self.schema_version}")
        if self.type not in ("survival_barrier", "regression", "binary_class"):
            raise ValueError(f"Unknown label contract type: {self.type}")
        if self.horizon_bars < 1:
            raise ValueError(f"horizon_bars must be >= 1, got {self.horizon_bars}")
        if self.atr_period < 5:
            raise ValueError(f"atr_period must be >= 5, got {self.atr_period}")

    # ── Factory ──

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LabelContract:
        """Parse from a dictionary (validated against schema)."""
        return cls(
            schema_version=data["schema_version"],
            contract_id=data["contract_id"],
            type=data["type"],
            horizon_bars=data["horizon_bars"],
            label_classes=dict(data.get("label_classes", {})),
            sl_atr_mult=float(data.get("barriers", {}).get("sl_atr_mult", 2.0)),
            tp_atr_mult=float(data.get("barriers", {}).get("tp_atr_mult", 3.5)),
            bar_timeframe=data.get("bar_timeframe", "M5"),
            atr_period=int(data.get("atr_config", {}).get("period", 14)),
            atr_timeframe=data.get("atr_config", {}).get("timeframe", "M5"),
            regression_target=data.get("regression_target"),
            spread_points=float(data.get("spread_points", data.get("spread_pips", 30))),
            slippage_points=float(data.get("slippage_points", data.get("slippage_pips", 10))),
            tick_value=float(data.get("tick_value", 0.01)),
            tick_size=float(data.get("tick_size", 0.01)),
            fallback_atr=float(data["fallback_atr"]) if "fallback_atr" in data else None,
            timeout_label=data.get("timeout_label", "timeout"),
            metadata=data.get("metadata", {}),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> LabelContract:
        """Load a Label Contract from a JSON file."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to dict (round-trippable)."""
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "type": self.type,
            "horizon_bars": self.horizon_bars,
            "label_classes": self.label_classes,
            "bar_timeframe": self.bar_timeframe,
            "atr_config": {
                "timeframe": self.atr_timeframe,
                "period": self.atr_period,
            },
        }
        if self.type == "survival_barrier":
            d["barriers"] = {
                "sl_atr_mult": self.sl_atr_mult,
                "tp_atr_mult": self.tp_atr_mult,
            }
            d["timeout_label"] = self.timeout_label
        d["spread_points"] = self.spread_points
        d["slippage_points"] = self.slippage_points
        d["tick_value"] = self.tick_value
        d["tick_size"] = self.tick_size
        if self.regression_target:
            d["regression_target"] = self.regression_target
        if self.metadata:
            d["metadata"] = self.metadata
        return d

    # ── Core algorithm ──

    def build_barrier_labels(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        *,
        entry_idx: int,
        side: str,
    ) -> BarrierResult:
        """Build barrier labels for a single entry point.

        Args:
            highs: High price array (numpy float array).
            lows: Low price array.
            closes: Close price array.
            entry_idx: Index in arrays where trade opens.
            side: "long" or "short".

        Returns:
            BarrierResult with label class and hit details.
        """
        if self.type != "survival_barrier":
            raise ValueError(
                f"build_barrier_labels requires type='survival_barrier', got '{self.type}'"
            )
        return _build_barrier_labels_array(
            highs,
            lows,
            closes,
            entry_idx=entry_idx,
            side=side,
            sl_atr_mult=self.sl_atr_mult,
            fallback_atr=self.fallback_atr,
            tp_atr_mult=self.tp_atr_mult,
            horizon_bars=self.horizon_bars,
            atr_period=self.atr_period,
            spread_points=self.spread_points,
            slippage_points=self.slippage_points,
            tick_value=self.tick_value,
            tick_size=self.tick_size,
            volume=0.01,
        )

    # ── Regression label builder ──

    def build_regression_labels(
        self,
        closes: np.ndarray,
        *,
        entry_idx: int,
        side: str = "long",
    ) -> float:
        """Build a regression label for a single entry point.

        Computes the forward log return over ``horizon_bars`` in basis points
        (× 10000), then applies friction dead-band squashing so that
        sub-friction returns are zeroed rather than producing phantom
        inverted signals.

        Formula:
            raw_bps = sign * log(close[t+H] / close[t]) * 10000
            friction_bps = (spread_points + slippage_points) * tick_size
                           / entry_price * 10000
            label = apply_friction_deadband(raw_bps, friction_bps)

        Args:
            closes: Close price array.
            entry_idx: Index in array where the trade hypothetically opens.
            side: "long" or "short".

        Returns:
            Adjusted forward return in basis points (float).
        """
        if self.type != "regression":
            raise ValueError(
                f"build_regression_labels requires type='regression', got '{self.type}'"
            )
        entry_price = float(closes[entry_idx])
        if entry_price <= 0:
            return 0.0

        end_idx = min(entry_idx + self.horizon_bars, len(closes) - 1)
        if end_idx <= entry_idx:
            return 0.0

        exit_price = float(closes[end_idx])
        if exit_price <= 0:
            return 0.0

        # Forward log return in basis points
        if side == "long":
            raw_bps = float(np.log(exit_price / entry_price) * 10000)
        else:
            raw_bps = float(np.log(entry_price / exit_price) * 10000)

        # Friction in basis points: (spread + slippage) / entry * 10000
        friction_points = self.spread_points + self.slippage_points
        friction_bps = friction_points * self.tick_size / entry_price * 10000

        return float(apply_friction_deadband(np.array([raw_bps]), friction_bps)[0])

    # ── Volatility-scaled regression labels ──

    def build_vol_scaled_regression_labels(
        self,
        closes: np.ndarray,
        highs: np.ndarray | None = None,
        lows: np.ndarray | None = None,
        *,
        entry_idx: int,
        side: str = "long",
    ) -> float:
        """Build a volatility-scaled regression label for anti-collapse training.

        Instead of returning raw bps, divides the forward return by ATR at
        entry to produce a unitless "ATR-multiple" label.  This eliminates
        heteroskedasticity: a +10 bps move during 0.5 ATR quiet hours gets
        the same label weight as a +20 bps move during 1.0 ATR volatile hours.

        Formula:
            raw_bps = log(close[t+H] / close[t]) * 10000
            atr_at_entry = _compute_atr(high, low, close, period=14)
            label = deadband(raw_bps / atr_at_entry, friction/atr_at_entry)

        Args:
            closes: Close price array.
            highs: High price array (required for ATR computation).
            lows: Low price array (required for ATR computation).
            entry_idx: Index in array where the trade hypothetically opens.
            side: "long" or "short".

        Returns:
            Volatility-scaled return in ATR multiples (float).
        """
        if self.type != "regression":
            raise ValueError(
                f"build_vol_scaled_regression_labels requires type='regression', got '{self.type}'"
            )
        entry_price = float(closes[entry_idx])
        if entry_price <= 0:
            return 0.0

        end_idx = min(entry_idx + self.horizon_bars, len(closes) - 1)
        if end_idx <= entry_idx:
            return 0.0

        exit_price = float(closes[end_idx])
        if exit_price <= 0:
            return 0.0

        # Forward log return in basis points
        if side == "long":
            raw_bps = float(np.log(exit_price / entry_price) * 10000)
        else:
            raw_bps = float(np.log(entry_price / exit_price) * 10000)

        # ATR at entry
        if highs is not None and lows is not None:
            atr_val = _compute_atr(
                highs[: entry_idx + 1],
                lows[: entry_idx + 1],
                closes[: entry_idx + 1],
                period=self.atr_period,
            )
        else:
            # Fallback: approximate ATR from close volatility (suboptimal)
            window = closes[max(0, entry_idx - self.atr_period) : entry_idx + 1]
            if len(window) < 2:
                return 0.0
            atr_val = float(np.std(np.diff(np.log(window))) * entry_price)
        if atr_val <= 1e-8:
            return 0.0

        # Friction in ATR multiples
        friction_points = self.spread_points + self.slippage_points
        friction_bps = friction_points * self.tick_size / entry_price * 10000
        friction_atr = friction_bps / atr_val

        # Scale return to ATR multiples
        raw_atr_mult = raw_bps / atr_val

        return float(apply_friction_deadband(np.array([raw_atr_mult]), friction_atr)[0])

    # ── Self-consistency checks ──

    def validate(self) -> list[str]:
        """Run self-consistency checks. Returns list of issues (empty = valid)."""
        issues: list[str] = []

        # Label classes must cover tp, sl, timeout for survival_barrier
        if self.type == "survival_barrier":
            expected = {"tp_hit_first", "sl_hit_first", self.timeout_label}
            actual = set(self.label_classes.values())
            missing = expected - actual
            if missing:
                issues.append(
                    f"label_classes missing expected values: {missing}. " f"Got: {actual}"
                )
            if self.sl_atr_mult <= 0:
                issues.append("sl_atr_mult must be positive")
            if self.tp_atr_mult <= 0:
                issues.append("tp_atr_mult must be positive")

        # Regression needs a target
        if self.type == "regression" and not self.regression_target:
            issues.append("regression type requires regression_target")

        return issues
