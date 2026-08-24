"""Phase 4 Shadow Ops — 暗影遥测运行时 (The Shadow Ops).

IC 部署令 (2026-08-24): Micro Scaler v2 + MetaExit v3 接入实盘执行引擎 —
消费真实 Tick/Bar, 计算实时预测与 Quantile Trigger, 输出端死焊暗影遥测
(Shadow Telemetry). 风控 DEFCON 1: 绝对禁止向 mt5_bridge_worker 发送哪怕
0.01 手的真实订单.

本包是**纯观察者** (PURE OBSERVER):

  - 生命周期计算点: live_cycle Phase 4 特征计算之后, 每 cycle 复用同一份
    V9_40 特征向量 (零额外 MT5 调用, 零新 I/O 到派发链).
  - Intent 标记: 产出 ``ShadowOpsSignal`` (venue="shadow_ops" + action="OBSERVE"),
    不是 StrategyDecision, 永不进入 strategy_evaluator / exec_queue.
  - Dispatcher 物理拦截: Layer-2 ``shadow_ops_dispatch_filter`` 在
    dispatch_live_order 入口, 任何携带 shadow 标记的 payload 物理拒收并旁路
    遥测 ledger.

Import 禁令 (Layer 1 构造性隔离):
    mt5_bridge_worker, live_order_sender, communication_dispatcher, zmq,
    execution_queue, live_execution_contract, dispatch_context

本包 __init__ 必须保持零副作用 (不 import lightgbm / 不 import 子模块),
否则派发链的函数内 import 会拖入重型依赖.
"""

from __future__ import annotations

# 注意: 严禁在此 import 任何子模块 — 包级 import 必须零副作用 (见模块 docstring).
__all__ = [
    "trigger_contract",
    "micro_scaler_scorer",
    "telemetry",
    "runtime",
    "dispatch_filter",
]
