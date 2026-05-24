"""Shadow trade tracker for candidate-brain signal accumulation.

Counts shadow signals from ``data/brain_votes/`` JSONL files so that
governance rules can auto-promote candidate brains once they reach the
required shadow-signal threshold (default 50).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ShadowBrainMetrics:
    """Metrics derived from shadow (candidate) brain signal history."""

    brain_id: str
    shadow_signal_count: int = 0
    long_count: int = 0
    short_count: int = 0
    neutral_count: int = 0
    sum_confidence: float = 0.0
    # Directional win/loss tracking (approximate — based on vote direction
    # vs. subsequent bar close in the same file; refined by governance scheduler)
    estimated_wins: int = 0
    estimated_losses: int = 0

    @property
    def win_rate(self) -> float | None:
        total = self.estimated_wins + self.estimated_losses
        if total == 0:
            return None
        return self.estimated_wins / total

    @property
    def avg_confidence(self) -> float:
        if self.shadow_signal_count == 0:
            return 0.0
        return self.sum_confidence / self.shadow_signal_count


@dataclass
class ShadowTracker:
    """Reads brain_votes JSONL files and computes per-brain shadow metrics."""

    base_dir: str = "data"
    shadow_target: int = 50

    def _votes_dir(self) -> Path:
        return Path(self.base_dir) / "brain_votes"

    def count_shadow_signals(self, brain_id: str) -> int:
        """Count non-neutral votes for a brain across all available vote files."""
        return self._collect(brain_id).shadow_signal_count

    def is_shadow_complete(self, brain_id: str) -> bool:
        """Has this brain accumulated enough shadow signals?"""
        return self.count_shadow_signals(brain_id) >= self.shadow_target

    def get_shadow_metrics(self, brain_id: str) -> ShadowBrainMetrics:
        """Get full shadow metrics for a brain."""
        return self._collect(brain_id)

    def all_candidate_metrics(self, candidate_ids: list[str]) -> dict[str, ShadowBrainMetrics]:
        """Batch-collect metrics for multiple candidate brains."""
        result: dict[str, ShadowBrainMetrics] = {}
        for bid in candidate_ids:
            metrics = self._collect(bid)
            if metrics.shadow_signal_count > 0:
                result[bid] = metrics
        return result

    def _collect(self, brain_id: str) -> ShadowBrainMetrics:
        metrics = ShadowBrainMetrics(brain_id=brain_id)
        votes_dir = self._votes_dir()
        if not votes_dir.exists():
            return metrics

        for fpath in sorted(votes_dir.glob("*.jsonl")):
            try:
                with open(fpath, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        entry = json.loads(line)
                        if entry.get("brain_id") != brain_id:
                            continue
                        direction = str(entry.get("direction", "neutral")).lower()
                        if direction in ("neutral", "flat"):
                            metrics.neutral_count += 1
                            continue
                        metrics.shadow_signal_count += 1
                        if direction == "long":
                            metrics.long_count += 1
                        elif direction == "short":
                            metrics.short_count += 1
                        confidence = float(entry.get("confidence", 0.0))
                        metrics.sum_confidence += confidence
            except (json.JSONDecodeError, OSError):
                continue

        return metrics


def build_shadow_summary(
    tracker: ShadowTracker,
    candidate_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Build governance-compatible summary dicts for candidate brains.

    Returns a dict keyed by brain_id, each value a summary dict that can
    be merged into the ``brain_summaries`` fed to ``GovernanceRuleEngine.evaluate()``.
    """
    summaries: dict[str, dict[str, Any]] = {}
    metrics_map = tracker.all_candidate_metrics(candidate_ids)
    for bid, m in metrics_map.items():
        summaries[bid] = {
            "brain_id": bid,
            "shadow_signal_count": m.shadow_signal_count,
            "shadow_long_count": m.long_count,
            "shadow_short_count": m.short_count,
            "shadow_avg_confidence": m.avg_confidence,
            "shadow_win_rate": m.win_rate,
            "health_signal": "healthy",
            "sample_count": m.shadow_signal_count,
            "composite_mean": min(m.avg_confidence, 1.0),
        }
    return summaries
