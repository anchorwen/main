from datetime import datetime
from typing import Any, Dict, List


class BrainPerformanceTracker:
    """Tracks rolling performance metrics for each brain.

    Maintains per-brain statistics that can drive governance decisions
    such as demotion from live to probation, or promotion from
    candidate to live.
    """

    def __init__(self, window_size: int = 100):
        self._window_size = window_size
        self._records: Dict[str, List[dict]] = {}

    def record_outcome(self, brain_id: str, scored_outcome: dict) -> None:
        if brain_id not in self._records:
            self._records[brain_id] = []
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "composite_score": scored_outcome.get("composite_score", 0),
            "execution_outcome": scored_outcome.get("execution_outcome"),
            "fill_grade": scored_outcome.get("fill_grade"),
            "dimensions": scored_outcome.get("dimensions", {}),
        }
        self._records[brain_id].append(entry)
        if len(self._records[brain_id]) > self._window_size:
            self._records[brain_id] = self._records[brain_id][-self._window_size:]

    def get_brain_summary(self, brain_id: str) -> dict:
        entries = self._records.get(brain_id, [])
        if not entries:
            return {
                "brain_id": brain_id,
                "sample_count": 0,
                "composite_mean": 0.0,
                "composite_min": 0.0,
                "composite_max": 0.0,
                "outcome_distribution": {},
                "health_signal": "insufficient_data",
                "recommendation": "observe",
            }

        scores = [e["composite_score"] for e in entries]
        outcomes = [e.get("execution_outcome", "unknown") for e in entries]
        outcome_dist = {}
        for o in outcomes:
            outcome_dist[o] = outcome_dist.get(o, 0) + 1

        mean_score = sum(scores) / len(scores)
        recent = scores[-min(20, len(scores)):]
        recent_mean = sum(recent) / len(recent)

        health = self._assess_health(mean_score, recent_mean, outcome_dist, len(entries))
        recommendation = self._recommend_action(health, mean_score, recent_mean)

        return {
            "brain_id": brain_id,
            "sample_count": len(entries),
            "composite_mean": round(mean_score, 4),
            "composite_min": round(min(scores), 4),
            "composite_max": round(max(scores), 4),
            "recent_mean": round(recent_mean, 4),
            "outcome_distribution": outcome_dist,
            "health_signal": health,
            "recommendation": recommendation,
        }

    def get_all_summaries(self) -> list[dict]:
        return [self.get_brain_summary(bid) for bid in sorted(self._records.keys())]

    def get_brain_ids(self) -> list[str]:
        return sorted(self._records.keys())

    def _assess_health(self, mean: float, recent_mean: float, outcomes: dict, count: int) -> str:
        if count < 10:
            return "insufficient_data"

        breach_count = outcomes.get("breach", 0) + outcomes.get("rejected", 0)
        total = sum(outcomes.values())
        breach_rate = breach_count / total if total > 0 else 0

        if breach_rate > 0.3:
            return "critical"
        if recent_mean < 0.3:
            return "degraded"
        if mean < 0.4:
            return "warning"
        if recent_mean >= 0.7 and mean >= 0.6:
            return "healthy"
        return "stable"

    def _recommend_action(self, health: str, mean: float, recent_mean: float) -> str:
        if health == "critical":
            return "freeze"
        if health == "degraded":
            return "demote_to_probation"
        if health == "warning":
            return "limit_exposure"
        if health == "healthy" and mean >= 0.75:
            return "eligible_for_promotion"
        if health == "insufficient_data":
            return "observe"
        return "maintain"
