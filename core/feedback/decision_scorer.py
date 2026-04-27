class DecisionScorer:
    """Scores individual decision outcomes on multiple dimensions.

    Produces a composite score and per-dimension breakdown that can be
    used by the brain performance tracker and governance layer.
    """

    DIMENSION_FILL = "fill_quality"
    DIMENSION_TIMING = "timing"
    DIMENSION_ACCURACY = "directional_accuracy"
    DIMENSION_RISK = "risk_compliance"

    def score(self, outcome: dict, *, market_context: dict | None = None) -> dict:
        market_context = market_context or {}

        fill_score = self._score_fill(outcome)
        timing_score = self._score_timing(outcome, market_context)
        accuracy_score = self._score_accuracy(outcome, market_context)
        risk_score = self._score_risk(outcome)

        dimensions = {
            self.DIMENSION_FILL: fill_score,
            self.DIMENSION_TIMING: timing_score,
            self.DIMENSION_ACCURACY: accuracy_score,
            self.DIMENSION_RISK: risk_score,
        }

        weights = {
            self.DIMENSION_FILL: 0.30,
            self.DIMENSION_TIMING: 0.15,
            self.DIMENSION_ACCURACY: 0.40,
            self.DIMENSION_RISK: 0.15,
        }
        composite = sum(
            dimensions[d]["score"] * weights[d] for d in dimensions
        )

        return {
            "composite_score": round(composite, 4),
            "dimensions": dimensions,
            "execution_outcome": outcome.get("execution_outcome"),
            "fill_grade": outcome.get("fill_quality", {}).get("grade"),
        }

    def _score_fill(self, outcome: dict) -> dict:
        fq = outcome.get("fill_quality", {})
        grade = fq.get("grade", "unknown")
        grade_scores = {
            "clean_fill": 1.0,
            "quantity_mismatch_fill": 0.7,
            "partial_cancel": 0.4,
            "partial_open": 0.3,
            "pending": 0.2,
            "cancelled": 0.1,
            "rejected": 0.0,
            "no_execution": 0.0,
            "unknown": 0.0,
        }
        return {
            "score": grade_scores.get(grade, 0.0),
            "grade": grade,
            "fill_ratio": fq.get("fill_ratio", 0.0),
        }

    def _score_timing(self, outcome: dict, market_context: dict) -> dict:
        timeline = outcome.get("timeline", {})
        event_count = timeline.get("event_count", 0)
        partial_fills = sum(
            1 for et in timeline.get("event_types", []) if et == "partially_filled"
        )

        if event_count == 0:
            return {"score": 0.0, "reason": "no_events"}

        if partial_fills == 0 and timeline.get("terminal_event_type") == "filled":
            return {"score": 1.0, "reason": "instant_fill"}
        if partial_fills <= 2:
            return {"score": 0.8, "reason": "fast_fill"}
        if partial_fills <= 5:
            return {"score": 0.5, "reason": "moderate_fill_time"}
        return {"score": 0.3, "reason": "slow_fill"}

    def _score_accuracy(self, outcome: dict, market_context: dict) -> dict:
        realized_pnl = market_context.get("realized_pnl")
        intended_side = outcome.get("intended_side")

        if realized_pnl is None:
            price_move = market_context.get("price_move_pct", 0)
            if intended_side == "long":
                directional_correct = price_move > 0
            elif intended_side == "short":
                directional_correct = price_move < 0
            else:
                return {"score": 0.5, "reason": "no_directional_signal"}

            if directional_correct:
                magnitude = min(abs(price_move) / 1.0, 1.0)
                return {"score": 0.5 + 0.5 * magnitude, "reason": "direction_correct"}
            else:
                magnitude = min(abs(price_move) / 1.0, 1.0)
                return {"score": max(0.5 - 0.5 * magnitude, 0.0), "reason": "direction_wrong"}

        if realized_pnl > 0:
            return {"score": min(0.7 + realized_pnl / 1000.0, 1.0), "reason": "profitable"}
        elif realized_pnl == 0:
            return {"score": 0.5, "reason": "breakeven"}
        else:
            return {"score": max(0.3 + realized_pnl / 1000.0, 0.0), "reason": "loss"}

    def _score_risk(self, outcome: dict) -> dict:
        recon = outcome.get("reconciliation")
        if recon is None:
            return {"score": 0.5, "reason": "no_reconciliation"}

        status = recon.get("status")
        if status == "matched":
            return {"score": 1.0, "reason": "clean_reconciliation"}
        if status == "partial":
            return {"score": 0.6, "reason": "partial_reconciliation"}
        if status == "breached":
            return {"score": 0.0, "reason": "reconciliation_breach"}
        if status == "stale":
            return {"score": 0.3, "reason": "stale_reconciliation"}
        return {"score": 0.2, "reason": "unmatched"}
