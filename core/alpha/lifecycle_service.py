"""Alpha lifecycle service."""

from dataclasses import replace
from datetime import UTC, datetime

from core.alpha.contracts import AlphaLifecycleState, AlphaRecord, AlphaTransitionRecord
from core.alpha.registry import AlphaRegistry
from core.alpha.schema_versions import SCHEMA_ALPHA_LIFECYCLE_SUMMARY


class AlphaLifecycleService:
    """Controls Alpha lifecycle transitions for B0 Alpha Factory."""

    VALID_TRANSITIONS = {
        AlphaLifecycleState.CANDIDATE.value: {
            AlphaLifecycleState.BACKTEST_PASSED.value,  # normal path
            AlphaLifecycleState.PAPER_TRADING.value,  # DQAF-050 fast-track: governance probation
            AlphaLifecycleState.PROBATION_LIVE.value,  # DQAF-050 fast-track: governance live
            AlphaLifecycleState.RETIRED.value,
        },
        AlphaLifecycleState.BACKTEST_PASSED.value: {
            AlphaLifecycleState.PAPER_TRADING.value,
            AlphaLifecycleState.RETIRED.value,
        },
        AlphaLifecycleState.PAPER_TRADING.value: {
            AlphaLifecycleState.PROBATION_LIVE.value,
            AlphaLifecycleState.THROTTLED.value,
            AlphaLifecycleState.RETIRED.value,
        },
        AlphaLifecycleState.PROBATION_LIVE.value: {
            AlphaLifecycleState.ACTIVE.value,
            AlphaLifecycleState.THROTTLED.value,
            AlphaLifecycleState.RETIRED.value,
        },
        AlphaLifecycleState.ACTIVE.value: {
            AlphaLifecycleState.THROTTLED.value,
            AlphaLifecycleState.RETIRED.value,
        },
        AlphaLifecycleState.THROTTLED.value: {
            AlphaLifecycleState.PAPER_TRADING.value,
            AlphaLifecycleState.PROBATION_LIVE.value,
            AlphaLifecycleState.RETIRED.value,
        },
        AlphaLifecycleState.RETIRED.value: set(),
    }

    def __init__(self, registry: AlphaRegistry):
        self._registry = registry
        self._transitions: list[AlphaTransitionRecord] = []

    def transition(
        self, alpha_id: str, to_state: AlphaLifecycleState | str, reason: str
    ) -> AlphaRecord:
        target = to_state.value if isinstance(to_state, AlphaLifecycleState) else to_state
        record = self._registry.require(alpha_id)
        current = record.state_value
        if target not in self.VALID_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid alpha transition: {current} -> {target}")
        updated = replace(record, state=target, updated_at=datetime.now(UTC).replace(tzinfo=None))
        self._registry.upsert(updated)
        self._transitions.append(
            AlphaTransitionRecord(
                alpha_id=alpha_id,
                from_state=current,
                to_state=target,
                reason=reason,
            )
        )
        return updated

    def mark_backtest_passed(self, alpha_id: str, reason: str = "backtest_passed") -> AlphaRecord:
        return self.transition(alpha_id, AlphaLifecycleState.BACKTEST_PASSED, reason)

    def start_paper_trading(
        self, alpha_id: str, reason: str = "paper_trading_started"
    ) -> AlphaRecord:
        return self.transition(alpha_id, AlphaLifecycleState.PAPER_TRADING, reason)

    def promote_to_probation_live(
        self, alpha_id: str, reason: str = "probation_live"
    ) -> AlphaRecord:
        return self.transition(alpha_id, AlphaLifecycleState.PROBATION_LIVE, reason)

    def activate(self, alpha_id: str, reason: str = "activated") -> AlphaRecord:
        return self.transition(alpha_id, AlphaLifecycleState.ACTIVE, reason)

    def throttle(self, alpha_id: str, reason: str = "throttled") -> AlphaRecord:
        return self.transition(alpha_id, AlphaLifecycleState.THROTTLED, reason)

    def retire(self, alpha_id: str, reason: str = "retired") -> AlphaRecord:
        return self.transition(alpha_id, AlphaLifecycleState.RETIRED, reason)

    def transitions(self, alpha_id: str | None = None) -> list[AlphaTransitionRecord]:
        if alpha_id is None:
            return list(self._transitions)
        return [transition for transition in self._transitions if transition.alpha_id == alpha_id]

    def summarize(self) -> dict:
        records = self._registry.list_records()
        by_state: dict[str, int] = {}
        for record in records:
            by_state[record.state_value] = by_state.get(record.state_value, 0) + 1
        return {
            "schema_version": SCHEMA_ALPHA_LIFECYCLE_SUMMARY,
            "alpha_count": len(records),
            "by_state": by_state,
            "transition_count": len(self._transitions),
        }
