"""Execution package exports."""

from core.execution.barrier_strategy import BarrierStrategy
from core.execution.dynamic_sl_tp import (
    DynamicSLTP,
    compute_dynamic_sl_tp,
    compute_sl_tp_levels,
)
from core.execution.execution_queue import (
    DEFAULT_PRIORITY,
    DispatchResult,
    ExecutionQueue,
    QueuedDecision,
)
from core.execution.fill_simulator import FillSimulationConfig, FillSimulator
from core.execution.fix_contracts import FixExecutionReport, FixMessage, FixSessionConfig
from core.execution.fix_execution_mapper import FixExecutionReportMapper
from core.execution.fix_gateway_adapter import FixGatewayAdapter
from core.execution.fix_message_builder import FixMessageBuilder
from core.execution.gateway_contracts import ExecutionGateway, Fill, OrderRequest, OrderState
from core.execution.micro_strategy import MicroStrategy
from core.execution.order_state_machine import OrderStateMachine
from core.execution.paper_gateway import PaperExecutionGateway
from core.execution.portfolio_risk import (
    PortfolioRiskController,
    PortfolioState,
    RiskResult,
    RiskVerdict,
)
from core.execution.quality_analyzer import ExecutionQualityAnalyzer
from core.execution.quality_contracts import (
    ExecutionBenchmark,
    ExecutionQualityMetric,
    ExecutionQualityReport,
)
from core.execution.regime_gate import RegimeGate
from core.execution.statarb_strategy import StatArbStrategy
from core.execution.strategy_budget import StrategyBudget
from core.execution.strategy_line import (
    StrategyDecision,
    StrategyLine,
    StrategyLineConfig,
    _counter_trend_action,
)

# ── Strategy layer (Phase 1) ────────────────────────────────────────────
from core.execution.strategy_type import (
    MAGIC_BARRIER,
    MAGIC_MICRO,
    MAGIC_STATARB,
    STRATEGY_NAME_TO_MAGIC,
    StrategyType,
)

__all__ = [
    # FIX / gateway layer
    "ExecutionBenchmark",
    "ExecutionGateway",
    "ExecutionQualityAnalyzer",
    "ExecutionQualityMetric",
    "ExecutionQualityReport",
    "Fill",
    "FillSimulationConfig",
    "FillSimulator",
    "FixExecutionReport",
    "FixExecutionReportMapper",
    "FixGatewayAdapter",
    "FixMessage",
    "FixMessageBuilder",
    "FixSessionConfig",
    "OrderRequest",
    "OrderState",
    "OrderStateMachine",
    "PaperExecutionGateway",
    # Strategy layer
    "BarrierStrategy",
    "DEFAULT_PRIORITY",
    "DispatchResult",
    "DynamicSLTP",
    "ExecutionQueue",
    "MAGIC_BARRIER",
    "MAGIC_MICRO",
    "MAGIC_STATARB",
    "MicroStrategy",
    "PortfolioRiskController",
    "PortfolioState",
    "QueuedDecision",
    "RegimeGate",
    "RiskResult",
    "RiskVerdict",
    "StatArbStrategy",
    "STRATEGY_NAME_TO_MAGIC",
    "StrategyBudget",
    "StrategyDecision",
    "StrategyLine",
    "StrategyLineConfig",
    "StrategyType",
    "_counter_trend_action",
    "compute_dynamic_sl_tp",
    "compute_sl_tp_levels",
]
