from core.contracts.enums import RiskDecisionStatus


class RiskPolicy:
    """A single risk check that can approve, constrain, or block an intent."""

    def __init__(self, name: str, check_fn):
        self.name = name
        self._check_fn = check_fn

    def evaluate(self, intent, control_snapshot, context: dict) -> dict:
        return self._check_fn(intent, control_snapshot, context)


class PositionLimitPolicy(RiskPolicy):
    def __init__(self, max_open_positions: int = 10):
        self._max = max_open_positions
        super().__init__("position_limit", self._check)

    def _check(self, intent, control_snapshot, context: dict) -> dict:
        current = context.get("open_position_count", 0)
        if intent.is_open_intent() and current >= self._max:
            return {
                "status": RiskDecisionStatus.DENY,
                "reason": f"open_position_limit_exceeded({current}/{self._max})",
                "tier": "position",
            }
        return {"status": RiskDecisionStatus.ALLOW, "reason": None, "tier": "position"}


class DrawdownPolicy(RiskPolicy):
    def __init__(self, max_drawdown_pct: float = 5.0):
        self._max_dd = max_drawdown_pct
        super().__init__("drawdown", self._check)

    def _check(self, intent, control_snapshot, context: dict) -> dict:
        current_dd = context.get("current_drawdown_pct", 0.0)
        if current_dd >= self._max_dd:
            if intent.is_open_intent():
                return {
                    "status": RiskDecisionStatus.DENY,
                    "reason": f"drawdown_limit_breached({current_dd:.2f}%/{self._max_dd:.2f}%)",
                    "tier": "portfolio",
                }
            return {
                "status": RiskDecisionStatus.FORCE_REDUCE,
                "reason": f"drawdown_high_reduce_only({current_dd:.2f}%)",
                "tier": "portfolio",
                "constraint": {"force_reduce_only": True},
            }
        if current_dd >= self._max_dd * 0.8:
            return {
                "status": RiskDecisionStatus.ALLOW_LIMITED,
                "reason": f"drawdown_warning({current_dd:.2f}%)",
                "tier": "portfolio",
                "constraint": {"max_risk_fraction": 0.5},
            }
        return {"status": RiskDecisionStatus.ALLOW, "reason": None, "tier": "portfolio"}


class ExposurePolicy(RiskPolicy):
    def __init__(self, max_notional: float = 1_000_000.0):
        self._max_notional = max_notional
        super().__init__("exposure", self._check)

    def _check(self, intent, control_snapshot, context: dict) -> dict:
        current = context.get("current_notional_exposure", 0.0)
        proposed = context.get("proposed_notional_exposure", 0.0)
        if intent.is_open_intent() and (current + proposed) >= self._max_notional:
            return {
                "status": RiskDecisionStatus.DENY,
                "reason": f"notional_exposure_exceeded({current + proposed:.0f}/{self._max_notional:.0f})",
                "tier": "exposure",
            }
        return {"status": RiskDecisionStatus.ALLOW, "reason": None, "tier": "exposure"}


class ConcentrationPolicy(RiskPolicy):
    def __init__(self, max_per_symbol: int = 3):
        self._max = max_per_symbol
        super().__init__("concentration", self._check)

    def _check(self, intent, control_snapshot, context: dict) -> dict:
        per_symbol = context.get("positions_per_symbol", {})
        count = per_symbol.get(intent.symbol, 0)
        if intent.is_open_intent() and count >= self._max:
            return {
                "status": RiskDecisionStatus.DENY,
                "reason": f"symbol_concentration_exceeded({intent.symbol}:{count}/{self._max})",
                "tier": "concentration",
            }
        return {"status": RiskDecisionStatus.ALLOW, "reason": None, "tier": "concentration"}


class ModePolicy(RiskPolicy):
    """Enforces system mode constraints on trading activity."""

    def __init__(self):
        super().__init__("mode", self._check)

    def _check(self, intent, control_snapshot, context: dict) -> dict:
        mode = control_snapshot.mode_state.current_mode
        mode_val = mode.value if hasattr(mode, "value") else str(mode)

        if mode_val == "halted":
            return {
                "status": RiskDecisionStatus.DENY,
                "reason": "system_halted",
                "tier": "mode",
            }
        if mode_val == "liquidation_only":
            if intent.is_open_intent():
                return {
                    "status": RiskDecisionStatus.DENY,
                    "reason": "liquidation_only_mode",
                    "tier": "mode",
                }
            return {
                "status": RiskDecisionStatus.LIQUIDATE_ONLY,
                "reason": "liquidation_only_mode_close_allowed",
                "tier": "mode",
            }
        if mode_val == "observe_only":
            return {
                "status": RiskDecisionStatus.DEFER,
                "reason": "observe_only_mode",
                "tier": "mode",
            }
        if mode_val == "cautious":
            return {
                "status": RiskDecisionStatus.ALLOW_LIMITED,
                "reason": "cautious_mode_limited",
                "tier": "mode",
                "constraint": {"max_risk_fraction": 0.5},
            }
        return {"status": RiskDecisionStatus.ALLOW, "reason": None, "tier": "mode"}
