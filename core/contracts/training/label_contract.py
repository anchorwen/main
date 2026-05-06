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
) -> BarrierResult:
    """Build barrier labels for a single entry point.

    Starting at entry_idx, walk forward up to horizon_bars (or end of data).
    For each bar, check if the high (for short SL / long TP) or low (for
    long SL / short TP) breaches the barrier. First breach wins.

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

    Returns:
        BarrierResult with label, hit info, and reference prices.
    """
    entry_price = float(c[entry_idx])

    # Compute ATR at entry (using data up to entry_idx)
    atr_val = _compute_atr(
        h[: entry_idx + 1], low[: entry_idx + 1], c[: entry_idx + 1], period=atr_period
    )
    if atr_val <= 0:
        # Fallback: use training mean ATR if no usable ATR
        atr_val = 2.31

    sl_dist = sl_atr_mult * atr_val
    tp_dist = tp_atr_mult * atr_val

    if side == "long":
        sl_price = entry_price - sl_dist
        tp_price = entry_price + tp_dist
    else:
        sl_price = entry_price + sl_dist
        tp_price = entry_price - tp_dist

    end_idx = min(entry_idx + horizon_bars + 1, len(c))

    for i in range(entry_idx + 1, end_idx):
        bar_high = float(h[i])
        bar_low = float(low[i])

        if side == "long":
            if bar_low <= sl_price:
                return BarrierResult(
                    label="sl_hit_first",
                    hit_bar_index=i - entry_idx,
                    hit_price=sl_price,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    atr_at_entry=round(atr_val, 6),
                    horizon_bars=horizon_bars,
                )
            if bar_high >= tp_price:
                return BarrierResult(
                    label="tp_hit_first",
                    hit_bar_index=i - entry_idx,
                    hit_price=tp_price,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    atr_at_entry=round(atr_val, 6),
                    horizon_bars=horizon_bars,
                )
        else:  # short
            if bar_high >= sl_price:
                return BarrierResult(
                    label="sl_hit_first",
                    hit_bar_index=i - entry_idx,
                    hit_price=sl_price,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    atr_at_entry=round(atr_val, 6),
                    horizon_bars=horizon_bars,
                )
            if bar_low <= tp_price:
                return BarrierResult(
                    label="tp_hit_first",
                    hit_bar_index=i - entry_idx,
                    hit_price=tp_price,
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
            tp_atr_mult=self.tp_atr_mult,
            horizon_bars=self.horizon_bars,
            atr_period=self.atr_period,
        )

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
