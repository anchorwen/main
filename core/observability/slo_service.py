"""SLO and error budget evaluation.

SloService turns runtime metrics into service-level objective status
and error-budget burn information for operational decision making.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from core.contracts.domain_keys import (
    PAYLOAD_KEY_COUNTERS,
    PAYLOAD_KEY_DESCRIPTION,
    PAYLOAD_KEY_DIRECTION,
    PAYLOAD_KEY_ERROR_BUDGET,
    PAYLOAD_KEY_ERROR_BUDGET_REMAINING_PCT,
    PAYLOAD_KEY_EXHAUSTED_COUNT,
    PAYLOAD_KEY_EXHAUSTED_OBJECTIVES,
    PAYLOAD_KEY_FAILED_OBJECTIVES,
    PAYLOAD_KEY_GAUGES,
    PAYLOAD_KEY_GENERATED_AT,
    PAYLOAD_KEY_HISTOGRAMS,
    PAYLOAD_KEY_MET,
    PAYLOAD_KEY_MIN_REMAINING_PCT,
    PAYLOAD_KEY_OBJECTIVE_COUNT,
    PAYLOAD_KEY_OBJECTIVES,
    PAYLOAD_KEY_RAW_COUNTERS,
    PAYLOAD_KEY_SCHEMA_VERSION,
    PAYLOAD_KEY_STATUS,
    PAYLOAD_KEY_TARGET,
    PAYLOAD_KEY_VALUE,
    SLO_DIRECTION_ABOVE,
    SLO_DIRECTION_BELOW,
    SLO_STATUS_BREACHING,
    SLO_STATUS_HEALTHY,
)
from core.observability.metric_names import (
    CYCLES_CIRCUIT_OPEN,
    CYCLES_ERRORS,
    CYCLES_THROTTLED,
    CYCLES_TOTAL,
    DISPATCH_FAILED,
    DISPATCH_PROTOCOL_VALIDATED,
    DISPATCH_SKIPPED,
    DISPATCH_TRANSPORT_DELIVERED,
    RECONCILIATION_BREACHED,
    RECONCILIATION_MATCHED,
    RECONCILIATION_PARTIAL,
    RECONCILIATION_UNMATCHED,
)
from core.observability.schema_versions import SCHEMA_SLO_REPORT


class SloService:
    """Evaluates SLOs from MetricsCollector counters and histograms."""

    DEFAULT_OBJECTIVES = {
        "decision_success_rate": {
            PAYLOAD_KEY_TARGET: 0.99,
            PAYLOAD_KEY_DESCRIPTION: "Successful decision cycles / total decision cycles",
        },
        "dispatch_success_rate": {
            PAYLOAD_KEY_TARGET: 0.98,
            PAYLOAD_KEY_DESCRIPTION: "Non-failed dispatches / total dispatches",
        },
        "reconciliation_match_rate": {
            PAYLOAD_KEY_TARGET: 0.95,
            PAYLOAD_KEY_DESCRIPTION: "Matched reconciliations / total reconciliations",
        },
        "throttle_rate": {
            PAYLOAD_KEY_TARGET: 0.05,
            PAYLOAD_KEY_DIRECTION: SLO_DIRECTION_BELOW,
            PAYLOAD_KEY_DESCRIPTION: "Throttled cycles / total decision cycles",
        },
        "circuit_open_rate": {
            PAYLOAD_KEY_TARGET: 0.01,
            PAYLOAD_KEY_DIRECTION: SLO_DIRECTION_BELOW,
            PAYLOAD_KEY_DESCRIPTION: "Circuit open cycles / total decision cycles",
        },
    }

    def __init__(self, metrics_collector=None, objectives: dict | None = None):
        self._metrics = metrics_collector
        self._objectives = objectives or self.DEFAULT_OBJECTIVES

    def evaluate(self) -> dict:
        snapshot = self._snapshot()
        counters = snapshot.get(PAYLOAD_KEY_COUNTERS, {})
        objectives = self._evaluate_objectives(counters)
        failed = [name for name, item in objectives.items() if not item[PAYLOAD_KEY_MET]]
        return {
            PAYLOAD_KEY_SCHEMA_VERSION: SCHEMA_SLO_REPORT,
            PAYLOAD_KEY_GENERATED_AT: datetime.now(UTC).replace(tzinfo=None).isoformat(),
            PAYLOAD_KEY_STATUS: SLO_STATUS_HEALTHY if not failed else SLO_STATUS_BREACHING,
            PAYLOAD_KEY_OBJECTIVE_COUNT: len(objectives),
            PAYLOAD_KEY_FAILED_OBJECTIVES: failed,
            PAYLOAD_KEY_OBJECTIVES: objectives,
            PAYLOAD_KEY_ERROR_BUDGET: self._build_error_budget(objectives),
            PAYLOAD_KEY_RAW_COUNTERS: counters,
        }

    def save_report(self, path: str) -> str:
        report = self.evaluate()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return str(target)

    def _snapshot(self) -> dict:
        if self._metrics is None:
            return {PAYLOAD_KEY_COUNTERS: {}, PAYLOAD_KEY_GAUGES: {}, PAYLOAD_KEY_HISTOGRAMS: {}}
        return self._metrics.snapshot()

    def _evaluate_objectives(self, counters: dict) -> dict:
        total_cycles = self._counter(counters, CYCLES_TOTAL)
        errors = self._counter(counters, CYCLES_ERRORS)
        throttled = self._counter(counters, CYCLES_THROTTLED)
        circuit_open = self._counter(counters, CYCLES_CIRCUIT_OPEN)

        dispatch_failed = self._counter(counters, DISPATCH_FAILED)
        dispatch_ok = (
            self._counter(counters, DISPATCH_TRANSPORT_DELIVERED)
            + self._counter(counters, DISPATCH_PROTOCOL_VALIDATED)
            + self._counter(counters, DISPATCH_SKIPPED)
        )
        dispatch_total = dispatch_ok + dispatch_failed

        recon_matched = self._counter(counters, RECONCILIATION_MATCHED)
        recon_breached = self._counter(counters, RECONCILIATION_BREACHED)
        recon_unmatched = self._counter(counters, RECONCILIATION_UNMATCHED)
        recon_partial = self._counter(counters, RECONCILIATION_PARTIAL)
        recon_total = recon_matched + recon_breached + recon_unmatched + recon_partial

        values = {
            "decision_success_rate": self._safe_rate(
                total_cycles - errors, total_cycles, default=1.0
            ),
            "dispatch_success_rate": self._safe_rate(dispatch_ok, dispatch_total, default=1.0),
            "reconciliation_match_rate": self._safe_rate(recon_matched, recon_total, default=1.0),
            "throttle_rate": self._safe_rate(throttled, total_cycles, default=0.0),
            "circuit_open_rate": self._safe_rate(circuit_open, total_cycles, default=0.0),
        }

        evaluated = {}
        for name, spec in self._objectives.items():
            target = float(spec[PAYLOAD_KEY_TARGET])
            direction = str(spec.get(PAYLOAD_KEY_DIRECTION, SLO_DIRECTION_ABOVE))
            value = values.get(name, 0.0)
            met = value >= target if direction == SLO_DIRECTION_ABOVE else value <= target
            budget = self._error_budget(value=value, target=target, direction=direction)
            evaluated[name] = {
                PAYLOAD_KEY_VALUE: round(value, 6),
                PAYLOAD_KEY_TARGET: target,
                PAYLOAD_KEY_DIRECTION: direction,
                PAYLOAD_KEY_MET: met,
                PAYLOAD_KEY_ERROR_BUDGET_REMAINING_PCT: round(budget, 6),
                PAYLOAD_KEY_DESCRIPTION: spec.get(PAYLOAD_KEY_DESCRIPTION, ""),
            }
        return evaluated

    def _build_error_budget(self, objectives: dict) -> dict:
        budgets = [item[PAYLOAD_KEY_ERROR_BUDGET_REMAINING_PCT] for item in objectives.values()]
        exhausted = [
            name
            for name, item in objectives.items()
            if item[PAYLOAD_KEY_ERROR_BUDGET_REMAINING_PCT] <= 0
        ]
        return {
            PAYLOAD_KEY_MIN_REMAINING_PCT: min(budgets) if budgets else 100.0,
            PAYLOAD_KEY_EXHAUSTED_COUNT: len(exhausted),
            PAYLOAD_KEY_EXHAUSTED_OBJECTIVES: exhausted,
        }

    def _error_budget(self, *, value: float, target: float, direction: str) -> float:
        if direction == SLO_DIRECTION_ABOVE:
            allowed_error = max(1.0 - target, 0.000001)
            actual_error = max(1.0 - value, 0.0)
            return max(0.0, (allowed_error - actual_error) / allowed_error * 100.0)
        allowed_bad = max(target, 0.000001)
        actual_bad = max(value, 0.0)
        return max(0.0, (allowed_bad - actual_bad) / allowed_bad * 100.0)

    def _counter(self, counters: dict, key: str) -> float:
        return float(counters.get(key, 0) or 0)

    def _safe_rate(self, numerator: float, denominator: float, default: float) -> float:
        if denominator <= 0:
            return default
        return max(0.0, min(1.0, numerator / denominator))
