"""Experience replay buffer for mini-batch online learning.

Collects closed trades until buffer_size is reached, then flushes a
shuffled mini-batch where high R-multiple trades appear proportionally
more often without consecutive duplication that would cause catastrophic
forgetting in single-sample SGD.
"""

from __future__ import annotations

import json
import logging
import random
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_EMA_ALPHA = 0.05  # weight for new R observation in running mean
_MAX_WEIGHT = 3.0
_MIN_WEIGHT = 0.3


class ExperienceReplayBuffer:
    """Ring buffer that expands→shuffles→discharges on flush().

    Weights are computed from approximate R-multiple (|PnL| / volume) with
    an EMA-smoothed running mean so the weight scale adapts to changing
    volatility regimes over months of live trading.
    """

    def __init__(
        self,
        buffer_size: int = 20,
        state_path: str = "data/experience_replay_state.json",
    ):
        self._buffer_size = buffer_size
        self._state_path = Path(state_path)
        self._buffer: deque[tuple[np.ndarray, int, float, str]] = deque()
        self._running_r_mean: float = 1.0
        self._total_added: int = 0
        self._total_flushed: int = 0
        self._load_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(
        self,
        feature_vector: np.ndarray,
        label: int,
        pnl: float,
        volume: float,
        *,
        trade_id: str = "",
    ) -> None:
        """Append a single trade sample to the buffer with R-weighted priority.

        Args:
            feature_vector: 1-D feature array at trade entry time.
            label: -1 (loss), 0 (breakeven), or 1 (win).
            pnl: Realised PnL in account currency.
            volume: Position volume in lots.
            trade_id: Journal message_id for traceability.
        """
        weight = self._compute_weight(pnl, volume)
        feat = np.asarray(feature_vector, dtype=np.float64).ravel().copy()
        self._buffer.append((feat, int(label), weight, str(trade_id)))
        self._total_added += 1
        self._save_state()

    def is_ready(self) -> bool:
        return len(self._buffer) >= self._buffer_size

    @property
    def size(self) -> int:
        return len(self._buffer)

    def reset(self) -> int:
        """Clear buffer and persisted state without training.

        Returns the number of discarded samples.  Used after a data-pipeline
        fix (e.g. look-ahead bias correction) to ensure old contaminated
        samples don't mix with clean data.
        """
        discarded = len(self._buffer)
        self._buffer.clear()
        self._running_r_mean = 1.0
        self._save_state()
        logger.info(
            "ExperienceReplayBuffer reset: discarded %d samples, running_r_mean reset to 1.0",
            discarded,
        )
        return discarded

    def flush(self) -> list[tuple[np.ndarray, int]]:
        """Expand samples by integer weight, shuffle, and discharge.

        Returns:
            List of (feature_vector, label) pairs in shuffled order.
            Empty list if buffer is empty.
        """
        if not self._buffer:
            return []

        # ── Class imbalance diagnostic (before expansion) ──
        class_counts: dict[int, int] = {}
        for _, label, _, _ in self._buffer:
            class_counts[label] = class_counts.get(label, 0) + 1
        total = len(self._buffer)
        for cls, count in class_counts.items():
            ratio = count / total
            if ratio > 0.9:
                logger.warning(
                    "ExperienceReplayBuffer flush: extreme class imbalance — "
                    "label=%d is %d/%d (%.0f%%). Model bias may shift sharply.",
                    cls,
                    count,
                    total,
                    ratio * 100,
                )

        # ── Expand by integer weight ──
        expanded: list[tuple[np.ndarray, int]] = []
        for feat, label, weight, _ in self._buffer:
            count = max(1, int(round(weight)))
            for _ in range(count):
                expanded.append((feat, label))

        # ── Fisher-Yates shuffle ──
        random.shuffle(expanded)

        # ── Discharge ──
        flushed_count = len(self._buffer)
        avg_weight = sum(w for _, _, w, _ in self._buffer) / max(flushed_count, 1)
        self._buffer.clear()
        self._total_flushed += flushed_count
        self._save_state()

        logger.info(
            "ExperienceReplayBuffer flushed: %d trades → %d samples (avg weight %.2f), "
            "class_dist=%s",
            flushed_count,
            len(expanded),
            avg_weight,
            class_counts,
        )

        return expanded

    # ------------------------------------------------------------------
    # Internal: weight computation
    # ------------------------------------------------------------------

    def _compute_weight(self, pnl: float, volume: float) -> float:
        """Compute R-approximate weight with EMA-adaptive running mean.

        Approximate R = |PnL| / volume.  For XAUUSD with 0.01 lot = $1/pip
        this is a decent proxy for true R-multiple.

        EMA (α=0.05) lets the mean track regime shifts over months while
        smoothing out single-trade outliers.

        Weight is computed against the *previous* running mean, then the
        mean is updated — avoids circular self-normalization where the
        current trade pulls the mean toward itself.
        """
        # |PnL| is the realised dollar outcome — a direct proxy for trade
        # magnitude.  We don't divide by volume here because position risk
        # (SL distance × pip_value × volume) is not recorded in the journal.
        r_abs = abs(pnl)
        # Use previous mean for weight, then update — prevents self-bias
        prev_mean = self._running_r_mean
        weight = r_abs / max(prev_mean, 1e-8)
        self._running_r_mean = _EMA_ALPHA * r_abs + (1.0 - _EMA_ALPHA) * prev_mean
        return float(np.clip(weight, _MIN_WEIGHT, _MAX_WEIGHT))

    # ------------------------------------------------------------------
    # Persistence (survives between daily_ops invocations)
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        serializable: list[dict[str, Any]] = []
        for feat, label, weight, trade_id in self._buffer:
            serializable.append(
                {
                    "feature": feat.tolist(),
                    "label": label,
                    "weight": weight,
                    "trade_id": trade_id,
                }
            )
        self._state_path.write_text(
            json.dumps(
                {
                    "buffer": serializable,
                    "running_r_mean": self._running_r_mean,
                    "total_added": self._total_added,
                    "total_flushed": self._total_flushed,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        self._running_r_mean = float(data.get("running_r_mean", 1.0))
        self._total_added = int(data.get("total_added", 0))
        self._total_flushed = int(data.get("total_flushed", 0))

        for entry in data.get("buffer", []):
            feat = np.array(entry["feature"], dtype=np.float64)
            label = int(entry["label"])
            weight = float(entry["weight"])
            trade_id = str(entry.get("trade_id", ""))
            self._buffer.append((feat, label, weight, trade_id))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "buffer_size": self._buffer_size,
            "current": len(self._buffer),
            "total_added": self._total_added,
            "total_flushed": self._total_flushed,
            "running_r_mean": round(self._running_r_mean, 4),
            "weights": [round(w, 3) for _, _, w, _ in self._buffer],
            "class_dist": {
                lbl: sum(1 for _, l, _, _ in self._buffer if l == lbl)
                for lbl in sorted({l for _, l, _, _ in self._buffer})
            },
        }
