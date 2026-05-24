class FeedbackLoop:
    """Orchestrates the complete feedback cycle.

    Wires together outcome collection, decision scoring, and brain
    performance tracking into a single callable that processes a
    completed decision cycle and produces actionable signals.
    """

    def __init__(
        self,
        outcome_collector,
        decision_scorer,
        brain_performance_tracker,
    ):
        self._outcome_collector = outcome_collector
        self._decision_scorer = decision_scorer
        self._brain_performance_tracker = brain_performance_tracker

    def process_decision_outcome(
        self,
        *,
        date_key: str,
        target: str,
        message_id: str,
        correlation_id: str,
        intended_action: str | None = None,
        intended_side: str | None = None,
        intended_quantity: float = 0,
        supporting_brain_ids: list[str] | None = None,
        opposing_brain_ids: list[str] | None = None,
        market_context: dict | None = None,
    ) -> dict:
        outcome = self._outcome_collector.collect(
            date_key=date_key,
            target=target,
            message_id=message_id,
            correlation_id=correlation_id,
            intended_action=intended_action,
            intended_side=intended_side,
            intended_quantity=intended_quantity,
        )

        scored = self._decision_scorer.score(outcome, market_context=market_context)

        supporting_brain_ids = supporting_brain_ids or []
        opposing_brain_ids = opposing_brain_ids or []

        for brain_id in supporting_brain_ids:
            self._brain_performance_tracker.record_outcome(brain_id, scored)

        inverted = self._invert_score(scored)
        for brain_id in opposing_brain_ids:
            self._brain_performance_tracker.record_outcome(brain_id, inverted)

        brain_summaries = {
            bid: self._brain_performance_tracker.get_brain_summary(bid)
            for bid in set(supporting_brain_ids + opposing_brain_ids)
        }

        governance_signals = self._extract_governance_signals(brain_summaries)

        return {
            "outcome": outcome,
            "scored": scored,
            "brain_summaries": brain_summaries,
            "governance_signals": governance_signals,
        }

    def _invert_score(self, scored: dict) -> dict:
        inverted = dict(scored)
        inverted["composite_score"] = round(1.0 - scored["composite_score"], 4)
        # Dimension scores are also inverted to keep them consistent with
        # the flipped composite — otherwise the composite inversion implies a
        # different quality judgment than the dimension scores suggest.
        for key in (
            "sharpe_component",
            "wr_component",
            "pf_component",
            "pnl_component",
            "dd_component",
        ):
            if key in inverted and isinstance(inverted[key], int | float):
                inverted[key] = round(1.0 - inverted[key], 4)
        return inverted

    def _extract_governance_signals(self, brain_summaries: dict) -> list[dict]:
        signals = []
        for brain_id, summary in brain_summaries.items():
            rec = summary.get("recommendation", "maintain")
            if rec in {"freeze", "demote_to_probation", "limit_exposure"}:
                signals.append(
                    {
                        "brain_id": brain_id,
                        "signal_type": "governance_action_required",
                        "recommendation": rec,
                        "health_signal": summary.get("health_signal"),
                        "composite_mean": summary.get("composite_mean"),
                    }
                )
            elif rec == "eligible_for_promotion":
                signals.append(
                    {
                        "brain_id": brain_id,
                        "signal_type": "promotion_candidate",
                        "recommendation": rec,
                        "health_signal": summary.get("health_signal"),
                        "composite_mean": summary.get("composite_mean"),
                    }
                )
        return signals
